# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Who may read productivity metrics, and that only this workspace's rows are read.

The router is enterprise and rides on the analytics licence feature: it is
absent (404, so /api/capabilities reports it off) without that licence, and
only an owner or admin reads it (like spend and review cost) — an editor, a
member or a viewer gets 403, and so does a caller with no role. A global admin
reads it too. The developer table and the sync settings are what this guards.
"""

from __future__ import annotations

import pytest

from tests.ee.productivity_support import api, deployment, event, merged_pr

PRS = [
    merged_pr(1, review=5, merged=3), merged_pr(2, review=4, merged=2, author="lee"),
    # Another workspace's PR in the same repository name: nothing here may see it.
    merged_pr(99, workspace="ws-2"),
]
OVERVIEW = "/api/analytics/productivity/overview?days=30"
READS = [
    "/api/analytics/productivity/overview?days=30",
    "/api/analytics/productivity/prs?days=30",
    "/api/analytics/productivity/developers?days=30",
    "/api/analytics/productivity/filters",
    "/api/analytics/productivity/sync",
]


@pytest.mark.parametrize(("role", "admin", "code"), [
    ("viewer", False, 403), ("member", False, 403), ("editor", False, 403), (None, False, 403),
    ("admin", False, 200), ("owner", False, 200), (None, True, 200),
])
@pytest.mark.parametrize("url", READS)
async def test_productivity_is_read_by_owner_and_admin_only(
    url, role, admin, code, monkeypatch, tmp_path,
) -> None:
    async with api(role=role, is_admin=admin, monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS) as c:
        assert (await c.get(url)).status_code == code


@pytest.mark.parametrize("role", ["viewer", "member", "editor"])
async def test_nobody_below_admin_can_change_or_start_anything(role, monkeypatch, tmp_path) -> None:
    async with api(role=role, monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS) as c:
        base = "/api/analytics/productivity"
        assert (await c.get(f"{base}/settings")).status_code == 403
        assert (await c.get(f"{base}/sync/estimate?provider=github&repo=acme/shop")).status_code == 403
        put = await c.put(f"{base}/settings", json={"changes": {"enabled": True}})
        run = await c.post(f"{base}/sync/run", json={"provider": "github", "repo": "acme/shop"})
        assert (put.status_code, run.status_code) == (403, 403)


async def test_only_this_workspaces_pull_requests_are_counted(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS) as c:
        body = (await c.get(OVERVIEW)).json()
    assert body["kpis"]["merged_prs"]["value"] == 2


@pytest.mark.parametrize("features", [(), ("sso",)], ids=["no-licence", "sso-only"])
async def test_without_an_analytics_licence_productivity_is_not_there(features, monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, features=features) as c:
        for url in READS:
            assert (await c.get(url)).status_code == 404, url


async def test_an_unknown_window_bucket_or_sort_is_refused(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS) as c:
        assert (await c.get("/api/analytics/productivity/overview?days=7")).status_code == 422
        assert (await c.get("/api/analytics/productivity/overview?days=30&bucket=year")).status_code == 422
        assert (await c.get("/api/analytics/productivity/prs?days=30&sort=colour")).status_code == 422
        assert (await c.get("/api/analytics/productivity/prs?days=30&limit=0")).status_code == 422


async def test_the_overview_reads_cycle_time_deployments_and_the_size_histogram(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1, first_commit=8, created=7, review=5, merged=3, deployed=2, lines=(30, 10)),
           merged_pr(2, first_commit=9, created=8, review=4, merged=2, deployed=1, lines=(500, 100))]
    deps = [deployment(1, days=2), deployment(2, days=20, failed=True, recovered=19)]
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs, deployments=deps) as c:
        body = (await c.get(OVERVIEW)).json()
    kpi = body["kpis"]
    assert kpi["merged_prs"]["value"] == 2
    assert kpi["cycle_time"]["n"] == 2
    assert kpi["lead_time"]["n"] == 2
    assert kpi["deploy_frequency"]["total"] == 2
    assert kpi["change_failure_rate"]["failed"] == 1
    assert {row["bucket"]: row["count"] for row in body["size_histogram"]} == {
        "xs": 0, "s": 1, "m": 0, "l": 1, "xl": 0}
    assert len(body["deployments"]) == 2
    assert body["breakdown"]["coding"]["value"] is not None


async def test_a_bots_pull_requests_are_not_the_teams_work(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1), merged_pr(2, author="dependabot[bot]")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs) as c:
        before = (await c.get(OVERVIEW)).json()["kpis"]["merged_prs"]["value"]
        saved = await c.put("/api/analytics/productivity/settings",
                            json={"changes": {"ignored_authors": ["dependabot[bot]"]}})
        assert saved.status_code == 200, saved.text
        after = (await c.get(OVERVIEW)).json()["kpis"]["merged_prs"]["value"]
    assert (before, after) == (2, 1)


async def test_the_slowest_prs_come_first_with_their_breakdown_and_a_link(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1, first_commit=8, created=7, review=5, merged=3),
           merged_pr(2, first_commit=29, created=28, review=20, merged=3, title="Slow one")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs) as c:
        rows = (await c.get("/api/analytics/productivity/prs?days=30&sort=cycle&limit=1")).json()["prs"]
    assert [r["number"] for r in rows] == [2]
    assert rows[0]["title"] == "Slow one" and rows[0]["url"].endswith("/pull/2")
    assert rows[0]["coding"] and rows[0]["pickup"] and rows[0]["review"]


async def test_the_filter_bar_offers_what_there_is_data_for(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(1), merged_pr(2, repo="acme/api", author="lee", target="develop")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs,
                   groups={"backend": ["acme/api"]}) as c:
        body = (await c.get("/api/analytics/productivity/filters")).json()
    assert [r["repo"] for r in body["repos"]] == ["acme/api", "acme/shop"]
    assert {a["key"] for a in body["authors"]} == {"dana", "lee"}
    assert body["targets"] == ["develop", "main"]


async def test_the_developer_table_lists_merged_work_and_reviews_given(monkeypatch, tmp_path) -> None:
    prs = [merged_pr(i, author="dana", merged=3 + i * 0.1) for i in range(1, 5)] + [merged_pr(9, author="lee")]
    events = [event(prs[0]["id"], actor="lee", kind="approval", n=1),
              event(prs[0]["id"], actor="lee", kind="comment", n=2),
              event(prs[1]["id"], actor="lee", kind="comment", n=3),
              event(prs[1]["id"], actor="robot", kind="comment", n=4, is_bot=True),
              event(prs[1]["id"], actor="dana", kind="comment", n=5, is_author=True)]
    async with api(role="admin", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=prs, events=events) as c:
        body = (await c.get("/api/analytics/productivity/developers?days=30")).json()
    people = {p["key"]: p for p in body["developers"]}
    assert people["dana"]["prs_merged"] == 4 and people["dana"]["cycle_p50"] is not None
    assert people["lee"]["reviews_given"] == 2 and people["lee"]["comments_given"] == 2
    assert people["lee"]["small_sample"] is True and people["lee"]["cycle_p50"] is None
    assert "robot" not in people, "a bot's comment is nobody's review"
    assert body["min_sample"] == 3


@pytest.mark.parametrize(("features", "on"), [(("analytics",), True), ((), False), (("sso",), False)])
async def test_capabilities_reports_the_page_on_only_when_its_routes_are_mounted(
    features, on, monkeypatch, tmp_path,
) -> None:
    from src.api.routers import capabilities as caps

    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, features=features) as c:
        doc = caps.build_capabilities(c.app_under_test)
    assert doc.features["productivity"].available is on
    assert doc.pages["/productivity"] is on
