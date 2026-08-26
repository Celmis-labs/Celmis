"""Two clocks that could not both be honoured, and one that was honoured nowhere.

GEMINI_TIMEOUT_SECONDS. `Settings.gemini_timeout_seconds` sat beside
`gemini_top_p` and `gemini_retry_attempts`, defaulted to 120, and was read by
NO code path in the repository — `genai.Client(api_key=…)` was constructed with
no `http_options`, so the exploration agent's Gemini calls had no request
timeout at all. A provider that accepted the connection and then went quiet
hung the turn, and the tool-use loop around it, for as long as the socket
stayed open.

A knob wired to nothing is worse than a missing one. A missing knob sends an
operator looking; this one answered them, and the answer was false — they set a
number, watched nothing change, and had no reason to suspect the setting rather
than the provider.

THE SANDBOX'S TWO CLOCKS. The server queues for a free slot AND THEN runs the
command, so it can hold a connection for `SLOT_WAIT_SECONDS + timeout`. The
client allowed `timeout + 60`. The comment where the wait is defined claimed it
was "shorter than the client's read timeout" — comparing two numbers as if they
were alternatives when they are sequential. Any queue wait past a minute and
the caller hung up on a job the sandbox went on running to completion: work
paid for whose result reached nobody, and only under load, which is exactly
when the queue is not empty.

The caller now declares its patience and the server bounds itself by it. The
party that will hang up is the one that decides, and a caller with no room left
is told "busy, try again" — an answer it can read — instead of being cut off.
"""

from __future__ import annotations

import inspect

# ─── the Gemini deadline is connected ────────────────────────────────


def test_the_client_is_built_with_the_configured_timeout():
    import src.llm.gemini_client as gc

    src = inspect.getsource(gc.GeminiClient.__init__)
    assert "http_options" in src, "genai.Client with no transport options"
    assert "gemini_timeout_seconds" in src, (
        "the setting is still read by nothing"
    )


def test_the_setting_still_exists_and_is_a_number():
    from src.config import Settings

    assert "gemini_timeout_seconds" in Settings.model_fields
    assert Settings().gemini_timeout_seconds > 0


def test_seconds_are_converted_to_the_sdk_s_milliseconds():
    """The SDK counts milliseconds; the setting is named seconds and stays
    named seconds, because that is what an operator thinks in. A factor of a
    thousand in the wrong direction is a deadline of a tenth of a second."""
    import src.llm.gemini_client as gc

    src = inspect.getsource(gc.GeminiClient.__init__)
    assert "* 1000" in src


# ─── the sandbox's two clocks add up ─────────────────────────────────


def test_the_caller_allows_for_the_queue_as_well_as_the_command():
    import src.agent.sandbox as client
    import src.sandbox.server as server

    command_budget = 300
    client_holds = command_budget + client.WAIT_BUDGET_SECONDS
    server_may_take = command_budget + min(
        server.SLOT_WAIT_SECONDS,
        client.WAIT_BUDGET_SECONDS - server.WAIT_BUDGET_RESERVE_SECONDS,
    )
    assert server_may_take < client_holds, (
        f"the sandbox may hold {server_may_take}s while the caller waits "
        f"{client_holds}s — the gap is a job whose result reaches nobody"
    )


def test_the_caller_declares_its_patience_on_the_wire():
    import src.agent.sandbox as client

    src = inspect.getsource(client.run)
    assert "wait_budget" in src, (
        "two processes with two independent numbers is how the old pairing "
        "came to guarantee a hang-up"
    )


def test_the_server_bounds_itself_by_what_the_caller_said():
    import src.sandbox.server as server

    src = inspect.getsource(server._run)
    assert "wait_budget" in src
    assert "max_wait" in src


def test_an_older_caller_that_says_nothing_still_works():
    """A field absent from the header is an api that predates it, and it must
    behave as it did before — never worse."""
    import src.sandbox.server as server

    src = inspect.getsource(server._run)
    assert "SLOT_WAIT_SECONDS" in src, "no fallback to this server's own ceiling"


def test_the_lease_takes_its_bound_as_an_argument():
    """It read the module constant directly, which is what made the caller's
    patience unrepresentable."""
    import src.sandbox.server as server

    sig = inspect.signature(server._lease)
    assert "max_wait" in sig.parameters


# ─── the CVE sweep can finish inside its own timebox ─────────────────


def test_the_osv_sweep_takes_a_deadline():
    """One POST per hundred packages plus a GET per advisory, each bounded at
    30s and nothing bounding the sequence — inside a 120-second timebox. A
    lockfile large enough to need five batches could not finish however
    healthy OSV was, and the review reported the scan as blind rather than as
    partial."""
    from src.deps.registries import fetch_vulns_batch

    assert "deadline" in inspect.signature(fetch_vulns_batch).parameters


def test_the_unreached_packages_read_as_incomplete_not_as_clean():
    """`failed_batches` already means "this scan is incomplete". Reusing it
    means no caller needs a second notion of partial, and none can read the
    shortfall as "no vulnerabilities"."""
    import src.deps.registries as reg

    src = inspect.getsource(reg.fetch_vulns_batch)
    assert "failed_batches +=" in src
    assert "deadline" in src


def test_the_sweep_stops_between_batches_never_inside_one():
    """A querybatch answer is paired positionally with the chunk that asked
    for it, so abandoning one halfway is how a package gets credited with
    another package's vulnerabilities."""
    import ast

    import src.deps.registries as reg

    tree = ast.parse(inspect.getsource(reg.fetch_vulns_batch).lstrip())
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.For))
    # The deadline check is the first statement of the batch loop's body.
    first = loop.body[0]
    assert isinstance(first, ast.If)
    assert "deadline" in ast.dump(first.test)


def test_the_agent_leaves_the_outer_timebox_room_to_notice():
    """Stopping cleanly has to beat being abandoned, or the findings already
    paid for never come home."""
    import src.review.agents.cve as cve

    assert cve._DEADLINE_MARGIN > 30, (
        "one in-flight OSV request is 30s; less margin than that and the "
        "sweep is abandoned before it can return"
    )
    assert cve._DEADLINE_MARGIN < cve.LOOKUP_TIMEOUT_SECONDS
