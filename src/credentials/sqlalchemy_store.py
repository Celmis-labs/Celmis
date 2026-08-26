"""SQLAlchemy-based CredentialStore — Postgres / MySQL / SQLite via URL.

Stage 16.4 (May 2026, SQLAlchemy 2.0+).

Backward-compatible with the sqlite3-based store: same API + same encrypted
format (Fernet AES-128-CBC + HMAC). Selectable via an env var:

    CREDENTIAL_STORE_URL=postgresql+psycopg://user:pass@host:5432/db
    CREDENTIAL_STORE_URL=postgresql+psycopg2://...
    CREDENTIAL_STORE_URL=sqlite:///path/to/credentials.db

If the env var is NOT set, the factory returns the existing CredentialStore
(raw sqlite3).

Schema (SQLAlchemy ORM):
    credentials_v2 (user_id, provider, account_label) primary key
    Same fields as the SQLite version — drop-in replacement.

Production deployment workflow:
    1. pip install psycopg[binary]
    2. createdb code_analyzer
    3. export CREDENTIAL_STORE_URL=postgresql+psycopg://localhost/code_analyzer
    4. analyzer init  # auto-creates tables
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    LargeBinary,
    String,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from src.credentials.store import (
    _DEFAULT_LABEL,
    _DEFAULT_USER,
    CredentialStoreError,
    StoredCredentials,
)
from src.sync.bitbucket_api import BitbucketCredentials

logger = logging.getLogger(__name__)


class _Base(DeclarativeBase):
    pass


class _CredentialRow(_Base):
    """ORM-mapped row in credentials_v2."""

    __tablename__ = "credentials_v2"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_label: Mapped[str] = mapped_column(String(64), primary_key=True)
    metadata_json: Mapped[str] = mapped_column("metadata", Text, nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_used_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SqlAlchemyCredentialStore:
    """SQLAlchemy backend — supports Postgres / MySQL / SQLite via URL.

    Same API contract as CredentialStore (the SQLite version) — a drop-in
    replacement via the factory.
    """

    def __init__(self, db_url: str, master_key: bytes) -> None:
        self.db_url = db_url
        # Connect-args for SQLite — required for multi-thread access in tests
        connect_args = {}
        if db_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self._engine = create_engine(db_url, connect_args=connect_args, future=True)
        self._SessionLocal = sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(master_key)
        self._init_schema()
        logger.info(
            "credential_store_sqlalchemy_init url=%s",
            self._sanitize_url(db_url),
        )

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Strip the password from the URL for logging."""
        import re

        return re.sub(r":([^:@/]+)@", r":[REDACTED]@", url)

    def _init_schema(self) -> None:
        _Base.metadata.create_all(self._engine)

    @contextmanager
    def _session(self):
        session: Session = self._SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
        encrypted = self._fernet.encrypt(secret.encode("utf-8"))
        now = datetime.now(UTC).isoformat()
        meta_json = json.dumps(metadata or {})

        with self._session() as session:
            existing = session.get(
                _CredentialRow, (user_id, provider, account_label),
            )
            if existing is None:
                row = _CredentialRow(
                    user_id=user_id,
                    provider=provider,
                    account_label=account_label,
                    metadata_json=meta_json,
                    encrypted_secret=encrypted,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.metadata_json = meta_json
                existing.encrypted_secret = encrypted
                existing.updated_at = now

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
        with self._session() as session:
            row = session.get(
                _CredentialRow, (user_id, provider, account_label),
            )
            if row is None:
                return None

            try:
                secret = self._fernet.decrypt(row.encrypted_secret).decode("utf-8")
            except InvalidToken as exc:
                raise CredentialStoreError(
                    "Cannot decrypt credentials — master key changed or DB corrupted"
                ) from exc

            metadata = json.loads(row.metadata_json)
            created = row.created_at
            updated = row.updated_at
            last_used = row.last_used_at

            if update_last_used:
                row.last_used_at = datetime.now(UTC).isoformat()

        return StoredCredentials(
            user_id=user_id,
            provider=provider,
            account_label=account_label,
            metadata=metadata,
            secret=secret,
            created_at=created,
            updated_at=updated,
            last_used_at=last_used,
        )

    def delete(
        self,
        provider: str,
        *,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
    ) -> bool:
        with self._session() as session:
            stmt = delete(_CredentialRow).where(
                _CredentialRow.user_id == user_id,
                _CredentialRow.provider == provider,
                _CredentialRow.account_label == account_label,
            )
            result = session.execute(stmt)
            deleted = (result.rowcount or 0) > 0

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
        with self._session() as session:
            stmt = select(_CredentialRow).where(_CredentialRow.user_id == user_id)
            if provider is not None:
                stmt = stmt.where(_CredentialRow.provider == provider)
            stmt = stmt.order_by(
                _CredentialRow.provider, _CredentialRow.account_label,
            )
            rows = session.execute(stmt).scalars().all()

        return [
            {
                "provider": r.provider,
                "account_label": r.account_label,
                "metadata": json.loads(r.metadata_json),
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "last_used_at": r.last_used_at,
            }
            for r in rows
        ]

    def is_authenticated(
        self,
        provider: str,
        *,
        user_id: str = _DEFAULT_USER,
        account_label: str = _DEFAULT_LABEL,
    ) -> bool:
        with self._session() as session:
            row = session.get(
                _CredentialRow, (user_id, provider, account_label),
            )
        return row is not None

    # ─── Bitbucket-specific (backward compat) ────────────────────────

    def save_bitbucket(self, creds: BitbucketCredentials) -> None:
        metadata = {
            "atlassian_email": creds.atlassian_email,
            "bitbucket_username": creds.bitbucket_username,
            "expires_at": creds.expires_at,
        }
        self.save("bitbucket", creds.api_token, metadata=metadata)

    def load_bitbucket(self) -> BitbucketCredentials | None:
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
