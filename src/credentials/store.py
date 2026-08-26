"""SQLite-backed encrypted multi-provider credential storage.

Threat model:
    Hostile: stolen disk image, malware reading the SQLite file, leaked backup
    Trusted: a process with the master key (env var or a chmod 600 file)
    NOT covered: dumping the process via ptrace (out of scope)

Every secret field is encrypted separately with Fernet (AES-128-CBC +
HMAC-SHA256).

Schema v2 (May 2026): supports multi-user and multi-account-per-provider.
Primary key: (user_id, provider, account_label).
    - user_id: 'default' in the MVP (single-user). Once we add app auth
      (email/password + Google OAuth) — it will be filled with real user IDs
      from the users table.
    - provider: 'bitbucket' | 'github' | 'gitlab'
    - account_label: 'default' or user-defined (for example 'work' / 'personal')

Migration from v1: automatic. If the old `credentials` table is found —
copy it into `credentials_v2` with user_id='default', account_label='default'.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.sync.bitbucket_api import BitbucketCredentials

logger = logging.getLogger(__name__)


_DEFAULT_USER = "default"
_DEFAULT_LABEL = "default"


_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS credentials_v2 (
    user_id          TEXT NOT NULL DEFAULT 'default',
    provider         TEXT NOT NULL,
    account_label    TEXT NOT NULL DEFAULT 'default',
    metadata         TEXT NOT NULL,
    encrypted_secret BLOB NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_used_at     TEXT,
    PRIMARY KEY (user_id, provider, account_label)
);

CREATE INDEX IF NOT EXISTS idx_credentials_v2_provider
    ON credentials_v2(provider);
CREATE INDEX IF NOT EXISTS idx_credentials_v2_user
    ON credentials_v2(user_id);
"""


class CredentialStoreError(Exception):
    """Decryption error — usually the wrong master key."""


@dataclass(frozen=True)
class StoredCredentials:
    """Generic envelope for the credentials of any provider.

    secret — decrypted plaintext (an API token, for example). The caller is
    responsible for its safety in their own area (do NOT log it, do NOT keep
    it in memory longer than needed).
    """

    user_id: str
    provider: str
    account_label: str
    metadata: dict[str, object]
    secret: str
    created_at: str
    updated_at: str
    last_used_at: str | None


class CredentialStore:
    """Multi-provider, multi-account encrypted credential store."""

    def __init__(self, db_path: Path, master_key: bytes) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(master_key)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_V2)
            conn.execute("PRAGMA journal_mode = WAL")
            self._migrate_v1_to_v2(conn)

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        """Automatic migration from legacy schema v1 → v2 when needed.

        v1 schema: credentials (provider PRIMARY KEY, metadata, encrypted_secret, ...)
        v2 schema: credentials_v2 (user_id, provider, account_label, ...)
        """
        # Does the old table exist?
        old = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='credentials'"
        ).fetchone()
        if old is None:
            return

        # Is there any data to migrate?
        rows = conn.execute("SELECT * FROM credentials").fetchall()
        if not rows:
            # Just drop the empty legacy table
            conn.execute("DROP TABLE credentials")
            return

        migrated = 0
        for row in rows:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO credentials_v2
                        (user_id, provider, account_label, metadata, encrypted_secret,
                         created_at, updated_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _DEFAULT_USER,
                        row["provider"],
                        _DEFAULT_LABEL,
                        row["metadata"],
                        row["encrypted_secret"],
                        row["created_at"],
                        row["updated_at"],
                        row["last_used_at"],
                    ),
                )
                migrated += 1
            except sqlite3.Error as e:
                logger.error("migration_row_failed provider=%s err=%s", row["provider"], e)

        # Drop the old table after a successful migration
        conn.execute("DROP TABLE credentials")
        logger.info("credential_schema_migrated v1→v2 rows=%d", migrated)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ─── Generic API ─────────────────────────────────────────────────

    def save(
        self,
        provider: str,
        secret: str,
        *,
        metadata: dict[str, object] | None = None,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
    ) -> None:
        """Save credentials. Existing (user_id, provider, account_label) — overwrite."""
        encrypted = self._fernet.encrypt(secret.encode("utf-8"))
        now = datetime.now(UTC).isoformat()
        meta_json = json.dumps(metadata or {})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials_v2
                    (user_id, provider, account_label, metadata, encrypted_secret,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, provider, account_label) DO UPDATE SET
                    metadata = excluded.metadata,
                    encrypted_secret = excluded.encrypted_secret,
                    updated_at = excluded.updated_at
                """,
                (user_id, provider, account_label, meta_json, encrypted, now, now),
            )
        logger.info(
            "credentials_saved user=%s provider=%s label=%s",
            user_id, provider, account_label,
        )

    def load(
        self,
        provider: str,
        *,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
        update_last_used: bool = True,
    ) -> StoredCredentials | None:
        """Load + decrypt credentials. None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT metadata, encrypted_secret, created_at, updated_at, last_used_at
                FROM credentials_v2
                WHERE user_id=? AND provider=? AND account_label=?
                """,
                (user_id, provider, account_label),
            ).fetchone()
        if row is None:
            return None
        try:
            secret = self._fernet.decrypt(row["encrypted_secret"]).decode("utf-8")
        except InvalidToken as e:
            raise CredentialStoreError(
                "Cannot decrypt credentials — master key changed or DB corrupted"
            ) from e
        metadata = json.loads(row["metadata"])

        if update_last_used:
            now = datetime.now(UTC).isoformat()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE credentials_v2 SET last_used_at=?
                    WHERE user_id=? AND provider=? AND account_label=?
                    """,
                    (now, user_id, provider, account_label),
                )

        return StoredCredentials(
            user_id=user_id,
            provider=provider,
            account_label=account_label,
            metadata=metadata,
            secret=secret,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
        )

    def delete(
        self,
        provider: str,
        *,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
    ) -> bool:
        """Delete credentials. True if a row was deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM credentials_v2
                WHERE user_id=? AND provider=? AND account_label=?
                """,
                (user_id, provider, account_label),
            )
            deleted = cur.rowcount > 0
        if deleted:
            logger.info(
                "credentials_deleted user=%s provider=%s label=%s",
                user_id, provider, account_label,
            )
        return deleted

    def list(
        self,
        *,
        user_id: str = _DEFAULT_USER,
        provider: str | None = None,
    ) -> list[dict[str, object]]:
        """List of credentials without the secrets — for the UI."""
        sql = """
            SELECT provider, account_label, metadata, created_at, updated_at, last_used_at
            FROM credentials_v2 WHERE user_id=?
        """
        params: tuple = (user_id,)
        if provider is not None:
            sql += " AND provider=?"
            params = (user_id, provider)
        sql += " ORDER BY provider, account_label"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "provider": r["provider"],
                "account_label": r["account_label"],
                "metadata": json.loads(r["metadata"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    def slots_with(self, *, provider: str,
                   account_label: str | None = None) -> list[str]:
        """Every `user_id` slot holding a credential for `provider`.

        Presence only — no secret is decrypted and none is returned. Added for
        the readiness probe, which asked "is an LLM configured" by looking in
        ONE slot: `_load_workspace_config()` defaults to the "default"
        workspace, and on a multi-tenant deployment every real key lives under
        `ws:{id}`. The probe therefore answered false forever on a system that
        was demonstrably making model calls, and a readiness field that is
        always false is one an operator learns to ignore.
        """
        sql = "SELECT DISTINCT user_id FROM credentials_v2 WHERE provider = ?"
        params: list[object] = [provider]
        if account_label is not None:
            sql += " AND account_label = ?"
            params.append(account_label)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return sorted(str(r[0]) for r in rows)

    def is_authenticated(
        self,
        provider: str,
        *,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM credentials_v2
                WHERE user_id=? AND provider=? AND account_label=?
                """,
                (user_id, provider, account_label),
            ).fetchone()
        return row is not None

    # ─── Bitbucket-specific (backward compat) ────────────────────────

    def save_bitbucket(self, creds: BitbucketCredentials) -> None:
        """Backward-compat wrapper. Saves into (default, bitbucket, default)."""
        metadata = {
            "atlassian_email": creds.atlassian_email,
            "bitbucket_username": creds.bitbucket_username,
            "expires_at": creds.expires_at,
        }
        self.save("bitbucket", creds.api_token, metadata=metadata)

    def load_bitbucket(self) -> BitbucketCredentials | None:
        """Backward-compat wrapper. None if there are no Bitbucket creds."""
        stored = self.load("bitbucket")
        if stored is None:
            return None
        meta = stored.metadata
        return BitbucketCredentials(
            atlassian_email=str(meta.get("atlassian_email", "")),
            api_token=stored.secret,
            bitbucket_username=str(meta.get("bitbucket_username", "")),
            expires_at=meta.get("expires_at"),  # type: ignore[arg-type]
        )

    def delete_bitbucket(self) -> None:
        self.delete("bitbucket")

    def is_bitbucket_authenticated(self) -> bool:
        return self.is_authenticated("bitbucket")


# ─── Master key management ───────────────────────────────────────────


def get_or_create_master_key(key_file: Path) -> bytes:
    """Get the master key from:
        1) ENV `CREDENTIAL_MASTER_KEY` (Fernet base64-encoded),
        2) the `key_file` file (generated earlier, with chmod 600),
        3) otherwise generate a new one and save it into the file.

    Returns a Fernet-compatible 32-byte URL-safe base64 key.
    """
    env_key = os.environ.get("CREDENTIAL_MASTER_KEY", "").strip()
    if env_key:
        try:
            Fernet(env_key.encode())
            return env_key.encode()
        except Exception as e:
            raise CredentialStoreError(
                f"CREDENTIAL_MASTER_KEY is not a valid Fernet key: {e}"
            ) from e

    if key_file.exists():
        key = key_file.read_bytes().strip()
        try:
            Fernet(key)
            return key
        except Exception as e:
            raise CredentialStoreError(
                f"Existing master key file {key_file} is corrupt: {e}"
            ) from e

    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError as e:
        logger.warning("could not chmod key file %s: %s", key_file, e)
    logger.info("master_key_generated path=%s — back up this file securely", key_file)
    return key


_default_store: object | None = None  # CredentialStore or SqlAlchemyCredentialStore


def get_credential_store():
    """Singleton factory.

    Backend selection (priority):
        1. CREDENTIAL_STORE_URL env var → SQLAlchemy backend
           (postgresql/mysql/sqlite via a SQLAlchemy URL)
        2. Default: legacy sqlite3-based CredentialStore (existing).

    Returns an instance that exposes the same API:
        save / load / delete / list / is_authenticated +
        save_bitbucket / load_bitbucket / delete_bitbucket / is_bitbucket_authenticated
    """
    global _default_store
    if _default_store is not None:
        return _default_store

    import os

    from src.config import get_settings

    settings = get_settings()
    secrets_dir = settings.workspace_dir / "secrets"
    key_file = secrets_dir / ".master.key"
    master_key = get_or_create_master_key(key_file)

    db_url = os.environ.get("CREDENTIAL_STORE_URL", "").strip()
    if db_url:
        # SQLAlchemy backend (Postgres / MySQL / external SQLite)
        from src.credentials.sqlalchemy_store import SqlAlchemyCredentialStore
        _default_store = SqlAlchemyCredentialStore(db_url, master_key)
        logger.info("credential_store_backend=sqlalchemy")
    else:
        # Legacy sqlite3 backend (default — backward compat)
        db_path = secrets_dir / "credentials.db"
        _default_store = CredentialStore(db_path, master_key)
        logger.info("credential_store_backend=sqlite3")

    return _default_store
