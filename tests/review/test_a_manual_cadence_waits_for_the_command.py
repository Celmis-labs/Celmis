"""`review_cadence = manual`: nothing is reviewed until a person asks.

Also the one contract every trigger speaks: `ReviewRequest` and which gates it
may skip.
"""

from __future__ import annotations

import asyncio

import pytest

from src.review.dispatch import execute_review
from src.review.scope import AUTOMATIC, ReviewRequest
from src.review.stages import StageRecorder
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _Agent,
    _finding,
    _keys,
    _orch,
    _Provider,
    _stage,
    bound,
    no_ledger,
    queue,
    store,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    _pr,
    env,  # noqa: F401
)

# ─── the request contract ────────────────────────────────────────────

SOFT = ("gate_draft", "gate_title", "gate_cadence")


@pytest.mark.parametrize("trigger", ["webhook", "poller"])
def test_an_automatic_trigger_meets_every_gate(trigger):
    request = ReviewRequest(trigger=trigger)
    assert not request.explicit
    assert not any(request.bypasses(g) for g in (*SOFT, "gate_target_branch"))


@pytest.mark.parametrize("trigger", ["command", "manual", "bulk", "cli", "mcp"])
def test_a_person_skips_the_soft_gates_but_not_the_branch_rule(trigger):
    request = ReviewRequest(trigger=trigger)
    assert request.explicit
    assert all(request.bypasses(g) for g in SOFT)
    assert not request.bypasses("gate_target_branch")


@pytest.mark.parametrize("trigger", ["webhook", "command"])
def test_force_also_skips_the_target_branch_rule(trigger):
    request = ReviewRequest(trigger=trigger, force=True)
    assert all(request.bypasses(g) for g in (*SOFT, "gate_target_branch"))


@pytest.mark.parametrize("gate", ["gate_enabled", "gate_size", "gate_hunks"])
def test_nothing_skips_the_switch_the_size_or_the_empty_diff(gate):
    assert not ReviewRequest(trigger="command", force=True).bypasses(gate)


def test_the_payload_round_trips_and_a_plain_job_stays_small():
    assert ReviewRequest(trigger="webhook").as_payload() == {"trigger": "webhook"}
    full = ReviewRequest(trigger="command", force=True, scope="full", resume=True)
    assert full.as_payload() == {"trigger": "command", "force": True,
                                 "scope": "full", "resume": True}
    assert ReviewRequest.from_payload(full.as_payload()) == full


@pytest.mark.parametrize("payload, trigger", [
    ({"source": "webhook"}, "webhook"),
    ({"source": "poller"}, "poller"),
    ({"source": "manual"}, "manual"),
    ({"trigger": "command", "source": "webhook"}, "command"),
    ({}, "manual"),
    ({"source": "something-else"}, "manual"),
])
def test_an_older_job_is_read_from_its_source_and_never_taken_for_a_push(payload, trigger):
    assert ReviewRequest.from_payload(payload).trigger == trigger


def test_a_bad_scope_is_ignored():
    assert ReviewRequest.from_payload({"trigger": "cli", "scope": "everything"}).scope is None


def test_the_default_request_is_automatic():
    assert AUTOMATIC.trigger == "webhook" and not AUTOMATIC.force


# ─── the orchestrator ────────────────────────────────────────────────


def _review(monkeypatch, *, request=None, **policy):
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])],
                 policy={**POLICY, "review_cadence": "manual", **policy})
    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=_Provider(_pr()), stages=rec,
                         workspace_id="ws-1", request=request)
    return result, rec


def test_a_push_is_not_reviewed_under_a_manual_cadence(env, monkeypatch):  # noqa: F811
    result, rec = _review(monkeypatch)

    assert result.batch.run_status.value == "skipped"
    assert _keys(rec)[-1] == "gate_cadence"
    assert "@celmis" in _stage(rec, "gate_cadence")["reason"]
    assert result.batch.findings == []


@pytest.mark.parametrize("trigger", ["command", "manual", "bulk", "cli", "mcp"])
def test_a_request_is_reviewed_under_a_manual_cadence(env, monkeypatch, trigger):  # noqa: F811
    result, rec = _review(monkeypatch, request=ReviewRequest(trigger=trigger))

    assert result.batch.run_status.value != "skipped"
    assert [f.agent for f in result.batch.findings] == ["defect"]
    assert _stage(rec, "gate_cadence")["status"] == "success"


def test_the_draft_gate_runs_before_the_context_and_a_person_skips_it(env, monkeypatch):  # noqa: F811
    pr = _pr(draft=True)
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])], policy=POLICY)
    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec)
    assert result.batch.run_status.value == "skipped"
    assert "context" not in _keys(rec), "a draft costs no context build"

    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec,
                         request=ReviewRequest(trigger="manual"))
    assert result.batch.run_status.value != "skipped"


def test_the_target_branch_rule_holds_for_a_person_until_it_is_forced(env, monkeypatch):  # noqa: F811
    policy = {**POLICY, "target_branches": ["release/*"]}
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])], policy=policy)

    for request, skipped in ((ReviewRequest(trigger="manual"), True),
                             (ReviewRequest(trigger="manual", force=True), False)):
        result = orch.review("github", "o/r", 1, provider=_Provider(_pr()),
                             stages=StageRecorder(), request=request)
        assert (result.batch.run_status.value == "skipped") is skipped


# ─── the queue ───────────────────────────────────────────────────────


def test_the_job_carries_the_request_to_the_worker(monkeypatch):
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    seen: dict = {}

    class _Orch:
        def review(self, *a, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", lambda: _Orch())
    monkeypatch.setattr(providers_mod, "get_provider_for", lambda *a, **kw: object())
    import src.review.issues as issues_mod
    monkeypatch.setattr(issues_mod, "record_failed_review", lambda **kw: None)

    payload = {"provider": "github", "repo": "o/r", "pr_number": 1,
               "workspace_id": "ws-1", "user_id": "u1", "source": "api",
               "trigger": "command", "force": True, "resume": True}
    with pytest.raises(Exception):  # noqa: B017 — the stub stops the run
        execute_review(payload)

    assert seen["request"] == ReviewRequest(trigger="command", force=True, resume=True)


def test_a_forced_request_has_its_own_dedup_key(store, monkeypatch):  # noqa: F811
    import src.sync.queue as q
    from src.review.dispatch import enqueue_review_run

    keys: list[str] = []
    monkeypatch.setattr(q, "enqueue", lambda **kw: keys.append(kw["dedup_key"]) or "job")

    kw = dict(user_id="u1", workspace_id="ws-1", source="api", store=store)
    enqueue_review_run("github", "o/r", 1, **kw, request=ReviewRequest(trigger="manual"))
    enqueue_review_run("github", "o/r", 1, **kw,
                       request=ReviewRequest(trigger="manual", force=True))
    assert len(keys) == 2 and keys[1] == keys[0] + ":force"


# ─── the webhook ─────────────────────────────────────────────────────

GATES = {"ignored_title_keywords": [], "review_cadence": "manual",
         "auto_pause_pushes": 3, "auto_pause_window_minutes": 15,
         "status_feedback": False}


def _gates(monkeypatch, **over):
    import src.review.review_defaults as rd

    monkeypatch.setattr(rd, "gate_settings_for_repo",
                        lambda provider, repo: {**GATES, **over})


def _deliver(head="a" * 12):
    from src.review.webhook import _dispatch_review

    asyncio.run(_dispatch_review(
        "github", "acme/payments", 7, head_sha=head, event="pull_request",
        expected_workspace_id="ws-1", pr_meta={"title": "Add caching"}))


@pytest.fixture
def rows(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.pool import StaticPool

    import src.review.issues as issues_mod
    from src.db.models import ReviewPullRequest

    @compiles(JSONB, "sqlite")
    def _json(type_, compiler, **kw) -> str:  # pragma: no cover
        return "JSON"

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(eng)
    monkeypatch.setattr(issues_mod, "_ENGINE", eng)
    yield eng
    eng.dispose()


def test_a_push_to_a_manual_repository_queues_nothing_and_says_why(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch)

    _deliver()

    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 7)
    assert row.status == "skipped" and "@celmis" in row.status_reason


def test_with_status_notes_on_one_job_is_queued_for_the_note(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch, status_feedback=True)

    _deliver()
    assert len(queue) == 1, "the orchestrator's gate posts the one note"
    queue.clear()

    from src.review import pr_state
    pr_state.claim_notice("ws-1", "github", "acme/payments", 7, engine=rows)
    _deliver("b" * 12)
    assert queue == []


def test_an_automatic_repository_is_untouched(
    bound, store, queue, no_ledger, rows, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch, review_cadence="automatic")

    _deliver()
    assert len(queue) == 1
