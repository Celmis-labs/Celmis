"""Auto-review per-repo configuration store.

Tracks which repos have auto-review enabled (per user) and the polling
state needed to incrementally fetch new PRs/MRs without missing or
duplicating reviews.

Schema:
    auto_review_config (
        user_id          TEXT NOT NULL,
        repo_slug        TEXT NOT NULL,
        provider         TEXT NOT NULL,
        full_name        TEXT NOT NULL,   -- owner/name
        url              TEXT NOT NULL,
        branch           TEXT,             -- NULL → repo default branch
        enabled          INTEGER NOT NULL DEFAULT 0,
        mode             TEXT NOT NULL DEFAULT 'polling',  -- polling|webhook|manual
        last_poll_etag   TEXT,
        last_seen_pr_id  INTEGER,
        last_polled_at   TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (user_id, repo_slug)
    )
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_review_config (
    user_id          TEXT NOT NULL,
    repo_slug        TEXT NOT NULL,
    provider         TEXT NOT NULL,
    full_name        TEXT NOT NULL,
    url              TEXT NOT NULL,
    workspace_id     TEXT NOT NULL DEFAULT 'default',
    branch           TEXT,
    enabled          INTEGER NOT NULL DEFAULT 0,
    mode             TEXT NOT NULL DEFAULT 'polling',
    last_poll_etag   TEXT,
    last_seen_pr_id  INTEGER,
    last_polled_at   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, repo_slug)
);

CREATE INDEX IF NOT EXISTS idx_auto_review_enabled
    ON auto_review_config(enabled);

CREATE INDEX IF NOT EXISTS idx_auto_review_repo
    ON auto_review_config(provider, full_name);
"""


@dataclass
class RepoConfig:
    user_id: str
    repo_slug: str
    provider: str
    full_name: str
    url: str
    workspace_id: str = "default"
    # Branch to clone/index. None → whatever the provider calls the default
    # branch. Persisted so every surface (index, dep audit, agent workspace)
    # works off the SAME ref instead of silently drifting to `main`.
    branch: str | None = None
    enabled: bool = False
    mode: str = "polling"
    last_poll_etag: str | None = None
    last_seen_pr_id: int | None = None
    last_polled_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            now = datetime.now(UTC).isoformat()
            self.created_at = now
            self.updated_at = now


class AutoReviewStore:
    """SQLite-backed per-repo auto-review config + polling state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")
            # Idempotent migration for DBs created before workspace_id existed.
            # SQLite (pre-3.35) has no ADD COLUMN IF NOT EXISTS, so probe first.
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(auto_review_config)"
            ).fetchall()}
            if "workspace_id" not in cols:
                conn.execute(
                    "ALTER TABLE auto_review_config "
                    "ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_auto_review_repo "
                    "ON auto_review_config(provider, full_name)"
                )
            # Same idempotent shape for `branch` (added later). Nullable with no
            # default, so existing rows read back as None = "default branch",
            # which is exactly the behaviour they had before the column existed.
            if "branch" not in cols:
                conn.execute(
                    "ALTER TABLE auto_review_config ADD COLUMN branch TEXT"
                )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def upsert(self, cfg: RepoConfig) -> RepoConfig:
        cfg.updated_at = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auto_review_config
                    (user_id, repo_slug, provider, full_name, url, workspace_id,
                     branch, enabled, mode,
                     last_poll_etag, last_seen_pr_id, last_polled_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, repo_slug) DO UPDATE SET
                    provider = excluded.provider,
                    full_name = excluded.full_name,
                    url = excluded.url,
                    workspace_id = excluded.workspace_id,
                    branch = excluded.branch,
                    enabled = excluded.enabled,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (
                    cfg.user_id, cfg.repo_slug, cfg.provider, cfg.full_name, cfg.url,
                    cfg.workspace_id, cfg.branch, int(cfg.enabled), cfg.mode,
                    cfg.last_poll_etag, cfg.last_seen_pr_id, cfg.last_polled_at,
                    cfg.created_at, cfg.updated_at,
                ),
            )
        return cfg

    def get(self, user_id: str, repo_slug: str) -> RepoConfig | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM auto_review_config
                   WHERE user_id=? AND repo_slug=?""",
                (user_id, repo_slug),
            ).fetchone()
        return self._row_to_cfg(row) if row else None

    def list_for_user(self, user_id: str) -> list[RepoConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_review_config WHERE user_id=? ORDER BY full_name",
                (user_id,),
            ).fetchall()
        return [self._row_to_cfg(r) for r in rows]

    def list_for_workspace(self, workspace_id: str) -> list[RepoConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_review_config WHERE workspace_id=? "
                "ORDER BY full_name",
                (workspace_id,),
            ).fetchall()
        return [self._row_to_cfg(r) for r in rows]

    def get_in_workspace(self, workspace_id: str, repo_slug: str) -> RepoConfig | None:
        """Repo by slug within a workspace — regardless of which member
        registered it (rows are keyed (user_id, repo_slug))."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM auto_review_config "
                "WHERE workspace_id=? AND repo_slug=? LIMIT 1",
                (workspace_id, repo_slug),
            ).fetchone()
        return self._row_to_cfg(row) if row else None

    def delete_in_workspace(self, workspace_id: str, repo_slug: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM auto_review_config "
                "WHERE workspace_id=? AND repo_slug=?",
                (workspace_id, repo_slug),
            )
        return cur.rowcount > 0

    def list_all(self) -> list[RepoConfig]:
        """Every registered repo config, across all users/workspaces."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_review_config ORDER BY full_name"
            ).fetchall()
        return [self._row_to_cfg(r) for r in rows]

    def list_enabled(self, mode: str | None = None) -> list[RepoConfig]:
        sql = "SELECT * FROM auto_review_config WHERE enabled=1"
        params: tuple = ()
        if mode:
            sql += " AND mode=?"
            params = (mode,)
        sql += " ORDER BY user_id, full_name"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_cfg(r) for r in rows]

    def workspace_for_repo(self, provider: str, full_name: str) -> str | None:
        """Deterministic repo → workspace mapping for webhook routing.

        Returns the single workspace_id this repo is bound to, or None if the
        repo is unknown OR bound to more than one workspace. An ambiguous repo
        must make the webhook FAIL CLOSED rather than guess and run a review
        under the wrong tenant's keys (the hijack the design critique flagged)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT workspace_id FROM auto_review_config "
                "WHERE provider=? AND full_name=?",
                (provider, full_name),
            ).fetchall()
        ws = {r["workspace_id"] for r in rows}
        return ws.pop() if len(ws) == 1 else None

    def config_for_repo(self, provider: str, full_name: str) -> RepoConfig | None:
        """The single config row for a repo, for webhook routing.

        `workspace_for_repo` answers only WHICH tenant, and the webhook needs
        two more things from the same row: whose credential to run under, and
        whether auto-review is switched on at all.

        Both were missing and both were bugs. The dispatcher hardcoded
        `user_id="default"`, so `resolve_auth` looked for a personal Claude
        credential in a slot nobody owns and every webhook-triggered review
        died in 0.06s with "no credential is configured" — while a manual
        trigger of the SAME pull request, two minutes later, completed in 18.6s
        on the credential that was there all along. And nothing consulted
        `enabled`, so a delivery ran a review on a repo whose owner had
        switched auto-review off.

        Same fail-closed rule as `workspace_for_repo`: unknown repo, or a repo
        bound to more than one workspace, returns None. An ambiguous repo must
        not run under a guessed tenant's keys.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_review_config WHERE provider=? AND full_name=?",
                (provider, full_name),
            ).fetchall()
        if not rows:
            return None
        if len({r["workspace_id"] for r in rows}) != 1:
            return None
        # One workspace, but possibly several members' rows. Prefer an enabled
        # one: a repo somebody has switched ON is the one the delivery is for,
        # and picking a disabled sibling row would refuse a review the owner
        # asked for. Ties break on the stable ordering the table returns.
        for row in rows:
            if row["enabled"]:
                return self._row_to_cfg(row)
        return self._row_to_cfg(rows[0])

    def workspace_for_slug(self, repo_slug: str) -> str | None:
        """Deterministic slug → workspace mapping, for surfaces that know only
        a repo slug: the vault re-index jobs, the incremental indexer, and the
        vector backfill.

        Returns None when the slug is registered NOWHERE, or is bound to more
        than one workspace. Both readings are "we do not know whose this is",
        and both must fail closed — a guess here does not route a webhook, it
        stamps an owner onto a vector point and hands one tenant's code to
        another. `existing_slug_binding` deliberately answers a different
        question (LIMIT 1, "is this slug taken at all"), which is why this is a
        second method and not a rename of that one.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT workspace_id FROM auto_review_config "
                "WHERE repo_slug=?",
                (repo_slug,),
            ).fetchall()
        ws = {r["workspace_id"] for r in rows}
        return ws.pop() if len(ws) == 1 else None

    def existing_workspace_binding(self, provider: str, full_name: str) -> str | None:
        """The workspace_id an already-registered repo is bound to (across all
        users), or None if unregistered. Enforces a 1:1 repo→workspace binding
        at registration time so a repo can never span two tenants."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workspace_id FROM auto_review_config "
                "WHERE provider=? AND full_name=? LIMIT 1",
                (provider, full_name),
            ).fetchone()
        return row["workspace_id"] if row else None

    def existing_slug_binding(self, repo_slug: str) -> str | None:
        """The workspace a SLUG is already bound to, across all providers.

        `full_name` is not the key that matters. Everything downstream —
        the clone, the graph, the vault — is keyed on `slug`, and
        ParsedRepo.slug flattens an owner path with '-', so
        gitlab.com/acme/group/billing and gitlab.com/acme-group/billing are two
        different repositories that produce one identical slug.

        Registering both put two tenants on one vault directory: the ownership
        check in docs.py verifies the slug belongs to the caller's workspace,
        passes, and then hands over the other tenant's notes — readable through
        GET /api/docs/{slug}/note and rewritable through the regenerate
        endpoint. The full_name guard never fired, because the full names
        genuinely differ.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workspace_id FROM auto_review_config "
                "WHERE repo_slug=? LIMIT 1",
                (repo_slug,),
            ).fetchone()
        return row["workspace_id"] if row else None

    def update_polling_state(
        self,
        user_id: str, repo_slug: str,
        *,
        etag: str | None = None,
        last_seen_pr_id: int | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE auto_review_config SET
                       last_poll_etag = COALESCE(?, last_poll_etag),
                       last_seen_pr_id = COALESCE(?, last_seen_pr_id),
                       last_polled_at = ?,
                       updated_at = ?
                   WHERE user_id=? AND repo_slug=?""",
                (etag, last_seen_pr_id, now, now, user_id, repo_slug),
            )

    def delete(self, user_id: str, repo_slug: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM auto_review_config WHERE user_id=? AND repo_slug=?",
                (user_id, repo_slug),
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_cfg(row: sqlite3.Row) -> RepoConfig:
        keys = row.keys()
        return RepoConfig(
            user_id=row["user_id"],
            repo_slug=row["repo_slug"],
            provider=row["provider"],
            full_name=row["full_name"],
            url=row["url"],
            workspace_id=row["workspace_id"] if "workspace_id" in keys else "default",
            branch=(row["branch"] or None) if "branch" in keys else None,
            enabled=bool(row["enabled"]),
            mode=row["mode"],
            last_poll_etag=row["last_poll_etag"],
            last_seen_pr_id=row["last_seen_pr_id"],
            last_polled_at=row["last_polled_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


_default_store: AutoReviewStore | None = None


def get_auto_review_store() -> AutoReviewStore:
    global _default_store
    if _default_store is None:
        from src.config import get_settings
        s = get_settings()
        db_path = s.workspace_dir / "secrets" / "auto_review.db"
        _default_store = AutoReviewStore(db_path)
    return _default_store


def workspace_for_repo_slug(repo_slug: str) -> str | None:
    """The tenant a repo slug belongs to, or None if nobody can say.

    The one place background jobs — which are handed a slug and nothing else —
    turn that slug into an owner for the data they write. Unreadable store,
    unregistered slug and ambiguous slug all come back as None, and every
    caller treats None the same way: write the point with no tenant, so it is
    admin-only until somebody registers the repo and re-runs the backfill.
    """
    try:
        ws = get_auto_review_store().workspace_for_slug(repo_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_for_slug_failed repo=%s err=%s", repo_slug, exc)
        return None
    if ws is None:
        logger.warning(
            "workspace_for_slug_unknown repo=%s — data written for this repo "
            "stays unattributed (global-admin-only) until it is registered",
            repo_slug,
        )
    return ws
