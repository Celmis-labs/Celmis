"""Shared builders for the productivity tests: an in-memory database, a scripted provider."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.db.models import (
    ProductivityDeployment,
    ProductivityDeploymentPr,
    ProductivityPrEvent,
    ProductivityPullRequest,
    ProductivityRepoSettings,
    ProductivitySyncState,
)
from src.productivity import settings as settings_mod
from src.productivity.providers.base import (
    ActivityRecord,
    PRDetail,
    ProductivityProvider,
    ProviderDeployment,
    ProviderError,
    PRRecord,
)
from src.productivity.ratelimit import RateLimited


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
WS, PROVIDER, REPO = "ws-1", "bitbucket", "acme/app"

TABLES = [m.__table__ for m in (
    ProductivityRepoSettings, ProductivityPullRequest, ProductivityPrEvent,
    ProductivityDeployment, ProductivityDeploymentPr, ProductivitySyncState)]


def make_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for table in TABLES:
            table.create(conn)
    return engine


def enable(engine, **changes) -> None:
    """Switch the repository on, with the settings a test names."""
    settings_mod.save(WS, PROVIDER, REPO, {"enabled": True, **changes}, engine=engine)


def at(days: float = 0, *, hours: float = 0, minutes: float = 0) -> datetime:
    """A moment relative to NOW (negative = earlier)."""
    return NOW + timedelta(days=days, hours=hours, minutes=minutes)


def pr(number: int, *, state: str = "merged", target: str = "main", source: str = "feature/x",
       created: datetime | None = None, updated: datetime | None = None,
       merged: datetime | None = None, title: str | None = None, author: str = "u-author",
       description: str = "", merge_sha: str | None = None, head_sha: str | None = None) -> PRRecord:
    created = created or at(-20)
    updated = updated or merged or created
    return PRRecord(
        number=number, title=title or f"PR {number}", state=state, created_at=created,
        updated_on=updated, author_key=author, author_name=author.title(), description=description,
        source_branch=source, target_branch=target,
        merged_at=merged if state == "merged" else None, merged_at_approx=False,
        closed_at=updated if state == "declined" else None,
        merge_commit_sha=merge_sha, head_sha=head_sha)


def comment(i: int, actor: str, when: datetime, raw: str = "looks fine") -> ActivityRecord:
    return ActivityRecord("comment", f"comment:{i}", actor, actor.title(), when, raw=raw)


def approval(actor: str, when: datetime) -> ActivityRecord:
    return ActivityRecord("approval", f"approval:{actor}:{when.isoformat()}", actor, actor.title(), when)


class FakeProvider(ProductivityProvider):
    """A provider scripted with PR records and details; records every call it gets."""

    name = "bitbucket"
    page_size = 2

    def __init__(self, prs=(), details=None, shas=None, provided=(), limit_at: int | None = None,
                 limit_until: datetime | None = None, sha_errors=()) -> None:  # noqa: D107
        self.prs = list(prs)
        self.details = dict(details or {})
        self.shas = dict(shas or {})
        self.provided = list(provided)
        self.limit_at, self.limit_until = limit_at, limit_until
        self.list_since: list[datetime] = []
        self.detail_calls: list[int] = []
        self.sha_calls: list[int] = []
        self.sha_errors = set(sha_errors)
        self.deployment_calls = 0
        self.client = SimpleNamespace(close=lambda: None)
        self.max_wait = 20.0

    def list_pull_requests(self, since: datetime) -> Iterator[list[PRRecord]]:
        self.list_since.append(since)
        mine = sorted((p for p in self.prs if p.updated_on and p.updated_on >= since),
                      key=lambda p: (p.updated_on, p.number))
        for i in range(0, len(mine), self.page_size):
            yield mine[i:i + self.page_size]

    def get_pull_request(self, number: int) -> PRRecord | None:
        return next((p for p in self.prs if p.number == number), None)

    def pr_detail(self, record: PRRecord) -> PRDetail:
        if self.limit_at is not None and len(self.detail_calls) >= self.limit_at:
            raise RateLimited(self.limit_until or NOW + timedelta(minutes=30), "fake limit")
        self.detail_calls.append(record.number)
        return self.details.get(record.number) or PRDetail(
            first_commit_at=(record.created_at or NOW) - timedelta(days=1), commits_count=1,
            additions=10, deletions=2, files_changed=1)

    def pr_commit_shas(self, number: int) -> list[str]:
        self.sha_calls.append(number)
        if number in self.sha_errors:
            raise ProviderError("bitbucket: HTTP 404")
        return list(self.shas.get(number, []))

    def deployments(self, since, settings) -> list[ProviderDeployment]:
        self.deployment_calls += 1
        return list(self.provided)

    def count_pull_requests(self, since: datetime) -> int | None:
        return len([p for p in self.prs if p.updated_on and p.updated_on >= since])


def prs_in(engine) -> dict[int, ProductivityPullRequest]:
    with Session(engine) as s:
        rows = s.query(ProductivityPullRequest).all()
        s.expunge_all()
    return {r.number: r for r in rows}


def state_of(engine) -> ProductivitySyncState:
    with Session(engine) as s:
        row = s.get(ProductivitySyncState, (WS, PROVIDER, REPO))
        s.expunge_all()
    return row


def deployments_in(engine) -> list[ProductivityDeployment]:
    with Session(engine) as s:
        rows = s.query(ProductivityDeployment).order_by(ProductivityDeployment.deployed_at).all()
        s.expunge_all()
    return rows


def aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; compare in UTC."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def sync(engine, provider: FakeProvider, **kw):
    """One run of the engine against the scripted provider."""
    from src.productivity.sync import run_repo_sync

    kw.setdefault("now", NOW)
    kw.setdefault("time_budget", 60.0)
    return run_repo_sync(WS, PROVIDER, REPO, provider_factory=lambda cfg: provider,
                         engine=engine, **kw)
