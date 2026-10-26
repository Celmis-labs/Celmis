"""A sqlite stand-in for `sync_jobs`, for the tests of the deferred list.

The queue's SQL is Postgres-flavoured in two places only (`FOR UPDATE SKIP
LOCKED` in `dequeue_one`, `CAST(.. AS jsonb)` in `enqueue`); everything the
deferral adds — park, claim, release, reschedule, expire, retry-now — is
portable SQL, so it runs here against a real database file. Writers are
serialised with BEGIN IMMEDIATE, the nearest sqlite gets to row locks.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

import src.sync.queue as jq

DDL = """
CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, workspace_id TEXT, dedup_key TEXT,
    payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
    next_run_at TIMESTAMP NOT NULL, locked_until TIMESTAMP, locked_by TEXT,
    last_error TEXT, enqueued_by TEXT, created_at TIMESTAMP, started_at TIMESTAMP,
    finished_at TIMESTAMP, cancel_requested BOOLEAN NOT NULL DEFAULT 0,
    deferred_since TIMESTAMP, defer_reason TEXT, defer_code TEXT,
    defer_checks INTEGER NOT NULL DEFAULT 0
)
"""


def make_engine(path) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})

    @sa.event.listens_for(engine, "connect")
    def _no_autobegin(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def _begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    with engine.begin() as conn:
        conn.exec_driver_sql(DDL)
    return engine


def use(monkeypatch, engine) -> None:
    monkeypatch.setattr(jq, "_ENGINE", engine)


def insert(engine, *, status="deferred", kind="review", ws="ws", payload=None,
           dedup=None, attempts=0, deferred_ago: timedelta | None = timedelta(minutes=5),
           next_in: timedelta = timedelta(seconds=-1), checks=0, reason="the provider is unavailable",
           job_id=None) -> str:
    now = datetime.now(UTC)
    job_id = job_id or str(uuid.uuid4())
    payload = payload if payload is not None else {
        "provider": "github", "repo": "o/r", "pr_number": 1, "workspace_id": ws,
        "user_id": "u", "run_id": f"run-{job_id[:6]}", "post_comments": True}
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO sync_jobs (id, kind, workspace_id, dedup_key, payload, status, "
            "attempts, next_run_at, deferred_since, defer_reason, defer_checks, created_at) "
            "VALUES (:id, :k, :ws, :d, :p, :s, :a, :n, :since, :why, :c, :now)"),
            {"id": job_id, "k": kind, "ws": ws, "d": dedup, "p": json.dumps(payload),
             "s": status, "a": attempts, "n": now + next_in,
             "since": (now - deferred_ago) if deferred_ago is not None else None,
             "why": reason if status == "deferred" else None, "c": checks, "now": now})
    return job_id


def row(engine, job_id: str) -> dict:
    with engine.begin() as conn:
        return dict(conn.execute(sa.text("SELECT * FROM sync_jobs WHERE id = :i"),
                                 {"i": job_id}).mappings().one())


def as_dt(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)
