"""Repository sharing over HTTP: workspace B gives workspace A one repository.

Not a test module (no ``test_`` prefix). It rides on `rbac_world`: two tenants,
real routers, real resolver, SQLite. ``B_REPO`` is B's repository (registered in
B, so a share of it is live), ``A_REPO`` is A's own. A holds the people the token
tests need (``member_a`` is a plain member, ``admin_a`` an administrator), B has
``admin_b`` and ``member_b``.

The routers under test are mounted beside the world's own: the owner's
(``/api/sharing``), the token issuer's (``/api/admin``), the person's own
(``/api/mcp``). Rows are written straight into the tables, the way the code under
test reads them, so a test can also make a row the routes would never write.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import src.security.audit as _audit_mod
from tests.api.rbac_world import (
    A_REPO,  # noqa: F401 — re-exported for the tests
    B_REPO,
    B_REPO_FULL,
    _sqlite_booleans,
    world,
)

_REAL_RECORD_ACTION = _audit_mod.record_action     # before any world replaces it

WS_A, WS_B = "wsid-a", "wsid-b"
HOLDER = "member_a"                          # a plain member of A, in no team
HOLDER_EMAIL = "member-a@acme-corp.io"
SECRET = "test-secret-long-enough-for-hs256-aaaaaaaaaaaa"


def routers() -> tuple:
    from src.api.routers import auth, mcp_access, mcp_tokens, sharing

    return (mcp_tokens.router, mcp_tokens.me_router, mcp_access.router, sharing.router,
            auth.router)


@contextlib.asynccontextmanager
async def shared_world(tmp_path, monkeypatch, extra_routers: tuple = ()):
    """The world with the sharing routes mounted and the sync engine of the
    resolver pointed at the same SQLite file (the token routes use it).

    A context manager and not only a fixture, so that a test which already builds
    its own world (the tenant matrix) can run in this one. `extra_routers` are
    mounted beside the sharing ones (the audit page, for the test that reads it)."""
    from src.access import resolver
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
    for name in ("CELMIS_MCP_TOKEN_ISSUERS", "CELMIS_MCP_SELF_SERVICE",
                 "CELMIS_MCP_TOKEN_MAX_DAYS"):
        monkeypatch.delenv(name, raising=False)
    async with world(tmp_path, monkeypatch,
                     extra_routers=(*routers(), *extra_routers)) as w:
        import src.llm.budget as budget
        from src.api.routers import auth

        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        monkeypatch.setattr(budget, "_ENGINE", engine)
        # The account route imported `record_action` by name, which the world's spy
        # does not reach.
        monkeypatch.setattr(auth, "record_action", lambda **kw: w.audit.append(kw))
        token_store.invalidate()
        w.engine = engine
        try:
            yield w
        finally:
            engine.dispose()
            token_store.invalidate()
            get_settings.cache_clear()


@pytest.fixture
async def sw(tmp_path, monkeypatch):
    """`shared_world`, as a fixture (registered for the tests of this package and of
    tests/security by their conftest)."""
    async with shared_world(tmp_path, monkeypatch) as w:
        yield w


def real_audit_trail(monkeypatch) -> None:
    """Put the real action-audit writer back (the world swaps it for a spy), into a
    file under the test's own WORKSPACE_DIR. The engine stays the world's own."""
    import src.security.audit as audit_mod

    monkeypatch.setattr(audit_mod, "record_action", _REAL_RECORD_ACTION)
    monkeypatch.setattr(audit_mod, "_default_logger", None)


def add_share(
    w,
    *,
    slug: str = B_REPO,
    full_name: str = B_REPO_FULL,
    owner: str = WS_B,
    grantee: str = WS_A,
    deny: list[str] | None = None,
    ask: bool = False,
    people: int = 5,
    days: float = 30,
    blocked: list[str] | None = None,
    revoked: bool = False,
    created_ago: timedelta = timedelta(days=90),
) -> str:
    """Insert a share and return its id. It was made ``created_ago`` ago, long
    before any call a test logs, unless the test is about the calls that came first."""
    from src.db.models import RepoShare

    share_id = str(uuid.uuid4())
    with Session(w.engine) as s:
        s.add(RepoShare(
            id=share_id, owner_workspace_id=owner, grantee_workspace_id=grantee,
            repo_slug=slug, repo_provider="github", repo_full_name=full_name,
            deny_globs=list(deny or []), allow_ask=ask, max_people=people,
            blocked_user_ids=list(blocked or []), created_by="master-admin",
            created_at=datetime.now(UTC) - created_ago,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            revoked_at=datetime.now(UTC) if revoked else None,
            revoked_by="master-admin" if revoked else None,
            revoke_reason="owner" if revoked else None))
        s.commit()
    return share_id


def share_row(w, share_id: str):  # noqa: ANN201
    from src.db.models import RepoShare

    with Session(w.engine) as s:
        row = s.get(RepoShare, share_id)
        s.expunge(row)
        return row


def add_call(
    w,
    share_ids: list[str] | None,
    *,
    user: str = HOLDER,
    tool: str = "read_symbol",
    status: str = "ok",
    workspace: str = WS_A,
    ago: timedelta = timedelta(minutes=5),
    repos: list[str] | None = None,
    token_id: str = "tok-1",
) -> None:
    """One call-log row, as the MCP server writes it."""
    from src.db.models import McpCallLog

    with Session(w.engine) as s:
        s.add(McpCallLog(
            ts=datetime.now(UTC) - ago, workspace_id=workspace, user_id=w.uid(user),
            token_id=token_id, kind="pat", client_id="", tool=tool, profile="dev",
            repos=list(repos or []), status=status, result_bytes=1234, result_items=7,
            duration_ms=9, args_hash="abc123", shares=share_ids))
        s.commit()


def actions(w, prefix: str = "") -> list[tuple[str, str, str]]:
    """``(action, workspace_id, target)`` of the audit rows written so far."""
    return [(r["action"], r.get("workspace_id"), r.get("target")) for r in w.audit
            if r["action"].startswith(prefix)]


def audit_rows(w, action: str) -> list[dict[str, Any]]:
    return [r for r in w.audit if r["action"] == action]


def register_b(w, slug: str, full_name: str, *, workspace: str = WS_B) -> None:
    """Register one more repository in the owner's workspace (a share is live only
    for a repository its owner still has registered under the same name)."""
    from src.api.auto_review import RepoConfig, get_auto_review_store

    get_auto_review_store().upsert(RepoConfig(
        user_id=w.uid("both"), repo_slug=slug, provider="github", full_name=full_name,
        url=f"https://github.com/{full_name}", workspace_id=workspace))


_COUNTER = iter(range(1, 10_000))


def add_fresh_share(w, **kw) -> str:
    """A live share of a repository of its own: B registers it first. There can be
    only one unended share of a repository to a workspace, so a test that needs
    several shares needs several repositories."""
    n = next(_COUNTER)
    slug, registered = f"github_bco-r{n}", f"bco/r{n}"
    register_b(w, slug, registered)
    kw.setdefault("full_name", registered)
    return add_share(w, slug=slug, **kw)
