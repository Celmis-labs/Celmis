"""Belonging to a workspace is not the right to read one of its repositories.

The review routes (`/api/reviews/...`, `/api/pull-requests/{id}/runs`) used to
check only that a run was the workspace's. A run is the CONTENT of one
repository — its raw diff, its findings — so it is readable exactly when the
repository is: the same default-deny the MCP tools apply. The same goes for
the places that quote a repository to somebody else: a group of repositories
(cross-repo drift greps its members), a Claude Code session (it clones the
repo), the list of who works on what, and a `metadata` rule, which names a
repository but never opens it.

Repo A (`github_aco-app`) is granted to team A: admin_a, editor_a and owner_a
are in it. member_a and viewer_a are in the workspace and in no team.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, world

RUN_ID = "run-acl-1"
PR_ID = "pr-acl-1"
DIFF = "diff --git a/x.py b/x.py\n+API_TOKEN_NAME = 'only a name'\n"

IN_TEAM = ["editor_a", "admin_a", "owner_a", "gadmin"]
OUT_OF_TEAM = ["member_a", "viewer_a"]


@pytest.fixture(autouse=True)
def _sync_resolver_on_the_world_database(tmp_path, monkeypatch):
    """The groups router asks the sync resolver; point it at the world's file."""
    from sqlalchemy import create_engine

    from src.access import resolver

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    yield
    engine.dispose()


def _groups_router():
    from src.api.routers import groups

    return groups.router


def _claude_code_router():
    from src.api.routers import claude_code

    return claude_code.router


async def _seed_run(w) -> None:
    from src.api.review_runs import ReviewRun, get_review_run_store
    from src.db.models import ReviewPullRequest

    store = get_review_run_store()
    store.insert(ReviewRun(
        id=RUN_ID, user_id=w.uid("admin_a"), pr_ref=f"github:{A_REPO_FULL}#7",
        status="complete", verdict="approve", findings_count=1,
        started_at=datetime.now(UTC).isoformat(), workspace_id=w.ws["ws-a"],
        pr_provider="github", pr_repo=A_REPO_FULL, pr_number=7))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_runs SET raw_diff = ?, findings_json = ? WHERE id = ?",
                     (DIFF, '[{"title": "a finding"}]', RUN_ID))
    async with w.factory() as s:
        s.add(ReviewPullRequest(
            id=PR_ID, workspace_id=w.ws["ws-a"], provider="github", repo=A_REPO_FULL,
            number=7, repo_slug=A_REPO, title="t"))
        await s.commit()


def _urls() -> list[str]:
    return [f"/api/reviews/{RUN_ID}", f"/api/reviews/{RUN_ID}/diff",
            f"/api/reviews/{RUN_ID}/findings", f"/api/pull-requests/{PR_ID}/runs"]


@pytest.mark.parametrize("who", IN_TEAM)
async def test_a_person_who_may_read_the_repository_reads_its_review(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed_run(w)
        for url in _urls():
            r = await w.client.get(url, headers=w.h(who, "ws-a"))
            assert r.status_code == 200, (url, r.text)
        listed = await w.client.get("/api/reviews/history", headers=w.h(who, "ws-a"))
        assert [x["id"] for x in listed.json()] == [RUN_ID]


@pytest.mark.parametrize("who", OUT_OF_TEAM)
async def test_a_workspace_member_without_a_grant_gets_no_diff_no_findings_no_run(
        tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed_run(w)
        for url in _urls():
            r = await w.client.get(url, headers=w.h(who, "ws-a"))
            assert r.status_code == 404, (url, r.status_code)
            assert "only a name" not in r.text and "a finding" not in r.text
        listed = await w.client.get("/api/reviews/history", headers=w.h(who, "ws-a"))
        assert listed.status_code == 200 and listed.json() == []


async def test_a_closed_repository_answers_like_a_run_that_does_not_exist(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _seed_run(w)
        shut = await w.client.get(f"/api/reviews/{RUN_ID}/diff", headers=w.h("member_a", "ws-a"))
        gone = await w.client.get("/api/reviews/no-such-run/diff", headers=w.h("member_a", "ws-a"))
        assert (shut.status_code, shut.json()) == (gone.status_code, gone.json())


async def test_a_team_rule_that_only_names_the_repository_does_not_open_its_runs(
        tmp_path, monkeypatch):
    """`metadata` is for lists and names: the team has a grant, but the rule
    written for it says no source, so no diff either."""
    from src.db.models import RepoAccessRule

    async with world(tmp_path, monkeypatch) as w:
        await _seed_run(w)
        async with w.factory() as s:
            s.add(RepoAccessRule(
                id="rule-meta", workspace_id=w.ws["ws-a"], team_id="team-a",
                repo_slug=A_REPO, visibility="metadata"))
            await s.commit()
        r = await w.client.get(f"/api/reviews/{RUN_ID}/diff", headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 404
        # Workspace admins and owners are never narrowed by a team rule.
        for who in ("admin_a", "owner_a"):
            r = await w.client.get(f"/api/reviews/{RUN_ID}/diff", headers=w.h(who, "ws-a"))
            assert r.status_code == 200, who


async def test_a_team_with_a_code_rule_reads_the_runs(tmp_path, monkeypatch):
    from src.db.models import RepoAccessRule

    async with world(tmp_path, monkeypatch) as w:
        await _seed_run(w)
        async with w.factory() as s:
            s.add(RepoAccessRule(
                id="rule-code", workspace_id=w.ws["ws-a"], team_id="team-a",
                repo_slug=A_REPO, visibility="code"))
            await s.commit()
        r = await w.client.get(f"/api/reviews/{RUN_ID}/diff", headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 200 and r.json()["diff"] == DIFF


# ─── groups: a group is a grep target ────────────────────────────────


async def _make_editor(w, who: str) -> None:
    """A group is changed by an editor or above: promote the person, keeping
    them OUT of the team that holds the grant, so the repository stays closed."""
    from src.db.models import WorkspaceMember

    async with w.factory() as s:
        member = await s.get(WorkspaceMember, (w.ws["ws-a"], w.uid(who)))
        member.role = "editor"
        await s.commit()


@pytest.mark.parametrize("who,expected", [("editor_a", 201), ("member_a", 422)])
async def test_a_group_can_only_hold_repositories_its_creator_may_read(
        tmp_path, monkeypatch, who, expected):
    async with world(tmp_path, monkeypatch, extra_routers=(_groups_router(),)) as w:
        await _make_editor(w, who)
        r = await w.client.post(
            "/api/repos/groups", json={"name": f"g-{who}", "repos": [f"github:{A_REPO_FULL}"]},
            headers=w.h(who, "ws-a"))
        assert r.status_code == expected, r.text


async def test_a_refused_group_member_reads_like_an_unregistered_one(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=(_groups_router(),)) as w:
        await _make_editor(w, "member_a")
        closed = await w.client.post(
            "/api/repos/groups", json={"name": "g1", "repos": [f"github:{A_REPO_FULL}"]},
            headers=w.h("member_a", "ws-a"))
        absent = await w.client.post(
            "/api/repos/groups", json={"name": "g2", "repos": ["github:nobody/nothing"]},
            headers=w.h("member_a", "ws-a"))
        assert closed.status_code == absent.status_code == 422
        assert "registered" in closed.json()["detail"]


async def test_adding_a_repository_to_a_group_needs_the_right_to_read_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=(_groups_router(),)) as w:
        await _make_editor(w, "member_a")
        ok = await w.client.post("/api/repos/groups", json={"name": "mine", "repos": []},
                                 headers=w.h("member_a", "ws-a"))
        assert ok.status_code == 201, ok.text
        add = await w.client.post("/api/repos/groups/mine/repos",
                                  json={"repos": [f"github:{A_REPO_FULL}"]},
                                  headers=w.h("member_a", "ws-a"))
        assert add.status_code == 422, add.text


# ─── claude code sessions clone the repository ───────────────────────


@pytest.mark.parametrize("who", OUT_OF_TEAM)
async def test_a_claude_code_session_cannot_clone_a_repository_its_creator_may_not_read(
        tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=(_claude_code_router(),)) as w:
        r = await w.client.post(
            "/api/agent-sessions", json={"repo_slug": A_REPO, "prompt": "audit it"},
            headers=w.h(who, "ws-a"))
        assert r.status_code == 400 and "Not registered in this workspace" in r.text, r.text


async def test_a_claude_code_session_for_a_readable_repository_gets_past_the_access_check(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=(_claude_code_router(),)) as w:
        r = await w.client.post(
            "/api/agent-sessions", json={"repo_slug": A_REPO, "prompt": "audit it"},
            headers=w.h("editor_a", "ws-a"))
        # Stops at the next gate (no Claude account is connected in this world).
        assert "Not registered in this workspace" not in r.text, r.text


# ─── who works on what ───────────────────────────────────────────────


async def test_the_developer_list_names_only_repositories_the_caller_may_see(
        tmp_path, monkeypatch):
    from src.db.models import OwnershipSnapshot

    async with world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            s.add(OwnershipSnapshot(
                id="own-1", repo_slug=A_REPO, paths={},
                stats={"top_owners": [{"identity": "dana@example.com", "commits": 9}]}))
            await s.commit()
        seen = await w.client.get("/api/repos/developers", headers=w.h("editor_a", "ws-a"))
        assert seen.status_code == 200
        assert [d["identity"] for d in seen.json()] == ["dana@example.com"]
        blind = await w.client.get("/api/repos/developers", headers=w.h("member_a", "ws-a"))
        assert blind.status_code == 200 and blind.json() == []
        assert A_REPO not in blind.text and "dana@example.com" not in blind.text


async def test_a_metadata_rule_names_the_repository_in_a_list_and_nowhere_else(
        tmp_path, monkeypatch):
    from src.api.deps import enforce_repo_permission, readable_repo_slugs
    from src.db.models import RepoAccessRule, Team, TeamMember

    async with world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            s.add(Team(id="team-m", name="meta", description="", workspace_id=w.ws["ws-a"]))
            s.add(TeamMember(team_id="team-m", user_id=w.uid("member_a"), role="member"))
            s.add(RepoAccessRule(
                id="rule-m", workspace_id=w.ws["ws-a"], team_id="team-m",
                repo_slug=A_REPO, visibility="metadata"))
            await s.commit()
        user = w.users["member_a"]
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as err:
            await enforce_repo_permission(A_REPO, user, "read", w.ws["ws-a"])
        assert err.value.status_code == 404
        named = await readable_repo_slugs(user, w.ws["ws-a"], [A_REPO])
        assert named == {A_REPO}


# ─── erasing a person takes their MCP tokens with them ───────────────


async def test_erasing_a_person_through_the_api_revokes_their_mcp_tokens(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session

    from src.api.routers import gdpr
    from src.db.models import McpToken
    from src.mcp_server import token_store

    monkeypatch.setenv("MCP_JWT_SECRET", "test-only-secret-" + "x" * 32)
    async with world(tmp_path, monkeypatch, extra_routers=(gdpr.router,)) as w:
        _tok, mine = token_store.mint(
            kind="pat", workspace_id=w.ws["ws-a"], user_id=w.uid("editor_a"),
            issued_by=w.uid("gadmin"), label="t", patterns=["*"], allow_write=False,
            profile="full", expires_in_days=30)
        _tok2, theirs = token_store.mint(
            kind="pat", workspace_id=w.ws["ws-a"], user_id=w.uid("admin_a"),
            issued_by=w.uid("gadmin"), label="t", patterns=["*"], allow_write=False,
            profile="full", expires_in_days=30)
        r = await w.client.delete(f"/api/gdpr/user/{w.uid('editor_a')}",
                                  headers=w.h("gadmin", "ws-a"))
        assert r.status_code == 200, r.text
        assert r.json().get("mcp_tokens_revoked") == 1
        with Session(token_store._engine()) as s:
            assert s.get(McpToken, mine.id).revoked_at is not None
            assert s.get(McpToken, theirs.id).revoked_at is None
