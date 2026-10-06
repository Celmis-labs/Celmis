"""A PR whose automatic reviews wait says so once, and says how to continue.

The note is posted by the orchestrator's `gate_cadence` (one writer), claimed
once per pause, in the repository's review language, naming the bot handle and
the commit the last review read. A person's request still reviews it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewPullRequest
from src.review import cadence, pr_state
from src.review.scope import ReviewRequest
from src.review.stages import StageRecorder
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _Agent,
    _finding,
    _keys,
    _orch,
    _Provider,
    _stage,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    _pr,
    env,  # noqa: F401
)


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


class _Noting(_Provider):
    def __init__(self, pr) -> None:
        super().__init__(pr)
        self.notes: list[str] = []

    def upsert_feedback_comment(self, pr, reason):
        self.notes.append(reason)
        return 1


def _pause(rows, *, reason="auto_pause", reviewed="1234567abcdef"):
    pr_state.register_push("ws-1", "github", "o/r", 1, "head00000001", engine=rows)
    if reviewed:
        pr_state.mark_reviewed("ws-1", "github", "o/r", 1, reviewed, engine=rows)
    pr_state.set_paused("ws-1", "github", "o/r", 1, reason=reason, engine=rows)


def _review(monkeypatch, provider, *, request=None, **policy):
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])],
                 policy={**POLICY, "review_cadence": "auto_pause", **policy})
    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=provider, stages=rec,
                         workspace_id="ws-1", request=request)
    return result, rec


def test_a_paused_pr_is_skipped_and_the_note_says_how_to_resume(env, monkeypatch, rows):  # noqa: F811
    _pause(rows)
    provider = _Noting(_pr())

    result, rec = _review(monkeypatch, provider)

    assert result.batch.run_status.value == "skipped"
    assert _keys(rec)[-1] == "gate_cadence"
    assert _stage(rec, "gate_cadence")["status"] == "skipped"
    (note,) = provider.notes
    assert "@celmis start-review" in note
    assert "1234567" in note, "it names the commit the last review read"


def test_the_note_is_posted_once_per_pause(env, monkeypatch, rows):  # noqa: F811
    _pause(rows)
    provider = _Noting(_pr())

    for _ in range(3):
        _review(monkeypatch, provider)

    assert len(provider.notes) == 1


def test_a_failed_post_can_be_claimed_again(rows):
    _pause(rows)
    assert pr_state.claim_notice("ws-1", "github", "o/r", 1, engine=rows) is True
    assert pr_state.claim_notice("ws-1", "github", "o/r", 1, engine=rows) is False
    pr_state.release_notice("ws-1", "github", "o/r", 1, engine=rows)
    assert pr_state.claim_notice("ws-1", "github", "o/r", 1, engine=rows) is True


def test_a_new_pause_after_a_resume_gets_its_own_note(rows):
    _pause(rows)
    assert pr_state.claim_notice("ws-1", "github", "o/r", 1, engine=rows)
    pr_state.resume("ws-1", "github", "o/r", 1, engine=rows)
    pr_state.set_paused("ws-1", "github", "o/r", 1, reason="manual", engine=rows)
    assert pr_state.claim_notice("ws-1", "github", "o/r", 1, engine=rows) is True


def test_the_note_is_written_in_the_review_language(env, monkeypatch, rows):  # noqa: F811
    _pause(rows)
    provider = _Noting(_pr())

    _review(monkeypatch, provider, review_language="uk")

    (note,) = provider.notes
    assert "@celmis start-review" in note
    assert "start-review" in note and not note.isascii(), "Ukrainian text, not English"


def test_a_pr_that_never_reviewed_does_not_name_a_commit(env, monkeypatch, rows):  # noqa: F811
    _pause(rows, reviewed=None)
    provider = _Noting(_pr())

    _review(monkeypatch, provider)

    (note,) = provider.notes
    assert "start-review" in note and "abcdef1" not in note


def test_no_note_when_the_repository_turned_status_notes_off(env, monkeypatch, rows):  # noqa: F811
    _pause(rows)
    provider = _Noting(_pr())

    result, _ = _review(monkeypatch, provider, status_feedback=False)

    assert result.batch.run_status.value == "skipped"
    assert provider.notes == []


@pytest.mark.parametrize("request_", [
    ReviewRequest(trigger="command", resume=True),
    ReviewRequest(trigger="manual"),
    ReviewRequest(trigger="cli"),
])
def test_a_persons_request_reviews_a_paused_pr(env, monkeypatch, rows, request_):  # noqa: F811
    _pause(rows)
    provider = _Noting(_pr())

    result, rec = _review(monkeypatch, provider, request=request_)

    assert result.batch.run_status.value != "skipped"
    assert provider.notes == []
    assert _stage(rec, "gate_cadence")["status"] == "success"


def test_a_person_pause_holds_even_when_the_cadence_is_automatic(env, monkeypatch, rows):  # noqa: F811
    _pause(rows, reason="manual")
    provider = _Noting(_pr())

    result, _ = _review(monkeypatch, provider, review_cadence="automatic")

    assert result.batch.run_status.value == "skipped"
    (note,) = provider.notes
    assert "@celmis start-review" in note


def test_an_auto_pause_lapses_when_the_repository_leaves_the_cadence(env, monkeypatch, rows):  # noqa: F811
    _pause(rows, reason="auto_pause")
    provider = _Noting(_pr())

    result, _ = _review(monkeypatch, provider, review_cadence="automatic")

    assert result.batch.run_status.value != "skipped"
    assert provider.notes == []


def test_an_unreadable_table_never_stops_a_review(env, monkeypatch):  # noqa: F811
    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(pr_state, "load", boom)
    provider = _Noting(_pr())

    result, rec = _review(monkeypatch, provider)

    assert result.batch.run_status.value != "skipped"
    assert _stage(rec, "gate_cadence")["status"] == "success"


@pytest.mark.parametrize("lang", ["en", "uk", "de"])
def test_every_sentence_of_the_pause_exists_in_every_language(lang):
    handle = "@celmis"
    for decision, reason in (
        (cadence.CadenceDecision("skip", "paused"), cadence.REASON_AUTO),
        (cadence.CadenceDecision("skip", "paused"), cadence.REASON_MANUAL),
        (cadence.CadenceDecision("skip", "cadence_manual"), None),
    ):
        text = cadence.notice(decision, lang, handle=handle, pushes=3, minutes=15,
                              reason=reason, last_reviewed_sha="abcdef1234")
        why = cadence.gate_reason(decision, lang, handle=handle, pushes=3,
                                  minutes=15, reason=reason)
        assert handle in text and handle in why
        assert "{" not in text and "{" not in why, "no placeholder left unfilled"


class _Failing(_Noting):
    """A provider whose note does not land: it raises, or answers nothing."""

    def __init__(self, pr, *, raises: bool) -> None:
        super().__init__(pr)
        self.raises = raises

    def upsert_feedback_comment(self, pr, reason):
        self.notes.append(reason)
        if self.raises:
            raise RuntimeError("provider is down")
        return None


@pytest.mark.parametrize("raises", [True, False], ids=["raises", "answers-nothing"])
def test_a_note_that_failed_to_post_is_tried_again_on_the_next_delivery(
    env, monkeypatch, rows, raises,  # noqa: F811
):
    _pause(rows)
    broken = _Failing(_pr(), raises=raises)

    _review(monkeypatch, broken)

    assert len(broken.notes) == 1
    assert pr_state.load("ws-1", "github", "o/r", 1, engine=rows).pause_notice_at is None, \
        "the claim is given back, or the PR stays paused with no word on how to resume"

    healthy = _Noting(_pr())
    _review(monkeypatch, healthy)
    assert len(healthy.notes) == 1


def test_a_lapsed_auto_pause_is_forgotten_when_a_review_goes_ahead(env, monkeypatch, rows):  # noqa: F811
    _pause(rows, reason="auto_pause")

    _review(monkeypatch, _Noting(_pr()), review_cadence="automatic")

    state = pr_state.load("ws-1", "github", "o/r", 1, engine=rows)
    assert not state.review_paused and state.paused_reason is None


def test_a_lapsed_auto_pause_does_not_come_back_with_the_cadence(rows):
    _pause(rows, reason="auto_pause")

    pr_state.register_push("ws-1", "github", "o/r", 1, "head00000002",
                           cadence_name="automatic", engine=rows)
    result = pr_state.register_push("ws-1", "github", "o/r", 1, "head00000003",
                                    cadence_name="auto_pause", engine=rows)

    assert not result.paused, "one push after switching back is not a burst"


def test_a_person_pause_survives_a_push_under_another_cadence(rows):
    _pause(rows, reason="manual")

    result = pr_state.register_push("ws-1", "github", "o/r", 1, "head00000002",
                                    cadence_name="automatic", engine=rows)

    assert result.paused and result.paused_reason == "manual"
