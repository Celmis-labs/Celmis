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

from tests.api.rbac_world import (
    A_REPO,
    B_PACK_ID,
    B_PACK_INVITE_ID,
    B_PACK_ITEM_ID,
    B_PACK_TOKEN,
    B_PACK_TOKEN_ID,
    B_REPO,
    B_SECRET,
    world,
)

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
    # workspace review defaults: always the ACTIVE workspace's, never B's
    ("GET", "/api/review-defaults", None, "no_leak"),
    ("PUT", "/api/review-defaults",
     {"summary_instructions": "pwned", "disabled_agents": ["defect"]}, "no_leak"),
    ("PUT", "/api/review-defaults",
     {"base_instruction": "pwned", "run_on_drafts": True}, "no_leak"),
    ("PUT", f"/api/review-policies/{B_REPO}",
     {"base_instruction": "pwned", "approve_when_clean": True}, "deny"),
    # the settings overview: the active workspace's defaults and repos only
    ("GET", "/api/review-settings/overview", None, "no_leak"),
    # review rules: the active workspace's only, B's ids and B's repo refused
    ("GET", "/api/review-rules", None, "no_leak"),
    ("GET", f"/api/review-rules?repo={B_REPO}", None, "no_leak"),
    ("GET", "/api/review-rules/library", None, "no_leak"),
    ("GET", "/api/review-rules/jobs", None, "no_leak"),
    ("GET", "/api/review-rules/jobs/rjob-b", None, "deny"),
    ("PATCH", "/api/review-rules/901", {"title": "pwned"}, "deny"),
    ("DELETE", "/api/review-rules/902", None, "deny"),
    ("POST", "/api/review-rules/bulk-status", {"ids": [901, 902], "status": "rejected"},
     "deny"),
    ("POST", "/api/review-rules/bulk-delete", {"ids": [901, 902]}, "deny"),
    ("POST", "/api/review-rules",
     {"repo_slug": B_REPO, "title": "pwned", "instructions": "x"}, "deny"),
    ("POST", "/api/review-rules/library/add",
     {"ids": ["general.no-ignored-exceptions"], "repo_slug": B_REPO}, "deny"),
    ("POST", "/api/review-rules/generate", {"repo_slug": B_REPO}, "deny"),
    ("POST", "/api/review-rules/import", {"repo_slug": B_REPO}, "deny"),
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
    # Codebase: B's knowledge pack, by id and by search
    ("GET", "/api/codebase/packs", None, "no_leak"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}", None, "deny"),
    ("PATCH", f"/api/codebase/packs/{B_PACK_ID}", {"title": "mine now"}, "deny"),
    ("DELETE", f"/api/codebase/packs/{B_PACK_ID}", None, "deny"),
    ("PUT", f"/api/codebase/packs/{B_PACK_ID}/teams", {"teams": []}, "deny"),
    ("POST", f"/api/codebase/packs/{B_PACK_ID}/members",
     {"email": "{me_email}", "permission": "maintain"}, "deny"),
    ("DELETE", f"/api/codebase/packs/{B_PACK_ID}/members/u-admin_b", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/versions", None, "deny"),
    ("POST", f"/api/codebase/packs/{B_PACK_ID}/versions/1/activate", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/versions/1/diff", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/versions/1/archive", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/items/{B_PACK_ITEM_ID}", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/invites", None, "deny"),
    ("POST", f"/api/codebase/packs/{B_PACK_ID}/invites", {}, "deny"),
    ("DELETE", f"/api/codebase/packs/{B_PACK_ID}/invites/{B_PACK_INVITE_ID}", None, "deny"),
    ("GET", f"/api/codebase/packs/{B_PACK_ID}/tokens", None, "deny"),
    ("POST", f"/api/codebase/packs/{B_PACK_ID}/tokens", {"label": "probe"}, "deny"),
    ("DELETE", f"/api/codebase/packs/{B_PACK_ID}/tokens/{B_PACK_TOKEN_ID}", None, "deny"),
    ("POST", "/api/codebase/invites/preview", {"token": B_PACK_TOKEN}, "deny"),
    ("POST", "/api/codebase/invites/redeem", {"token": B_PACK_TOKEN}, "deny"),
    ("GET", "/api/codebase/search?q=b_secret+ledger+reconcile", None, "no_leak"),
    ("POST", "/api/codebase/brief", {"task": "b_secret ledger reconcile procedure"}, "no_leak"),
]


def _actor(name: str) -> str:
    # `both` is a member of B, so it may see B when it ASKS for B. What it
    # must not do is carry B into A: it is probed with A as its active
    # workspace (`both_in_a`), where the B-registered repo is not A's. The
    # same suffix says it of the platform's own accounts (`su_in_a`).
    return name.removesuffix("_in_a")


async def _b_pack_untouched(w) -> None:
    """B's knowledge pack is exactly as seeded: no rename, no new version, member,
    team grant, invite or publish token, the link not used, the token neither
    revoked nor used, nobody of A named on it."""
    from sqlalchemy import func, select

    from src.db.models import (
        KnowledgeItem,
        KnowledgePack,
        KnowledgePackInvite,
        KnowledgePackMember,
        KnowledgePackTeam,
        KnowledgePackToken,
        KnowledgePackVersion,
    )

    pack = await w.scalar(KnowledgePack, B_PACK_ID)
    assert pack is not None and pack.title == f"{B_SECRET} pack"
    assert pack.audience == "workspace" and pack.current_version_id is not None
    invite = await w.scalar(KnowledgePackInvite, B_PACK_INVITE_ID)
    assert (invite.used_count, invite.revoked_at) == (0, None)
    token = await w.scalar(KnowledgePackToken, B_PACK_TOKEN_ID)
    assert (token.revoked_at, token.last_used_at) == (None, None)
    async with w.factory() as s:
        for model, expected in ((KnowledgePackVersion, 1), (KnowledgeItem, 1),
                                (KnowledgePackMember, 1), (KnowledgePackTeam, 0),
                                (KnowledgePackInvite, 1), (KnowledgePackToken, 1)):
            n = await s.scalar(select(func.count()).select_from(model).where(
                model.pack_id == B_PACK_ID))
            assert n == expected, f"{model.__tablename__}: {n}"
        assert await s.scalar(select(func.count()).select_from(KnowledgePack)) == 1, \
            "a pack was created"


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
        WorkspaceReviewDefaults,
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
    defaults = await w.scalar(WorkspaceReviewDefaults, w.ws["ws-b"])
    assert defaults.summary_instructions == f"{B_SECRET} summary instructions"
    assert defaults.disabled_agents == ["structural"]
    assert defaults.base_instruction is None and defaults.run_on_drafts is None
    policy = await w.scalar(RepoReviewPolicy, B_REPO)
    assert policy is not None and policy.prompt_template == f"{B_SECRET} prompt rules"
    assert policy.base_instruction is None and policy.approve_when_clean is None
    assert (await w.scalar(ReviewIssue, "issue-b")).status == "open"
    assert (await w.scalar(IncomingAlert, "alert-b")).status == "new"
    await _b_pack_untouched(w)
    assert await w.scalar(AutomationRun, "run-b") is not None
    assert _load_override("defect", w.ws["ws-b"]) == f"{B_SECRET} agent system prompt"
    from src.db.models import ReviewRule, ReviewRuleJob

    b_rule = await w.scalar(ReviewRule, 901)
    assert (b_rule.title, b_rule.status) == (f"{B_SECRET} rule", "active")
    assert (await w.scalar(ReviewRule, 902)).status == "pending"
    assert (await w.scalar(ReviewRuleJob, "rjob-b")).status == "completed"
    async with w.factory() as s:
        from sqlalchemy import func, select

        n = await s.scalar(select(func.count()).select_from(ReviewRule).where(
            ReviewRule.repo_slug == B_REPO))
        assert n == 1, "a rule was written for B's repository"
    from src.api.auto_review import get_auto_review_store

    assert get_auto_review_store().get_in_workspace(w.ws["ws-b"], B_REPO) is not None


async def _attempt(w, actor: str, via: str, probes, *, slug_stays_hidden: bool = False) -> list[str]:
    """Every probe, as `actor`; the failures, one line each.

    With `slug_stays_hidden` B's repository slug must not appear in an answer unless
    the request itself named it (a 404 may say what the caller typed): the check for
    a world in which B has SHARED that repository with A."""
    who = _actor(actor)
    failures: list[str] = []
    for method, path, body, verdict in probes:
        if who in ("both", "su", "gadmin") and (path.startswith("/api/workspaces/wsid-b")
                              or path == "/api/workspaces"
                              or path.startswith("/api/codebase/invites/")):
            # Addressed to B by id, these answer about a workspace `both`
            # really belongs to — legitimately (an invite link to B's pack
            # is theirs to use: they hold a role in B). What `both` is
            # probed for is carrying B's repository into A.
            continue
        path = path.replace("{me}", w.uid(who))
        if body and "{me_email}" in str(body):
            body = {**body, "email": w.users[who].email}
        headers = {"X-Test-User": who}
        if actor.endswith("_in_a"):
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
        elif slug_stays_hidden and B_REPO in text and B_REPO not in path:
            failures.append(f"{method} {path}: {r.status_code} named the shared repository")
    return failures


@pytest.mark.parametrize("via", ["header", "cookie"])
@pytest.mark.parametrize("actor", A_ROLES)
async def test_a_roles_cannot_reach_workspace_b(tmp_path, monkeypatch, actor, via):
    import src.ee.analytics.router as analytics

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    async with world(tmp_path, monkeypatch) as w:
        failures = await _attempt(w, actor, via, PROBES)
        assert not failures, "\n".join(failures)
        await _b_untouched(w)


# What B has SHARED with A is for tokens only: a share is the owner's consent that
# the superadmin can put on an MCP token, and no other surface reads it. So every
# probe above must still be answered as before when B shares the very repository
# the probes go for, and the routes of sharing itself must tell A's people nothing.
SHARE_PROBES: list[tuple[str, str, dict | None, str]] = [
    ("GET", "/api/sharing/shares", None, "no_leak"),
    ("GET", "/api/sharing/shares/{share}", None, "deny"),
    ("GET", "/api/sharing/shares/{share}/activity", None, "deny"),
    ("PATCH", "/api/sharing/shares/{share}", {"add_deny": ["**"]}, "deny"),
    ("PATCH", "/api/sharing/shares/{share}", {"max_people": 1}, "deny"),
    ("POST", "/api/sharing/shares/{share}/revoke", None, "deny"),
    # the token side: the superadmin's alone
    ("GET", "/api/admin/mcp-shares?workspace=ws-a", None, "deny"),
    ("GET", "/api/admin/mcp-repos?workspace=ws-a", None, "deny"),
    ("GET", "/api/admin/mcp-calls", None, "deny"),
    ("GET", "/api/admin/mcp-tokens", None, "deny"),
]


@pytest.mark.parametrize("via", ["header", "cookie"])
@pytest.mark.parametrize("actor", A_ROLES)
async def test_a_share_to_a_does_not_open_workspace_b_to_anyone_in_a(
        tmp_path, monkeypatch, actor, via):
    import src.ee.analytics.router as analytics
    from tests.api.sharing_world import add_share, share_row, shared_world

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    async with shared_world(tmp_path, monkeypatch) as w:
        share_id = add_share(w)
        probes = [(m, p.replace("{share}", share_id), b, v)
                  for m, p, b, v in (*PROBES, *SHARE_PROBES)]
        failures = await _attempt(w, actor, via, probes, slug_stays_hidden=True)
        assert not failures, "\n".join(failures)
        await _b_untouched(w)
        row = share_row(w, share_id)
        assert (row.revoked_at, row.deny_globs, row.max_people) == (None, [], 5), \
            "a refused request changed the share"


async def test_the_answers_do_not_change_for_a_workspace_that_holds_a_share(tmp_path, monkeypatch):
    """Not only 'no leak' but 'nothing at all': for A's people every probe returns
    the status it returns when B has shared nothing."""
    import src.ee.analytics.router as analytics
    from tests.api.sharing_world import add_share, shared_world

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])

    async def statuses(w, probes) -> dict[tuple[str, str], int]:
        out = {}
        for who in ("admin_a", "member_a"):
            for method, path, body, _verdict in probes:
                r = await w.client.request(
                    method, path.replace("{me}", w.uid(who)), json=body,
                    headers={"X-Test-User": who, "X-Workspace": "ws-a"})
                out[(who, f"{method} {path}")] = r.status_code
        return out

    async with shared_world(tmp_path / "plain", monkeypatch) as w:
        before = await statuses(w, PROBES)
    (tmp_path / "shared").mkdir()
    async with shared_world(tmp_path / "shared", monkeypatch) as w:
        add_share(w)
        after = await statuses(w, PROBES)
    assert after == before


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


#: The routes that go for B's repository by its slug: the ones a share must not make
#: reachable for anybody working in A, the platform's own accounts included.
REPO_PROBES = [p for p in PROBES if B_REPO in p[1] and p[3] == "deny"]


@pytest.mark.parametrize("actor", ["su_in_a", "gadmin_in_a"])
async def test_the_superadmin_working_in_a_finds_the_shared_repository_nowhere_on_the_rest_surface(
        tmp_path, monkeypatch, actor):
    """The superadmin is the one who puts a share on a token, so it is the account
    most likely to be taken for somebody who may read the repository over HTTP.
    A share is read by tokens and nothing else: the pages, the review features and
    the repository routes answer the superadmin working in A exactly as they answer
    about a repository A has never heard of."""
    import src.ee.analytics.router as analytics
    from tests.api.sharing_world import add_share, shared_world

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    assert len(REPO_PROBES) > 10, "the probe list must reach many routes"
    async with shared_world(tmp_path, monkeypatch) as w:
        add_share(w)
        failures = await _attempt(w, actor, "header", REPO_PROBES, slug_stays_hidden=True)
        assert not failures, "\n".join(failures)
        await _b_untouched(w)


SLUG_PARAMS = ("slug", "repo_slug", "repo")
GHOST_SLUG = "github_nobody-nothing"


def _slug_keyed_routes() -> list[tuple[str, str]]:
    """(method, path template) of every route whose only path parameter is a
    repository slug, among the routers the world mounts. Read from the routers, so
    a route added tomorrow is probed tomorrow without anybody remembering to list
    it."""
    import re

    from fastapi.routing import APIRoute

    from tests.api.rbac_world import _routers
    from tests.api.sharing_world import routers

    found = []
    for route in (r for router in (*_routers(), *routers()) for r in router.routes):
        if not isinstance(route, APIRoute):
            continue
        params = re.findall(r"\{(\w+)(?::\w+)?\}", route.path)
        if params and all(p in SLUG_PARAMS for p in params):
            found += [(m, route.path) for m in sorted(route.methods - {"HEAD", "OPTIONS"})]
    return found


@pytest.mark.parametrize("actor", ["admin_a", "member_a", "su_in_a", "gadmin_in_a"])
async def test_every_route_keyed_by_a_repository_answers_for_a_shared_one_as_for_a_missing_one(
        tmp_path, monkeypatch, actor):
    """The strongest form of 'a share is for tokens only': for each route of the API
    that names a repository by its slug, A's people get the answer they get for a
    slug that is registered nowhere, whatever the verb. No guess about which routes
    matter: the walk takes them all."""
    import re

    import src.ee.analytics.router as analytics
    from tests.api.sharing_world import add_share, shared_world

    monkeypatch.setattr(analytics, "_load_runs", lambda ws, since: [])
    async with shared_world(tmp_path, monkeypatch) as w:
        add_share(w)
        routes = _slug_keyed_routes()
        assert len(routes) >= 15, "the walk found the repository routes"
        who = _actor(actor)
        headers = {"X-Test-User": who, "X-Workspace": "ws-a"}
        differ = []
        for method, template in routes:
            body = {} if method in ("POST", "PUT", "PATCH") else None
            seen = {}
            for slug in (B_REPO, GHOST_SLUG):
                url = re.sub(r"\{\w+(?::\w+)?\}", slug, template)
                r = await w.client.request(method, url, json=body, headers=headers)
                assert r.status_code < 500, (method, url, r.status_code, r.text[:200])
                seen[slug] = r.status_code
                if slug == B_REPO:
                    assert B_SECRET not in r.text, (method, url)
            if seen[B_REPO] != seen[GHOST_SLUG]:
                differ.append((method, template, seen))
        assert not differ, f"a shared repository is answered differently from a missing one: {differ}"
        await _b_untouched(w)
