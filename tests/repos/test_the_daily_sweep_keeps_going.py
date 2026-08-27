"""A sweep that stops at the first unreachable remote is not a sweep.

The daily pass looks at every registered repository. On a real installation
one of them will be a repository whose token expired, or which was renamed, or
whose provider is having an afternoon. If that ends the pass, every repository
after it in the list is silently never checked — and the failure is invisible,
because the ones that did get checked look fine.

The stagger is the other half. Fifty repositories asked in the same second is
fifty git processes and fifty requests to one provider, which is how a
scheduled task earns a rate limit for the whole workspace.
"""
from __future__ import annotations

import pytest

from src.repos import refresh_scheduler
from src.repos.freshness import FreshnessCheck


class _Cfg:
    def __init__(self, slug, ws="ws-1"):
        self.repo_slug = slug
        self.workspace_id = ws
        self.user_id = "u1"


def _store(monkeypatch, configs, *, raises=False):
    class _S:
        def list_all(self):
            if raises:
                raise RuntimeError("no database")
            return configs
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: _S())


@pytest.mark.asyncio
async def test_every_repository_is_looked_at(monkeypatch):
    _store(monkeypatch, [_Cfg("a"), _Cfg("b"), _Cfg("c")])
    seen = []
    monkeypatch.setattr(
        "src.repos.freshness.check_repo",
        lambda slug, **kw: seen.append(slug) or FreshnessCheck(slug, "up_to_date"))
    summary = await refresh_scheduler.sweep_once(stagger=0)
    assert seen == ["a", "b", "c"]
    assert summary["checked"] == 3 and summary["up_to_date"] == 3


@pytest.mark.asyncio
async def test_one_exploding_repository_does_not_end_the_pass(monkeypatch):
    _store(monkeypatch, [_Cfg("a"), _Cfg("boom"), _Cfg("c")])

    def _check(slug, **kw):
        if slug == "boom":
            raise RuntimeError("provider on fire")
        return FreshnessCheck(slug, "up_to_date")

    monkeypatch.setattr("src.repos.freshness.check_repo", _check)
    summary = await refresh_scheduler.sweep_once(stagger=0)
    assert summary["checked"] == 3, "the pass stopped at the failure"
    assert summary["unreachable"] == 1
    assert summary["up_to_date"] == 2


@pytest.mark.asyncio
async def test_the_summary_counts_each_outcome_separately(monkeypatch):
    """"Checked 4" tells an operator nothing. Which four does."""
    _store(monkeypatch, [_Cfg("a"), _Cfg("b"), _Cfg("c"), _Cfg("d")])
    states = {"a": "up_to_date", "b": "behind", "c": "unreachable",
              "d": "never_indexed"}
    monkeypatch.setattr("src.repos.freshness.check_repo",
                        lambda slug, **kw: FreshnessCheck(slug, states[slug]))
    s = await refresh_scheduler.sweep_once(stagger=0)
    assert (s["up_to_date"], s["behind"], s["unreachable"], s["never_indexed"]) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_a_database_that_will_not_list_is_not_a_crash(monkeypatch):
    _store(monkeypatch, [], raises=True)
    s = await refresh_scheduler.sweep_once(stagger=0)
    assert s["checked"] == 0


@pytest.mark.asyncio
async def test_each_repository_is_checked_in_its_own_workspace(monkeypatch):
    """A shared credential would be a cross-tenant read.

    The sweep runs outside any request, so nothing else supplies the tenant.
    Passing the wrong one would have this instance ask a provider with one
    workspace's token about another workspace's repository.
    """
    _store(monkeypatch, [_Cfg("a", ws="ws-1"), _Cfg("b", ws="ws-2")])
    seen = {}

    def _check(slug, **kw):
        seen[slug] = kw["workspace_id"]
        return FreshnessCheck(slug, "up_to_date")

    monkeypatch.setattr("src.repos.freshness.check_repo", _check)
    await refresh_scheduler.sweep_once(stagger=0)
    assert seen == {"a": "ws-1", "b": "ws-2"}


# ─── the schedule itself ─────────────────────────────────────────────

def test_a_zero_interval_disables_the_sweep(monkeypatch):
    """Driving indexing from webhooks alone is a supported choice.

    Achieving it by setting the interval to a million hours is not the same
    as saying so, and reads in a config file as a mistake.
    """
    monkeypatch.setenv("CELMIS_REFRESH_INTERVAL_HOURS", "0")
    refresh_scheduler.stop_refresh_scheduler()
    refresh_scheduler.start_refresh_scheduler()
    assert refresh_scheduler._TASK is None


def test_a_nonsense_interval_falls_back_to_daily_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("CELMIS_REFRESH_INTERVAL_HOURS", "every-day-please")
    assert refresh_scheduler._hours() == 24.0


def test_starting_twice_does_not_start_twice(monkeypatch):
    """Two sweeps means every repository checked twice and every count doubled."""
    import asyncio

    async def _main():
        monkeypatch.setenv("CELMIS_REFRESH_INTERVAL_HOURS", "24")
        refresh_scheduler.stop_refresh_scheduler()
        refresh_scheduler.start_refresh_scheduler()
        first = refresh_scheduler._TASK
        refresh_scheduler.start_refresh_scheduler()
        assert refresh_scheduler._TASK is first
        refresh_scheduler.stop_refresh_scheduler()

    asyncio.run(_main())


def test_a_typo_in_any_setting_does_not_kill_the_task(monkeypatch):
    """`_hours` was guarded and the other two were not.

    Both bare `float()` calls sat INSIDE the task, before its loop, so
    `CELMIS_REFRESH_STAGGER_SECONDS=5s` would raise on the first tick and the
    sweep would never run again — leaving an unhandled-task warning nobody
    reads and an index that quietly stops updating. Which is the failure this
    feature exists to prevent, one level up.
    """
    for name, default in (("CELMIS_REFRESH_INTERVAL_HOURS", 24.0),
                          ("CELMIS_REFRESH_STAGGER_SECONDS", 5.0),
                          ("CELMIS_REFRESH_FIRST_DELAY_SECONDS", 120.0)):
        monkeypatch.setenv(name, "every-day-please")
        assert refresh_scheduler._number(name, default) == default
        monkeypatch.setenv(name, "")
        assert refresh_scheduler._number(name, default) == default
        monkeypatch.delenv(name)
        assert refresh_scheduler._number(name, default) == default


def test_a_valid_setting_is_still_honoured(monkeypatch):
    monkeypatch.setenv("CELMIS_REFRESH_STAGGER_SECONDS", "0.5")
    assert refresh_scheduler._number("CELMIS_REFRESH_STAGGER_SECONDS", 5.0) == 0.5
