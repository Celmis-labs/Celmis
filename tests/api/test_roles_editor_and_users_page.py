"""The editor is the prompt editor; the Users page is the superadmin's.

Editor (inside its own workspace): agent system prompts, review policies
(prompt template, folder rules, per-agent prompt overrides), analytics — and
none of members, invites, teams, LLM keys/config, git connections.

The review-policy write used to check no WORKSPACE role at all — only the
repo-level team grant — so any member with `review` on a repository could
rewrite the prompts every review of it runs with. Now it asks three things:
editor+ in the workspace, the repository is the workspace's own, and the
team grant.

Users page (/api/admin/users…): superadmin only, the same grant function as
every other membership path, an audit row for every change.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, B_REPO, B_SECRET, world

POLICY = {"prompt_template": "Prefer early returns.",
          "folder_rules": [{"pattern": "src/**", "prompt": "No prints."}],
          "agent_prompt_overrides": {"defect": "Be terse about defects."}}
PROMPT = {"system_prompt": "You are a careful reviewer. Report real defects only."}


# ─── agent prompts ───────────────────────────────────────────────────


@pytest.mark.parametrize("actor, status", [
    ("editor_a", 200), ("admin_a", 200), ("owner_a", 200),
    ("member_a", 403), ("viewer_a", 403),
])
async def test_who_may_edit_an_agent_prompt(tmp_path, monkeypatch, actor, status):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.put("/api/agents/defect/prompt", json=PROMPT, headers=w.h(actor))
        assert r.status_code == status, r.text
        got = (await w.client.get("/api/agents/defect", headers=w.h(actor))).json()
        assert got["has_override"] == (status == 200)
        r = await w.client.delete("/api/agents/defect/prompt", headers=w.h(actor))
        assert r.status_code == status, r.text


# ─── review policies ─────────────────────────────────────────────────


@pytest.mark.parametrize("actor, status", [
    ("editor_a", 200), ("admin_a", 200), ("owner_a", 200),
    # member_a: even WITH a team grant on the repo, a member is not an editor
    ("member_a", 403), ("viewer_a", 403),
])
async def test_who_may_edit_a_review_policy(tmp_path, monkeypatch, actor, status):
    from src.db.models import RepoReviewPolicy, RepoTeamAccess, TeamMember

    async with world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            # Give the member the repo grant, so the refusal is the ROLE's.
            s.add(TeamMember(team_id=w.ids["team_a"], user_id=w.uid("member_a"),
                             role="member"))
            s.add(TeamMember(team_id=w.ids["team_a"], user_id=w.uid("viewer_a"),
                             role="member"))
            await s.commit()
        assert await w.scalar(RepoTeamAccess, (A_REPO, w.ids["team_a"])) is not None
        r = await w.client.put(f"/api/review-policies/{A_REPO}", json=POLICY,
                               headers=w.h(actor))
        assert r.status_code == status, r.text
        row = await w.scalar(RepoReviewPolicy, A_REPO)
        if status == 200:
            assert row.prompt_template == POLICY["prompt_template"]
            assert row.folder_rules == POLICY["folder_rules"]
            assert row.agent_prompt_overrides == POLICY["agent_prompt_overrides"]
            d = await w.client.delete(f"/api/review-policies/{A_REPO}", headers=w.h(actor))
            assert d.status_code == 204, d.text
            assert await w.scalar(RepoReviewPolicy, A_REPO) is None
        else:
            assert row is None


async def test_an_editor_needs_the_repo_grant_too(tmp_path, monkeypatch):
    """Repo-level RBAC inside the workspace still applies: a repository whose
    team grants exclude the editor stays closed to them."""
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.db.models import RepoReviewPolicy, RepoTeamAccess, Team

    async with world(tmp_path, monkeypatch) as w:
        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug="github_aco-locked", provider="github",
            full_name="aco/locked", url="https://github.com/aco/locked",
            workspace_id=w.ws["ws-a"]))
        async with w.factory() as s:
            s.add(Team(id="team-locked", name="locked", description="",
                       workspace_id=w.ws["ws-a"]))
            s.add(RepoTeamAccess(repo_slug="github_aco-locked", team_id="team-locked",
                                 permission="admin"))
            await s.commit()
        r = await w.client.put("/api/review-policies/github_aco-locked", json=POLICY,
                               headers=w.h("editor_a"))
        assert r.status_code == 403, r.text
        assert await w.scalar(RepoReviewPolicy, "github_aco-locked") is None


async def test_a_policy_cannot_be_written_for_a_repo_the_workspace_does_not_have(
        tmp_path, monkeypatch):
    """The row is keyed by slug alone. Writing one for somebody else's repo
    squatted the slug, so its real owner was refused its own policy."""
    from src import deployment
    from src.db.models import RepoReviewPolicy

    async with world(tmp_path, monkeypatch) as w:
        # single_tenant: the repo-grant check falls open for a repo with no
        # grants, so the workspace check is the only thing standing here.
        monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
        deployment.reset_mode_cache()
        r = await w.client.put("/api/review-policies/github_nobody-nothing", json=POLICY,
                               headers=w.h("admin_a"))
        assert r.status_code == 404, r.text
        assert await w.scalar(RepoReviewPolicy, "github_nobody-nothing") is None


# ─── what the editor may NOT do ──────────────────────────────────────


@pytest.mark.parametrize("method, path, body", [
    ("PUT", "/api/llm/config", {"provider": "openai", "model": "gpt-4o-mini"}),
    ("PUT", "/api/connections/github", {"token": "ghp_" + "x" * 36}),
    ("DELETE", "/api/connections/github", None),
    ("POST", "/api/invites", {"role": "member"}),
    ("GET", "/api/invites", None),
    ("POST", "/api/teams", {"name": "editors-team"}),
    ("PUT", "/api/teams/team-a/members/u-member_a", {"role": "member"}),
    ("PUT", "/api/teams/team-a/repos/github_aco-app", {"permission": "admin"}),
    ("PUT", "/api/workspaces/wsid-a/members/u-viewer_a", {"role": "member"}),
    ("DELETE", "/api/workspaces/wsid-a/members/u-viewer_a", None),
    ("POST", "/api/alerts/ingest-token", None),
])
async def test_the_editor_holds_no_admin_power(tmp_path, monkeypatch, method, path, body):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.request(method, path, json=body, headers=w.h("editor_a"))
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"
        assert await w.role("viewer_a", "ws-a") == "viewer"


@pytest.mark.parametrize("actor, status", [
    ("editor_a", 200), ("admin_a", 200), ("member_a", 403), ("viewer_a", 403),
])
async def test_analytics_is_open_to_the_editor(tmp_path, monkeypatch, actor, status):
    import src.ee.analytics.router as analytics

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.get("/api/analytics/summary", headers=w.h(actor))
        assert r.status_code == status, r.text


# ─── the Users page ──────────────────────────────────────────────────


@pytest.mark.parametrize("actor", ["gadmin", "owner_a", "admin_a", "editor_a", "member_a"])
async def test_the_users_page_is_the_superadmins(tmp_path, monkeypatch, actor):
    async with world(tmp_path, monkeypatch) as w:
        uid, ws = w.uid("member_a"), w.ws["ws-b"]
        for method, path, body in [
            ("GET", "/api/admin/users", None),
            ("GET", "/api/admin/workspaces", None),
            ("GET", f"/api/admin/users/{uid}/memberships", None),
            ("PUT", f"/api/admin/users/{uid}/memberships/{ws}", {"role": "member"}),
            ("DELETE", f"/api/admin/users/{uid}/memberships/{w.ws['ws-a']}", None),
        ]:
            r = await w.client.request(method, path, json=body, headers=w.h(actor))
            assert r.status_code == 403, f"{actor} {method} {path}: {r.status_code}"
        assert await w.role("member_a", "ws-b") is None
        assert await w.role("member_a", "ws-a") == "member"
        assert not w.audit


async def test_search_finds_people_and_hides_platform_accounts(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        everyone = (await w.client.get("/api/admin/users", headers=w.h("su"))).json()
        ids = {u["id"] for u in everyone}
        assert "u-admin_a" in ids and "master-admin" not in ids and "default" not in ids
        hits = (await w.client.get("/api/admin/users?q=ADMIN-B",
                                   headers=w.h("su"))).json()
        assert [u["email"] for u in hits] == ["admin-b@acme-corp.io"]
        both = next(u for u in everyone if u["id"] == "u-both")
        assert both["memberships"] == 2


async def test_one_person_admin_of_one_workspace_and_editor_of_another(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        uid = w.uid("member_a")
        base = f"/api/admin/users/{uid}/memberships"
        r = await w.client.put(f"{base}/{w.ws['ws-b']}", json={"role": "editor"},
                               headers=w.h("su"))
        assert r.status_code == 200, r.text
        r = await w.client.put(f"{base}/{w.ws['ws-a']}", json={"role": "admin"},
                               headers=w.h("su"))
        got = {m["workspace_slug"]: m["role"] for m in r.json()}
        assert got == {"ws-a": "admin", "ws-b": "editor"}
        listed = (await w.client.get(base, headers=w.h("su"))).json()
        assert {m["workspace_name"] for m in listed} == {"Alpha", f"Bravo {B_SECRET}"}

        r = await w.client.delete(f"{base}/{w.ws['ws-b']}", headers=w.h("su"))
        assert r.status_code == 200 and [m["workspace_slug"] for m in r.json()] == ["ws-a"]

        changes = [(a["workspace_id"], a["detail"]["old_role"], a["detail"]["new_role"])
                   for a in w.audit if a["action"] == "workspace.member_role_changed"]
        assert changes == [
            (w.ws["ws-b"], None, "editor"),
            (w.ws["ws-a"], "member", "admin"),
            (w.ws["ws-b"], "editor", None),
        ]
        for a in w.audit:
            assert a["actor_id"] == "master-admin" and a["target"] == uid
            assert a["detail"]["via"] == "admin_users"


@pytest.mark.parametrize("path_user, ws_key, role, status", [
    ("u-member_a", "ws-b", "superuser", 422),
    ("u-member_a", "nope", "member", 404),
    ("master-admin", "ws-a", "owner", 404),
    ("u-ghost", "ws-a", "member", 404),
])
async def test_the_users_page_refuses_nonsense(tmp_path, monkeypatch, path_user, ws_key,
                                               role, status):
    async with world(tmp_path, monkeypatch) as w:
        ws = w.ws.get(ws_key, ws_key)
        r = await w.client.put(f"/api/admin/users/{path_user}/memberships/{ws}",
                               json={"role": role}, headers=w.h("su"))
        assert r.status_code == status, r.text
        assert not w.audit


async def test_the_users_page_and_the_members_route_share_one_rule():
    """Not two implementations agreeing by luck: both call the same writer."""
    import ast
    import inspect

    import src.api.routers.admin_users as admin_users
    import src.api.routers.invites as invites
    import src.api.routers.workspaces as workspaces

    def calls(mod) -> set[str]:
        tree = ast.parse(inspect.getsource(mod))
        return {n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    for mod in (admin_users, workspaces, invites):
        assert "change_membership" in calls(mod), mod.__name__


def test_b_repo_constant_is_bs():
    """Guard for the fixture itself: the secret-bearing repo is B's."""
    assert B_SECRET.lower() in B_REPO
