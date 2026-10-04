"""Workspace A's people cannot reach workspace B — asked of every surface.

Two workspaces. A has an admin, an editor, a member; B has an admin, a member
and data on every surface: members, an invite, a team with a repo grant, a
registered repository with a review policy, an issue, a reviewed PR, an alert,
an automation run, an agent prompt override. Every visible string of B's
carries the marker ``B_SECRET`` (tests/api/rbac_world.py).

Each of A's roles then tries every route against B, three ways:

  * naming B's workspace in ``X-Workspace`` (and, separately, in the
    ``x-workspace`` cookie the UI uses for SSE) — `current_workspace_id` must
    not honour a workspace the caller is not a member of;
  * naming B's objects directly — workspace id, team id, invite id, issue,
    alert and run ids, B's repository slug in the path;
  * and, for the routes that answer about "the active workspace", reading
    whatever comes back.

The verdict per route is one of two, and both are strict:

  ``deny``     403 or 404. Nothing else — not a 200 with an empty body, not a
               500 that happens to carry no data.
  ``no_leak``  any non-5xx status, and the marker is nowhere in the body.
               These are list/read routes scoped to the caller's own
               workspace: answering with A's data is the correct behaviour.

After the attempts, B's state is read back from the database: a 403 that
wrote anyway would pass a status-only check.

What was found and fixed while writing this (each case below fails on the
previous code):

  * repo routes by slug fell back to "a row this user registered", which may
    belong to ANOTHER workspace — a member of A who had registered B's repo
    (or had been removed from B) indexed it, listed its PRs and branches with
    B's stored token (`both` exercises it);
  * GET /api/review-policies/{slug}/branches read any clone on disk by slug;
  * team/repo grants were looked up across ALL workspaces' teams, so B's
    grants decided access to A's copy of the same slug;
  * a team could take a member from outside its workspace;
  * PUT /api/review-policies/{slug} wrote a policy row for any slug.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, B_REPO, B_SECRET, world

A_ROLES = ["admin_a", "editor_a", "member_a", "both_in_a"]

# (method, path, json body, verdict). `{me}` is the actor's own user id.
PROBES: list[tuple[str, str, dict | None, str]] = [
    # workspace + members (path id)
    ("GET", "/api/workspaces/wsid-b/members", None, "deny"),
    ("PUT", "/api/workspaces/wsid-b/members/{me}", {"role": "admin"}, "deny"),
    ("PUT", "/api/workspaces/wsid-b/members/u-member_b", {"role": "viewer"}, "deny"),
    ("DELETE", "/api/workspaces/wsid-b/members/u-member_b", None, "deny"),
    ("DELETE", "/api/workspaces/wsid-b/members/u-admin_b", None, "deny"),
    ("POST", "/api/workspaces/wsid-b/members/u-member_b/reset-link", None, "deny"),
    ("DELETE", "/api/workspaces/wsid-b", None, "deny"),
    ("GET", "/api/workspaces", None, "no_leak"),
    # invites
    ("GET", "/api/invites", None, "no_leak"),
    ("POST", "/api/invites", {"role": "member"}, "no_leak"),
    ("DELETE", "/api/invites/inv-b", None, "no_leak"),
    # teams
    ("GET", "/api/teams", None, "no_leak"),
    ("GET", "/api/teams/me", None, "no_leak"),
    ("GET", "/api/teams/team-b/members", None, "deny"),
    ("GET", "/api/teams/team-b/repos", None, "deny"),
    ("PUT", "/api/teams/team-b/members/{me}", {"role": "owner"}, "deny"),
    ("DELETE", "/api/teams/team-b/members/u-member_b", None, "deny"),
    ("PUT", f"/api/teams/team-b/repos/{B_REPO}", {"permission": "admin"}, "deny"),
    ("DELETE", f"/api/teams/team-b/repos/{B_REPO}", None, "deny"),
    ("DELETE", "/api/teams/team-b", None, "deny"),
    # repositories by B's slug
    ("GET", "/api/repos", None, "no_leak"),
    ("POST", f"/api/repos/{B_REPO}/index", None, "deny"),
    ("POST", f"/api/repos/{B_REPO}/check-freshness", None, "deny"),
    ("POST", f"/api/repos/{B_REPO}/generate-vault", None, "deny"),
    ("GET", f"/api/repos/{B_REPO}/pulls", None, "deny"),
    ("GET", f"/api/repos/{B_REPO}/branches", None, "deny"),
    ("PATCH", f"/api/repos/{B_REPO}/auto-review", {"enabled": True}, "deny"),
    ("PATCH", f"/api/repos/{B_REPO}/branch", {"branch": "main"}, "deny"),
    ("DELETE", f"/api/repos/{B_REPO}", None, "deny"),
    # review policies on B's repo
    ("GET", "/api/review-policies", None, "no_leak"),
    ("GET", f"/api/review-policies/{B_REPO}", None, "no_leak"),
    ("GET", f"/api/review-policies/{B_REPO}/prompt-preview", None, "no_leak"),
    ("GET", f"/api/review-policies/{B_REPO}/prompt-preview?agent=verifier", None, "no_leak"),
    ("GET", "/api/review-policies/overrides-summary", None, "no_leak"),
    ("GET", f"/api/review-policies/{B_REPO}/branches", None, "deny"),
    ("PUT", f"/api/review-policies/{B_REPO}", {"prompt_template": "pwned"}, "deny"),
    ("DELETE", f"/api/review-policies/{B_REPO}", None, "deny"),
    # reviews, issues, pull requests, analytics
    ("POST", "/api/reviews/trigger", {"pr_ref": "github:bco/b_secret#1"}, "deny"),
    ("GET", "/api/reviews/history", None, "no_leak"),
    ("GET", "/api/issues", None, "no_leak"),
    ("PATCH", "/api/issues/issue-b", {"status": "dismissed"}, "deny"),
    ("GET", "/api/pull-requests", None, "no_leak"),
    ("GET", "/api/analytics/summary", None, "no_leak"),
    # agent prompts, LLM config, connections
    ("GET", "/api/agents", None, "no_leak"),
    ("GET", "/api/agents/defect", None, "no_leak"),
    ("PUT", "/api/agents/defect/prompt",
     {"system_prompt": "Overwritten by somebody from workspace A."}, "no_leak"),
    ("DELETE", "/api/agents/defect/prompt", None, "no_leak"),
    ("GET", "/api/llm/config", None, "no_leak"),
    ("GET", "/api/connections", None, "no_leak"),
    # alerts, automation
    ("GET", "/api/alerts", None, "no_leak"),
    ("PATCH", "/api/alerts/alert-b", {"status": "fixed"}, "deny"),
    ("GET", "/api/alerts/ingest-token", None, "no_leak"),
    ("GET", "/api/automation/history", None, "no_leak"),
    ("GET", "/api/automation/sessions", None, "no_leak"),
    ("GET", "/api/automation/runs/run-b", None, "deny"),
    ("POST", "/api/automation/runs/run-b/stop", None, "deny"),
]


def _actor(name: str) -> str:
    # `both` is a member of B, so it may see B when it ASKS for B. What it
    # must not do is carry B into A: it is probed with A as its active
    # workspace (`both_in_a`), where the B-registered repo is not A's.
    return "both" if name == "both_in_a" else name


async def _b_untouched(w) -> None:
    from src.api.routers.agents import _load_override
    from src.db.models import (
        AutomationRun,
        IncomingAlert,
        RepoReviewPolicy,
        RepoTeamAccess,
        ReviewIssue,
        Team,
        TeamMember,
        Workspace,
        WorkspaceInvite,
    )

    assert await w.scalar(Workspace, w.ws["ws-b"]) is not None
    assert await w.role("admin_b", "ws-b") == "admin"
    assert await w.role("member_b", "ws-b") == "member"
    for who in ("admin_a", "editor_a", "member_a"):
        assert await w.role(who, "ws-b") is None, f"{who} got into B"
    assert (await w.scalar(WorkspaceInvite, "inv-b")).revoked is False
    assert await w.scalar(Team, "team-b") is not None
    assert await w.scalar(TeamMember, ("team-b", w.uid("member_b"))) is not None
    for who in ("admin_a", "editor_a", "member_a", "both"):
        assert await w.scalar(TeamMember, ("team-b", w.uid(who))) is None
    assert (await w.scalar(RepoTeamAccess, (B_REPO, "team-b"))).permission == "admin"
    policy = await w.scalar(RepoReviewPolicy, B_REPO)
    assert policy is not None and policy.prompt_template == f"{B_SECRET} prompt rules"
    assert (await w.scalar(ReviewIssue, "issue-b")).status == "open"
    assert (await w.scalar(IncomingAlert, "alert-b")).status == "new"
    assert await w.scalar(AutomationRun, "run-b") is not None
    assert _load_override("defect", w.ws["ws-b"]) == f"{B_SECRET} agent system prompt"
    from src.api.auto_review import get_auto_review_store

    assert get_auto_review_store().get_in_workspace(w.ws["ws-b"], B_REPO) is not None


@pytest.mark.parametrize("via", ["header", "cookie"])
@pytest.mark.parametrize("actor", A_ROLES)
async def test_a_roles_cannot_reach_workspace_b(tmp_path, monkeypatch, actor, via):
    import src.ee.analytics.router as analytics

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    async with world(tmp_path, monkeypatch) as w:
        who = _actor(actor)
        failures: list[str] = []
        for method, path, body, verdict in PROBES:
            if who == "both" and (path.startswith("/api/workspaces/wsid-b")
                                  or path == "/api/workspaces"):
                # Addressed to B by id, these answer about a workspace `both`
                # really belongs to — legitimately. What `both` is probed for
                # is carrying B's repository into A.
                continue
            path = path.replace("{me}", w.uid(who))
            headers = {"X-Test-User": who}
            if actor == "both_in_a":
                headers["X-Workspace"] = "ws-a"
            elif via == "header":
                headers["X-Workspace"] = "ws-b"
            else:
                w.client.cookies.set("x-workspace", "ws-b")
            r = await w.client.request(method, path, json=body, headers=headers)
            w.client.cookies.clear()
            text = r.text
            if r.status_code >= 500:
                failures.append(f"{method} {path}: {r.status_code} {text[:120]}")
            elif verdict == "deny" and r.status_code not in (403, 404):
                failures.append(f"{method} {path}: expected 403/404, got {r.status_code} "
                                f"{text[:120]}")
            elif B_SECRET in text:
                failures.append(f"{method} {path}: {r.status_code} leaked B data")
        assert not failures, "\n".join(failures)
        await _b_untouched(w)


@pytest.mark.parametrize("actor, asks, gets", [
    ("admin_a", "ws-b", "ws-a"),
    ("editor_a", "ws-b", "ws-a"),
    ("member_a", "ws-b", "ws-a"),
    ("admin_b", "ws-a", "ws-b"),
    ("both", "ws-b", "ws-b"),      # a real member of B may switch to it
    ("both", "ws-a", "ws-a"),
    ("gadmin", "ws-b", "ws-b"),    # platform power: a global admin sees any
])
async def test_the_active_workspace_is_never_one_you_are_not_in(tmp_path, monkeypatch,
                                                                actor, asks, gets):
    """`current_workspace_id` itself, through the route that reports it."""
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.get("/api/workspaces", headers=w.h(actor, asks))
        assert r.status_code == 200, r.text
        assert r.json()["active_id"] == w.ws[gets]
        listed = {x["slug"] for x in r.json()["workspaces"]}
        if actor in ("admin_a", "editor_a", "member_a"):
            assert listed == {"ws-a"}


async def test_admin_b_cannot_reach_a_either(tmp_path, monkeypatch):
    """The matrix is symmetric in spirit; one spot check from the other side."""
    async with world(tmp_path, monkeypatch) as w:
        for method, path in [
            ("GET", f"/api/workspaces/{w.ws['ws-a']}/members"),
            ("GET", f"/api/teams/{w.ids['team_a']}/members"),
            ("GET", f"/api/repos/{A_REPO}/branches"),
            ("PUT", f"/api/review-policies/{A_REPO}"),
        ]:
            r = await w.client.request(method, path, headers=w.h("admin_b", "ws-a"),
                                       json={} if method == "PUT" else None)
            assert r.status_code in (403, 404), f"{method} {path}: {r.status_code}"


async def test_a_grant_in_b_does_not_decide_access_in_a(tmp_path, monkeypatch):
    """`repo_team_access` is keyed by slug. When both tenants register the
    same repository, B's grants must neither close A's copy to A's members
    nor open it to someone through a team of B's."""
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.api.deps import _effective_repo_permission
    from src.db.models import RepoTeamAccess

    async with world(tmp_path, monkeypatch) as w:
        # A registers B's slug too; only B has a grant on it (team-b, admin),
        # and `member_b`'s team is that one.
        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=B_REPO, provider="github",
            full_name="bco/b_secret", url="https://github.com/bco/b_secret",
            workspace_id=w.ws["ws-a"]))
        assert await w.scalar(RepoTeamAccess, (B_REPO, "team-b")) is not None
        perm, any_grants = await _effective_repo_permission(
            B_REPO, w.users["member_b"], w.ws["ws-a"])
        assert (perm, any_grants) == (None, False)
        perm, any_grants = await _effective_repo_permission(
            B_REPO, w.users["member_b"], w.ws["ws-b"])
        assert (perm, any_grants) == ("admin", True)
