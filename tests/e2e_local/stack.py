"""An in-process Celmis on a real socket, for the developer-MCP end-to-end tests.

What is real: the FastAPI app from ``build_app()`` with its middleware, the MCP
mounts (``/mcp/`` and, when the tools lane has landed, ``/mcp/dev/``), the
token verifier, the access resolver, FalkorDBLite graphs built by the real
indexer from real ``git clone`` checkouts, and a SQLite database created from
the ORM metadata. What is replaced: pollers and schedulers are switched off,
the LLM is stubbed (``ask`` never leaves the machine), the clock is the real
one, and the JWT secret is random per run.

One uvicorn worker in this process: FalkorDBLite is single-process.

Postgres: pass ``database_url`` (``postgresql+asyncpg://...``) to run the same
stack on a real database; the schema is then created with ``alembic upgrade
head`` instead of ``create_all``, which is what catches Postgres-only SQL in
migrations. ``scripts/dev_mcp_e2e.py --pg`` starts a throwaway container.

Used by pytest (``tests/e2e_local``) and by ``scripts/dev_mcp_e2e.py --spawn``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import tests.api.rbac_world  # noqa: F401 - registers JSONB -> JSON for SQLite
from tests.e2e_local.world import REPOS, ROOT, World, _git, materialize

#: Email of the instance's master (superadmin) identity.
MASTER_EMAIL = "root@acme.example.com"

#: name -> (email, global is_admin, {workspace slug: role})
CAST: dict[str, tuple[str, bool, dict[str, str]]] = {
    "su": (MASTER_EMAIL, True, {}),
    "owner": ("owner@acme.example.com", False, {"ws-a": "owner"}),
    "admin": ("admin@acme.example.com", False, {"ws-a": "admin"}),
    "editor": ("editor@acme.example.com", False, {"ws-a": "editor"}),
    "member": ("member@acme.example.com", False, {"ws-a": "member"}),
    "viewer": ("viewer@acme.example.com", False, {"ws-a": "viewer"}),
    "dev": ("dev@acme.example.com", False, {"ws-a": "member"}),
    "other": ("other@acme.example.com", False, {"ws-b": "member"}),
    "loner": ("loner@acme.example.com", False, {}),
}
WORKSPACES = {"ws-a": "wsid-a", "ws-b": "wsid-b"}
#: A repository of the OTHER workspace, so cross-tenant patterns have a target.
OTHER_REPO_URL = "https://github.com/bco/ledger"


def lane_present(lane: str) -> bool:
    """Feature probe: has the named lane's code landed in this tree?

    ``access``: the token store module. ``tools``: the dev profile is built in
    ``http_app`` (probed in the source, so no import side effects).
    """
    try:
        if lane == "access":
            return importlib.util.find_spec("src.mcp_server.token_store") is not None
        if lane == "tools":
            if importlib.util.find_spec("src.mcp_server.dev_profile") is not None:
                return True
            text = (ROOT / "src" / "mcp_server" / "http_app.py").read_text(encoding="utf-8")
            return "build_dev_mcp" in text
    except (ImportError, ValueError, OSError):
        return False
    raise ValueError(f"unknown lane {lane!r}")


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.lines.append(self.format(record))


@dataclass
class Repo:
    logical: str
    slug: str
    workspace: str
    clone: Path
    head: str


class Stack:
    """``with Stack(tmp) as st:`` then talk to ``st.url`` over HTTP."""

    def __init__(self, root: Path, *, mode: str = "multi_tenant",
                 database_url: str | None = None, extra_env: dict[str, str] | None = None,
                 repos: tuple[str, ...] = tuple(REPOS)) -> None:
        self.root = Path(root)
        self.mode = mode
        self.database_url = database_url
        self.extra_env = dict(extra_env or {})
        self.wanted_repos = repos
        self.world: World | None = None
        self.repos: dict[str, Repo] = {}
        self.users: dict[str, Any] = {}
        self.ws_ids = dict(WORKSPACES)
        self.url = ""
        self.logs = _LogCapture()
        self._env_backup: dict[str, str | None] = {}
        self._patches = contextlib.ExitStack()
        self._server = None
        self._thread: threading.Thread | None = None
        self._engine = None
        self.jwt_secret = secrets.token_urlsafe(48)

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> Stack:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _setenv(self, **values: str) -> None:
        for key, value in values.items():
            self._env_backup.setdefault(key, os.environ.get(key))
            os.environ[key] = value

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = self.root / "data"
        data.mkdir(exist_ok=True)
        # SQLite: DATABASE_URL names the SYNC driver, because the blocking
        # lookups (resolver, index state, token store) pass it straight to
        # create_engine; only the one async engine below uses aiosqlite.
        db_url = self.database_url or f"sqlite:///{self.root / 'celmis.db'}"
        self._setenv(
            CELMIS_DEPLOYMENT_MODE=self.mode,
            CELMIS_MASTER_EMAIL=MASTER_EMAIL,
            WORKSPACE_DIR=str(data),
            DATABASE_URL=db_url,
            MCP_JWT_SECRET=self.jwt_secret,
            CELMIS_DISABLE_POLLER="1",
            CELMIS_DISABLE_SYNC_WORKER="1",
            CELMIS_DISABLE_OWNERSHIP_SCHED="1",
            CELMIS_DISABLE_REFRESH_SCHED="1",
            CELMIS_DISABLE_SAMPLER="1",
            CELMIS_REFRESH_INTERVAL_HOURS="0",
            # Nothing here may call out: the LLM and embeddings are off.
            CELMIS_E2E_NO_LLM="1",
            MCP_ALLOWED_HOSTS="127.0.0.1:*",
            CELMIS_RL_MCP="1000000",
            CELMIS_RL_DEFAULT="1000000",
            CELMIS_RL_AUTH="1000000",
            CELMIS_RL_REVIEW="1000000",
            **self.extra_env,
        )
        os.environ.pop("MCP_ALLOW_UNAUTHENTICATED", None)

        from src import deployment
        from src.config import get_settings

        deployment.reset_mode_cache()
        get_settings.cache_clear()
        self.settings = get_settings()
        self.settings.ensure_directories()

        self._create_database(db_url)
        self._patch_stores()
        self._seed()
        if self.wanted_repos:
            self._add_repos()

        root_logger = logging.getLogger()
        self.logs.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        root_logger.addHandler(self.logs)
        self._prev_level = root_logger.level
        root_logger.setLevel(logging.INFO)

        self._serve()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
        logging.getLogger().removeHandler(self.logs)
        with contextlib.suppress(Exception):
            logging.getLogger().setLevel(self._prev_level)
        if self._engine is not None:
            with contextlib.suppress(Exception):
                import asyncio

                asyncio.run(self._engine.dispose())
        self._patches.close()
        for key, old in self._env_backup.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        from src import deployment
        from src.config import get_settings

        get_settings.cache_clear()
        deployment.reset_mode_cache()

    # ── database and stores ──────────────────────────────────────────

    def _create_database(self, db_url: str) -> None:
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import Engine
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        import src.access.resolver as resolver_mod
        import src.db.session as session_mod
        from src.db.models import Base
        from tests.api.rbac_world import _sqlite_booleans

        sqlite = db_url.startswith("sqlite")
        if sqlite:
            # Every engine anything in the process opens on SQLite gets the
            # Postgres boolean/to_char shims, not only the ones made here.
            def shim(dbapi_conn, record):
                _sqlite_booleans(dbapi_conn, record)

            event.listen(Engine, "connect", shim)
            self._patches.callback(event.remove, Engine, "connect", shim)
            sync = create_engine(db_url)
            with sync.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            Base.metadata.create_all(sync)
            sync.dispose()
            async_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        else:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                           check=True, capture_output=True, env={**os.environ})
            async_url = db_url

        engine = create_async_engine(async_url)
        self._engine = engine
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False,
                                     autoflush=False)
        self._factory = factory
        self._patches.enter_context(mock.patch.object(session_mod, "_engine", engine))
        self._patches.enter_context(mock.patch.object(session_mod, "_session_factory", factory))
        # Process-wide caches of a blocking engine on the previous database.
        self._patches.enter_context(mock.patch.object(resolver_mod, "_ENGINE", None))

    def _patch_stores(self) -> None:
        import src.api.auto_review as ar_mod
        import src.api.review_runs as runs_mod
        import src.credentials.store as cred_mod
        import src.users.store as users_mod

        self._users_store = users_mod.UserStore(self.root / "users.db")
        self._ar_store = ar_mod.AutoReviewStore(self.root / "ar.db")
        self._patches.enter_context(mock.patch.object(users_mod, "_default_store",
                                                      self._users_store))
        self._patches.enter_context(mock.patch.object(ar_mod, "_default_store", self._ar_store))
        self._patches.enter_context(mock.patch.object(cred_mod, "_default_store", None))
        self._patches.enter_context(mock.patch.object(runs_mod, "_default_store", None))

    def _seed(self) -> None:
        from sqlalchemy.orm import Session

        from src.db.models import Workspace, WorkspaceMember
        from src.users import User

        for name, (email, is_admin, _ws) in CAST.items():
            u = User(id="master-admin" if name == "su" else f"u-{name}", email=email,
                     name=name, is_admin=is_admin)
            self._users_store.create(u)
            self.users[name] = self._users_store.get_by_id(u.id)

        with Session(self.sync_engine()) as s:
            for slug, wid in self.ws_ids.items():
                s.add(Workspace(id=wid, name=f"Workspace {slug}", slug=slug, description=""))
            for name, (_e, _adm, memberships) in CAST.items():
                for slug, role in memberships.items():
                    s.add(WorkspaceMember(workspace_id=self.ws_ids[slug],
                                          user_id=self.users[name].id, role=role))
            s.commit()

    def sync_engine(self):
        """A blocking engine on the stack's database (seeding, read-backs)."""
        from sqlalchemy import create_engine

        url = (self.database_url or f"sqlite:///{self.root / 'celmis.db'}")
        url = url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
        return create_engine(url)

    # ── repositories ─────────────────────────────────────────────────

    def _add_repos(self) -> None:
        self.world = materialize(self.root / "src-repos", only=list(self.wanted_repos))
        for logical, (_dirname, url) in REPOS.items():
            if logical in self.world.repos:
                self.add_repo(logical, url, self.world.repos[logical], workspace="ws-a")

    def add_repo(self, logical: str, url: str, source: Path, *, workspace: str = "ws-a",
                 index: bool = True) -> Repo:
        """Register ``url`` in ``workspace``, clone ``source`` as its checkout, index it."""
        from src.api.auto_review import RepoConfig
        from src.sync.git_providers import parse_repo_url

        parsed = parse_repo_url(url)
        clone = self.settings.repo_path(parsed.slug)
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--no-local", f"file://{source}", str(clone)],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        self._ar_store.upsert(RepoConfig(
            user_id=self.users["su"].id, repo_slug=parsed.slug, provider=parsed.provider.value,
            full_name=f"{parsed.owner}/{parsed.name}", url=url,
            workspace_id=self.ws_ids[workspace], branch="develop"))
        head = _git(clone, "rev-parse", "HEAD")
        if index:
            self.reindex(parsed.slug)
        repo = Repo(logical, parsed.slug, workspace, clone, head)
        self.repos[logical] = repo
        return repo

    def reindex(self, slug: str) -> None:
        """Index (or re-index) the checkout in place and record the freshness row."""
        import inspect

        from src.indexing.graph.pipeline import index_repo_graph
        from src.repos import index_state

        clone = self.settings.repo_path(slug)
        index_repo_graph(clone, slug, self.settings)
        head = _git(clone, "rev-parse", "HEAD")
        params = inspect.signature(index_state.record_index_success).parameters
        extra = {"indexed_branch": "develop"} if "indexed_branch" in params else {}
        index_state.record_index_success(slug, sha=head, full_rebuild=True, **extra)
        index_state.record_remote_check(slug, remote_sha=head)

    def observe_remote(self, logical: str) -> str:
        """Record the clone's HEAD as the remote's head, as the push webhook or poller does.

        After ``push_commit`` the index still describes the old tree; this makes the
        freshness line say so (``STALE``) until ``reindex`` catches up.
        """
        from src.repos import index_state

        repo = self.repos[logical]
        head = _git(repo.clone, "rev-parse", "HEAD")
        index_state.record_remote_check(repo.slug, remote_sha=head)
        return head

    def push_commit(self, logical: str, rel: str, content: str, message: str = "change") -> str:
        """Commit a change in the source repo and fast-forward the clone (not the index)."""
        repo = self.repos[logical]
        assert self.world is not None
        src = self.world.repos[logical]
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(content, encoding="utf-8")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", message)
        _git(repo.clone, "pull", "-q", "--ff-only", "origin", "develop")
        return _git(repo.clone, "rev-parse", "HEAD")

    def slug_of(self, logical: str) -> str:
        return self.repos[logical].slug

    # ── tokens ───────────────────────────────────────────────────────

    def token(self, who: str, *, repos: list[str] | None = None, workspace: str = "ws-a",
              allow_write: bool = False, expires_in: int = 3600, scopes: list[str] | None = None,
              profile: str = "dev", issued_by: str = "su") -> str:
        """A bearer token for ``who``.

        With the access lane present: a superadmin-issued row in ``mcp_tokens``
        with ``repos`` as its patterns (logical names are mapped to slugs). Before
        it lands: a legacy JWT with the workspace claim, which is what the tree
        accepted then. ``repos`` is ignored in that case, and callers that need
        it carry ``needs_lane("access")``.
        """
        patterns = [self.repos[r].slug if r in self.repos else r for r in (repos or ["*"])]
        if lane_present("access"):
            from src.mcp_server import token_store

            kwargs: dict[str, Any] = dict(
                kind="pat", workspace_id=self.ws_ids[workspace], user_id=self.users[who].id,
                issued_by=self.users[issued_by].id, label=f"e2e-{who}", patterns=patterns,
                allow_write=allow_write, profile=profile,
                expires_in_days=max(1, expires_in // 86400))
            token, view = token_store.mint(**kwargs)
            self.last_token_id = view.id
            return token
        from src.mcp_server.auth import JwtConfig, issue_token

        sc = scopes or ["read:graph", "read:reviews", "read:code"] + (
            ["write:repos"] if allow_write else [])
        return issue_token(JwtConfig.from_env(), subject=self.users[who].id, scopes=sc,
                           expires_in=expires_in,
                           extra_claims={"workspace_id": self.ws_ids[workspace]})

    def legacy_token(self, who: str, *, workspace: str = "ws-a",
                     scopes: list[str] | None = None) -> str:
        """A pre-grant JWT (no row behind it), the kind older clients still hold."""
        from src.mcp_server.auth import JwtConfig, issue_token

        return issue_token(JwtConfig.from_env(), subject=self.users[who].id,
                           scopes=scopes or ["read:graph", "read:reviews", "read:code"], expires_in=3600,
                           extra_claims={"workspace_id": self.ws_ids[workspace]})

    def expire_token(self, token_id: str) -> None:
        """Move a grant's expiry into the past (the grant row, not the JWT)."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text

        with self.sync_engine().begin() as conn:
            conn.execute(text("UPDATE mcp_tokens SET expires_at = :t WHERE id = :i"),
                         {"t": datetime.now(UTC) - timedelta(minutes=5), "i": token_id})
        from src.mcp_server import token_store

        token_store.invalidate(token_id)

    def revoke_token(self, token_id: str, by: str = "su") -> None:
        from sqlalchemy.orm import Session

        from src.mcp_server import token_store

        with Session(self.sync_engine()) as s:
            token_store.revoke(s, token_id, self.users[by].id)

    def add_foreign_repo(self) -> Repo:
        """A small repository in the OTHER workspace: the target of cross-tenant probes."""
        src = self.root / "src-repos" / "bco-ledger"
        src.mkdir(parents=True, exist_ok=True)
        (src / "ledger.py").write_text(
            "def create_order(total: int) -> int:\n    return total\n", encoding="utf-8")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "e2e",
               "GIT_AUTHOR_EMAIL": "e2e@example.com", "GIT_COMMITTER_NAME": "e2e",
               "GIT_COMMITTER_EMAIL": "e2e@example.com"}
        for args in (["init", "-q", "-b", "develop"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", *args], cwd=src, check=True, capture_output=True, env=env)
        return self.add_repo("bco/ledger", OTHER_REPO_URL, src, workspace="ws-b")

    def wait_audit(self, count: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        """Audit rows are written off the request path: wait until ``count`` exist."""
        deadline = time.time() + timeout
        rows = self.audit_rows()
        while len(rows) < count and time.time() < deadline:
            time.sleep(0.1)
            rows = self.audit_rows()
        return rows

    def expired_token(self, who: str) -> str:
        from src.mcp_server.auth import JwtConfig, issue_token

        return issue_token(JwtConfig.from_env(), subject=self.users[who].id,
                           scopes=["read:graph"], expires_in=-60,
                           extra_claims={"workspace_id": self.ws_ids["ws-a"]})

    # ── serving ──────────────────────────────────────────────────────

    def _serve(self) -> None:
        import uvicorn

        from src.api.main import build_app

        app = build_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                lifespan="on", workers=1)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="celmis-e2e", daemon=True)
        thread.start()
        deadline = time.time() + 60
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("the e2e server died during start-up")
            if time.time() > deadline:
                raise TimeoutError("the e2e server did not start")
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        self._server, self._thread = server, thread
        self.url = f"http://127.0.0.1:{port}"

    # ── what the tests read back ─────────────────────────────────────

    def audit_rows(self) -> list[dict[str, Any]]:
        """Rows of ``mcp_call_log`` (empty before the access lane lands)."""
        if not lane_present("access"):
            return []
        from sqlalchemy import text

        with self.sync_engine().connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text("SELECT * FROM mcp_call_log"))]

    def log_text(self) -> str:
        return "\n".join(self.logs.lines)

    def fresh_id(self) -> str:
        return uuid.uuid4().hex
