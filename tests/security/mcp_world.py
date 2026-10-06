"""A workspace, a cast, four repositories and real MCP tokens — for the access tests.

Not a test module (no ``test_`` prefix); the token and matrix tests import the
`mcp_world` fixture from here.

What is REAL: the resolver and the effective-access layer (SQLite through the
same sync engine path as production), the token rows and their signed JWTs, the
identity resolver that reads them, the repository registry, and the graph files
the tools open. What is replaced: the transport. `get_access_token` returns
whichever bearer the test sets, which is all the HTTP edge hands the tools.

Workspace A holds three repositories, one in each state the rule cares about:

  GRANTED  a team grant (team-g: read)
  RULED    a research rule at `code` for team-r, denying ``secrets/**``
  WILD     no rule, no grant: the default-deny case

Workspace B holds FOREIGN, a repository that must not exist for anybody in A.
"""

from __future__ import annotations

import secrets
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite
from src.config import Settings

WS_A, WS_B = "wsid-a", "wsid-b"
GRANTED, RULED, WILD = "github_aco-app", "github_aco-ruled", "github_aco-wild"
FOREIGN = "github_bco-secret"
GHOST = "github_nobody-nothing"          # registered nowhere
ALL_A = {GRANTED, RULED, WILD}
SECRET_ID = "secrets/keys.py::load_key"
HANDLER = "src/app.py::handler"
HELPER = "src/util.py::helper"

#: name -> (email, global admin, role in A or None)
CAST = {
    "su": ("su@acme.io", True, None),
    "owner": ("owner@acme.io", False, "owner"),
    "admin": ("admin@acme.io", False, "admin"),
    "mg": ("mg@acme.io", False, "member"),       # in team-g
    "mr": ("mr@acme.io", False, "member"),       # in team-r
    "mn": ("mn@acme.io", False, "member"),       # in no team
    "outsider": ("out@acme.io", False, None),    # member of B only
}


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)


@pytest.fixture(scope="module")
def mcp_graphs(tmp_path_factory):
    """Graph files and clone markers for the four repositories, built once."""
    from tests.security.test_mcp_graph_obeys_access_rules_single_tenant import _build

    root = tmp_path_factory.mktemp("mcp-world")
    settings = Settings(workspace_dir=root)
    for slug in (GRANTED, RULED, WILD, FOREIGN):
        _build(settings, slug)
        (settings.repos_dir / slug / ".git").mkdir(parents=True, exist_ok=True)
    return root


_TOOLS: dict | None = None


class McpWorld:
    """Handles the tests use; plain attributes, no behaviour worth reading."""

    def __init__(self) -> None:
        self.engine = None
        self.secret = ""
        self.bearer: str | None = None
        self.users: dict[str, str] = {}

    # ── tokens ───────────────────────────────────────────────────────
    def issue(self, who: str, patterns, *, kind: str = "cli", workspace: str = WS_A,
              days: int | None = 30, write: bool = False, profile: str = "full",
              issued_by: str = "su"):
        """A signed token for ``who`` backed by a real row. Returns (token, view)."""
        from src.mcp_server import token_store

        return token_store.mint(
            kind=kind, workspace_id=workspace, user_id=self.users[who],
            issued_by=self.users[issued_by], label="test", patterns=list(patterns),
            allow_write=write, profile=profile,
            expires_in_days=days)

    def self_token(self, who: str, **kw):
        return self.issue(who, ["*"], kind="self", **kw)

    def legacy_token(self, who: str) -> str:
        """The pre-grant kind: signed, workspace-bound, no row behind it."""
        from src.mcp_server.auth import JwtConfig, issue_token

        return issue_token(JwtConfig.from_env(), subject=self.users[who],
                           scopes=["read:graph", "read:groups", "read:reviews"],
                           extra_claims={"workspace_id": WS_A})

    def as_(self, token: str | None) -> None:
        self.bearer = token

    def row(self, token_id: str):
        from sqlalchemy.orm import Session

        from src.db.models import McpToken

        with Session(self.engine) as s:
            return s.get(McpToken, token_id)

    def session(self):
        from sqlalchemy.orm import Session

        return Session(self.engine)

    def tools(self) -> dict:
        """The stdio tool table. Built once per process: the tools read the
        caller from the request context on every call, not at build time."""
        global _TOOLS
        if _TOOLS is None:
            from src.mcp_server import build_server

            _TOOLS = {n: t.fn for n, t in build_server()._tool_manager._tools.items()}
        return _TOOLS


@pytest.fixture
def mcp_world(tmp_path, monkeypatch, mcp_graphs):
    from sqlalchemy.orm import Session

    from src.access import resolver
    from src.api.auto_review import AutoReviewStore, RepoConfig
    from src.config import get_settings
    from src.db.models import (
        Base,
        RepoAccessRule,
        RepoTeamAccess,
        Team,
        TeamMember,
        Workspace,
        WorkspaceMember,
    )
    from src.deployment import reset_mode_cache
    from src.mcp_server import identity, token_store
    from src.users import User
    from src.users import store as users_mod
    from src.users.store import UserStore

    w = McpWorld()
    w.secret = secrets.token_urlsafe(48)
    monkeypatch.setenv("MCP_JWT_SECRET", w.secret)
    for name in ("MCP_JWT_SECRET_PREVIOUS", "CELMIS_JWT_SECRET_PREVIOUS",
                 "CELMIS_UNRULED_REPO_ACCESS", "CELMIS_MCP_LEGACY_TOKENS",
                 "CELMIS_MCP_SELF_SERVICE", "CELMIS_MCP_TOKEN_MAX_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(mcp_graphs))
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    get_settings.cache_clear()
    reset_mode_cache()
    token_store.invalidate()

    w.engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(w.engine, "connect", _sqlite_booleans)
    Base.metadata.create_all(w.engine)
    monkeypatch.setattr(resolver, "_ENGINE", w.engine)

    ustore = UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", ustore)
    for name, (email, admin, _role) in CAST.items():
        uid = f"u-{name}"
        ustore.create(User(id=uid, email=email, name=name, is_admin=admin))
        w.users[name] = uid

    with Session(w.engine) as s:
        s.add_all([
            Workspace(id=WS_A, name="Alpha", slug="ws-a"),
            Workspace(id=WS_B, name="Bravo", slug="ws-b"),
            Team(id="team-g", name="granted-team", workspace_id=WS_A),
            Team(id="team-r", name="ruled-team", workspace_id=WS_A),
            TeamMember(team_id="team-g", user_id=w.users["mg"]),
            TeamMember(team_id="team-r", user_id=w.users["mr"]),
            RepoTeamAccess(repo_slug=GRANTED, team_id="team-g", permission="read"),
            RepoAccessRule(id="rule-r", workspace_id=WS_A, team_id="team-r",
                           repo_slug=RULED, visibility="code",
                           deny_globs=["secrets/**"]),
            WorkspaceMember(workspace_id=WS_B, user_id=w.users["outsider"], role="member"),
        ])
        for name, (_e, _a, role) in CAST.items():
            if role:
                s.add(WorkspaceMember(workspace_id=WS_A, user_id=w.users[name], role=role))
        s.commit()

    store = AutoReviewStore(tmp_path / "ar.db")
    for ws, slug in ((WS_A, GRANTED), (WS_A, RULED), (WS_A, WILD), (WS_B, FOREIGN)):
        full = slug.split("_", 1)[1].replace("-", "/", 1)
        store.upsert(RepoConfig(user_id=w.users["su"], repo_slug=slug, provider="github",
                                full_name=full, url=f"https://github.com/{full}",
                                workspace_id=ws))
    monkeypatch.setattr("src.api.auto_review._default_store", store)

    import mcp.server.auth.middleware.auth_context as ctx
    def _access_token():
        """What the HTTP edge hands the tools: the bearer and the scopes it carries."""
        if not w.bearer:
            return None
        import jwt as pyjwt

        claims = pyjwt.decode(w.bearer, options={"verify_signature": False})
        return types.SimpleNamespace(token=w.bearer, client_id="c",
                                     scopes=str(claims.get("scope", "")).split())

    monkeypatch.setattr(ctx, "get_access_token", _access_token)
    monkeypatch.setattr(identity, "_LEGACY_LOGGED", set())

    yield w

    w.engine.dispose()
    token_store.invalidate()
    get_settings.cache_clear()
    monkeypatch.delenv("CELMIS_DEPLOYMENT_MODE", raising=False)
    reset_mode_cache()


def set_env(monkeypatch, **env: str) -> None:
    """Set CELMIS_* variables and drop the settings cache so they take effect."""
    from src.config import get_settings
    from src.deployment import reset_mode_cache

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_mode_cache()


def past(days: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
