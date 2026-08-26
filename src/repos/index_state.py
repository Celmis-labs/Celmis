"""What we know about a repository's index: when, at which revision, and
whether the last attempt failed.

The `repo_index_state` table has existed since migration b5f83a1c9d02, but the
only writer was the incremental pass (`src/sync/incremental.py`). The FULL
index — the one every "Index" button and every `KIND_INDEX_REPO_FULL` job runs
— wrote nothing, so in production the table held zero rows after six
successful full indexes, and every surface answered "is this repo indexed?"
with `settings.repo_graph_path(slug).exists()`.

A file-exists check cannot tell an hour-old graph from a March one, cannot name
the revision it was built from, and reads a repo whose indexing has failed six
times exactly like a repo nobody has indexed yet. That is how 161 benchmark
reviews went out with no graph context and no surface could say so.

Three rules this module exists to hold:

  * one row, one meaning. Both writers (full and incremental) go through here,
    so `last_indexed_sha` always means "the clone HEAD we successfully indexed"
    and never "the HEAD we were aiming at when we died";
  * a failure never destroys the last good revision. `last_indexed_sha` and
    `last_error` are two independent facts and the row holds both — "the last
    good index was X, and the attempt after it failed with Y";
  * bookkeeping never fails the work. An index that succeeded and then could
    not write this row has still succeeded; every write here swallows and logs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.db.models import RepoIndexState
from src.db.session import get_database_url

logger = logging.getLogger(__name__)

# `last_error` holds "<iso8601 timestamp> | <message>". There is no
# last_error_at column and adding one is not worth a migration: a repo whose
# index keeps dying needs a WHEN as much as a WHAT ("failed" with no date reads
# as a fresh problem forever), and the column is free-form Text that only ever
# reaches a human. Rows written before this encoding — and any written by hand
# — decode as (whole text, None): the message survives intact and only the
# time is unknown, so nothing is lost when the prefix is absent.
_ERROR_SEP = " | "

# GroupIndexResult.failures concatenates one line per repo and each line
# carries an exception message, so `str(exc)` off a group index is unbounded in
# principle. It ends up in a tooltip; keep the head, which is where the cause
# is, and say that the rest was dropped.
_ERROR_MAX_CHARS = 2000


@dataclass(frozen=True)
class RepoIndexInfo:
    """One repository's index bookkeeping, detached from SQLAlchemy.

    Callers get plain values so a surface can render this without importing a
    session, a model or a dialect.
    """

    repo_slug: str
    last_indexed_sha: str | None
    last_indexed_at: datetime | None
    last_full_rebuild_at: datetime | None
    last_indexed_files: int
    last_error: str | None
    last_error_at: datetime | None

    @property
    def short_sha(self) -> str | None:
        """First 8 chars — what a UI shows and what our log lines use."""
        return self.last_indexed_sha[:8] if self.last_indexed_sha else None

    @property
    def failed_since_last_success(self) -> bool:
        """An attempt after the last good index failed.

        True is the state that made this module necessary: the graph on disk,
        if any, is older than the newest attempt and nothing on the repo page
        would have said so.
        """
        return self.last_error is not None


# ─── engine / session ────────────────────────────────────────────────
#
# Same connection style as src/sync/incremental.py, which is the only other
# code that reaches this table: a plain sync engine, because indexing runs in
# a worker thread (`asyncio.to_thread`) where there is no event loop to await
# an AsyncSession on. Disposed after use — an index job builds one of these
# per run and a pool that outlives the run holds a connection for nothing.


def _engine():
    url = get_database_url().replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"
    )
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def _session():
    engine = _engine()
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


# ─── error encoding ──────────────────────────────────────────────────


def _encode_error(message: str, at: datetime) -> str:
    text = (message or "").strip() or "index failed (no message)"
    if len(text) > _ERROR_MAX_CHARS:
        text = text[:_ERROR_MAX_CHARS] + " …(truncated)"
    return f"{at.isoformat()}{_ERROR_SEP}{text}"


def _decode_error(raw: str | None) -> tuple[str | None, datetime | None]:
    """(message, when) — `when` is None for text written without a prefix."""
    if not raw:
        return None, None
    head, sep, tail = raw.partition(_ERROR_SEP)
    if not sep:
        return raw, None
    try:
        return tail, _aware(datetime.fromisoformat(head))
    except ValueError:
        return raw, None


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive value.

    Every timestamp in this table is written as `datetime.now(UTC)`, but a
    backend without timezone support (sqlite, which is what the suite runs on)
    hands it back naive. Callers comparing against `datetime.now(UTC)` would
    raise on the mixed comparison.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_info(row: RepoIndexState) -> RepoIndexInfo:
    message, when = _decode_error(row.last_error)
    return RepoIndexInfo(
        repo_slug=row.repo_slug,
        last_indexed_sha=row.last_indexed_sha,
        last_indexed_at=_aware(row.last_indexed_at),
        last_full_rebuild_at=_aware(row.last_full_rebuild_at),
        last_indexed_files=int(row.last_incremental_files or 0),
        last_error=message,
        last_error_at=when,
    )


# ─── read ────────────────────────────────────────────────────────────


def read_index_state(repo_slug: str) -> RepoIndexInfo | None:
    """What we know about this repo's index, or None if we know nothing.

    Never raises. The repositories list renders fine without a database and
    must keep doing so — losing the freshness column is a worse outcome than
    a 500, but it is not a reason for one.
    """
    try:
        with _session() as session:
            row = session.get(RepoIndexState, repo_slug)
            return _to_info(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("index_state_read_failed repo=%s err=%s", repo_slug, exc)
        return None


def read_index_states(repo_slugs: Iterable[str]) -> dict[str, RepoIndexInfo]:
    """Batch form for list screens — one query, keyed by slug.

    Slugs with no row are absent from the mapping rather than mapped to None,
    so `states.get(slug)` reads the same as `read_index_state(slug)`.
    Never raises; an empty dict means "we could not ask", which renders the
    same as "nothing recorded".
    """
    slugs = [s for s in dict.fromkeys(repo_slugs) if s]
    if not slugs:
        return {}
    try:
        with _session() as session:
            rows = session.scalars(
                select(RepoIndexState).where(RepoIndexState.repo_slug.in_(slugs))
            ).all()
            return {row.repo_slug: _to_info(row) for row in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("index_state_read_batch_failed n=%d err=%s", len(slugs), exc)
        return {}


# ─── write ───────────────────────────────────────────────────────────


def _row(session: Session, repo_slug: str) -> RepoIndexState:
    row = session.get(RepoIndexState, repo_slug)
    if row is None:
        row = RepoIndexState(repo_slug=repo_slug)
        session.add(row)
    return row


def record_index_success(
    repo_slug: str,
    *,
    sha: str | None,
    files: int = 0,
    full_rebuild: bool,
) -> None:
    """The index finished and the graph on disk is this revision.

    `sha` is the clone HEAD that was indexed — `SyncResult.commit_sha` on the
    full path, `git rev-parse HEAD` on the incremental one. None is allowed and
    means "indexed, revision unknown": the previously recorded revision is
    LEFT ALONE rather than erased, because this run learning nothing is not
    the same as the earlier run's answer becoming untrue.

    Clears `last_error`: a success at this revision retires the previous
    failure. Never raises — see the module docstring.
    """
    now = datetime.now(UTC)
    try:
        with _session() as session:
            row = _row(session, repo_slug)
            # Never downgrade a known revision to an unknown one. `sha=None`
            # means THIS run could not name what it indexed; the row may
            # already hold the revision an earlier run did name, and the
            # graph on disk is still that one. Overwriting it with NULL
            # destroys a true fact and leaves an absence that reads as
            # "never recorded" — the same silence this column exists to end.
            if sha is not None:
                row.last_indexed_sha = sha
            row.last_indexed_at = now
            row.last_incremental_files = int(files or 0)
            if full_rebuild:
                row.last_full_rebuild_at = now
            row.last_error = None
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "index_state_write_failed repo=%s sha=%s err=%s",
            repo_slug, (sha or "?")[:8], exc,
        )


def record_index_unchanged(repo_slug: str, *, sha: str) -> None:
    """HEAD is the revision we already indexed — nothing to do, but we looked.

    Deliberately leaves `last_error` alone. Clearing it here would claim a
    failure was fixed by a run that did no indexing work at all.
    """
    now = datetime.now(UTC)
    try:
        with _session() as session:
            row = _row(session, repo_slug)
            row.last_indexed_sha = sha
            row.last_indexed_at = now
            row.last_incremental_files = 0
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "index_state_touch_failed repo=%s sha=%s err=%s",
            repo_slug, sha[:8], exc,
        )


def record_index_failure(repo_slug: str, error: str) -> None:
    """The attempt died. Say so without forgetting the last good revision.

    Touches `last_error` ONLY. `last_indexed_sha`, `last_indexed_at` and
    `last_full_rebuild_at` keep describing the last index that actually
    succeeded, because "the graph is from revision X and the newest attempt
    failed" is the sentence a person needs; overwriting the sha with the one we
    failed at would turn that into a lie about what is on disk.

    Writes a row for a repo that has never succeeded either — cal.diy failed
    six times and left nothing anywhere but a dead queue row.

    Never raises: a failed index must surface as the index error, not as this
    module's error on top of it.
    """
    now = datetime.now(UTC)
    try:
        with _session() as session:
            row = _row(session, repo_slug)
            row.last_error = _encode_error(error, now)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "index_state_failure_write_failed repo=%s err=%s", repo_slug, exc,
        )


__all__ = [
    "RepoIndexInfo",
    "read_index_state",
    "read_index_states",
    "record_index_failure",
    "record_index_success",
    "record_index_unchanged",
]
