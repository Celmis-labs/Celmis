# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Builders for the productivity metrics tests: rows, and an app that serves them.

The app is mounted the way production mounts it (`mount_enterprise`, a
licence signed by the suite's throwaway key). Both the async session the
metrics read through and the sync engine the AGPL settings and sync code use
point at ONE sqlite file, so a settings change made through the API is seen by
the metrics read that follows it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.db.models import (
    ProductivityDeployment,
    ProductivityDeploymentPr,
    ProductivityPrEvent,
    ProductivityPullRequest,
    ProductivityRepoSettings,
    ProductivitySyncState,
    ReviewIssue,
)
from src.db.session import get_async_session
from src.ee import license as lic
from src.ee import mount_enterprise
from src.ee.analytics import productivity_router
from src.productivity import db as productivity_db
from tests.ee.licensing import mint_test_license, trust_test_key


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


WS = "ws-1"
NOW = datetime.now(UTC)
REPO = "acme/shop"
PROVIDER = "github"

TABLES = [m.__table__ for m in (
    ProductivityRepoSettings, ProductivityPullRequest, ProductivityPrEvent,
    ProductivityDeployment, ProductivityDeploymentPr, ProductivitySyncState, ReviewIssue)]


def ago(days: float = 0, *, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def merged_pr(number: int, *, repo: str = REPO, author: str = "dana", target: str = "main",
              created: float = 6, first_commit: float | None = 7, review: float | None = 5,
              merged: float = 3, lines: tuple[int, int] = (20, 5), kind: str = "feature",
              deployed: float | None = None, detail: str = "full", provider: str = PROVIDER,
              title: str | None = None, workspace: str = WS) -> dict:
    """A merged PR, every instant given as days ago."""
    return {
        "id": f"{workspace}-{repo}-{number}", "workspace_id": workspace, "provider": provider,
        "repo": repo, "number": number, "title": title or f"PR {number}",
        "url": f"https://git.example.com/{repo}/pull/{number}",
        "author_key": author, "author_name": author.title(), "state": "merged",
        "target_branch": target, "created_at": ago(created),
        "first_commit_at": ago(first_commit) if first_commit is not None else None,
        "first_review_at": ago(review) if review is not None else None,
        "merged_at": ago(merged), "additions": lines[0], "deletions": lines[1],
        "files_changed": 2, "kind": kind,
        "prod_deployed_at": ago(deployed) if deployed is not None else None,
        "detail_state": detail,
    }


def deployment(number: int, *, repo: str = REPO, days: float = 2, failed: bool = False,
               recovered: float | None = None, branch: str = "main", workspace: str = WS,
               provider: str = PROVIDER) -> dict:
    return {
        "id": f"d-{workspace}-{repo}-{number}", "workspace_id": workspace, "provider": provider,
        "repo": repo, "branch": branch, "deployed_at": ago(days), "source": "merge",
        "external_id": f"ext-{number}", "is_failure": failed,
        "recovered_at": ago(recovered) if recovered is not None else None,
    }


def _add_pr(session: Session, row: dict) -> None:
    session.add(ProductivityPullRequest(**row))


@asynccontextmanager
async def api(*, role: str | None, tmp_path, monkeypatch, is_admin: bool = False,
              prs=(), deployments=(), events=(), issues=(), repos=None, groups=None,
              features=("analytics",), enqueue=None):
    """An app serving /api/analytics/productivity over the given rows.

    `repos` is what the workspace has connected (provider, repo, user_id);
    `groups` maps a group name to the full names in it.
    """
    path = tmp_path / "productivity.sqlite"
    sync_engine = sa.create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with sync_engine.begin() as conn:
        for table in TABLES:
            table.create(conn)
    with Session(sync_engine) as s:
        for row in prs:
            _add_pr(s, row)
        s.flush()
        for row in deployments:
            s.add(ProductivityDeployment(**row))
        for row in events:
            s.add(ProductivityPrEvent(**row))
        for row in issues:
            s.add(ReviewIssue(**row))
        s.commit()

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    connected = repos if repos is not None else [
        {"provider": PROVIDER, "repo": REPO, "repo_slug": "github_acme-shop", "user_id": "u-1"}]
    monkeypatch.setattr(productivity_router, "_workspace_repos", lambda ws: list(connected))
    named = {k: frozenset(v) for k, v in (groups or {}).items()}
    monkeypatch.setattr(productivity_router, "_group_repos", lambda ws, g: named.get(g))
    monkeypatch.setattr(productivity_db, "_ENGINE", sync_engine)
    monkeypatch.setattr(deps_module, "workspace_role", lambda uid, ws: role)
    monkeypatch.setattr(
        deps_module, "is_workspace_admin",
        lambda u, ws: bool(getattr(u, "is_admin", False)) or role in ("owner", "admin"))
    if enqueue is not None:
        from src.sync import queue as jq

        monkeypatch.setattr(jq, "enqueue", enqueue)

    trust_test_key(monkeypatch)
    app = FastAPI()
    mount_enterprise(app, env={lic.ENV_KEY: mint_test_license(features)} if features else {})

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u-1", email="u@test", is_admin=is_admin)
    app.dependency_overrides[current_workspace_id] = lambda: WS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.sync_engine = sync_engine  # type: ignore[attr-defined]
        c.app_under_test = app  # type: ignore[attr-defined]
        yield c
    await engine.dispose()
    sync_engine.dispose()


def event(pr_id: str, *, actor: str, kind: str = "comment", days: float = 4, n: int = 1,
          is_bot: bool = False, is_author: bool = False, workspace: str = WS) -> dict:
    return {
        "id": f"e-{pr_id}-{actor}-{kind}-{n}", "workspace_id": workspace, "pr_id": pr_id,
        "kind": kind, "external_id": f"{kind}-{actor}-{n}", "actor_key": actor,
        "actor_name": actor.title(), "at": ago(days), "is_bot": is_bot, "is_author": is_author,
    }
