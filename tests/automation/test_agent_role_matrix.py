"""The in-app agent restricts every verb by the role of the account asking.

The agent is a second door to what the pages and routes do. A door is only as
safe as its strictest gate being the same one the first door has, so this file
runs the REAL path — `chat.execute`, the real actions, the real role and team
lookups against a real (SQLite) database, the real route functions the actions
delegate to — for every verb of the catalogue and for every kind of asker:

    viewer, member, editor, admin, owner         roles of workspace A
    member / viewer WITH a team grant            the repository half of a gate
    member / viewer WITHOUT one                  (A's repo is granted to a team)
    member of ANOTHER workspace, and an account  not a member at all
      with no membership
    a global admin, and the superadmin           exempt from role and grants

and asserts "refused or not" against `MATRIX`: each row is the rule the
equivalent route enforces, with the route's file and the action's file named,
so a reader can check the row against the code. Only the outside world is
stubbed (the queue, the model, the git provider); every gate runs for real.

Two things a new verb cannot slip past:

  * `test_every_catalogue_verb_has_a_role_decision` — a verb in CATALOGUE with
    no row here fails CI, so adding one forces the question "who may do it?";
  * the approval-endpoint tests at the end of this file: the pressing person's
    own role is asked at execute time, and nobody else's plan can be pressed.

Rows where the agent is deliberately stricter than the page are marked
`stricter`; rows where the page itself is permissive (a viewer may queue an
audit) are marked `route-open` — those are policy questions, pinned here so a
change is made on purpose.
"""

from __future__ import annotations

import types
from dataclasses import dataclass

import pytest

from src.automation.actions import ActionError, Actor
from src.automation.chat import CATALOGUE, Plan, Step, execute
from tests.api.rbac_world import A_REPO, A_REPO_FULL, world

WS_A = "wsid-a"
SECRET_REPO = "github_aco-secret"
SECRET_FULL = "aco/secret"

ALL_ROLES = frozenset({"viewer", "member", "editor", "admin", "owner"})
MEMBER_UP = frozenset({"member", "editor", "admin", "owner"})        # PROPOSER / ISSUE_WRITE
EDITOR_UP = frozenset({"editor", "admin", "owner"})                  # PROMPT_EDITOR / ANALYTICS
ADMIN_UP = frozenset({"admin", "owner"})                             # WORKSPACE_ADMIN


# ─── who is asking ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Asker:
    key: str                 # user in the rbac world
    role: str | None         # role in workspace A (None = not a member)
    is_admin: bool = False   # global admin / superadmin
    grant: bool = False      # a team grants them the repository


ASKERS = {
    "viewer": Asker("viewer_a", "viewer"),
    "member": Asker("member_a", "member"),
    "editor": Asker("editor_a", "editor", grant=True),
    "admin": Asker("admin_a", "admin", grant=True),
    "owner": Asker("owner_a", "owner", grant=True),
    "viewer+grant": Asker("viewer_g", "viewer", grant=True),
    "member+grant": Asker("member_g", "member", grant=True),
    "other-ws member": Asker("member_b", None),
    "no membership": Asker("loner", None),
    "global admin": Asker("gadmin", None, is_admin=True),
    "superadmin": Asker("su", None, is_admin=True),
}


@dataclass(frozen=True)
class Row:
    """One verb (or one scope of a verb) and what it takes.

    roles   workspace roles the ROUTE admits (no membership: never)
    grant   team permission needed on the repository: "" none, "read",
            "review", or "never" (a repository only a global admin may read)
    route   the gate on the equivalent route — file:line
    action  where the action enforces it — file
    note    `stricter` = the action asks for more than the page; `route-open`
            = the page asks for nothing (policy question);
            `pending-by-design` = the agent only files a proposal — INTENDED
            (user decision): a MEMBER may propose review rules through the
            agent even though the route needs an editor; the proposal is
            PENDING and an editor/admin approves it, so nothing changes until
            someone allowed to decides
    """

    verb: str
    args: dict
    roles: frozenset
    grant: str
    route: str
    action: str
    note: str = ""


def _allowed(row: Row, who: Asker) -> bool:
    if who.is_admin:
        return True
    if row.grant == "never":
        return False
    if who.role is None or who.role not in row.roles:
        return False
    return not row.grant or who.grant


R = Row
MATRIX: dict[str, Row] = {
    # ── answers about the product: no data ──
    "explain": R("explain", {"topic": "product"}, ALL_ROLES, "",
                 "none (canned text)", "chat.py execute() explain"),
    "help": R("help", {}, ALL_ROLES, "", "none (canned text)", "chat.py execute() help"),

    # ── what the workspace has ──
    "list_repos": R("list_repos", {}, ALL_ROLES, "",
                    "repos.py GET /api/repos readable_repo_slugs (team read grants)",
                    "actions.py list_repos readable_repo_slugs"),
    "audit_status": R("audit_status", {}, ALL_ROLES, "",
                      "deps.py:151 get_current_user", "actions.py get_dep_audit"),
    "list_findings": R("list_findings", {"run_id": "run-a"}, ALL_ROLES, "",
                       "deps.py:363 get_current_user", "actions.py list_dep_findings"),
    "review_settings[ws]": R("review_settings", {}, ALL_ROLES, "",
                             "review_defaults.py:176 member", "actions.py read_review_settings"),
    "review_settings[repo]": R("review_settings", {"repo_slug": A_REPO}, ALL_ROLES, "read",
                              "review_defaults.py overrides-summary: read grant per repo",
                              "actions.py read_review_settings"),

    # ── queue work ──
    "generate_docs": R("generate_docs", {"repo_slugs": [A_REPO]}, ALL_ROLES, "",
                       "docs.py:160 get_current_user", "actions.py generate_docs",
                       "route-open"),
    "start_dep_audit": R("start_dep_audit", {}, ALL_ROLES, "",
                         "deps.py:109 get_current_user", "actions.py start_dep_audit",
                         "route-open"),
    "set_auto_review": R("set_auto_review", {"repo_slugs": [A_REPO], "enabled": True},
                         ALL_ROLES, "", "repos.py:718 get_current_user",
                         "actions.py set_auto_review", "route-open"),
    "index_repo": R("index_repo", {"repo_slugs": [A_REPO]}, ALL_ROLES, "review",
                    "repos.py:396 get_current_user (no grant)",
                    "actions_reviews.py index_repo + _require_repo_review", "stricter"),
    "review_pr": R("review_pr", {"repo_slug": A_REPO, "number": 5}, ALL_ROLES, "review",
                   "repos.py:1555 require_repo_permission('review')",
                   "actions_reviews.py review_pr + _require_repo_review"),

    # ── review configuration ──
    # Intended, user decision: members propose (pending); editors+ approve.
    "propose_review_rules[ws]": R(
        "propose_review_rules", {"rules": [{"title": "t", "instructions": "do x"}]},
        MEMBER_UP, "", "review_rules.py:271 require_prompt_editor (agent files PENDING)",
        "actions.py propose_review_rules _require_role(PROPOSER)", "pending-by-design"),
    "propose_review_rules[repo]": R(
        "propose_review_rules",
        {"repo_slug": A_REPO, "rules": [{"title": "t", "instructions": "do x"}]},
        MEMBER_UP, "review", "review_rules.py:271 require_prompt_editor (agent files PENDING)",
        "actions.py propose_review_rules _require_role + _require_repo_review",
        "pending-by-design"),
    "generate_review_rules": R(
        "generate_review_rules", {"repo_slug": A_REPO}, MEMBER_UP, "review",
        "review_rules.py:404 require_prompt_editor (agent files PENDING)",
        "actions.py generate_review_rules _require_role + _require_repo_review",
        "pending-by-design"),
    "update_review_setting[ws]": R(
        "update_review_setting",
        {"scope": "workspace", "key": "run_on_drafts", "value": True},
        ADMIN_UP, "", "review_defaults.py:196 require_workspace_admin",
        "actions.py update_review_setting"),
    "update_review_setting[ws guidelines]": R(
        "update_review_setting",
        {"scope": "workspace", "key": "agent_prompt_guidelines",
         "value": {"defect": "Be terse."}},
        EDITOR_UP, "", "agents.py require_prompt_editor",
        "actions.py _update_guidelines require_prompt_editor"),
    "update_review_setting[repo]": R(
        "update_review_setting",
        {"scope": "repo", "repo_slug": A_REPO, "key": "run_on_drafts", "value": True},
        EDITOR_UP, "review", "review_policies.py upsert_policy: prompt editor + review grant",
        "actions.py update_review_setting + _require_repo_review"),

    # ── reading reviews, issues, code ──
    "list_reviews[ws]": R("list_reviews", {}, ALL_ROLES, "",
                          "reviews.py:191 get_current_user; agent also hides unreadable repos",
                          "actions_reviews.py list_reviews"),
    "list_reviews[repo]": R("list_reviews", {"repo_slug": SECRET_REPO}, ALL_ROLES, "never",
                            "reviews.py:191 get_current_user; agent adds the read grant",
                            "actions_reviews.py list_reviews + _can_read", "stricter"),
    "get_review_run": R("get_review_run", {"run_id": "run-secret"}, ALL_ROLES, "never",
                        "reviews.py:203 get_current_user; agent adds the read grant",
                        "actions_reviews.py get_review_run + _can_read", "stricter"),
    "list_issues[ws]": R("list_issues", {}, ALL_ROLES, "",
                         "issues.py:105 get_current_user; agent hides unreadable repos",
                         "actions_reviews.py list_issues"),
    "list_issues[repo]": R("list_issues", {"repo_slug": SECRET_REPO}, ALL_ROLES, "never",
                           "issues.py:105 get_current_user; agent adds the read grant",
                           "actions_reviews.py list_issues + _can_read", "stricter"),
    "update_issue": R("update_issue", {"issue_ids": ["issue-a"], "status": "fixed"},
                      MEMBER_UP, "", "issues.py PATCH ISSUE_WRITE_ROLES",
                      "actions_reviews.py apply_issue_status"),
    "ask_code": R("ask_code", {"question": "how does it work?", "repo_slugs": [A_REPO]},
                  ALL_ROLES, "read", "qa.py research access + read grant",
                  "actions_reviews.py ask_code + _can_read"),
    "search_code": R("search_code", {"kind": "search", "query": "thing", "repo_slug": A_REPO},
                     ALL_ROLES, "read", "search.py research access; agent adds the read grant",
                     "actions_reviews.py search_code + _can_read", "stricter"),

    # ── money and operations ──
    "get_spend": R("get_spend", {}, ADMIN_UP, "", "spend.py summary/daily require_workspace_admin",
                   "actions_ops.py get_spend _require_spend_reader"),
    "get_usage": R("get_usage", {}, EDITOR_UP, "",
                   "usage.py:52 get_current_user (any member)",
                   "actions_ops.py get_usage ANALYTICS_ROLES", "stricter"),
    "get_budget": R("get_budget", {}, ADMIN_UP, "", "spend.py get_budget require_workspace_admin",
                    "actions_ops.py get_budget _require_spend_reader"),
    "set_budget": R("set_budget", {"monthly_usd_cap": 100}, ADMIN_UP, "",
                    "spend.py:419 require_workspace_admin", "actions_ops.py set_budget"),
    "list_alerts": R("list_alerts", {}, ALL_ROLES, "", "alerts.py:365 get_current_user",
                     "actions_ops.py list_alerts", "route-open"),
    "ack_alert": R("ack_alert", {"alert_id": "alert-a"}, ALL_ROLES, "",
                   "alerts.py:387 get_current_user", "actions_ops.py ack_alert",
                   "route-open"),
    "list_jobs": R("list_jobs", {}, ALL_ROLES, "", "jobs.py:97 get_current_user "
                   "(agent: this workspace's rows only)", "actions_ops.py list_jobs"),
    "retry_job": R("retry_job", {"job_id": "job-a"}, ADMIN_UP, "",
                   "jobs.py:132 require_workspace_admin", "actions_ops.py _own_job"),
    "cancel_job": R("cancel_job", {"job_id": "job-a"}, ADMIN_UP, "",
                    "jobs.py:146 require_workspace_admin", "actions_ops.py _own_job"),
    "cancel_dep_audit": R("cancel_dep_audit", {"run_id": "run-a"}, ALL_ROLES, "",
                          "deps.py:170 get_current_user", "actions_ops.py cancel_dep_audit",
                          "route-open"),
    "audit_delta": R("audit_delta", {}, ALL_ROLES, "", "deps.py:781 get_current_user",
                     "actions_ops.py audit_delta"),
    "export_sbom": R("export_sbom", {}, ALL_ROLES, "", "deps.py:683 get_current_user",
                     "actions_ops.py export_sbom"),
    "list_members": R("list_members", {}, ALL_ROLES, "",
                      "workspaces.py:243 member of the workspace",
                      "actions_ops.py list_members"),
}


# ─── the catalogue is covered ────────────────────────────────────────


def test_every_catalogue_verb_has_a_role_decision():
    decided = {row.verb for row in MATRIX.values()}
    missing = sorted(set(CATALOGUE) - decided)
    assert not missing, (
        f"verbs with no row in MATRIX: {missing}. A new agent verb needs an "
        f"explicit answer to 'which role may do this, and what does the "
        f"equivalent route require?' — add it to MATRIX.")
    stale = sorted(decided - set(CATALOGUE))
    assert not stale, f"MATRIX rows for verbs that no longer exist: {stale}"


def test_every_row_names_its_route_and_its_action():
    for key, row in MATRIX.items():
        assert row.route and row.action, key


# ─── the world ───────────────────────────────────────────────────────


@pytest.fixture
async def mx(tmp_path, monkeypatch):
    """Workspace A with the cast, a second repository nobody non-admin may
    read, a stubbed outside world — and real gates."""
    from datetime import UTC, datetime

    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.config import get_settings
    from src.db.models import (
        DepAuditRun,
        IncomingAlert,
        RepoTeamAccess,
        ReviewIssue,
        Team,
        TeamMember,
        WorkspaceMember,
    )
    from src.users import User, get_user_store

    async with world(tmp_path, monkeypatch) as w:
        store = get_user_store()
        for name in ("member_g", "viewer_g"):
            u = User(id=f"u-{name}", email=f"{name}@acme-corp.io", name=name)
            store.create(u)
            w.users[name] = store.get_by_id(u.id)
        now = datetime.now(UTC)

        def issue(id_, repo, full, title, n):
            return ReviewIssue(
                id=id_, workspace_id=WS_A, repo_slug=repo, fingerprint=f"f-{id_}",
                file_path="a.py", line=1, agent="defect", rule_id="defect.x",
                category="bug", severity="error", title=title, body="",
                suggestion=None, status="open", resolution_source=None,
                pr_provider="github", pr_repo=full, pr_number=n, pr_url=None,
                first_run_id="r1", last_run_id="r1", occurrences=1,
                first_seen_at=now, last_seen_at=now, closed_at=None)

        async with w.factory() as s:
            for name, role in (("member_g", "member"), ("viewer_g", "viewer")):
                s.add(WorkspaceMember(workspace_id=WS_A, user_id=w.uid(name), role=role))
                s.add(TeamMember(team_id=w.ids["team_a"], user_id=w.uid(name), role="member"))
            # A second repository of A, granted to a team with nobody in it.
            s.add(Team(id="team-empty", name="nobody", description="", workspace_id=WS_A))
            s.add(RepoTeamAccess(repo_slug=SECRET_REPO, team_id="team-empty",
                                 permission="admin"))
            s.add(DepAuditRun(id="run-a", workspace_id=WS_A, status="done",
                              summary={"repos_total": 1}))
            s.add(issue("issue-a", A_REPO, A_REPO_FULL, "A issue", 1))
            s.add(issue("issue-secret", SECRET_REPO, SECRET_FULL, "SECRET_REPO_ISSUE", 2))
            s.add(IncomingAlert(id="alert-a", workspace_id=WS_A, title="a down"))
            await s.commit()

        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=SECRET_REPO, provider="github",
            full_name=SECRET_FULL, url=f"https://github.com/{SECRET_FULL}",
            workspace_id=WS_A))

        # An indexed repository, so docs/index have something to act on.
        graph = get_settings().repo_graph_path(A_REPO)
        graph.parent.mkdir(parents=True, exist_ok=True)
        graph.write_text("{}")

        _stub_outside_world(monkeypatch)
        await _teach_sqlite_date_trunc()
        yield w


async def _teach_sqlite_date_trunc() -> None:
    """The spend series groups by Postgres' date_trunc; SQLite has none."""
    from sqlalchemy import event

    import src.db.session as session_mod

    def _register(dbapi_conn, _record) -> None:
        dbapi_conn.create_function("date_trunc", 2, lambda _unit, ts: str(ts or "")[:10])

    event.listen(session_mod._engine.sync_engine, "connect", _register)
    await session_mod._engine.dispose()     # connections made so far lack it


def _stub_outside_world(monkeypatch) -> None:
    """The queue, the model and the git provider — nothing that decides who
    may do what."""
    import src.review.rules_generate as rules_generate
    import src.review.rules_store as rules_store
    import src.sync.queue as queue
    from src.api.routers import qa, reviews, search
    from src.automation import actions_reviews as ar
    from src.llm import budget

    jobs: list[dict] = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: jobs.append(kw) or f"job-{len(jobs)}")
    monkeypatch.setattr(queue, "mark_cancelled", lambda *a, **k: None)
    monkeypatch.setattr(queue, "request_cancel", lambda *a, **k: True)
    monkeypatch.setattr(queue, "retry_dead", lambda *a, **k: True)
    monkeypatch.setattr(queue, "stats", lambda **k: {"pending": 0})
    own = {"id": "job-a", "kind": "k", "status": "dead", "workspace_id": WS_A}
    other = {"id": "job-b", "kind": "k", "status": "dead", "workspace_id": "wsid-b"}
    monkeypatch.setattr(queue, "get_job",
                        lambda jid: {"job-a": own, "job-b": other}.get(jid))
    monkeypatch.setattr(queue, "list_jobs", lambda **k: [own])

    async def _propose(ws, repo_slug, rules, **kw):
        return {"ids": ["r1"]}

    async def _generate(ws, repo_slug, actor):
        return {"job_id": "gen-1"}

    monkeypatch.setattr(rules_store, "propose_rules", _propose)
    monkeypatch.setattr(rules_generate, "generate_rules", _generate)
    monkeypatch.setattr(ar, "queue_pr_reviews", lambda cfg, targets, **kw: (
        [{"number": n, "run_id": f"run-{n}", "status": "queued"} for n in targets], []))
    monkeypatch.setattr(ar, "_spawn_inline", lambda payloads: None)

    def _out(run_id, repo_full):
        return types.SimpleNamespace(
            id=run_id, pr_ref=f"{repo_full}#1", pr_provider="github", pr_repo=repo_full,
            pr_number=1, status="complete", verdict="approve", findings_count=0,
            critical=0, error=0, warning=0, info=0, posted=True,
            started_at="2026-10-05T10:00:00", elapsed_seconds=1, summary="",
            status_reason=None, agents_run=[], agents_failed=[])

    runs = {"run-a": _out("run-a", A_REPO_FULL), "run-secret": _out("run-secret", SECRET_FULL)}
    monkeypatch.setattr(reviews, "history", lambda limit, user, ws: list(runs.values()))
    monkeypatch.setattr(reviews, "get_run", lambda run_id, user, ws: runs[run_id])
    monkeypatch.setattr(reviews, "get_findings",
                        lambda run_id, limit, offset, user, ws: {"findings": [], "total": 0})

    async def _answer(**kw):
        return "an answer", {"files_read": [], "blocked_repos": []}

    monkeypatch.setattr(qa, "_generate_full", _answer)
    monkeypatch.setattr(budget, "enforce", lambda ws: None)
    # The spend ledger is a blocking engine of its own; the figure is not the subject.
    monkeypatch.setattr(budget, "get_status", lambda ws="default": budget.BudgetStatus(
        workspace_id=ws, cap_usd=100.0, spent_usd=1.0, hard_stop=False, alert_pct=80))
    monkeypatch.setattr(search, "search",
                        lambda q, repo, limit, user, ws: {"symbols": [], "notes": []})


def _actor(w, who: Asker) -> Actor:
    return Actor(user_id=w.uid(who.key), email=f"{who.key}@acme-corp.io",
                 workspace_id=WS_A, label="matrix")


#: What a refusal by a gate says. Every refusal the agent shows a person is
#: one of these; anything else (a missing run, a job in the wrong state) is the
#: verb running and finding nothing to do, which is not a refusal of the asker.
_GATE_WORDS = ("requires", "no team is granted", "not a member", "could not tell who",
               "needs one of these roles", "yours:", "name one you can read")


def _refused(text: str) -> bool:
    return any(word in text.lower() for word in _GATE_WORDS)


def _all_skipped_by_gate(out: dict) -> bool:
    """`index_repo` reports a repository it may not touch as skipped, not as
    an error — refused when nothing was queued and the reasons are gates."""
    skipped = out.get("skipped") or []
    return bool(skipped) and not out.get("queued") and all(
        _refused(s.get("reason", "")) for s in skipped)


async def _run(w, who: Asker, row: Row) -> str:
    """'refused' | 'ok' | 'other:<message>' for one asker doing one row."""
    async with w.factory() as session:
        plan = Plan(steps=[Step(action=row.verb, arguments=dict(row.args))])
        try:
            out = await execute(plan, _actor(w, who), session)
        except ActionError as exc:
            return "refused" if _refused(str(exc)) else f"other:{exc}"
    first = out["steps"][0]["result"]
    return "refused" if row.verb == "index_repo" and _all_skipped_by_gate(first) else "ok"


# ─── the matrix ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(MATRIX))
async def test_each_verb_is_gated_like_its_route(mx, key):
    row = MATRIX[key]
    wrong: list[str] = []
    for name, who in ASKERS.items():
        got = await _run(mx, who, row)
        if not _allowed(row, who):
            if got != "refused":
                wrong.append(f"{name}: expected a refusal, got {got}")
        elif got == "refused":
            wrong.append(f"{name}: refused, but {row.route} lets them")
        elif got.startswith("other:") and name in ("admin", "global admin"):
            wrong.append(f"{name}: the verb did not run for an allowed asker ({got})")
    assert not wrong, f"{key} [{row.route}]: " + "; ".join(wrong)


# ─── what an allowed asker can SEE ───────────────────────────────────


async def test_a_reader_is_never_shown_a_repository_their_team_excludes(mx):
    """Unscoped reads must not name the secret repository to somebody the
    grant excludes — and still show it to who may read it."""
    plan = Plan(steps=[Step(action="list_issues", arguments={"status": "open"})])
    async with mx.factory() as session:
        out = await execute(plan, _actor(mx, ASKERS["member+grant"]), session)
        issues = out["steps"][0]["result"]
        assert "SECRET_REPO_ISSUE" not in repr(issues) and SECRET_REPO not in repr(issues)
        assert [i["id"] for i in issues["issues"]] == ["issue-a"]
        assert issues["total"] == 1

        out = await execute(plan, _actor(mx, ASKERS["global admin"]), session)
        assert {i["id"] for i in out["steps"][0]["result"]["issues"]} == {
            "issue-a", "issue-secret"}


async def test_the_repo_list_names_only_what_the_asker_may_read(mx):
    """User decision: `list_repos` (agent and MCP) and `GET /api/repos` hide
    the repositories the asker's team grants do not allow reading; workspace
    owners/admins and global admins see everything. Counts follow."""
    async def repos(who):
        async with mx.factory() as session:
            out = await execute(Plan(steps=[Step(action="list_repos")]),
                                _actor(mx, ASKERS[who]), session)
        return out["steps"][0]["result"]

    seen = await repos("member+grant")
    assert [r["repo"] for r in seen["repos"]] == [A_REPO]
    assert seen["count"] == 1 and SECRET_REPO not in repr(seen)
    assert (await repos("member"))["repos"] == [], "no team, no grant, nothing named"
    for who in ("admin", "owner", "global admin"):
        got = await repos(who)
        assert {r["repo"] for r in got["repos"]} == {A_REPO, SECRET_REPO}, who

    # The counts in the review-settings snapshot do not leak the hidden one.
    async with mx.factory() as session:
        out = await execute(Plan(steps=[Step(action="review_settings")]),
                            _actor(mx, ASKERS["member+grant"]), session)
    assert out["steps"][0]["result"]["auto_review"]["repos_total"] == 1


async def test_reviews_of_an_excluded_repository_are_not_listed(mx):
    async with mx.factory() as session:
        out = await execute(
            Plan(steps=[Step(action="list_reviews", arguments={})]),
            _actor(mx, ASKERS["member+grant"]), session)
    repos = {r["repo"] for r in out["steps"][0]["result"]["runs"]}
    assert SECRET_FULL not in repos and A_REPO_FULL in repos


async def test_ask_code_leaves_out_a_repository_the_team_excludes(mx):
    async with mx.factory() as session:
        with pytest.raises(ActionError):
            await execute(
                Plan(steps=[Step(action="ask_code", arguments={
                    "question": "what is in there?", "repo_slugs": [SECRET_REPO]})]),
                _actor(mx, ASKERS["member+grant"]), session)
        out = await execute(
            Plan(steps=[Step(action="ask_code", arguments={"question": "what is in there?"})]),
            _actor(mx, ASKERS["member+grant"]), session)
    assert out["steps"][0]["result"]["repos"] == [A_REPO]


async def test_list_jobs_shows_only_this_workspaces_rows(mx, monkeypatch):
    import src.sync.queue as queue

    seen: dict = {}
    monkeypatch.setattr(queue, "list_jobs", lambda **k: seen.update(k) or [])
    async with mx.factory() as session:
        for name in ("viewer", "global admin", "superadmin"):
            await execute(Plan(steps=[Step(action="list_jobs", arguments={})]),
                          _actor(mx, ASKERS[name]), session)
            assert seen.get("workspace_id") == WS_A, name


# ─── ids from the model are re-checked against the workspace ─────────


@pytest.mark.parametrize("verb, args", [
    ("ack_alert", {"alert_id": "alert-b"}),
    ("update_issue", {"issue_ids": ["issue-b"], "status": "fixed"}),
    ("retry_job", {"job_id": "job-b"}),
    ("cancel_job", {"job_id": "job-b"}),
    ("list_findings", {"run_id": "run-of-b"}),
    ("audit_delta", {"run_id": "run-of-b"}),
    ("export_sbom", {"run_id": "run-of-b"}),
    ("cancel_dep_audit", {"run_id": "run-of-b"}),
    ("set_auto_review", {"repo_slugs": ["github_bco-b_secret"]}),
    ("generate_docs", {"repo_slugs": ["github_bco-b_secret"]}),
    ("start_dep_audit", {"repo_slugs": ["github_bco-b_secret"]}),
    ("index_repo", {"repo_slugs": ["github_bco-b_secret"]}),
    ("review_pr", {"repo_slug": "github_bco-b_secret", "number": 1}),
    ("ask_code", {"question": "x?", "repo_slugs": ["github_bco-b_secret"]}),
    ("search_code", {"kind": "search", "query": "x", "repo_slug": "github_bco-b_secret"}),
    ("review_settings", {"repo_slug": "github_bco-b_secret"}),
    ("list_issues", {"repo_slug": "github_bco-b_secret"}),
])
async def test_an_id_from_another_workspace_reaches_nothing(mx, verb, args):
    """The model names ids; an owner of A gets "not found" for B's, never B's data."""
    from src.db.models import DepAuditRun

    async with mx.factory() as s:
        s.add(DepAuditRun(id="run-of-b", workspace_id="wsid-b", status="done",
                          summary={"B_SECRET": 1}))
        await s.commit()
    async with mx.factory() as session:
        try:
            out = await execute(Plan(steps=[Step(action=verb, arguments=args)]),
                                _actor(mx, ASKERS["owner"]), session)
        except ActionError as exc:
            assert "B_SECRET" not in str(exc)
            return
    assert "B_SECRET" not in repr(out)
    result = out["steps"][0]["result"]
    assert not (result.get("queued") or result.get("updated") or result.get("count")), (
        f"{verb} acted on another workspace's id: {result}")


# ─── a person who is no longer a member ──────────────────────────────


@pytest.mark.parametrize("verb", sorted(CATALOGUE))
async def test_somebody_who_is_not_a_member_can_run_nothing(mx, verb):
    row = next(r for r in MATRIX.values() if r.verb == verb)
    assert await _run(mx, ASKERS["no membership"], row) == "refused"
    assert await _run(mx, ASKERS["other-ws member"], row) == "refused"


# ─── the card and the action agree ───────────────────────────────────

_CARD_VERBS = ("propose_review_rules", "generate_review_rules", "update_review_setting",
               "review_pr", "update_issue", "set_budget", "retry_job", "cancel_job",
               "ack_alert", "cancel_dep_audit")
_ROLE_ASKER = {"viewer": "viewer+grant", "member": "member+grant", "editor": "editor",
               "admin": "admin", "owner": "owner"}


@pytest.mark.parametrize("key", sorted(k for k, r in MATRIX.items()
                                       if r.verb in _CARD_VERBS and r.grant != "never"))
async def test_the_card_refuses_a_role_exactly_when_the_action_would(mx, key):
    """`_role_refusal` is a courtesy — the card says before the press what the
    action will say after it. A card that refuses someone the action lets
    through (or the other way round) is a lie in one direction or the other.
    The asker holds the team grant, so only the ROLE half is being compared."""
    from src.automation.chat import resolve_scope

    row = MATRIX[key]
    wrong = []
    for role, asker_name in _ROLE_ASKER.items():
        plan = resolve_scope(
            Plan(steps=[Step(action=row.verb, arguments=dict(row.args))]),
            workspace_id=WS_A, caller={"role": role})
        card_refuses = "needs one of these roles" in (plan.steps[0].blocked or "")
        action_refuses = await _run(mx, ASKERS[asker_name], row) == "refused"
        if card_refuses != action_refuses:
            wrong.append(f"{role}: card refuses={card_refuses}, action refuses={action_refuses}")
    assert not wrong, f"{key}: " + "; ".join(wrong)


# ─── plan → press: the approval is the pressing person's own ─────────


async def _plan_row(w, *, by: str, verb: str, args: dict, ws: str = WS_A, id_: str = "plan-1"):
    from src.db.models import AutomationRun

    async with w.factory() as s:
        s.add(AutomationRun(
            id=id_, workspace_id=ws, user_id=w.uid(by), user_email=f"{by}@acme-corp.io",
            message="do it", status="planned", note="",
            steps=[{"action": verb, "arguments": args, "note": ""}]))
        await s.commit()
    return id_


async def _press(w, who: str, plan_id: str, ws_slug: str = "ws-a"):
    return await w.client.post("/api/automation/execute", json={"plan_id": plan_id},
                               headers=w.h(who, ws_slug))


async def _status(w, plan_id: str) -> str:
    from src.db.models import AutomationRun

    return (await w.scalar(AutomationRun, plan_id)).status


async def test_the_person_who_planned_it_can_press_it(mx):
    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    r = await _press(mx, "admin_a", pid)
    assert r.status_code == 202, r.text
    assert await _status(mx, pid) == "started"


@pytest.mark.parametrize("presser", ["viewer_a", "member_a", "editor_a", "admin2_a", "gadmin"])
async def test_nobody_else_can_press_someones_plan(mx, presser):
    """Only the workspace OWNER may press a colleague's plan (user decision).
    Another admin, and a global admin who is not an owner of THIS workspace,
    still may not."""
    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    r = await _press(mx, presser, pid)
    assert r.status_code == 403, r.text
    assert await _status(mx, pid) == "planned", "somebody else's press used the plan up"


async def test_the_workspace_owner_may_press_a_colleagues_plan(mx):
    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    r = await _press(mx, "owner_a", pid)
    assert r.status_code == 202, r.text
    assert await _status(mx, pid) == "started"


async def test_an_owner_of_another_workspace_may_not(mx):
    """Owner means owner of THIS workspace."""
    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    r = await _press(mx, "admin_b", pid)
    assert r.status_code in (403, 404), r.text
    assert await _status(mx, pid) == "planned"


async def test_the_owners_press_still_meets_the_role_recheck(mx):
    """The plan was made by a member for a verb a member may run; an owner
    pressing it runs as the owner and is checked as the owner — here that
    changes nothing. And the owner who lost the role before pressing is just
    another colleague."""
    from src.db.models import WorkspaceMember

    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    async with mx.factory() as s:
        (await s.get(WorkspaceMember, (WS_A, mx.uid("owner_a")))).role = "editor"
        await s.commit()
    r = await _press(mx, "owner_a", pid)
    assert r.status_code == 403, r.text
    assert await _status(mx, pid) == "planned"


async def test_a_role_lost_between_plan_and_press_is_felt_at_the_press(mx):
    """The card was shown to an admin; by the time they press they are a viewer."""
    from src.db.models import WorkspaceMember

    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    async with mx.factory() as s:
        (await s.get(WorkspaceMember, (WS_A, mx.uid("admin_a")))).role = "viewer"
        await s.commit()
    r = await _press(mx, "admin_a", pid)
    assert r.status_code == 409, r.text
    assert "admin" in r.json()["detail"]
    assert await _status(mx, pid) == "failed"


async def test_a_member_removed_between_plan_and_press_reaches_nothing(mx):
    from src.db.models import WorkspaceMember

    pid = await _plan_row(mx, by="member_a", verb="set_auto_review",
                          args={"repo_slugs": [A_REPO], "enabled": True})
    async with mx.factory() as s:
        await s.delete(await s.get(WorkspaceMember, (WS_A, mx.uid("member_a"))))
        await s.commit()
    r = await _press(mx, "member_a", pid)
    assert r.status_code == 404, r.text          # pinned elsewhere: A's plan does not exist
    assert await _status(mx, pid) == "planned"


async def test_another_workspaces_plan_does_not_exist(mx):
    pid = await _plan_row(mx, by="admin_a", verb="set_budget", args={"monthly_usd_cap": 50})
    assert (await _press(mx, "admin_b", pid, "ws-b")).status_code == 404
    assert (await _press(mx, "admin_b", pid, "ws-a")).status_code == 404


async def test_the_actor_is_the_authenticated_caller_not_the_plan_text(mx):
    """Whatever the model wrote into a step's arguments, the identity and the
    workspace come from the request: a `user_id` / `workspace_id` smuggled into
    the arguments changes nothing."""
    pid = await _plan_row(mx, by="viewer_a", verb="set_budget", id_="plan-x", args={
        "monthly_usd_cap": 50, "user_id": mx.uid("owner_a"), "workspace_id": WS_A,
        "role": "owner", "is_admin": True})
    r = await _press(mx, "viewer_a", pid)
    assert r.status_code == 409, r.text
    assert "admin" in r.json()["detail"]


# ─── MCP: the same actions, the token owner's identity ───────────────

#: catalogue verb → the MCP ops tool that runs it (src/mcp_server/ops_tools.py)
_MCP_TOOL = {
    "get_spend": "get_spend", "get_usage": "get_usage", "get_budget": "get_budget",
    "list_alerts": "list_alerts", "list_jobs": "list_jobs", "audit_delta": "audit_delta",
    "export_sbom": "export_sbom", "list_members": "list_members",
    "review_settings": "get_review_settings", "set_budget": "set_budget",
    "ack_alert": "ack_alert", "retry_job": "retry_job", "cancel_job": "cancel_job",
    "cancel_dep_audit": "cancel_dep_audit", "update_review_setting": "update_review_setting",
    "propose_review_rules": "propose_review_rules",
    "generate_review_rules": "generate_review_rules",
}


def _ops_tools_for(w, who: Asker) -> dict:
    """The MCP ops tools, wired to `who` the way a token owner's identity is."""
    from src.mcp_server import ops_tools

    tools: dict = {}

    class Stub:
        def tool(self, name=None, description=None, **_kw):
            def keep(fn):
                tools[name] = fn
                return fn
            return keep

    async def in_session(fn):
        async with w.factory() as session:
            return await fn(session)

    ops_tools.register_ops_tools(
        Stub(), lambda label, **_kw: _actor(w, who), in_session, lambda scope: (lambda fn: fn))
    return tools


@pytest.mark.parametrize("key", sorted(k for k, r in MATRIX.items() if r.verb in _MCP_TOOL))
async def test_an_mcp_tool_is_gated_exactly_like_the_agent_verb(mx, key):
    """MCP runs the same action as the agent, for the identity the token names:
    for every asker, refused-or-not is the same on both doors."""
    row = MATRIX[key]
    wrong = []
    for name, who in ASKERS.items():
        tool = _ops_tools_for(mx, who)[_MCP_TOOL[row.verb]]
        out = await tool(**row.args)
        via_mcp = "refused" if not out["ok"] and _refused(out["error"]) else "ok"
        via_agent = "refused" if await _run(mx, who, row) == "refused" else "ok"
        if via_mcp != via_agent:
            wrong.append(f"{name}: mcp {via_mcp}, agent {via_agent}")
    assert not wrong, f"{key}: " + "; ".join(wrong)


def test_every_mcp_tool_that_runs_an_action_runs_it_as_the_caller():
    """The HTTP mount builds an Actor from the token's owner (`_actor`) and
    nothing else: a tool that imported an action and made its own Actor — or
    passed none — would be a door without the identity."""
    import re
    from pathlib import Path

    checked = 0
    for module in ("http_app.py", "server.py"):
        text = (Path(__file__).resolve().parents[2] / "src" / "mcp_server" / module
                ).read_text(encoding="utf-8")
        blocks = re.split(r"\n\s*@mcp\.tool\(", text)[1:]
        for block in blocks:
            name = re.search(r'name="(\w+)"', block)
            if name is None or "async def " not in block:
                continue
            # The tool's own body: up to the next helper the builder defines.
            body = re.split(r"\n    (?:async )?def _", block.split("async def ", 1)[1])[0]
            if "src.automation" not in body:
                continue
            checked += 1
            assert "_actor(" in body, f"{module}: tool {name.group(1)} runs an action without _actor()"
            assert "Actor(" not in body.replace("_actor(", ""), (
                f"{module}: tool {name.group(1)} builds its own Actor")
    assert checked >= 8, "the scan found no tools — the pattern needs updating"
