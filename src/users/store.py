"""SQLite-backed UserStore.

Abstract interface via UserStore — a Postgres backend can implement the same
contract (Stage 16.4). Default is SQLite, for simplicity.

Schema:
    users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        auth_method TEXT NOT NULL,
        password_hash BLOB,
        google_sub TEXT UNIQUE,
        is_admin INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        scopes TEXT NOT NULL,  -- JSON array
        created_at TEXT NOT NULL,
        last_login_at TEXT,
        name TEXT NOT NULL DEFAULT '',
        oidc_iss TEXT,          -- added later: ALTER on open, see _migrate
        oidc_sub TEXT           -- UNIQUE together with oidc_iss
    )

NOTE: the schema is the same for SQLite and Postgres (minimal type
differences). Migration to Postgres — Stage 16.4 + drop-in replacement via an
alternate UserStore impl.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from src.users.models import User, UserAuthMethod

logger = logging.getLogger(__name__)


DEFAULT_USER_ID = "default"


class UserNotFoundError(KeyError):
    """No user exists with the given id/email/google_sub."""


class UserExistsError(ValueError):
    """Email or google_sub already taken."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    auth_method   TEXT NOT NULL,
    password_hash BLOB,
    google_sub    TEXT UNIQUE,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1,
    scopes        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    name          TEXT NOT NULL DEFAULT '',
    oidc_iss      TEXT,
    oidc_sub      TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub);
"""


class UserStore:
    """SQLite-backed user store. Single-process safe."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a users.db created by an older build up to the current schema.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a column
        added to `_SCHEMA` never reaches a database that predates it. Each step
        here is idempotent: it asks what the table has before changing it.
        """
        have = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for column in ("oidc_iss", "oidc_sub"):
            if column not in have:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError as exc:
                    # Check-then-act: another thread or process opening the
                    # same users.db may have added it since PRAGMA ran.
                    if "duplicate column" not in str(exc).lower():
                        raise
        # SQLite cannot add a UNIQUE column with ALTER; a partial unique index
        # gives the same guarantee and leaves every non-OIDC row out of it.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oidc "
            "ON users(oidc_iss, oidc_sub) WHERE oidc_sub IS NOT NULL"
        )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ─── CRUD ────────────────────────────────────────────────────

    def create(self, user: User) -> User:
        """Insert new user. Raises UserExistsError if email/google_sub taken."""
        if not user.email:
            raise ValueError("user.email required")

        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users
                    (id, email, auth_method, password_hash, google_sub,
                     is_admin, is_active, scopes, created_at, last_login_at, name,
                     oidc_iss, oidc_sub)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.email.lower().strip(),
                        user.auth_method.value,
                        user.password_hash,
                        user.google_sub,
                        int(user.is_admin),
                        int(user.is_active),
                        json.dumps(user.scopes),
                        user.created_at,
                        user.last_login_at,
                        user.name,
                        user.oidc_iss,
                        user.oidc_sub,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "users.email" in msg:
                    raise UserExistsError(f"email {user.email!r} already taken") from exc
                if "users.google_sub" in msg:
                    raise UserExistsError(
                        f"google_sub {user.google_sub!r} already linked"
                    ) from exc
                if "users.oidc" in msg:
                    raise UserExistsError(
                        f"oidc subject {user.oidc_sub!r} already linked"
                    ) from exc
                if "users.id" in msg or "primary key" in msg:
                    raise UserExistsError(f"id {user.id!r} already exists") from exc
                raise

        logger.info("user_created id=%s email=%s method=%s",
                    user.id, user.email, user.auth_method.value)
        return user

    def get_by_id(self, user_id: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?",
                (email.lower().strip(),),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_google_sub(self, google_sub: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE google_sub = ?", (google_sub,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_oidc(self, issuer: str, subject: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE oidc_iss = ? AND oidc_sub = ?",
                (issuer, subject),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list(self, *, active_only: bool = True) -> list[User]:
        sql = "SELECT * FROM users"
        params: tuple = ()
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_user(r) for r in rows if r]

    def update(self, user: User) -> User:
        """Update existing user. Raises UserNotFoundError if id missing."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE users SET
                    email = ?, auth_method = ?, password_hash = ?,
                    google_sub = ?, is_admin = ?, is_active = ?,
                    scopes = ?, last_login_at = ?, name = ?,
                    oidc_iss = ?, oidc_sub = ?
                WHERE id = ?
                """,
                (
                    user.email.lower().strip(),
                    user.auth_method.value,
                    user.password_hash,
                    user.google_sub,
                    int(user.is_admin),
                    int(user.is_active),
                    json.dumps(user.scopes),
                    user.last_login_at,
                    user.name,
                    user.oidc_iss,
                    user.oidc_sub,
                    user.id,
                ),
            )
            if cur.rowcount == 0:
                raise UserNotFoundError(f"user id={user.id!r} not found")
        return user

    def delete(self, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cur.rowcount > 0

    # ─── single-column writes, conditional on the row as it is NOW ─────
    #
    # `update` writes every column from the object it is handed, so a caller
    # holding a copy read a moment ago puts back whatever that copy said —
    # an account erased or deactivated in between included. The seat flows
    # (src/users/seats.py) change one thing and must not touch the rest, so
    # each of these is one UPDATE that names the state it expects and reports
    # whether it found it. SQLite applies it atomically, whatever process
    # the other writer is in.

    def set_active(self, user_id: str, active: bool, *, expect: bool | None = None) -> bool:
        """Switch an account on or off and nothing else. With ``expect``, only
        when the row is in that state now. True when a row was changed."""
        sql, params = "UPDATE users SET is_active = ? WHERE id = ?", [int(active), user_id]
        if expect is not None:
            sql += " AND is_active = ?"
            params.append(int(expect))
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount > 0

    def bind_oidc(self, user_id: str, issuer: str, subject: str) -> bool:
        """Bind an ACTIVE account that has no single sign-on identity yet.
        False when the row is gone, switched off or already bound. Raises
        UserExistsError when another account holds that identity."""
        try:
            with self._connect() as conn:
                return conn.execute(
                    "UPDATE users SET oidc_iss = ?, oidc_sub = ? WHERE id = ? "
                    "AND is_active = 1 AND (oidc_sub IS NULL OR oidc_sub = '')",
                    (issuer, subject, user_id),
                ).rowcount > 0
        except sqlite3.IntegrityError as exc:
            raise UserExistsError("oidc subject already linked") from exc

    def unbind_oidc(self, user_id: str, issuer: str, subject: str) -> bool:
        """Take back a binding made by :meth:`bind_oidc`: only if the row still
        holds exactly that identity."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE users SET oidc_iss = NULL, oidc_sub = NULL WHERE id = ? "
                "AND oidc_iss = ? AND oidc_sub = ?",
                (user_id, issuer, subject),
            ).rowcount > 0

    def count_people(self, *, exclude_ids: Collection[str] = (), exclude_email: str = "",
                     oidc_only: bool = False) -> int:
        """How many ACTIVE accounts there are, without loading them: not those
        in ``exclude_ids``, not the unbound (no Google, no OIDC) account that
        holds ``exclude_email`` (the master address), and with ``oidc_only``
        only those bound to single sign-on. The platform's own accounts are
        named by the caller (src/users/seats.py), not here."""
        where, params = ["is_active = 1"], []
        ids = sorted(set(exclude_ids))
        if ids:
            where.append(f"id NOT IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        email = exclude_email.strip().lower()
        if email:
            where.append("NOT (email = ? AND (google_sub IS NULL OR google_sub = '') "
                         "AND (oidc_sub IS NULL OR oidc_sub = ''))")
            params.append(email)
        if oidc_only:
            where.append("oidc_sub IS NOT NULL AND oidc_sub != ''")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS c FROM users WHERE " + " AND ".join(where), params,
            ).fetchone()
        return int(row["c"]) if row else 0

    def update_last_login(self, user_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now, user_id),
            )

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT count(*) AS c FROM users").fetchone()
        return int(row["c"]) if row else 0

    def ensure_default_user(self) -> User:
        """Idempotent: create the 'default' user if the table is empty.

        The default user is the single-user mode entrypoint. Email is a
        placeholder ('default@local'), no password (auth method NONE).

        Multi-user mode kicks in when an admin creates real users and
        disables the default one.
        """
        existing = self.get_by_id(DEFAULT_USER_ID)
        if existing is not None:
            return existing

        default = User(
            id=DEFAULT_USER_ID,
            email="default@local",
            auth_method=UserAuthMethod.PASSWORD,
            password_hash=None,  # no login password — protected by OS user
            is_admin=True,
            scopes=["read:graph", "read:groups", "write:groups", "admin"],
            name="Default Local User",
        )
        return self.create(default)

    # ─── helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        scopes = json.loads(row["scopes"]) if row["scopes"] else []
        return User(
            id=str(row["id"]),
            email=str(row["email"]),
            auth_method=UserAuthMethod(row["auth_method"]),
            password_hash=row["password_hash"],
            google_sub=row["google_sub"],
            is_admin=bool(row["is_admin"]),
            is_active=bool(row["is_active"]),
            scopes=list(scopes),
            created_at=str(row["created_at"]),
            last_login_at=row["last_login_at"],
            name=str(row["name"] or ""),
            oidc_iss=row["oidc_iss"],
            oidc_sub=row["oidc_sub"],
        )


# ─── Module-level singleton ────────────────────────────────────────


_default_store: UserStore | None = None


def get_user_store() -> UserStore:
    """Singleton."""
    global _default_store
    if _default_store is None:
        from src.config import get_settings
        settings = get_settings()
        db_path = settings.workspace_dir / "secrets" / "users.db"
        _default_store = UserStore(db_path)
        # Auto-create default user on first init
        _default_store.ensure_default_user()
    return _default_store
