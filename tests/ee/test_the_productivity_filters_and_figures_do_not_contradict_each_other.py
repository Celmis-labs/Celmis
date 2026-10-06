# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""What the filter bar offers always gives a dashboard, and the figures say what they rest on.

A target branch no deployment lands on must not zero the delivery figures, an
ignored account is not offered as an author, a repository registered by two
members is one repository, and a period whose history was never synced says so.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.db.models import ProductivitySyncState
from src.ee.analytics import productivity_router
from tests.ee.productivity_support import (
    NOW,
    PROVIDER,
    REPO,
    WS,
    ago,
    api,
    deployment,
    merged_pr,
)

BASE = "/api/analytics/productivity"


async def test_a_target_branch_no_deployment_lands_on_leaves_the_delivery_figures_alone(
    monkeypatch, tmp_path,
) -> None:
    prs = [merged_pr(1, target="develop", deployed=1), merged_pr(2, target="main", deployed=1)]
    deps = [deployment(1, days=2), deployment(2, days=3)]
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path,
                   prs=prs, deployments=deps) as c:
        everything = (await c.get(f"{BASE}/overview", params={"days": 30})).json()
        develop = (await c.get(f"{BASE}/overview", params={"days": 30, "target": "develop"})).json()
        main = (await c.get(f"{BASE}/overview", params={"days": 30, "target": "main"})).json()
    assert everything["kpis"]["deploy_frequency"]["total"] == 2
    assert develop["kpis"]["deploy_frequency"]["total"] == 2, "deployments never land on develop"
    assert develop["kpis"]["merged_prs"]["value"] == 1, "pull requests are still narrowed"
    assert main["kpis"]["deploy_frequency"]["total"] == 2


async def test_a_target_branch_deployments_do_land_on_narrows_them(monkeypatch, tmp_path) -> None:
    deps = [deployment(1, days=2, branch="main"), deployment(2, days=3, branch="release")]
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path,
                   prs=[merged_pr(1)], deployments=deps) as c:
        release = (await c.get(f"{BASE}/overview", params={"days": 30, "target": "release"})).json()
    assert release["kpis"]["deploy_frequency"]["total"] == 1


async def test_an_ignored_account_is_not_offered_as_an_author(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1, author="dana"), merged_pr(2, author="dependabot")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs) as c:
        before = (await c.get(f"{BASE}/filters")).json()
        await c.put(f"{BASE}/settings", json={"changes": {"ignored_authors": ["dependabot"]}})
        after = (await c.get(f"{BASE}/filters")).json()
    assert {a["key"] for a in before["authors"]} == {"dana", "dependabot"}
    assert [a["key"] for a in after["authors"]] == ["dana"]


async def test_an_account_ignored_in_one_repository_only_is_still_offered(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1, author="dana", repo="acme/api"), merged_pr(2, author="dana", repo=REPO)]
    repos = [{"provider": PROVIDER, "repo": r, "repo_slug": r.replace("/", "-"), "user_id": "u-1"}
             for r in (REPO, "acme/api")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs,
                   repos=repos) as c:
        await c.put(f"{BASE}/settings", json={
            "provider": PROVIDER, "repo": "acme/api", "changes": {"ignored_authors": ["dana"]}})
        authors = (await c.get(f"{BASE}/filters")).json()["authors"]
    assert [a["key"] for a in authors] == ["dana"]


def test_a_repository_registered_by_two_members_is_one_repository(monkeypatch) -> None:
    def row(user: str, repo: str = REPO):
        return SimpleNamespace(provider=PROVIDER, full_name=repo, repo_slug="slug", user_id=user)

    store = SimpleNamespace(list_for_workspace=lambda ws: [row("u-9"), row("u-2"), row("u-5", "acme/api")])
    import src.api.auto_review as auto_review

    monkeypatch.setattr(auto_review, "get_auto_review_store", lambda: store)
    repos = productivity_router._workspace_repos(WS)
    assert [(r["repo"], r["user_id"]) for r in repos] == [("acme/api", "u-5"), (REPO, "u-2")], (
        "once each, with the registrant that sorts first"
    )


def _synced_from(client, days_ago: float, repo: str = REPO) -> None:
    with Session(client.sync_engine) as s:
        s.add(ProductivitySyncState(workspace_id=WS, provider=PROVIDER, repo=repo,
                                    backfill_from=ago(days_ago)))
        s.commit()


async def test_a_previous_period_older_than_the_synced_history_is_marked_incomplete(
    monkeypatch, tmp_path,
) -> None:
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path,
                   prs=[merged_pr(1)]) as c:
        _synced_from(c, 40)
        short = (await c.get(f"{BASE}/overview", params={"days": 30})).json()
        deep = (await c.get(f"{BASE}/overview", params={"days": 15})).json()
    assert short["previous_incomplete"] is True and short["covered_from"] is not None
    assert deep["previous_incomplete"] is False, "15 + 15 days fit inside 40"


async def test_a_workspace_with_no_sync_history_does_not_claim_an_incomplete_period(
    monkeypatch, tmp_path,
) -> None:
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path,
                   prs=[merged_pr(1)]) as c:
        body = (await c.get(f"{BASE}/overview", params={"days": 30})).json()
    assert body["previous_incomplete"] is False and body["covered_from"] is None


async def test_the_latest_backfill_start_among_the_repositories_decides(monkeypatch, tmp_path) -> None:
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path,
                   prs=[merged_pr(1)]) as c:
        _synced_from(c, 180)
        _synced_from(c, 20, repo="acme/api")
        body = (await c.get(f"{BASE}/overview", params={"days": 15})).json()
    assert body["previous_incomplete"] is True, "one repository only has 20 days"


def test_the_overview_without_coverage_information_stays_as_before() -> None:
    from src.ee.analytics import productivity as m

    body = m.overview([], [], days=30, now=NOW)
    assert body["previous_incomplete"] is False and body["covered_from"] is None
