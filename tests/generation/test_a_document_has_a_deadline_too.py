"""The other place that asks a model for a large answer, and its clock.

`ApiEngine.generate` was careful about the parameters around it — there is a
comment right above the call explaining that a `None` temperature and a `None`
output ceiling meant "the provider's default" rather than "the installation's",
and that a truncated document arrived with no signal it had been cut. The clock
is the field the same reasoning did not reach: no `timeout`, so 120 seconds,
and no `num_retries`, so LiteLLM's default of THREE — a real ceiling of 480
seconds that no setting named and no operator could reach.

A document is capped at `gemini_max_output_tokens` (8192) over a whole module's
code context, which is the size class of the review calls that were measurably
being cut at 120 (16 agent failures in 8 hours, every classified one an
APITimeoutError). Unlike a review, a vault build is a batch job with nobody
watching, so the cost of waiting is patience and the cost of cutting is a
module with no documentation at all.

Not measured on this installation, and the fix says so rather than inventing a
figure: `llm_spend` holds 2436 review calls and zero documentation ones,
because no vault has ever been built here. The call now logs its own duration,
so the number that replaces this one can be measured instead of argued.

AND THE AGENT ENGINE HAD NO DEADLINE AT ALL. `async for message in
client.receive_response()` waits for the next message; a session that stops
producing them waits forever, and `max_turns` does not help because turns are
only counted when they arrive.

That got worse the day the job lease started renewing itself. A hung build used
to lose its lease after ten minutes and a sibling worker took the row — wrong,
but it moved. The heartbeat now keeps saying "a worker is alive on this", which
is TRUE, and is exactly why the row would never come back.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.config import Settings

# ─── both clocks have names ──────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["generation_timeout_seconds", "generation_num_retries",
             "generation_agent_timeout_seconds"],
)
def test_the_setting_exists(name):
    assert name in Settings.model_fields


def test_the_api_deadline_beats_the_one_that_was_cutting_calls():
    assert Settings().generation_timeout_seconds > 120, (
        "120 is the value measured cutting review calls of the same size"
    )


def test_the_resend_budget_is_stated_not_inherited():
    """LiteLLM's default of 3 quietly makes any deadline a ceiling four times
    its own size: 120 became 480, and 600 would become 2400."""
    s = Settings()
    assert s.generation_num_retries < 3
    assert s.generation_num_retries >= 1, (
        "not zero: there is no ladder above this one — the generator records "
        "the module as failed and moves on, so this is the only resend"
    )


def test_the_agent_session_gets_a_longer_clock_than_one_call():
    """It bounds a different thing: up to 24 exchanges, most of them MCP tool
    calls, rather than one completion."""
    s = Settings()
    assert s.generation_agent_timeout_seconds > s.generation_timeout_seconds


def test_the_session_clock_leaves_room_for_every_turn():
    import src.generation.claude_docs as cd

    per_turn = Settings().generation_agent_timeout_seconds / cd._MAX_TURNS
    assert per_turn >= 30, (
        f"{per_turn:.0f}s a turn is tighter than an MCP tool call against the "
        f"index; the session would die on a healthy build"
    )


# ─── and the calls pass them ─────────────────────────────────────────


def test_the_api_call_states_its_clock_and_its_resends():
    import src.generation.engines as eng

    tree = ast.parse(inspect.getsource(eng.ApiEngine.generate).lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "generate"]
    assert calls, "the engine no longer calls the client"
    kwargs = {k.arg for k in calls[0].keywords if k.arg}
    assert "timeout" in kwargs, "inherits generate()'s own 120s default"
    assert "num_retries" in kwargs, "inherits LiteLLM's default of 3"


def test_the_api_call_reads_the_settings_not_a_literal():
    import src.generation.engines as eng

    src = inspect.getsource(eng.ApiEngine.generate)
    assert "generation_timeout_seconds" in src
    assert "generation_num_retries" in src


def test_the_agent_session_is_bounded():
    import src.generation.claude_docs as cd

    src = inspect.getsource(cd.ClaudeDocsEngine._run)
    assert "asyncio.timeout" in src or "wait_for" in src, (
        "a session that stops producing messages waits forever, and the "
        "renewed lease means the worker waits with it"
    )
    assert "_session_timeout()" in src


def test_the_session_clock_is_read_per_call():
    """An operator raising it should not need a restart."""
    import src.generation.claude_docs as cd

    tree = ast.parse(inspect.getsource(cd._session_timeout))
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "get_settings"
               for n in ast.walk(tree))


# ─── the duration is recorded, so the next number is measured ────────


def test_the_call_says_how_long_it_took():
    """The deadline above had to be argued from the review path because this
    one has never been measured here. Whoever revisits it should not have to."""
    import src.generation.engines as eng

    src = inspect.getsource(eng.ApiEngine.generate)
    assert "generation_call_finished" in src
    assert "elapsed" in src


def test_the_log_names_the_deadline_it_ran_against():
    """An elapsed time without the bound it was measured against cannot tell
    the next reader whether a slow call was near the limit."""
    import src.generation.engines as eng

    src = inspect.getsource(eng.ApiEngine.generate)
    assert "deadline=%d" in src
