"""The deferred list is looked at on a timer, and let go of when it must be.

Pinned here, against a real database file:

  * an available group moves back to 'pending', oldest first, and the PR is
    reviewed at whatever its head is by then (the job carries no sha);
  * one probe per (workspace, set of models), not per job;
  * an unavailable group waits 1, 2, 5, 10, then 15 minutes between checks —
    and a job that is not due is left alone;
  * two processors at the same moment release every job exactly once;
  * a job whose models cannot even be resolved keeps waiting, not released blind;
  * after CELMIS_REVIEW_DEFER_MAX_HOURS the job is dead, its run row failed with
    the reason, a placeholder on the PR rewritten (never a finished summary),
    and ERROR is logged once;
  * a restart loses nothing — the list is a table.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

import src.review.model_preflight as mp
import src.review.model_wait as mw
import src.sync.queue as jq
from src.review.model_preflight import ModelCheck, ReviewModels
from tests.sync.deferral_support import as_dt, insert, make_engine, row, use


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = make_engine(tmp_path / "jobs.db")
    use(monkeypatch, e)
    return e


class _Model:
    """Scripts review_models/check_models: which models a repo uses, what each
    workspace's probe says, and how many probes were sent."""

    def __init__(self, monkeypatch, *, models=("m-1",)):
        self.models = models
        self.answers: dict[str, ModelCheck] = {}
        self.probes: list[str] = []
        self._lock = threading.Lock()
        monkeypatch.setattr(mp, "review_models", self._review_models)
        monkeypatch.setattr(mp, "check_models", self._check_models)

    def _review_models(self, provider, repo, *, workspace_id, user_id):
        models = self.models[repo] if isinstance(self.models, dict) else self.models
        return ReviewModels(object(), [("defect", m) for m in models])

    def _check_models(self, workspace_id, models):
        with self._lock:
            self.probes.append(workspace_id)
        return self.answers.get(workspace_id, ModelCheck(True))

    def down(self, ws, code="provider_unavailable", reason="the provider is unavailable"):
        self.answers[ws] = ModelCheck(False, code, reason)


def _payload(ws="ws", repo="o/r", n=1):
    return {"provider": "github", "repo": repo, "pr_number": n, "workspace_id": ws,
            "user_id": "u", "run_id": f"run-{ws}-{repo}-{n}", "post_comments": False}


def test_an_available_model_releases_the_group_oldest_first(engine, monkeypatch):
    model = _Model(monkeypatch)
    old = insert(engine, payload=_payload(n=1), deferred_ago=timedelta(hours=3))
    new = insert(engine, payload=_payload(n=2), deferred_ago=timedelta(minutes=5))
    mid = insert(engine, payload=_payload(n=3), deferred_ago=timedelta(hours=1))

    counts = mw.process_deferred()

    assert counts["released"] == 3 and counts["waiting"] == 0
    assert model.probes == ["ws"], "three jobs, one workspace, one model: one probe"
    rows = {j: row(engine, j) for j in (old, mid, new)}
    assert {r["status"] for r in rows.values()} == {"pending"}
    order = sorted(rows, key=lambda j: as_dt(rows[j]["next_run_at"]))
    assert order == [old, mid, new]
    assert all(as_dt(r["next_run_at"]) <= datetime.now(UTC) + timedelta(seconds=1)
               for r in rows.values())
    assert all(r["attempts"] == 0 for r in rows.values())


def test_the_release_is_logged_with_counts_and_the_deferral_is_not_repeated(engine, monkeypatch, caplog):
    _Model(monkeypatch)
    insert(engine, payload=_payload(n=1))
    caplog.set_level(logging.INFO)
    mw.process_deferred()
    released = [r for r in caplog.records if "review_defer_released" in r.getMessage()]
    assert len(released) == 1 and released[0].levelno == logging.INFO
    assert "released=1" in released[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_groups_are_workspace_and_model_set_and_each_is_probed_once(engine, monkeypatch):
    model = _Model(monkeypatch, models={"o/a": ("m-1",), "o/b": ("m-1",), "o/c": ("m-2",)})
    model.down("ws-2")
    a = insert(engine, ws="ws-1", payload=_payload("ws-1", "o/a"))
    b = insert(engine, ws="ws-1", payload=_payload("ws-1", "o/b"))
    c = insert(engine, ws="ws-1", payload=_payload("ws-1", "o/c"))
    d = insert(engine, ws="ws-2", payload=_payload("ws-2", "o/a"))

    counts = mw.process_deferred()

    assert sorted(model.probes) == ["ws-1", "ws-1", "ws-2"], (
        "ws-1 has two model sets, ws-2 one: three probes for four jobs")
    assert counts["released"] == 3 and counts["waiting"] == 1
    assert [row(engine, j)["status"] for j in (a, b, c)] == ["pending"] * 3
    assert row(engine, d)["status"] == "deferred"
    assert row(engine, d)["defer_checks"] == 1


def test_a_group_that_is_still_down_waits_one_two_five_ten_fifteen_minutes(engine, monkeypatch):
    model = _Model(monkeypatch)
    model.down("ws", code="rate_limited", reason="the provider is rate-limiting this workspace")
    jid = insert(engine, payload=_payload(), checks=0)
    waits = []
    for _ in range(6):
        mw.process_deferred()
        r = row(engine, jid)
        assert r["status"] == "deferred"
        waits.append(round((as_dt(r["next_run_at"]) - datetime.now(UTC)).total_seconds() / 60))
        with engine.begin() as c:  # time passes: the next check is due
            c.exec_driver_sql("UPDATE sync_jobs SET next_run_at = ?",
                              ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(sep=" "),))
    assert waits == [2, 5, 10, 15, 15, 15]
    r = row(engine, jid)
    assert r["defer_code"] == "rate_limited" and r["attempts"] == 0
    assert r["defer_reason"] == "the provider is rate-limiting this workspace"


def test_a_job_whose_next_check_is_not_due_is_left_alone(engine, monkeypatch):
    model = _Model(monkeypatch)
    jid = insert(engine, payload=_payload(), next_in=timedelta(minutes=5))
    counts = mw.process_deferred()
    assert counts["claimed"] == 0 and model.probes == []
    assert row(engine, jid)["status"] == "deferred"


def test_a_job_that_is_not_a_review_is_not_this_processors_business(engine, monkeypatch):
    _Model(monkeypatch)
    jid = insert(engine, kind="index_repo", payload={"repo_slug": "x"})
    mw.process_deferred()
    assert row(engine, jid)["status"] == "deferred"


def test_two_processors_at_once_release_every_job_exactly_once(engine, monkeypatch):
    _Model(monkeypatch)
    ids = [insert(engine, payload=_payload(n=i), deferred_ago=timedelta(minutes=i + 1))
           for i in range(1, 13)]
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def _go():
        barrier.wait()
        results.append(mw.process_deferred())

    threads = [threading.Thread(target=_go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sum(r["released"] for r in results) == len(ids), results
    assert sum(r["claimed"] for r in results) == len(ids), (
        "a row claimed by one processor must not be claimed by the other")
    assert {row(engine, j)["status"] for j in ids} == {"pending"}


def test_a_claimed_job_is_not_due_for_a_sibling_while_it_is_probed(engine):
    insert(engine, payload=_payload())
    first = jq.claim_due_deferred("review")
    second = jq.claim_due_deferred("review")
    assert len(first) == 1 and second == []
    # a processor that died would hand the row back after the claim window
    r = row(engine, first[0]["id"])
    assert as_dt(r["next_run_at"]) > datetime.now(UTC) + timedelta(seconds=60)


def test_a_job_released_by_hand_meanwhile_is_not_released_twice(engine):
    jid = insert(engine, payload=_payload())
    (claimed,) = jq.claim_due_deferred("review")
    assert jq.retry_dead(jid) is True        # somebody pressed "retry now"
    assert jq.release_deferred([claimed["id"]]) == 0
    assert row(engine, jid)["status"] == "pending"


def test_models_that_cannot_be_resolved_keep_the_job_waiting(engine, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(mp, "review_models", _boom)
    jid = insert(engine, payload=_payload())
    counts = mw.process_deferred()
    assert counts["released"] == 0 and counts["waiting"] == 1
    assert row(engine, jid)["status"] == "deferred"


def test_a_restart_loses_nothing(engine, tmp_path, monkeypatch):
    _Model(monkeypatch)
    jid = insert(engine, payload=_payload())
    engine.dispose()
    use(monkeypatch, make_engine(tmp_path / "jobs.db"))  # a new process, the same database
    assert mw.process_deferred()["released"] == 1
    assert row(jq._engine(), jid)["status"] == "pending"


def test_the_knobs_have_defaults_and_are_read(monkeypatch):
    monkeypatch.delenv("CELMIS_REVIEW_DEFER_CHECK_SECONDS", raising=False)
    monkeypatch.delenv("CELMIS_REVIEW_DEFER_MAX_HOURS", raising=False)
    assert mw.check_seconds() == 60 and mw.max_age() == timedelta(hours=24)
    monkeypatch.setenv("CELMIS_REVIEW_DEFER_CHECK_SECONDS", "30")
    monkeypatch.setenv("CELMIS_REVIEW_DEFER_MAX_HOURS", "2")
    assert mw.check_seconds() == 30 and mw.max_age() == timedelta(hours=2)
    monkeypatch.setenv("CELMIS_REVIEW_DEFER_MAX_HOURS", "banana")
    assert mw.max_age() == timedelta(hours=24)


# ─── expiry ──────────────────────────────────────────────────────────


@pytest.fixture
def runs(tmp_path, monkeypatch):
    from src.api import review_runs as runs_mod
    from src.api.review_runs import ReviewRun, ReviewRunStore
    from src.review.dispatch import _park_run
    from src.review.stages import StageRecorder

    store = ReviewRunStore(tmp_path / "runs.db")
    monkeypatch.setattr(runs_mod, "_default_store", store)
    import src.review.issues as issues_mod

    monkeypatch.setattr(issues_mod, "record_failed_review", lambda **kw: True)

    def make(run_id="run-1"):
        store.insert(ReviewRun(id=run_id, user_id="u", pr_ref="github:o/r#1",
                               workspace_id="ws", pr_provider="github", pr_repo="o/r",
                               pr_number=1))
        stages = StageRecorder(sink=runs_mod.run_stage_sink(run_id, store))
        _park_run(store, run_id, stages, "the provider is unavailable")
        return store

    store.make = make
    return store


def test_a_job_deferred_past_the_limit_dies_with_a_reason_and_fails_its_run(
        engine, runs, monkeypatch, caplog):
    _Model(monkeypatch)
    runs.make("run-1")
    jid = insert(engine, payload={**_payload(), "run_id": "run-1"},
                 deferred_ago=timedelta(hours=25))
    fresh = insert(engine, payload=_payload(n=2), deferred_ago=timedelta(hours=2))
    caplog.set_level(logging.INFO)

    counts = mw.process_deferred()

    assert counts["expired"] == 1
    r = row(engine, jid)
    assert r["status"] == "dead" and r["last_error"] == "model unavailable for 24 h"
    assert r["finished_at"] is not None and r["attempts"] == 0
    assert row(engine, fresh)["status"] == "pending", "the young job is still released"

    run = runs.get("run-1")
    assert run.status == "failed" and run.finished_at
    assert "model unavailable for 24 h" in run.summary
    assert run.stages[-1]["key"] == "finished" and run.stages[-1]["status"] == "failed"
    wait = next(s for s in run.stages if s["key"] == "model_wait")
    assert wait["status"] == "failed"

    errors = [x for x in caplog.records if x.levelno == logging.ERROR]
    assert len(errors) == 1 and "review_deferred_expired" in errors[0].getMessage()
    assert "NOT done" in errors[0].getMessage()
    mw.process_deferred()
    assert len([x for x in caplog.records if x.levelno == logging.ERROR]) == 1, (
        "ERROR once per job, not once per tick")


def test_the_limit_is_the_knob(engine, runs, monkeypatch):
    _Model(monkeypatch)
    runs.make("run-1")
    jid = insert(engine, payload={**_payload(), "run_id": "run-1"},
                 deferred_ago=timedelta(hours=3))
    monkeypatch.setenv("CELMIS_REVIEW_DEFER_MAX_HOURS", "2")
    mw.process_deferred()
    r = row(engine, jid)
    assert r["status"] == "dead" and r["last_error"] == "model unavailable for 2 h"


def _github(monkeypatch, comment_body):
    import src.api.routers.llm as llm_router
    import src.review.providers as providers_mod
    from tests.review.test_a_rerun_does_not_double_the_comments import _FakeGitHub
    from tests.review.test_the_pr_hears_the_review_begin_and_end import _pr, _Provider

    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})
    fake = _FakeGitHub()
    cid = fake.add_issue(comment_body)
    provider = _Provider(fake, _pr())
    monkeypatch.setattr(providers_mod, "get_provider_for", lambda *a, **kw: provider)
    return fake, cid


def test_an_expired_review_tells_the_pr_it_was_not_done_over_its_placeholder(
        engine, runs, monkeypatch):
    from tests.review.test_a_rerun_does_not_double_the_comments import MARKER
    from tests.review.test_the_pr_hears_the_review_begin_and_end import (
        STATUS_IN_PROGRESS_MARK,
        _install_settings,
    )

    _install_settings(monkeypatch)
    _Model(monkeypatch)
    fake, cid = _github(monkeypatch, f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n## postponed")
    runs.make("run-1")
    insert(engine, payload={**_payload(), "run_id": "run-1", "post_comments": True},
           deferred_ago=timedelta(hours=30))

    mw.process_deferred()

    [comment] = fake.issue
    assert comment["id"] == cid, "the same comment is rewritten, never a second one"
    assert "Review not done: the model stayed unavailable for 24 h" in comment["body"]
    assert "@celmis review" in comment["body"]
    assert STATUS_IN_PROGRESS_MARK not in comment["body"]


def test_an_expired_review_never_overwrites_a_finished_summary(engine, runs, monkeypatch):
    from tests.review.test_a_rerun_does_not_double_the_comments import MARKER
    from tests.review.test_the_pr_hears_the_review_begin_and_end import _install_settings

    _install_settings(monkeypatch)
    _Model(monkeypatch)
    fake, cid = _github(monkeypatch, f"{MARKER}\n## Code Review Completed\nall good")
    runs.make("run-1")
    insert(engine, payload={**_payload(), "run_id": "run-1", "post_comments": True},
           deferred_ago=timedelta(hours=30))

    mw.process_deferred()

    [comment] = fake.issue
    assert "all good" in comment["body"] and "not done" not in comment["body"]


def test_an_expired_review_with_comments_off_touches_no_pr(engine, runs, monkeypatch):
    _Model(monkeypatch)
    import src.review.providers as providers_mod

    def _no(*a, **kw):
        raise AssertionError("the provider must not be opened")

    monkeypatch.setattr(providers_mod, "get_provider_for", _no)
    runs.make("run-1")
    insert(engine, payload={**_payload(), "run_id": "run-1", "post_comments": False},
           deferred_ago=timedelta(hours=30))
    mw.process_deferred()
    assert runs.get("run-1").status == "failed"


# ─── Expiry on a PR that never had a placeholder ─────────────────────


def test_an_expired_review_with_no_comment_on_the_pr_leaves_one_note(
        engine, runs, monkeypatch):
    from tests.review.test_the_pr_hears_the_review_begin_and_end import _install_settings

    _install_settings(monkeypatch)
    _Model(monkeypatch)
    fake, _ = _github(monkeypatch, "an unrelated human comment")
    fake.issue.clear()
    runs.make("run-1")
    insert(engine, payload={**_payload(), "run_id": "run-1", "post_comments": True},
           deferred_ago=timedelta(hours=30))

    mw.process_deferred()

    [comment] = fake.issue
    assert "Review not done: the model stayed unavailable for 24 h" in comment["body"]
    assert "@celmis review" in comment["body"]


# ─── Cancelling a waiting review ─────────────────────────────────────


def test_cancel_deferred_only_touches_a_waiting_row(engine):
    waiting = insert(engine)
    running = insert(engine, status="running", deferred_ago=None)

    got = jq.cancel_deferred(waiting)
    assert got is not None and got["kind"] == "review"
    r = row(engine, waiting)
    assert r["status"] == "cancelled" and r["finished_at"] is not None
    assert r["last_error"] == "cancelled by an administrator"
    assert jq.cancel_deferred(waiting) is None, "a second cancel finds nothing to cancel"
    assert jq.cancel_deferred(running) is None
    assert row(engine, running)["status"] == "running"
    # and the processor no longer sees it
    assert jq.claim_due_deferred(jq.KIND_REVIEW) == []


def test_the_cancel_endpoint_closes_the_run_and_rewrites_the_placeholder(
        engine, runs, monkeypatch):
    from types import SimpleNamespace

    import src.api.routers.jobs as jobs_router
    from tests.review.test_a_rerun_does_not_double_the_comments import MARKER
    from tests.review.test_the_pr_hears_the_review_begin_and_end import (
        STATUS_IN_PROGRESS_MARK,
        _install_settings,
    )

    _install_settings(monkeypatch)
    fake, cid = _github(monkeypatch, f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n## postponed")
    runs.make("run-1")
    jid = insert(engine, payload={**_payload(), "run_id": "run-1", "post_comments": True})
    admin = SimpleNamespace(is_admin=True, email="admin@example.com")

    assert jobs_router.cancel(jid, user=admin, workspace_id="ws") == {"ok": True}

    assert row(engine, jid)["status"] == "cancelled"
    run = runs.get("run-1")
    assert run.status == "failed" and "cancelled by an administrator" in run.summary
    [comment] = fake.issue
    assert comment["id"] == cid
    assert "cancelled by an administrator" in comment["body"]
    assert "will run again automatically" not in comment["body"]


def test_deleting_a_waiting_job_closes_its_run_first(engine, runs, monkeypatch):
    from types import SimpleNamespace

    import src.api.routers.jobs as jobs_router

    runs.make("run-1")
    jid = insert(engine, payload={**_payload(), "run_id": "run-1"})
    admin = SimpleNamespace(is_admin=True, email="admin@example.com")

    jobs_router.delete_job(jid, user=admin, workspace_id="ws")

    assert jq.get_job(jid) is None
    assert runs.get("run-1").status == "failed", "the run is not left 'deferred' for good"


# ─── A late deferral from a worker that lost the row ─────────────────


def test_a_late_deferral_from_a_worker_that_lost_the_row_changes_nothing(engine):
    mine = insert(engine, status="running", deferred_ago=None)
    theirs = insert(engine, status="running", deferred_ago=None)
    import sqlalchemy as sa

    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE sync_jobs SET locked_by = :w WHERE id = :i"),
                     {"w": jq._worker_id(), "i": mine})
        conn.execute(sa.text("UPDATE sync_jobs SET locked_by = 'another-worker' "
                             "WHERE id = :i"), {"i": theirs})

    assert jq.mark_deferred(theirs, "the provider is unavailable") is False
    assert row(engine, theirs)["status"] == "running"
    assert jq.mark_deferred(mine, "the provider is unavailable") is True
    assert row(engine, mine)["status"] == "deferred"
