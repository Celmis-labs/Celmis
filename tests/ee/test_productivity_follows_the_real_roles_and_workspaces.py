# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Productivity metrics against REAL membership rows, not a faked role.

`test_productivity_is_for_owners_and_admins_only` pins the rule with the role
stubbed. This one runs the same routes through the workspace resolver and the
membership table (tests/api/rbac_world.py): owner and admin of the active
workspace read, a global admin reads, and an editor (even one whose team may
read every repository), a member, a viewer, somebody who belongs to another
workspace and somebody who belongs to none do not. Whoever does read sees only
the active workspace's rows: naming another workspace, or another workspace's
repository, answers with this workspace's figures or nothing.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from tests.api.rbac_world import A_REPO_FULL, B_SECRET, world
from tests.ee.productivity_support import merged_pr

BASE = "/api/analytics/productivity"
READS = [
    f"{BASE}/overview?days=30",
    f"{BASE}/prs?days=30",
    f"{BASE}/developers?days=30",
    f"{BASE}/filters",
    f"{BASE}/sync",
    f"{BASE}/settings",
    f"{BASE}/sync/estimate?provider=github&repo={A_REPO_FULL}",
]
WRITES = [
    ("PUT", f"{BASE}/settings", {"changes": {"enabled": True}}),
    ("POST", f"{BASE}/sync/run", {"provider": "github", "repo": A_REPO_FULL}),
]
ALLOWED = ("owner_a", "admin_a", "gadmin", "su")
REFUSED = ("editor_a", "member_a", "viewer_a", "member_b")


def _routers() -> tuple:
    from src.ee.analytics import productivity_router

    return (productivity_router.router,)


def _seed(w, monkeypatch, tmp_path) -> None:
    """One merged PR per workspace; the other workspace's author and repository
    carry B_SECRET."""
    from src.db.models import ProductivityPullRequest
    from src.productivity import db as productivity_db

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    monkeypatch.setattr(productivity_db, "_ENGINE", engine)
    with Session(engine) as s:
        s.add(ProductivityPullRequest(**merged_pr(
            1, repo=A_REPO_FULL, author="dana", workspace=w.ws["ws-a"])))
        s.add(ProductivityPullRequest(**merged_pr(
            2, repo=f"bco/{B_SECRET}", author=f"{B_SECRET}-author", workspace=w.ws["ws-b"])))
        s.commit()


async def _get(w, who, url, ws="ws-a"):
    return await w.client.get(url, headers=w.h(who, ws))


@pytest.mark.parametrize("who", ALLOWED)
@pytest.mark.parametrize("url", READS)
async def test_owner_admin_and_global_admin_read_the_workspaces_figures(
    who, url, monkeypatch, tmp_path,
) -> None:
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _seed(w, monkeypatch, tmp_path)
        r = await _get(w, who, url)
        # The estimate needs a saved git credential, which this workspace has
        # not got: past the gate that is its own 409, not a refusal.
        want = 409 if "/sync/estimate" in url else 200
        assert r.status_code == want, f"{who} {url}: {r.status_code} {r.text[:200]}"
        assert B_SECRET not in r.text, "another workspace's rows reached this one"


@pytest.mark.parametrize("who", REFUSED)
@pytest.mark.parametrize("url", READS)
async def test_nobody_below_admin_reads_a_figure(who, url, monkeypatch, tmp_path) -> None:
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _seed(w, monkeypatch, tmp_path)
        r = await _get(w, who, url)
        # A member of B who names A is served as B's own: they are not an
        # admin there either, so the answer is the same refusal.
        assert r.status_code == 403, f"{who} {url}: {r.status_code}"
        assert "dana" not in r.text and B_SECRET not in r.text


@pytest.mark.parametrize("who", REFUSED)
@pytest.mark.parametrize(("method", "url", "body"), WRITES)
async def test_nobody_below_admin_changes_a_setting_or_starts_a_sync(
    who, method, url, body, monkeypatch, tmp_path,
) -> None:
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        r = await w.client.request(method, url, json=body, headers=w.h(who, "ws-a"))
        assert r.status_code == 403, f"{who} {method} {url}: {r.status_code}"


async def test_an_editor_whose_team_may_read_the_repository_still_may_not_read_its_figures(
    monkeypatch, tmp_path,
) -> None:
    """Repository access is not the key to this page: the developer table names
    people, so the role is (the A team holds the repository, and editor_a is in it)."""
    from src.api.deps import readable_repo_slugs

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _seed(w, monkeypatch, tmp_path)
        readable = await readable_repo_slugs(
            w.users["editor_a"], w.ws["ws-a"], ["github_aco-app"])
        assert "github_aco-app" in readable, "the premise: this editor holds the repository"
        for url in (f"{BASE}/overview?days=30&repo={A_REPO_FULL}", f"{BASE}/developers?days=30"):
            assert (await _get(w, "editor_a", url)).status_code == 403


async def test_an_admin_of_another_workspace_gets_that_workspaces_figures_not_this_ones(
    monkeypatch, tmp_path,
) -> None:
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _seed(w, monkeypatch, tmp_path)
        mine = (await _get(w, "admin_b", f"{BASE}/prs?days=30", ws="ws-a")).json()
        assert [p["number"] for p in mine["prs"]] == [2]
        assert "dana" not in str(mine)
        theirs = (await _get(w, "admin_a", f"{BASE}/prs?days=30")).json()
        assert [p["number"] for p in theirs["prs"]] == [1]
        assert B_SECRET not in str(theirs)


async def test_a_repository_filter_cannot_reach_into_another_workspace(
    monkeypatch, tmp_path,
) -> None:
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _seed(w, monkeypatch, tmp_path)
        r = await _get(w, "admin_a", f"{BASE}/prs?days=30&repo=bco/{B_SECRET}")
        assert r.status_code in (200, 404, 422)
        assert B_SECRET not in r.text
        if r.status_code == 200:
            assert r.json()["prs"] == []
