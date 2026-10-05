"""`handle_automation_plan` end to end (queue, row and model stubbed): an
explained read from the operations family is handed to the second model call
with its own verb, and a read that already wrote its answer (`ask_code`) is the
note as it is, with no second call."""
from __future__ import annotations

import asyncio

import pytest

WS = "ws-1"


@pytest.fixture
def run(monkeypatch):
    import src.automation.chat as chat
    import src.sync.handlers as handlers
    import src.sync.queue as queue

    seen: dict = {"finished": [], "explain": []}
    monkeypatch.setattr(chat, "resolve_scope", lambda p, **k: p)
    monkeypatch.setattr(queue, "is_cancel_requested", lambda _id: False)
    monkeypatch.setattr(handlers, "_partial_note_writer", lambda _rid: (lambda _t: None))
    monkeypatch.setattr(handlers, "_cancel_poller", lambda _jid: (lambda: False))

    async def _finish(run_id, **kw):
        seen["finished"].append((run_id, kw))

    monkeypatch.setattr(handlers, "_finish_automation_plan", _finish)

    def _explain(message, action, snapshot, **kw):
        seen["explain"].append((action, snapshot))
        return "explained"

    monkeypatch.setattr(chat, "explain_read", _explain)

    def go(verb: str, result: dict) -> dict:
        plan = chat.Plan(steps=[chat.Step(action=verb, arguments={})],
                         note="plan note", language="en")
        monkeypatch.setattr(chat, "interpret", lambda *a, **k: plan)

        async def _reads(_plan, _payload):
            return {"steps": [{"action": verb, "result": result}]}

        monkeypatch.setattr(handlers, "_run_automation_reads", _reads)
        asyncio.run(handlers.handle_automation_plan({"id": "j-1", "payload": {
            "run_id": "r-1", "workspace_id": WS, "user_id": "u-1",
            "message": "q", "history": None}}))
        return seen

    return go


def test_an_ops_read_is_explained_from_its_own_data(run):
    seen = run("get_spend", {"total_usd": 12.5})
    (_, kw), = seen["finished"]
    assert kw["status"] == "answered" and kw["note"] == "explained"
    assert seen["explain"] == [("get_spend", {"total_usd": 12.5})]


def test_ask_code_answer_is_the_note_without_a_second_call(run):
    seen = run("ask_code", {"answer": "It lives in src/x.py"})
    (_, kw), = seen["finished"]
    assert kw["status"] == "answered"
    assert kw["note"] == "It lives in src/x.py"
    assert seen["explain"] == []
