"""Three things that had to be true before an agent session can be a chat.

Two of them were latent — harmless while a session is one turn, wrong the
moment it is many — and one is a live bug today.

  * The PreToolUse hook waved every `mcp__*` tool through with `return {}`:
    no deny check, no path check. Survivable only because the deny list made
    the shell unreachable. It is the single filesystem barrier there is, and
    widening the agent's permissions without closing it first would be
    pointless.

  * `ResultMessage.usage` is CUMULATIVE across the conversation. One result
    per session hid that; one result per turn would bill turn 1 ten times over
    — a ten-turn chat writes roughly fifty-five turns of tokens to the
    tenant's Usage page.

  * `stream_end` is sent whenever this connection has no live queue, which
    includes an API restart while the session is still running. The client
    read it as terminal and stopped reconnecting, so every deploy left an open
    session page permanently dead. That one bites today.
"""

from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = (ROOT / "src" / "agent" / "runner.py").read_text()
ROUTES = (ROOT / "src" / "api" / "routers" / "claude_code.py").read_text()


# ─── the path hook ───────────────────────────────────────────────────


def test_mcp_tools_are_no_longer_exempt_from_the_path_check():
    assert 'startswith("mcp__")' not in RUNNER or "return {}" not in RUNNER, (
        "the mcp__ exemption is back"
    )
    idx = RUNNER.find("def _make_path_hook(")
    assert idx > 0
    body = RUNNER[idx:idx + 4000]
    # Every tool now reaches the path check.
    assert "_escapes_workspace(root, tool_input)" in body
    assert "if tool_name.startswith(\"mcp__\"):\n            return {}" not in body


def test_the_path_check_still_refuses_an_escape():
    """The guard itself, unchanged — pinned so closing the exemption cannot
    quietly coincide with weakening what it checks."""
    from src.agent.runner import _escapes_workspace

    root = ROOT  # any real directory
    assert _escapes_workspace(root, {"file_path": "/etc/passwd"}) == "/etc/passwd"
    assert _escapes_workspace(root, {"file_path": "src/agent/runner.py"}) is None
    # A sibling whose name merely starts with the root's is not inside it.
    assert _escapes_workspace(root, {"path": str(root) + "-evil"}) is not None


# ─── the ledger ──────────────────────────────────────────────────────


class _Msg:
    def __init__(self, i, o, cached=0):
        self.usage = {"input_tokens": i, "output_tokens": o,
                      "cache_read_input_tokens": cached}
        self.model = "claude-code"


def test_spend_records_the_delta_not_the_running_total(monkeypatch):
    from src.agent import runner

    written: list[dict] = []
    import src.llm.budget as budget
    monkeypatch.setattr(budget, "record_spend",
                        lambda **kw: written.append(kw))

    row = type("R", (), {"id": "s1", "workspace_id": "ws", "user_id": "u"})()
    prior = runner._record_agent_spend(row, _Msg(100, 10))
    assert prior == (100, 10, 0)
    assert written[-1]["tokens_in"] == 100

    # Turn two: the SDK reports the conversation total, not this turn's.
    prior = runner._record_agent_spend(row, _Msg(250, 30), prior)
    assert prior == (250, 30, 0)
    assert written[-1]["tokens_in"] == 150, "the running total was billed again"
    assert written[-1]["tokens_out"] == 20

    assert sum(w["tokens_in"] for w in written) == 250, "over-counted overall"


def test_a_result_that_repeats_the_same_totals_writes_nothing(monkeypatch):
    from src.agent import runner

    written: list[dict] = []
    import src.llm.budget as budget
    monkeypatch.setattr(budget, "record_spend",
                        lambda **kw: written.append(kw))

    row = type("R", (), {"id": "s1", "workspace_id": "ws", "user_id": "u"})()
    prior = runner._record_agent_spend(row, _Msg(100, 10))
    before = len(written)
    prior = runner._record_agent_spend(row, _Msg(100, 10), prior)
    assert len(written) == before, "a zero delta still produced a ledger row"


def test_a_counter_that_goes_backwards_never_writes_a_negative(monkeypatch):
    """A provider that resets or reorders its counters must not corrupt the
    ledger."""
    from src.agent import runner

    written: list[dict] = []
    import src.llm.budget as budget
    monkeypatch.setattr(budget, "record_spend",
                        lambda **kw: written.append(kw))

    row = type("R", (), {"id": "s1", "workspace_id": "ws", "user_id": "u"})()
    prior = runner._record_agent_spend(row, _Msg(500, 50))
    runner._record_agent_spend(row, _Msg(100, 10), prior)
    assert all(w["tokens_in"] >= 0 and w["tokens_out"] >= 0 for w in written)


def test_the_caller_threads_the_accumulator_through():
    """A correct function called wrongly is still an over-count."""
    body = inspect.getsource(
        __import__("src.agent.runner", fromlist=["x"])._drive_agent)
    assert "spend_prior" in body, "the accumulator is never passed"
    assert "_record_agent_spend, row, message, spend_prior" in body


# ─── the stream ──────────────────────────────────────────────────────


def test_stream_end_reports_the_session_status():
    """It is a statement about the CONNECTION. Without the session's own
    status the client cannot tell "finished" from "the API restarted"."""
    idx = ROUTES.find('"stream_end"')
    assert idx > 0
    frame = ROUTES[max(0, idx - 900):idx + 900]
    assert "session_status" in frame
    assert "_TERMINAL_STATUSES" in frame
    # `final` means "cannot continue", not "no stream". A paused session closes
    # the connection and stays resumable, and the client needs both facts to
    # decide between keeping the composer and offering a new session.
    assert '"resumable"' in frame


def test_paused_closes_the_stream_without_being_final():
    """Two different questions that used to share one answer: is there a
    process behind this connection, and can this conversation continue.

    A paused session has no process — so the stream must close, or the client
    waits forever on something that does not exist — but it is precisely the
    case that CAN continue. Conflating them puts "start a new session" in front
    of somebody whose session is sitting there waiting for them.
    """
    from src.api.routers.claude_code import (
        _STREAM_CLOSED_STATUSES,
        _TERMINAL_STATUSES,
    )

    assert "paused" in _STREAM_CLOSED_STATUSES
    assert "paused" not in _TERMINAL_STATUSES
    assert _TERMINAL_STATUSES < _STREAM_CLOSED_STATUSES


def test_only_a_terminal_status_counts_as_final():
    from src.api.routers.claude_code import _TERMINAL_STATUSES

    assert "done" in _TERMINAL_STATUSES and "error" in _TERMINAL_STATUSES
    for alive in ("running", "queued"):
        assert alive not in _TERMINAL_STATUSES, (
            f"'{alive}' marked terminal — a restart would end the page"
        )


def test_the_client_stops_reconnecting_only_when_the_session_is_over():
    page = (ROOT / "web" / "app" / "(app)" / "claude" / "[id]"
            / "page.tsx").read_text()
    assert 'ev === "stream_end" && data.final === true' in page, (
        "stream_end is terminal on its own again — every deploy kills the page"
    )
    idx = page.find("const finished =")
    assert idx > 0
    assert "doneRef.current = true" in page[idx:idx + 400]
