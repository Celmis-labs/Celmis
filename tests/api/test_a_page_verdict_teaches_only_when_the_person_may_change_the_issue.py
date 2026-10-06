"""The reviews page's verdict reaches the learning signals — under the same
rule that lets it close the issue.

A member's dismissal (and taking it back) is carried to the signals, with the
finding recomputed from the run on the server; a viewer's verdict is recorded
on the page and teaches nothing.
(A run of another workspace is refused by `record_ui` itself: see the signals tests.)
"""

from __future__ import annotations

import pytest

from src.review.learning import signals
from tests.api.test_feedback_moves_an_issue_only_for_who_may_change_it import (
    _CLEAR,
    _body,
    api,
    ledger,  # noqa: F401
)


@pytest.fixture
def taught(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        signals, "record_ui",
        lambda ws, run_id, state, **kw: calls.append(("put", ws, run_id, state, kw)) or "created")
    monkeypatch.setattr(
        signals, "clear_ui",
        lambda ws, run_id, **kw: calls.append(("clear", ws, run_id, kw)) or 1)
    monkeypatch.setattr(signals, "snapshot_from_run", lambda run_id, key, **_kw: None)
    return calls


async def test_a_members_dismissal_teaches_the_reviewer(ledger, taught, monkeypatch):  # noqa: F811
    async with api(role="member", monkeypatch=monkeypatch) as c:
        assert (await c.put("/api/feedback/run/r2", json=_body())).status_code == 200
    [(kind, ws, run, state, kw)] = taught
    assert (kind, ws, run, state) == ("put", "ws-1", "r2", "dismissed")
    assert kw["actor"] == "u@test" and kw["snapshot"].title == "Unchecked return"
    assert kw["snapshot"].rule_id == "defect.ret"


async def test_taking_the_verdict_back_takes_the_signal_back(ledger, taught, monkeypatch):  # noqa: F811
    async with api(role="member", monkeypatch=monkeypatch) as c:
        await c.put("/api/feedback/run/r2", json=_body())
        assert (await c.delete(_CLEAR)).status_code == 204
    assert [t[0] for t in taught] == ["put", "clear"]
    assert taught[1][3]["snapshot"].title == "Unchecked return"


async def test_a_viewers_verdict_is_kept_on_the_page_and_teaches_nothing(
        ledger, taught, monkeypatch):  # noqa: F811
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        assert (await c.put("/api/feedback/run/r3", json=_body())).status_code == 200
        assert (await c.delete(_CLEAR)).status_code == 204
    assert taught == []


async def test_a_failing_signal_store_does_not_fail_the_verdict(
        ledger, taught, monkeypatch):  # noqa: F811
    def boom(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(signals, "record_ui", boom)
    async with api(role="member", monkeypatch=monkeypatch) as c:
        assert (await c.put("/api/feedback/run/r2", json=_body())).status_code == 200


def test_the_finding_is_found_in_the_run_by_what_it_says_when_the_page_key_is_not_ours(
        monkeypatch):
    class Store:
        def findings_of(self, run_id):
            return [{"file_path": "src/a.py", "line": 12, "title": "Unchecked return",
                     "rule_id": "defect.ret", "body": "The stored body.", "category": "bug"},
                    {"file_path": "src/b.py", "line": 3, "title": "Other", "rule_id": None}]

    monkeypatch.setattr("src.api.review_runs.get_review_run_store", lambda: Store())
    page_key = "Q2hlY2tlZFJldHVybg"  # what the page mints: not signals.feedback_key
    snap = signals.snapshot_from_run(
        "r2", page_key, file_path="src/a.py", title="Unchecked return ", rule_id="defect.ret")
    assert snap is not None and snap.body == "The stored body."
    assert signals.snapshot_from_run("r2", page_key, file_path="src/zzz.py", title="x") is None
    assert signals.snapshot_from_run("r2", page_key) is None
