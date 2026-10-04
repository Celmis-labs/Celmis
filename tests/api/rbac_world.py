"""Two tenants, a cast of roles, the real routers — for the role tests.

Not a test module (no ``test_`` prefix); `tests/api/test_roles_*.py` and
`tests/security/test_tenant_isolation_matrix.py` build on it.

What is REAL here, because it is what the tests are about:

  * `current_workspace_id` — the header/cookie resolution, membership lookup
    and fall-back. It runs against an SQLite file through the same blocking
    engine path it uses in production (`deps.sync_database_url`).
  * `workspace_role`, `is_workspace_admin`, `require_*` dependencies, the
    team/repo grant lookup (`_effective_repo_permission`), every router's own
    checks, the AutoReview, credential and user stores (SQLite in tmp).
  * the deployment mode: multi_tenant, the mode in which isolation is claimed.

What is replaced: `get_current_user` reads an ``X-Test-User`` header instead of
decoding a JWT (token handling is not the subject), and `record_action` is
spied on so the audit rows can be read back.

Workspace B is seeded with data whose every visible string carries
``B_SECRET`` — so "no data of B" is checkable as "the marker is not in the
response body", for every route, without a per-route parser.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)
    # Postgres `to_char(ts, 'YYYY-MM-DD')` (automation session grouping).
    dbapi_conn.create_function("to_char", 2, lambda ts, _fmt: str(ts or "")[:10])


B_SECRET = "B_SECRET"
MASTER_EMAIL = "root@acme-corp.io"

A_REPO = "github_aco-app"
A_REPO_FULL = "aco/app"
B_REPO = "github_bco-b_secret"
B_REPO_FULL = "bco/b_secret"

#: name → (email, global is_admin, {workspace slug: role})
CAST: dict[str, tuple[str, bool, dict[str, str]]] = {
    "su": (MASTER_EMAIL, True, {}),
    "gadmin": ("gadmin@acme-corp.io", True, {}),
    "owner_a": ("owner-a@acme-corp.io", False, {"ws-a": "owner"}),
    "admin_a": ("admin-a@acme-corp.io", False, {"ws-a": "admin"}),
    "admin2_a": ("admin2-a@acme-corp.io", False, {"ws-a": "admin"}),
    "editor_a": ("editor-a@acme-corp.io", False, {"ws-a": "editor"}),
    "member_a": ("member-a@acme-corp.io", False, {"ws-a": "member"}),
    "viewer_a": ("viewer-a@acme-corp.io", False, {"ws-a": "viewer"}),
    "admin_b": ("admin-b@acme-corp.io", False, {"ws-b": "admin"}),
    "member_b": ("member-b@acme-corp.io", False, {"ws-b": "member"}),
    # Shares workspace A with A's admins (so the enrolment rule lets them name
    # it), and is a member of B too.
    "both": ("both@acme-corp.io", False, {"ws-a": "member", "ws-b": "member"}),
    "loner": ("loner@acme-corp.io", False, {}),
}


@dataclass
class World:
    client: AsyncClient
    users: dict[str, Any]
    ws: dict[str, str]                      # slug → id
    ids: dict[str, str]                     # named seeded rows
    audit: list[dict] = field(default_factory=list)
    factory: Any = None

    def h(self, who: str, ws_slug: str | None = None) -> dict[str, str]:
        headers = {"X-Test-User": who}
        if ws_slug:
            headers["X-Workspace"] = ws_slug
        return headers

    def uid(self, who: str) -> str:
        return self.users[who].id

    async def role(self, who: str, ws_slug: str) -> str | None:
        from src.db.models import WorkspaceMember

        async with self.factory() as s:
            m = await s.get(WorkspaceMember, (self.ws[ws_slug], self.uid(who)))
            return m.role if m is not None else None

    async def scalar(self, model, key):
        async with self.factory() as s:
            return await s.get(model, key)


def _routers():
    from src.api.routers import (
        access_requests,
        admin_users,
        agents,
        alerts,
        automation,
        connections,
        invites,
        issues,
        llm,
        pull_requests,
        repos,
        review_defaults,
        review_policies,
        review_rules,
        reviews,
        teams,
        workspaces,
    )
    from src.ee.analytics import router as analytics

    return [workspaces.router, invites.router, teams.router, admin_users.router,
            agents.router, review_policies.router, review_defaults.router,
            review_rules.router, repos.router, reviews.router,
            issues.router, pull_requests.router, alerts.router, automation.router,
            llm.router, connections.router, analytics.router, access_requests.router]


@contextlib.asynccontextmanager
async def world(tmp_path: Path, monkeypatch, extra_routers: tuple = ()):
    # ── environment: tenancy mode, master identity, data dir ──────────
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "data"))
    db_file = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    from src import deployment
    from src.config import get_settings

    deployment.reset_mode_cache()
    get_settings.cache_clear()

    import src.api.auto_review as ar_mod
    import src.api.review_runs as runs_mod
    import src.credentials.store as cred_mod
    import src.db.session as session_mod
    import src.users.store as users_mod
    from src.users.store import UserStore

    monkeypatch.setattr(users_mod, "_default_store", UserStore(tmp_path / "users.db"))
    monkeypatch.setattr(ar_mod, "_default_store", ar_mod.AutoReviewStore(tmp_path / "ar.db"))
    monkeypatch.setattr(cred_mod, "_default_store", None)
    monkeypatch.setattr(runs_mod, "_default_store", None)

    # ── database: one SQLite file, async for the routers, sync for deps ─
    from src.db.models import Base

    sync_engine = create_engine(f"sqlite:///{db_file}")
    sync_engine.dispose()
    with sync_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    # Postgres spells boolean server defaults `true()` / `false()`; SQLite has
    # no such functions, so every INSERT relying on one failed. Registered on
    # each connection the routers open.
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False,
                                 autoflush=False)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(session_mod, "_session_factory", factory)

    # ── the cast ─────────────────────────────────────────────────────
    from src.users import User

    store = users_mod._default_store
    users: dict[str, Any] = {}
    for name, (email, is_admin, _ws) in CAST.items():
        u = User(id="master-admin" if name == "su" else f"u-{name}", email=email,
                 name=name, is_admin=is_admin)
        store.create(u)
        users[name] = store.get_by_id(u.id)

    # ── audit spy ────────────────────────────────────────────────────
    import src.security.audit as audit_mod

    audit_rows: list[dict] = []
    monkeypatch.setattr(audit_mod, "record_action", lambda **kw: audit_rows.append(kw))

    ws_ids = {"ws-a": "wsid-a", "ws-b": "wsid-b"}
    ids = await _seed(factory, users, ws_ids)

    app = FastAPI()
    for r in (*_routers(), *extra_routers):
        app.include_router(r)

    from src.api.deps import get_current_user

    def _who(x_test_user: str = Header(...)):
        u = store.get_by_id(users[x_test_user].id) if x_test_user in users else None
        if u is None:
            raise HTTPException(status_code=401)
        return u

    app.dependency_overrides[get_current_user] = _who

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t") as c:
        try:
            yield World(client=c, users=users, ws=ws_ids, ids=ids,
                        audit=audit_rows, factory=factory)
        finally:
            await engine.dispose()
            get_settings.cache_clear()
            deployment.reset_mode_cache()


async def _seed(factory, users, ws_ids) -> dict[str, str]:
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.api.routers.agents import _save_override
    from src.db.models import (
        AutomationRun,
        IncomingAlert,
        RepoReviewPolicy,
        RepoTeamAccess,
        ReviewIssue,
        ReviewPullRequest,
        ReviewRule,
        ReviewRuleJob,
        Team,
        TeamMember,
        Workspace,
        WorkspaceInvite,
        WorkspaceMember,
        WorkspaceReviewDefaults,
    )

    a, b = ws_ids["ws-a"], ws_ids["ws-b"]
    now = datetime.now(UTC)
    ids = {"team_a": "team-a", "team_b": "team-b", "invite_b": "inv-b",
           "issue_b": "issue-b", "alert_b": "alert-b", "run_b": "run-b"}
    async with factory() as s:
        s.add(Workspace(id=a, name="Alpha", slug="ws-a", description=""))
        s.add(Workspace(id=b, name=f"Bravo {B_SECRET}", slug="ws-b",
                        description=B_SECRET))
        for name, (_e, _adm, memberships) in CAST.items():
            for slug, role in memberships.items():
                s.add(WorkspaceMember(workspace_id=ws_ids[slug],
                                      user_id=users[name].id, role=role))
        # Teams: A's editors and admins may review A's repo; B's team, B's repo.
        s.add(Team(id=ids["team_a"], name="alpha-team", description="",
                   workspace_id=a))
        s.add(Team(id=ids["team_b"], name=f"team {B_SECRET}", description=B_SECRET,
                   workspace_id=b))
        for who in ("admin_a", "editor_a", "owner_a"):
            s.add(TeamMember(team_id=ids["team_a"], user_id=users[who].id, role="member"))
        s.add(TeamMember(team_id=ids["team_b"], user_id=users["member_b"].id,
                         role="member"))
        s.add(RepoTeamAccess(repo_slug=A_REPO, team_id=ids["team_a"], permission="review"))
        s.add(RepoTeamAccess(repo_slug=B_REPO, team_id=ids["team_b"], permission="admin"))
        s.add(WorkspaceInvite(
            id=ids["invite_b"], workspace_id=b, token_hash=uuid.uuid4().hex,
            email=f"{B_SECRET.lower()}@bravo-corp.io", role="member", max_uses=1,
            expires_at=now + timedelta(days=3), created_by="admin-b@acme-corp.io"))
        s.add(RepoReviewPolicy(repo_slug=B_REPO, workspace_id=b,
                               prompt_template=f"{B_SECRET} prompt rules",
                               target_branches=[], folder_rules=[],
                               agent_prompt_overrides={
                                   "security": f"{B_SECRET} repo security prompt",
                                   "verifier": f"{B_SECRET} repo verifier prompt",
                               }))
        s.add(WorkspaceReviewDefaults(
            workspace_id=b, disabled_agents=["structural"],
            summary_instructions=f"{B_SECRET} summary instructions",
            target_branches=[f"{B_SECRET.lower()}-main"]))
        s.add(ReviewIssue(
            id=ids["issue_b"], workspace_id=b, repo_slug=B_REPO, fingerprint="fpb",
            file_path=f"{B_SECRET}.py", line=1, agent="defect", rule_id="defect.x",
            category="bug", severity="error", title=f"{B_SECRET} issue", body="",
            suggestion=None, status="open", resolution_source=None,
            pr_provider="github", pr_repo=B_REPO_FULL, pr_number=1, pr_url=None,
            first_run_id="r1", last_run_id="r1", occurrences=1,
            first_seen_at=now, last_seen_at=now, closed_at=None))
        s.add(ReviewPullRequest(
            id="pr-b", workspace_id=b, provider="github", repo=B_REPO_FULL, number=1,
            title=f"{B_SECRET} pr", author="x", state="open", reviews_count=1,
            last_review_status="complete", head_ref="f", opened_at=now, updated_at=now))
        s.add(IncomingAlert(id=ids["alert_b"], workspace_id=b, title=f"{B_SECRET} alert"))
        s.add(AutomationRun(id=ids["run_b"], workspace_id=b, user_id=users["admin_b"].id,
                            message=f"{B_SECRET} run", steps=[]))
        # Review rules of B: one workspace-wide, one on B's repo, and a job.
        s.add(ReviewRule(id=901, workspace_id=b, repo_slug=None,
                         title=f"{B_SECRET} rule", instructions=f"{B_SECRET} do",
                         status="active", origin="manual", agents=[]))
        s.add(ReviewRule(id=902, workspace_id=b, repo_slug=B_REPO,
                         title=f"{B_SECRET} repo rule", instructions=f"{B_SECRET} do",
                         status="pending", origin="generated", agents=[]))
        s.add(ReviewRuleJob(id="rjob-b", workspace_id=b, repo_slug=B_REPO,
                            kind="generate", status="completed",
                            progress=f"{B_SECRET} done", result={}))
        await s.commit()

    store = get_auto_review_store()
    store.upsert(RepoConfig(user_id=users["admin_a"].id, repo_slug=A_REPO,
                            provider="github", full_name=A_REPO_FULL,
                            url=f"https://github.com/{A_REPO_FULL}", workspace_id=a))
    # B's repo was registered by `both` — a member of A too. Their own row
    # lives in B; that it is "theirs" must not carry it into A.
    store.upsert(RepoConfig(user_id=users["both"].id, repo_slug=B_REPO,
                            provider="github", full_name=B_REPO_FULL,
                            url=f"https://github.com/{B_REPO_FULL}", workspace_id=b))
    _save_override("defect", f"{B_SECRET} agent system prompt", updated_by="admin-b",
                   workspace_id=b)
    return ids
