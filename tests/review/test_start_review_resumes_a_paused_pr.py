"""`@celmis start-review` on a paused pull request resumes it, and the review
covers every push that was skipped meanwhile (`ReviewRequest.resume`)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewPullRequest
from src.review import cadence, pr_state
from src.review.commands import handlers
from src.review.commands.handlers import CommandContext
from src.review.dispatch import QueuedReview
from tests.review.comment_support import FakeProvider, command, event


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def rows(monkeypatch):
    import src.review.issues as issues_mod

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(eng)
    monkeypatch.setattr(issues_mod, "_ENGINE", eng)
    yield eng
    eng.dispose()


@pytest.fixture
def requests_seen(monkeypatch):
    seen = []

    def fake(provider, repo, number, **kw):
        seen.append(kw["request"])
        return QueuedReview("run-1", "queued", "", {})

    monkeypatch.setattr("src.review.dispatch.enqueue_review_run", fake)
    return seen


def _start():
    handlers._start_review(CommandContext(
        ev=event(), provider=FakeProvider(), command=command(), settings={},
        workspace_id="ws", user_id="u"))


def test_a_paused_pr_is_resumed_and_the_review_covers_the_skipped_pushes(
        rows, requests_seen):
    pr_state.set_paused("ws", "github", "acme/shop", 7, paused=True,
                        reason=cadence.REASON_MANUAL, by="auto")
    _start()
    assert requests_seen[0].resume is True
    assert pr_state.load("ws", "github", "acme/shop", 7).review_paused is False


def test_a_pr_that_was_not_paused_is_reviewed_without_the_resume_flag(
        rows, requests_seen):
    _start()
    assert requests_seen[0].resume is False
    assert requests_seen[0].trigger == "command"


def test_a_pr_state_that_cannot_be_written_does_not_stop_the_review(
        requests_seen, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(pr_state, "resume", broken)
    _start()
    assert len(requests_seen) == 1 and requests_seen[0].resume is False


def test_pause_is_not_a_comment_command():
    """Pausing is a button on the pull-requests page; the docs promise no
    `@celmis pause` comment, so the parser must not know one either."""
    from src.review.commands import parser
    assert "pause" not in parser.COMMAND_NAMES
    assert not hasattr(cadence, "REASON_COMMAND")
