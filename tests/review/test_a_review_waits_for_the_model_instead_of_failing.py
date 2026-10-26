"""A review that cannot reach its model waits; it does not publish "REVIEW FAILED".

Through the real orchestrator over a fake GitHub:

  * every agent down for an availability reason, with a queue behind the
    review: ModelUnavailable, the placeholder rewritten as "postponed" (still
    a placeholder), no failure banner, no summary comment of the PR;
  * the same outage with no queue behind it (CLI, MCP): today's failed review;
  * a partial review, or a failure that is not about reachability, keeps
    today's behaviour whatever the caller can do.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.model_preflight import ModelUnavailable
from src.review.providers.base import STATUS_IN_PROGRESS_MARK
from tests.review.test_a_rerun_does_not_double_the_comments import _FakeGitHub
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    GOOD_REPLY,
    _Agent,
    _Client,
    _finding,
    _orch,
    _pr,
    _Provider,
    _run,
    env,  # noqa: F401 — the fixture
)


class _Down(ReviewAgent):
    def __init__(self, name: str, code: str | None = "provider_unavailable") -> None:
        self.name = name
        self._code = code

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, error="boom: secret-token-123",
                              error_code=self._code)


def _down(*codes):
    return [_Down(f"a{i}", c) for i, c in enumerate(codes)]


def test_an_outage_postpones_the_review_and_the_pr_is_told_it_will_run_again(env, monkeypatch):  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=_down("provider_unavailable", "rate_limited"),
                 client=_Client(GOOD_REPLY))

    with pytest.raises(ModelUnavailable) as caught:
        _run(orch, _Provider(fake, _pr()), defer_on_outage=True)

    assert caught.value.code == "provider_unavailable"
    [comment] = fake.issue
    body = comment["body"]
    assert "postponed" in body.lower()
    assert STATUS_IN_PROGRESS_MARK in body
    assert "REVIEW FAILED" not in body and "Review failed" not in body
    assert "secret-token-123" not in body
    assert fake.reviews == [], "no review of the PR is posted for an outage"


def test_the_postponed_note_is_written_in_the_review_language(env, monkeypatch):  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=_down("provider_unavailable"),
                 client=_Client(GOOD_REPLY), policy={"review_language": "uk"})

    with pytest.raises(ModelUnavailable):
        _run(orch, _Provider(fake, _pr()), defer_on_outage=True)

    [comment] = fake.issue
    assert "відкладено" in comment["body"].lower()
    assert "postponed" not in comment["body"].lower()


def test_without_a_queue_behind_it_the_outage_is_the_failed_review_it_always_was(env, monkeypatch):  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=_down("provider_unavailable", "provider_unavailable"),
                 client=_Client(GOOD_REPLY))

    result = _run(orch, _Provider(fake, _pr()))  # defer_on_outage defaults to False

    assert result.batch.nothing_ran
    bodies = " ".join(c["body"] for c in fake.issue + fake.reviews)
    assert "REVIEW FAILED" in bodies or "Review failed" in bodies
    assert "postponed" not in bodies.lower()


@pytest.mark.parametrize("codes", [
    ("provider_unavailable", "unreadable_reply"),
    ("provider_unavailable", None),
    ("context_too_long", "context_too_long"),
    ("unreadable_reply",),
])
def test_a_failure_that_is_not_about_reachability_is_not_postponed(env, monkeypatch, codes):  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=_down(*codes), client=_Client(GOOD_REPLY))

    result = _run(orch, _Provider(fake, _pr()), defer_on_outage=True)

    assert result.batch.nothing_ran
    assert "postponed" not in " ".join(c["body"] for c in fake.issue).lower()


def test_a_partial_review_is_posted_not_postponed(env, monkeypatch):  # noqa: F811
    fake = _FakeGitHub()
    agents = [_Agent("defect", findings=[_finding()]), _Down("security")]
    orch = _orch(monkeypatch, agents=agents, client=_Client(GOOD_REPLY))

    result = _run(orch, _Provider(fake, _pr()), defer_on_outage=True)

    assert not result.batch.nothing_ran
    assert "security" in result.batch.agents_failed
    assert "postponed" not in " ".join(c["body"] for c in fake.issue).lower()


def test_an_outage_before_any_placeholder_writes_nothing_to_the_pr(env, monkeypatch):  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=_down("provider_unavailable"),
                 client=_Client(GOOD_REPLY),
                 policy={"started_comment_enabled": False})

    with pytest.raises(ModelUnavailable):
        _run(orch, _Provider(fake, _pr()), defer_on_outage=True)

    assert fake.issue == [] and fake.reviews == []
