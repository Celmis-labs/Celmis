"""`ask` finishes with its own message before the MCP call envelope cuts it.

The contract (docs/MCP_DEV.md "Limits per token", docs/mcp-access.md):

  * a call is limited to `CELMIS_MCP_CALL_TIMEOUT_SECONDS` (default 60); `ask`
    used to wait 150 s regardless, so it was always the envelope that ended a
    slow one, with its generic "the call took longer than 60s";
  * now `ask` derives its deadline from the envelope: what is left of the call's
    time minus a margin (the smaller of 10 s and a quarter of the timeout), so
    at the defaults the answer stops about 10 s before the envelope's cut, at
    about 50 s, and the tool says "The answer took too long (more than 50s) —
    narrow the question or the repositories." as its own message, not as a
    limit refusal; the message shows the nominal limit (50), not the 49.97 s
    that are left a moment after the call began;
  * outside an MCP call (the web assistant) nothing changes: the 150 s ceiling;
  * there is no new setting: one number (the call timeout) decides both;
  * the transport answers every call as an event stream that pings every 15 s,
    so no proxy sees 100 s of silence while an answer is being written.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import types
from pathlib import Path

import pytest

from src.automation import actions_reviews as ar
from src.automation.actions import ActionError
from src.mcp_server import call_envelope, callctx, limits

SLOW = 30.0          # a model call that would outlive every deadline below


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", raising=False)
    limits.reset()
    callctx.reset()
    yield
    limits.reset()
    callctx.reset()


def _in_call(deadline_in: float | None):
    """Pretend to be inside a call whose envelope stops waiting `deadline_in` s from now."""
    rec = callctx.begin("ask", "dev")
    rec.deadline = None if deadline_in is None else time.monotonic() + deadline_in
    return rec


# ─── the derived deadline ────────────────────────────────────────────


def test_outside_a_call_the_ceiling_applies():
    assert limits.answer_deadline() == limits.ANSWER_CEILING_S == 150.0
    assert limits.answer_deadline(12.0) == 12.0


def test_a_call_with_no_time_limit_gets_the_ceiling_too():
    _in_call(None)
    assert limits.answer_deadline() == 150.0


@pytest.mark.parametrize(("timeout", "expected"), [
    (60, 50.0),          # the default: a margin of 10 s
    (20, 15.0),          # short budget: a quarter of it
    (8, 6.0),
    (300, 150.0),        # long budget: never past the ceiling
])
def test_inside_a_call_the_answer_gets_what_is_left_minus_a_margin(monkeypatch, timeout, expected):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", str(timeout))
    _in_call(timeout)
    assert limits.answer_deadline() == pytest.approx(expected, abs=0.5)


def test_time_the_tool_spent_before_asking_is_not_given_again(monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "60")
    _in_call(35.0)                       # 25 s of the 60 are already gone
    assert limits.answer_deadline() == pytest.approx(25.0, abs=0.5)


def test_a_call_already_past_its_time_still_gets_a_deadline_that_ends_it_at_once(monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "60")
    _in_call(-3.0)
    assert limits.answer_deadline() == limits.ANSWER_MIN_S


def test_at_the_default_the_deadline_leaves_the_envelope_at_least_ten_seconds():
    assert limits.TIMEOUT_DEFAULT == 60.0
    _in_call(limits.call_timeout())
    assert limits.call_timeout() - limits.answer_deadline() >= 9.5
    assert limits.answer_deadline() < limits.ANSWER_CEILING_S


def test_the_message_shows_the_nominal_limit_not_the_few_milliseconds_short_of_it():
    """The deadline is worked out a moment after the call began (49.97 s for a nominal 50), and
    `int()` of that said "more than 49s" while the documentation says 50."""
    assert "more than 50s" in limits.answer_too_slow(49.97)
    assert "more than 50s" in limits.answer_too_slow(50.0)
    assert "more than 1s" in limits.answer_too_slow(0.2), "never 'more than 0s'"


def test_at_the_default_timeout_the_deadline_a_call_really_gets_is_announced_as_50s():
    """Through `answer_deadline`, as `ask` does: a call that has just begun under the default 60 s."""
    assert limits.call_timeout() == 60.0
    _in_call(60.0)
    deadline = limits.answer_deadline()
    assert 49.0 < deadline <= 50.0, "a moment has passed since the call began: a hair under 50"
    msg = limits.answer_too_slow(deadline)
    assert "more than 50s" in msg and "49s" not in msg
    assert msg == "The answer took too long (more than 50s) — narrow the question or the repositories."


# ─── the envelope announces its deadline ─────────────────────────────


async def test_the_envelope_records_when_it_will_stop_waiting(monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "7")
    callctx.begin("ask", "dev")
    seen: list[float | None] = []

    async def handler(_req):
        seen.append(callctx.current().deadline)
        return "fine"

    before = time.monotonic()
    result, limited = await call_envelope._within_limits(handler, object())

    assert (result, limited) == ("fine", False)
    assert before + 7 <= seen[0] <= time.monotonic() + 7


async def test_with_the_timeout_switched_off_no_deadline_is_announced(monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "0")
    callctx.begin("ask", "dev")
    seen: list[float | None] = []

    async def handler(_req):
        seen.append(callctx.current().deadline)
        return "fine"

    await call_envelope._within_limits(handler, object())
    assert seen == [None]


# ─── ask, end to end through the envelope ────────────────────────────


@pytest.fixture
def pipeline(monkeypatch):
    """`ask_code` over a model call that takes `pipeline.seconds` and a repository the caller may read."""
    import src.llm.budget as budget
    from src.api.routers import qa

    state = types.SimpleNamespace(seconds=SLOW, cancelled=False, calls=0)

    async def generate(**_kw):
        state.calls += 1
        try:
            await asyncio.sleep(state.seconds)
        except asyncio.CancelledError:
            state.cancelled = True
            raise
        return "It is done in create_order.", {"files_read": ["app/orders.py"]}

    async def can_read(*_a, **_kw):
        return True

    monkeypatch.setattr(qa, "_generate_full", generate)
    monkeypatch.setattr(budget, "enforce", lambda ws: None)
    monkeypatch.setattr(ar, "_user_for", lambda actor: types.SimpleNamespace(id="u-1", is_admin=False))
    monkeypatch.setattr(ar, "_registered", lambda actor: {"acme-api": object()})
    monkeypatch.setattr(ar, "resolve_repo", lambda actor, slug: slug)
    monkeypatch.setattr(ar, "_can_read", can_read)
    return state


ACTOR = types.SimpleNamespace(workspace_id="ws-1", token_filter=None)


async def _ask_as_the_tool_does(_req) -> str:
    """What the dev `ask` tool does with the pipeline: the answer, or the message as an error line."""
    try:
        res = await ar.ask_code(ACTOR, None, question="how is it done?", repo_slugs=["acme-api"])
    except ActionError as exc:
        return f"error: {exc}"
    return str(res["answer"])


async def test_a_slow_answer_ends_with_the_tools_own_message_before_the_envelope_cuts(
        pipeline, monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "2")       # margin 0.5 s: ask gives up at ~1.5 s
    callctx.begin("ask", "dev")
    started = time.monotonic()

    result, limited = await call_envelope._within_limits(_ask_as_the_tool_does, object())

    assert time.monotonic() - started < 2.0, "it answered before the envelope's own cut"
    assert limited is False, "the envelope did not have to refuse the call"
    assert "took too long (more than 1s)" in result and "narrow the question" in result
    assert "took longer than" not in result, "not the envelope's generic wording"
    assert pipeline.cancelled, "the model call behind it was stopped, not left running"


async def test_a_quick_answer_is_returned_whole(pipeline, monkeypatch):
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "2")
    pipeline.seconds = 0.01
    callctx.begin("ask", "dev")

    result, limited = await call_envelope._within_limits(_ask_as_the_tool_does, object())

    assert result == "It is done in create_order." and limited is False


async def test_outside_an_mcp_call_the_assistant_keeps_the_ceiling(pipeline, monkeypatch):
    monkeypatch.setattr(ar, "ASK_TIMEOUT_S", 1.2)
    started = time.monotonic()

    out = await _ask_as_the_tool_does(None)

    assert 1.0 < time.monotonic() - started < 3.0
    assert "took too long (more than 1s)" in out


async def test_a_project_token_question_is_bound_the_same_way(pipeline, monkeypatch):
    from src.mcp_server import project_tools

    scope = types.SimpleNamespace(
        view=types.SimpleNamespace(id="t-1", workspace_id="ws-1", project_id="p-1"),
        name="Acme", slugs=["acme-api"], file_scopes=())
    monkeypatch.setattr(project_tools, "resolve_scope", lambda view: scope)
    monkeypatch.setattr(project_tools, "_readable", lambda s: ["acme-api"])
    monkeypatch.setenv("CELMIS_MCP_CALL_TIMEOUT_SECONDS", "2")

    async def handler(_req):
        try:
            return str((await project_tools.ask_project(object(), "how?"))["answer"])
        except project_tools.ProjectToolError as exc:
            return f"error: {exc}"

    callctx.begin("ask_project", "full")
    started = time.monotonic()
    result, limited = await call_envelope._within_limits(handler, object())

    assert time.monotonic() - started < 2.0 and limited is False
    assert "took too long (more than 1s)" in result


# ─── nothing new to configure; the transport keeps the line alive ────


def test_no_new_setting_was_added_for_it():
    root = Path(__file__).resolve().parents[2]
    text = (root / "src/mcp_server/limits.py").read_text(encoding="utf-8")
    assert "CELMIS_MCP_ASK" not in text and "ANSWER_TIMEOUT" not in text
    assert text.count("CELMIS_MCP_CALL_TIMEOUT_SECONDS") >= 1


def test_every_mcp_answer_is_an_event_stream_that_pings_well_inside_a_proxys_idle_window():
    """A CDN drops a response that is silent for about 100 s. The streamable HTTP transport answers each
    POST with server-sent events (`json_response` off) and `sse-starlette` writes a ping frame every 15 s
    until the answer is ready, so a 50 s answer never goes quiet. If an upgrade changes any of the three,
    this is where it shows."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from sse_starlette.sse import EventSourceResponse

    assert FastMCP("probe").settings.json_response is False
    assert EventSourceResponse.DEFAULT_PING_INTERVAL <= 30
    assert "ping=" not in inspect.getsource(StreamableHTTPServerTransport)
    src = Path(__file__).resolve().parents[2] / "src"
    for path in src.rglob("*.py"):
        if "mcp_server" in path.parts:
            body = path.read_text(encoding="utf-8")
            assert "json_response=True" not in body, path.name
            assert "stateless_http=True" not in body, path.name
