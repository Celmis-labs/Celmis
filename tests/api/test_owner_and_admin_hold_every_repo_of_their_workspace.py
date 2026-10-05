"""Owner and admin of a workspace have full access to EVERY repository of it,
whatever the teams grant — in the one place that decides (the grant resolver
`enforce_repo_permission`, and the research-access resolver that Q&A, search
and MCP share). Strictly that workspace's own repositories: an owner of B
holds nothing in A. Members, editors and viewers keep the team grants.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from tests.api.rbac_world import world

SECRET = "github_aco-secret"
SECRET_FULL = "aco/secret"


async def _register_secret(w) -> None:
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.db.models import RepoTeamAccess, Team

    async with w.factory() as s:
        s.add(Team(id="team-empty", name="nobody", description="",
                   workspace_id=w.ws["ws-a"]))
        s.add(RepoTeamAccess(repo_slug=SECRET, team_id="team-empty", permission="admin"))
        await s.commit()
    get_auto_review_store().upsert(RepoConfig(
        user_id=w.uid("admin_a"), repo_slug=SECRET, provider="github",
        full_name=SECRET_FULL, url=f"https://github.com/{SECRET_FULL}",
        workspace_id=w.ws["ws-a"]))


async def _may(w, who: str, ws: str, perm: str = "admin", slug: str = SECRET) -> bool:
    from src.api.deps import enforce_repo_permission

    try:
        await enforce_repo_permission(slug, w.users[who], perm, w.ws[ws])
    except HTTPException:
        return False
    return True


@pytest.mark.parametrize("who, allowed", [
    ("owner_a", True), ("admin_a", True), ("admin2_a", True),
    ("editor_a", False), ("member_a", False), ("viewer_a", False),
    ("gadmin", True),
])
async def test_the_grant_check_for_each_role(tmp_path, monkeypatch, who, allowed):
    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        for perm in ("read", "review", "admin"):
            assert await _may(w, who, "ws-a", perm) is allowed, (who, perm)


async def test_an_owner_of_another_workspace_holds_nothing_here(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        # Asking in A (not a member there), and asking in B about A's repo.
        assert not await _may(w, "admin_b", "ws-a")
        assert not await _may(w, "admin_b", "ws-b")


async def test_a_repository_that_is_not_the_workspaces_is_not_covered(tmp_path, monkeypatch):
    """An owner is not given a slug the registry does not hold for the workspace."""
    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        assert not await _may(w, "owner_a", "ws-a", slug="github_nobody-here")


async def test_the_research_access_resolver_follows_the_same_rule(tmp_path, monkeypatch):
    """Q&A, code search, docs and MCP all resolve through this."""
    from sqlalchemy.orm import Session

    from src.access.resolver import resolve_access_sync

    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        try:
            with Session(engine) as s:
                def level(who: str, ws: str) -> str:
                    out = resolve_access_sync(
                        s, user_id=w.uid(who), is_admin=False,
                        workspace_id=w.ws[ws], repos=[SECRET])
                    return out[SECRET].visibility

                assert level("owner_a", "ws-a") == "code"
                assert level("admin_a", "ws-a") == "code"
                assert level("editor_a", "ws-a") == "none"
                assert level("member_a", "ws-a") == "none"
                assert level("admin_b", "ws-a") == "none"
                assert level("admin_b", "ws-b") == "none"
        finally:
            engine.dispose()
