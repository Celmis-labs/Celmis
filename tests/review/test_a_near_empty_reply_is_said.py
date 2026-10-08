"""A model that answers a big diff with `[]` in a token or two is a valid review
that the run must still say out loud — and must not turn into a different verdict."""

from __future__ import annotations

from types import SimpleNamespace

from src.review.models import Hunk, ReviewBatch
from src.review.orchestrator import terse_reply_note


def _pr(changed: int):
    hunk = Hunk(file_path="a.py", old_file_path="a.py", old_start=1, old_count=1,
                new_start=1, new_count=changed, content="\n".join("+x" for _ in range(changed)))
    return SimpleNamespace(hunks=[hunk])


def _agent(name: str, tin: int, tout: int, findings=()):
    return SimpleNamespace(agent=name, tokens_in=tin, tokens_out=tout,
                           findings=list(findings), error=None)


def test_terse_replies_on_a_large_diff_are_named():
    results = [_agent("defect", 9000, 2), _agent("security", 7000, 1), _agent("contract", 8000, 400)]
    note = terse_reply_note(results, _pr(80))
    assert note is not None
    assert note.agent == "defect, security"
    assert "reasoning" in note.reason

    batch = ReviewBatch(pull_request=_pr(80))
    before = batch.compute_verdict()
    batch.parameter_adjustments.append(note)
    text = batch.adjustments_notice
    assert "ADJUSTED" in text and "defect, security" in text
    assert batch.compute_verdict() == before


def test_a_tiny_diff_is_not_flagged():
    assert terse_reply_note([_agent("defect", 9000, 2)], _pr(10)) is None


def test_a_normal_reply_is_not_flagged():
    assert terse_reply_note([_agent("defect", 9000, 350)], _pr(80)) is None


def test_missing_usage_is_not_flagged():
    assert terse_reply_note([_agent("defect", 0, 0)], _pr(80)) is None
