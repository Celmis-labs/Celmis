"""Opt-in: an install that never switches a repository on spends no API calls and writes no rows."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.db.models import ProductivityPullRequest, ProductivitySyncState
from src.productivity import scheduler
from src.productivity import settings as settings_mod
from tests.productivity.support import (
    PROVIDER,
    REPO,
    WS,
    FakeProvider,
    enable,
    make_engine,
    pr,
    prs_in,
)


def test_a_repository_nobody_enabled_is_not_synced() -> None:
    engine = make_engine()
    provider = FakeProvider([pr(1)])
    built = []
    from src.productivity.sync import run_repo_sync
    from tests.productivity.support import NOW

    result = run_repo_sync(WS, PROVIDER, REPO, engine=engine, now=NOW,
                           provider_factory=lambda cfg: built.append(cfg) or provider)
    assert result.status == "disabled"
    assert built == [] and provider.list_since == []
    with Session(engine) as s:
        assert s.query(ProductivityPullRequest).count() == 0
        assert s.query(ProductivitySyncState).count() == 0


def test_the_built_in_default_is_off() -> None:
    assert settings_mod.BUILTIN.enabled is False
    assert settings_mod.load(WS, PROVIDER, REPO, engine=make_engine()).enabled is False


def test_a_workspace_default_switches_every_repository_on_and_a_repo_can_opt_out() -> None:
    engine = make_engine()
    settings_mod.save(WS, "", "", {"enabled": True}, engine=engine)
    assert settings_mod.load(WS, PROVIDER, REPO, engine=engine).enabled is True
    settings_mod.save(WS, PROVIDER, REPO, {"enabled": False}, engine=engine)
    assert settings_mod.load(WS, PROVIDER, REPO, engine=engine).enabled is False
    assert settings_mod.load(WS, PROVIDER, "acme/other", engine=engine).enabled is True
    assert settings_mod.load("another-ws", PROVIDER, REPO, engine=engine).enabled is False


def _store(*repos):
    return SimpleNamespace(list_all=lambda: [
        SimpleNamespace(workspace_id=w, provider=p, full_name=f, repo_slug=f.replace("/", "-"), user_id="u1")
        for w, p, f in repos])


def test_a_tick_with_nothing_enabled_queues_nothing_and_reads_no_repositories() -> None:
    engine = make_engine()
    asked = []
    queued = scheduler.tick_once(
        engine=engine, store=SimpleNamespace(list_all=lambda: asked.append(1) or []),
        enqueue=lambda **kw: "id")
    assert queued == 0 and asked == []


def test_a_tick_queues_one_job_for_each_enabled_repository_only() -> None:
    engine = make_engine()
    enable(engine)       # acme/app on bitbucket in ws-1
    jobs = []

    def enqueue(**kw):
        jobs.append(kw)
        return f"job-{len(jobs)}"

    queued = scheduler.tick_once(
        engine=engine, enqueue=enqueue,
        store=_store((WS, PROVIDER, REPO), (WS, PROVIDER, "acme/other"), ("ws-2", PROVIDER, REPO)))
    assert queued == 1
    (job,) = jobs
    assert job["dedup_key"] == f"prodsync:{WS}:{PROVIDER}:{REPO}"
    assert job["payload"]["repo"] == REPO and job["payload"]["user_id"] == "u1"
    assert job["workspace_id"] == WS


def test_a_tick_that_meets_a_queued_job_does_not_count_it_twice() -> None:
    engine = make_engine()
    enable(engine)
    assert scheduler.tick_once(engine=engine, enqueue=lambda **kw: None, store=_store((WS, PROVIDER, REPO))) == 0


def test_the_scheduler_is_off_at_zero_minutes(monkeypatch) -> None:
    monkeypatch.setenv("CELMIS_PRODUCTIVITY_INTERVAL_MINUTES", "0")
    scheduler.stop_productivity_scheduler()
    scheduler.start_productivity_scheduler()
    assert scheduler._TASK is None


def test_a_typo_in_the_interval_falls_back_instead_of_killing_the_tick(monkeypatch) -> None:
    monkeypatch.setenv("CELMIS_PRODUCTIVITY_INTERVAL_MINUTES", "hourly")
    assert scheduler._minutes() == 60.0


def test_enabling_a_repository_does_not_start_anything_by_itself() -> None:
    engine = make_engine()
    enable(engine)
    assert prs_in(engine) == {}
