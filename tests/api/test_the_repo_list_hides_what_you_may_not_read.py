"""GET /api/repos lists the repositories the caller may read.

Owners, admins and global admins see every repository of the workspace;
everyone else sees those a team of theirs grants `read` on. The agent's
`list_repos` and MCP `list_workspace_repos` answer the same way (see
tests/automation/test_agent_role_matrix.py).
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, world

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


async def _slugs(w, who: str) -> set[str]:
    r = await w.client.get("/api/repos", headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return {x["slug"] for x in r.json()}


@pytest.mark.parametrize("who, expected", [
    ("viewer_a", set()),
    ("member_a", set()),
    ("editor_a", {A_REPO}),
    ("admin_a", {A_REPO, SECRET}),
    ("owner_a", {A_REPO, SECRET}),
    ("gadmin", {A_REPO, SECRET}),
])
async def test_each_role_lists_what_it_may_read(tmp_path, monkeypatch, who, expected):
    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        assert await _slugs(w, who) == expected


async def test_the_hidden_repository_is_not_named_anywhere_in_the_body(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _register_secret(w)
        r = await w.client.get("/api/repos", headers=w.h("editor_a", "ws-a"))
        assert SECRET not in r.text and SECRET_FULL not in r.text
        assert A_REPO_FULL in r.text
