# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The settings page's API: who may save, what is refused, and what a save sets going.

Reading and saving are owner/admin only, like the rest of the page.
The values are the AGPL sync's own (src/productivity/settings.py) and its
validation is what answers a bad value. Switching a repository on queues its
first sync at once, so the first numbers do not wait for the hourly tick.
"""

from __future__ import annotations

import pytest

from tests.ee.productivity_support import PROVIDER, REPO, WS, api

SETTINGS = "/api/analytics/productivity/settings"
OTHER = {"provider": "github", "repo": "acme/api", "repo_slug": "github_acme-api", "user_id": "u-1"}
MINE = {"provider": PROVIDER, "repo": REPO, "repo_slug": "github_acme-shop", "user_id": "u-1"}


class Queue:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def __call__(self, **job):
        self.jobs.append(job)
        return f"job-{len(self.jobs)}"


@pytest.mark.parametrize(("role", "code"), [("viewer", 403), ("member", 403), ("editor", 403),
                                            ("admin", 200), ("owner", 200)])
async def test_only_an_owner_or_admin_may_change_the_settings(role, code, monkeypatch, tmp_path) -> None:
    async with api(role=role, monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        r = await c.put(SETTINGS, json={"changes": {"failure_window_days": 10}})
    assert r.status_code == code, r.text


async def test_an_admin_can_read_the_settings_with_their_layers(monkeypatch, tmp_path) -> None:
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        body = (await c.get(SETTINGS)).json()
    assert body["builtin"]["enabled"] is False and body["resolved"]["backfill_days"] == 180
    assert body["workspace"] == {} and body["repo"] == {}
    assert body["limits"]["deploy_sources"] == ["merge", "provider", "tags"]


async def test_a_repository_value_overrides_the_workspace_one_and_clearing_it_inherits(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        await c.put(SETTINGS, json={"changes": {"failure_window_days": 10}})
        await c.put(SETTINGS, json={**{k: MINE[k] for k in ("provider", "repo")},
                                    "changes": {"failure_window_days": 3}})
        repo = (await c.get(SETTINGS, params={"provider": PROVIDER, "repo": REPO})).json()
        assert repo["workspace"]["failure_window_days"] == 10
        assert repo["repo"]["failure_window_days"] == 3 and repo["resolved"]["failure_window_days"] == 3
        cleared = await c.put(SETTINGS, json={**{k: MINE[k] for k in ("provider", "repo")},
                                              "changes": {"failure_window_days": None}})
    assert cleared.json()["resolved"]["failure_window_days"] == 10


async def test_a_bad_value_or_an_unknown_setting_is_refused_and_nothing_is_saved(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        too_far = await c.put(SETTINGS, json={"changes": {"backfill_days": 100000}})
        pattern = await c.put(SETTINGS, json={"changes": {"revert_patterns": ["(unclosed"]}})
        unknown = await c.put(SETTINGS, json={"changes": {"enabled": True, "colour": "red"}})
        saved = (await c.get(SETTINGS)).json()
    assert too_far.status_code == 422 and pattern.status_code == 422 and unknown.status_code == 422
    assert saved["workspace"] == {}


async def test_a_repository_of_another_workspace_cannot_be_configured_or_queued(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, repos=[MINE]) as c:
        put = await c.put(SETTINGS, json={"provider": "github", "repo": "evil/repo", "changes": {"enabled": True}})
        get = await c.get(SETTINGS, params={"provider": "github", "repo": "evil/repo"})
        run = await c.post("/api/analytics/productivity/sync/run", json={"provider": "github", "repo": "evil/repo"})
        half = await c.put(SETTINGS, json={"provider": "github", "changes": {}})
    assert (put.status_code, get.status_code, run.status_code, half.status_code) == (404, 404, 404, 422)


async def test_switching_a_repository_on_queues_its_first_sync(monkeypatch, tmp_path) -> None:
    queue = Queue()
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, repos=[MINE, OTHER], enqueue=queue) as c:
        r = await c.put(SETTINGS, json={"provider": PROVIDER, "repo": REPO, "changes": {"enabled": True}})
    assert r.json()["queued"] == 1
    assert [(j["payload"]["repo"], j["workspace_id"]) for j in queue.jobs] == [(REPO, WS)]


async def test_switching_the_workspace_default_on_queues_every_repository_that_inherits_it(monkeypatch, tmp_path) -> None:
    queue = Queue()
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, repos=[MINE, OTHER], enqueue=queue) as c:
        await c.put(SETTINGS, json={"provider": "github", "repo": "acme/api", "changes": {"enabled": False}})
        r = await c.put(SETTINGS, json={"changes": {"enabled": True}})
    assert r.json()["queued"] == 1, "acme/api said no for itself"
    assert [j["payload"]["repo"] for j in queue.jobs] == [REPO]


async def test_a_save_that_does_not_switch_anything_on_queues_nothing(monkeypatch, tmp_path) -> None:
    queue = Queue()
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, enqueue=queue) as c:
        r = await c.put(SETTINGS, json={"changes": {"deploy_group_minutes": 10}})
    assert r.json()["queued"] == 0 and queue.jobs == []


async def test_a_manual_sync_needs_the_repository_to_be_switched_on(monkeypatch, tmp_path) -> None:
    queue = Queue()
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, enqueue=queue) as c:
        off = await c.post("/api/analytics/productivity/sync/run", json={"provider": PROVIDER, "repo": REPO})
    assert off.status_code == 409 and queue.jobs == []


async def test_an_admin_may_sync_now_and_may_read_everything_again(monkeypatch, tmp_path) -> None:
    queue = Queue()
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, enqueue=queue) as c:
        from src.productivity import settings as settings_mod

        settings_mod.save(WS, "", "", {"enabled": True}, engine=c.sync_engine)
        run = await c.post("/api/analytics/productivity/sync/run", json={"provider": PROVIDER, "repo": REPO})
        full = await c.post("/api/analytics/productivity/sync/run",
                            json={"provider": PROVIDER, "repo": REPO, "full": True})
    assert run.status_code == 200 and run.json()["queued"] is True
    assert full.status_code == 200
    assert [j["payload"]["full"] for j in queue.jobs] == [False, True]


async def test_an_admin_can_queue_a_full_re_read_and_a_second_request_says_one_is_already_queued(monkeypatch, tmp_path) -> None:
    jobs: list[dict] = []

    def enqueue(**job):
        if any(j["dedup_key"] == job["dedup_key"] for j in jobs):
            return None
        jobs.append(job)
        return "job-1"

    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, enqueue=enqueue) as c:
        from src.productivity import settings as settings_mod

        settings_mod.save(WS, "", "", {"enabled": True}, engine=c.sync_engine)
        first = await c.post("/api/analytics/productivity/sync/run",
                             json={"provider": PROVIDER, "repo": REPO, "full": True})
        again = await c.post("/api/analytics/productivity/sync/run", json={"provider": PROVIDER, "repo": REPO})
    assert first.json()["queued"] is True and jobs[0]["payload"]["full"] is True
    assert again.json()["queued"] is False and "already" in again.json()["detail"]


async def test_the_sync_panel_lists_every_connected_repository_with_its_progress(monkeypatch, tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import Session

    from src.db.models import ProductivitySyncState
    from src.productivity import settings as settings_mod

    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, repos=[MINE, OTHER]) as c:
        settings_mod.save(WS, PROVIDER, REPO, {"enabled": True}, engine=c.sync_engine)
        until = datetime.now(UTC) + timedelta(minutes=20)
        with Session(c.sync_engine) as s:
            s.add(ProductivitySyncState(workspace_id=WS, provider=PROVIDER, repo=REPO, prs_total=1031,
                                        prs_detailed=412, prs_pending=619, rate_limited_until=until))
            s.commit()
        body = (await c.get("/api/analytics/productivity/sync")).json()
    repos = {r["repo"]: r for r in body["repos"]}
    assert repos[REPO]["enabled"] is True and repos["acme/api"]["enabled"] is False
    assert (repos[REPO]["status"]["prs_detailed"], repos[REPO]["status"]["prs_total"]) == (412, 1031)
    assert repos["acme/api"]["status"] is None
    assert body["any_enabled"] is True and body["backfilling"] is True
    assert body["rate_limited_until"] is not None


async def test_the_backfill_estimate_asks_the_provider_once_and_never_for_another_workspaces_repo(monkeypatch, tmp_path) -> None:
    from src.productivity import sync as sync_mod

    asked: list[tuple] = []

    class Provider:
        requests_per_pr, page_size = 4, 50

        def count_pull_requests(self, since):
            return 100

    def build(ws, provider, repo, *, user_id="default", rate_per_hour=500):
        asked.append((ws, provider, repo, user_id, rate_per_hour))
        return Provider()

    monkeypatch.setattr(sync_mod, "build_provider", build)
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        ok = await c.get("/api/analytics/productivity/sync/estimate",
                         params={"provider": PROVIDER, "repo": REPO, "backfill_days": 90})
        stranger = await c.get("/api/analytics/productivity/sync/estimate",
                               params={"provider": PROVIDER, "repo": "evil/repo"})
    assert ok.status_code == 200
    assert ok.json()["requests"] == 402 and ok.json()["prs"] == 100 and ok.json()["days"] == 90
    assert asked == [(WS, PROVIDER, REPO, "u-1", 500)]
    assert stranger.status_code == 404


async def test_a_missing_credential_is_said_plainly_on_the_estimate(monkeypatch, tmp_path) -> None:
    from src.productivity import sync as sync_mod
    from src.productivity.providers.base import ProviderError

    def build(*a, **k):
        raise ProviderError("no github credential saved for workspace ws-1")

    monkeypatch.setattr(sync_mod, "build_provider", build)
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path) as c:
        r = await c.get("/api/analytics/productivity/sync/estimate", params={"provider": PROVIDER, "repo": REPO})
    assert r.status_code == 409 and "credential" in r.json()["detail"]
