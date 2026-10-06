"""`review_cadence = auto_pause`: the push that makes N inside the window pauses the PR.

Pure window arithmetic (`cadence`), the per-PR row (`pr_state`, on SQLite) and
the webhook that drives both.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewPullRequest
from src.review import cadence, pr_state
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    bound,
    no_ledger,
    queue,
    store,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


# ─── the window ──────────────────────────────────────────────────────


def test_only_the_pushes_inside_the_window_count():
    pushes = [_at(-30).isoformat(), _at(-14).isoformat(), _at(-1).isoformat()]
    kept = cadence.prune(pushes, T0, 15)
    assert kept == [_at(-14), _at(-1)]


def test_an_unreadable_entry_is_dropped_and_a_naive_time_is_utc():
    kept = cadence.prune(["not a time", None, "2026-10-06T11:55:00"], T0, 15)
    assert kept == [datetime(2026, 10, 6, 11, 55, tzinfo=UTC)]


@pytest.mark.parametrize("count, limit, expected", [
    (2, 3, False), (3, 3, True), (4, 3, True), (1, 0, True), (1, 1, True),
])
def test_the_limit_is_reached_not_exceeded(count, limit, expected):
    assert cadence.should_pause(count, limit) is expected


@pytest.mark.parametrize("name, paused, reason, action, code", [
    ("automatic", False, None, "review", "ok"),
    ("auto_pause", False, None, "review", "ok"),
    ("auto_pause", True, "auto_pause", "skip", "paused"),
    ("manual", False, None, "skip", "cadence_manual"),
    # a mechanical pause lapses when the repository leaves auto_pause ...
    ("automatic", True, "auto_pause", "review", "ok"),
    ("manual", True, "auto_pause", "skip", "cadence_manual"),
    # ... a person's pause holds whatever the cadence says
    ("automatic", True, "manual", "skip", "paused"),
    ("automatic", True, "command", "skip", "paused"),
    ("garbage", False, None, "review", "ok"),
    (None, False, None, "review", "ok"),
])
def test_what_the_cadence_decides(name, paused, reason, action, code):
    decision = cadence.decide(name, paused, reason)
    assert (decision.action, decision.code) == (action, code)


# ─── the row ─────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(eng)
    yield eng
    eng.dispose()


def _push(engine, head, minute, *, limit=3, window=15, name="auto_pause"):
    return pr_state.register_push(
        "ws-1", "github", "o/r", 1, head, cadence_name=name, limit=limit,
        window_minutes=window, now=_at(minute), engine=engine)


def test_the_third_push_in_fifteen_minutes_pauses_and_is_itself_skipped(engine):
    first = _push(engine, "a" * 12, 0)
    second = _push(engine, "b" * 12, 5)
    third = _push(engine, "c" * 12, 9)

    assert (first.paused, second.paused) == (False, False)
    assert third.is_push and third.paused and third.newly_paused
    assert third.paused_reason == "auto_pause" and third.pushes == 3
    state = pr_state.load("ws-1", "github", "o/r", 1, engine=engine)
    assert state.review_paused and state.paused_reason == "auto_pause"


def test_pushes_spread_over_more_than_the_window_never_pause(engine):
    results = [_push(engine, f"{i:012x}", minute) for i, minute in enumerate((0, 8, 16, 24, 32))]
    assert not any(r.paused for r in results)
    assert results[-1].pushes == 2


def test_a_redelivery_of_the_same_head_is_not_a_push(engine):
    _push(engine, "a" * 12, 0)
    _push(engine, "b" * 12, 1)
    again = _push(engine, "b" * 12, 2)
    assert not again.is_push and again.pushes == 2 and not again.paused


def test_a_short_sha_and_a_full_sha_are_one_commit(engine):
    full = "aaa111bbb222" + "0" * 28
    _push(engine, full, 0)
    assert not _push(engine, "aaa111bbb222", 1).is_push


@pytest.mark.parametrize("name", ["automatic", "manual"])
def test_other_cadences_count_pushes_but_never_pause(engine, name):
    results = [_push(engine, f"{i:012x}", i, name=name) for i in range(6)]
    assert not any(r.paused for r in results)


def test_the_limit_and_window_are_the_settings_not_constants(engine):
    results = [_push(engine, f"{i:012x}", i, limit=2, window=1) for i in range(0, 4, 2)]
    assert not any(r.paused for r in results), "two pushes two minutes apart, window 1"
    assert _push(engine, "f" * 12, 4.2, limit=2, window=1).paused is False
    assert _push(engine, "e" * 12, 4.5, limit=2, window=1).paused is True


def test_a_paused_pr_is_not_paused_again_by_each_push(engine):
    for i in range(3):
        _push(engine, f"{i:012x}", i)
    later = _push(engine, "9" * 12, 4)
    assert later.paused and not later.newly_paused


def test_resume_clears_the_pause_and_the_pushes_that_caused_it(engine):
    for i in range(3):
        _push(engine, f"{i:012x}", i)

    assert pr_state.resume("ws-1", "github", "o/r", 1, engine=engine) is True
    state = pr_state.load("ws-1", "github", "o/r", 1, engine=engine)
    assert not state.review_paused and state.paused_reason is None
    assert state.recent_pushes == 0, "the old pushes must not pause it again at once"
    assert not _push(engine, "7" * 12, 5).paused
    assert pr_state.resume("ws-1", "github", "o/r", 1, engine=engine) is False


def test_a_person_can_pause_a_pr_that_was_never_seen(engine):
    assert pr_state.set_paused("ws-1", "github", "o/r", 9, reason="command",
                               by="alice", engine=engine) is True
    state = pr_state.load("ws-1", "github", "o/r", 9, engine=engine)
    assert state.review_paused and state.paused_reason == "command"
    assert state.paused_by == "alice"
    with pytest.raises(ValueError):
        pr_state.set_paused("ws-1", "github", "o/r", 9, reason="because", engine=engine)


def test_a_pr_never_seen_has_no_state(engine):
    assert pr_state.load("ws-1", "github", "o/r", 404, engine=engine) is None


def test_mark_reviewed_sets_the_baseline_only_for_a_known_pr(engine):
    assert pr_state.mark_reviewed("ws-1", "github", "o/r", 1, "abc", engine=engine) is False
    _push(engine, "a" * 12, 0)
    assert pr_state.mark_reviewed("ws-1", "github", "o/r", 1, "abcdef0", now=_at(1),
                                  engine=engine) is True
    assert pr_state.load("ws-1", "github", "o/r", 1, engine=engine).last_reviewed_sha == "abcdef0"
    assert pr_state.mark_reviewed("ws-1", "github", "o/r", 1, "", engine=engine) is False


# ─── the webhook ─────────────────────────────────────────────────────

GATES = {"ignored_title_keywords": [], "review_cadence": "auto_pause",
         "auto_pause_pushes": 3, "auto_pause_window_minutes": 15,
         "status_feedback": False}


@pytest.fixture
def rows(monkeypatch, engine):
    import src.review.issues as issues_mod

    monkeypatch.setattr(issues_mod, "_ENGINE", engine)
    return engine


def _gates(monkeypatch, **over):
    import src.review.review_defaults as rd

    monkeypatch.setattr(rd, "gate_settings_for_repo",
                        lambda provider, repo: {**GATES, **over})


def _deliver(head: str, *, number: int = 7):
    from src.review.webhook import _dispatch_review

    asyncio.run(_dispatch_review(
        "github", "acme/payments", number, head_sha=head, event="pull_request",
        expected_workspace_id="ws-1", pr_meta={"title": "Add caching"}))


def test_the_third_push_is_not_queued_and_leaves_a_run_row(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch)

    queued = []
    for head in ("a" * 12, "b" * 12, "c" * 12, "d" * 12):
        _deliver(head)
        queued.append(len(queue))
        queue.clear()

    assert queued == [1, 1, 0, 0]
    rows_ = store.list_for_pr("ws-1", "github", "acme/payments", 7)
    assert rows_ and all(r.status == "skipped" for r in rows_)
    assert "start-review" in rows_[0].status_reason or "resume" in rows_[0].status_reason.lower()


def test_a_paused_pr_is_queued_once_more_when_the_note_is_still_to_be_posted(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    """With status notes on, the push that pauses the PR queues one job: the
    orchestrator's gate skips it and posts the one note that says how to resume."""
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch, status_feedback=True)

    for head in ("a" * 12, "b" * 12):
        _deliver(head)
        queue.clear()
    _deliver("c" * 12)
    assert len(queue) == 1

    pr_state.claim_notice("ws-1", "github", "acme/payments", 7, engine=rows)
    queue.clear()
    _deliver("d" * 12)
    assert queue == [], "the note was posted: later pushes are only written down"


def test_one_noisy_pr_does_not_pause_its_neighbour(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch)
    for head in ("a" * 12, "b" * 12, "c" * 12):
        _deliver(head)
    queue.clear()

    _deliver("a" * 12, number=8)
    assert len(queue) == 1


def test_an_unreadable_pr_table_does_not_stop_a_review(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(pr_state, "register_push", boom)
    _deliver("a" * 12)
    assert len(queue) == 1
