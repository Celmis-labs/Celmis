"""Tests для SqlAlchemyCredentialStore — exercise через SQLite URL.

Postgres-specific behavior tested manually за production deploy. Тут
focused на API parity з sqlite3-based CredentialStore.
"""

from __future__ import annotations

import sqlite3

import pytest
from cryptography.fernet import Fernet

from src.credentials.sqlalchemy_store import SqlAlchemyCredentialStore
from src.credentials.store import CredentialStoreError
from src.sync.bitbucket_api import BitbucketCredentials


@pytest.fixture
def master_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture
def store(tmp_path, master_key):
    db_path = tmp_path / "creds.db"
    url = f"sqlite:///{db_path}"
    return SqlAlchemyCredentialStore(url, master_key)


# ─── Generic API parity ────────────────────────────────────────


class TestGenericAPI:
    def test_save_and_load(self, store: SqlAlchemyCredentialStore) -> None:
        store.save("github", "ghp_abc123", metadata={"username": "konst"})
        result = store.load("github")
        assert result is not None
        assert result.provider == "github"
        assert result.account_label == "default"
        assert result.user_id == "default"
        assert result.secret == "ghp_abc123"
        assert result.metadata == {"username": "konst"}

    def test_load_missing_returns_none(self, store: SqlAlchemyCredentialStore) -> None:
        assert store.load("github") is None

    def test_overwrite_updates_secret(self, store: SqlAlchemyCredentialStore) -> None:
        store.save("github", "old_token", metadata={"x": 1})
        store.save("github", "new_token", metadata={"x": 2})
        result = store.load("github")
        assert result.secret == "new_token"
        assert result.metadata == {"x": 2}

    def test_delete_removes_row(self, store: SqlAlchemyCredentialStore) -> None:
        store.save("github", "ghp_abc")
        assert store.delete("github") is True
        assert store.load("github") is None

    def test_delete_nonexistent_returns_false(
        self, store: SqlAlchemyCredentialStore,
    ) -> None:
        assert store.delete("github") is False

    def test_is_authenticated(self, store: SqlAlchemyCredentialStore) -> None:
        assert store.is_authenticated("github") is False
        store.save("github", "token")
        assert store.is_authenticated("github") is True

    def test_multiple_accounts(self, store: SqlAlchemyCredentialStore) -> None:
        store.save("github", "personal", account_label="personal")
        store.save("github", "work", account_label="work")
        assert store.load("github", account_label="personal").secret == "personal"
        assert store.load("github", account_label="work").secret == "work"

    def test_user_isolation(self, store: SqlAlchemyCredentialStore) -> None:
        store.save("github", "tokenA", user_id="user_a")
        store.save("github", "tokenB", user_id="user_b")
        assert store.load("github", user_id="user_a").secret == "tokenA"
        assert store.load("github", user_id="user_b").secret == "tokenB"

    def test_list_no_secrets_leaked(
        self, store: SqlAlchemyCredentialStore,
    ) -> None:
        store.save("github", "secret-token", metadata={"u": "x"})
        items = store.list()
        assert len(items) == 1
        assert "secret" not in items[0]
        assert "encrypted_secret" not in items[0]

    def test_list_filter_by_provider(
        self, store: SqlAlchemyCredentialStore,
    ) -> None:
        store.save("github", "t1")
        store.save("gitlab", "t2")
        store.save("bitbucket", "t3")
        gh = store.list(provider="github")
        assert len(gh) == 1
        assert gh[0]["provider"] == "github"


# ─── Bitbucket compat ─────────────────────────────────────────


class TestBitbucketCompat:
    def test_save_load_bitbucket(self, store: SqlAlchemyCredentialStore) -> None:
        creds = BitbucketCredentials(
            atlassian_email="user@example.com",
            api_token="ATATT3xFfGF0_test",
            bitbucket_username="userX",
        )
        store.save_bitbucket(creds)

        loaded = store.load_bitbucket()
        assert loaded is not None
        assert loaded.atlassian_email == "user@example.com"
        assert loaded.api_token == "ATATT3xFfGF0_test"
        assert loaded.bitbucket_username == "userX"

    def test_delete_bitbucket(self, store: SqlAlchemyCredentialStore) -> None:
        creds = BitbucketCredentials(
            atlassian_email="u@e.com", api_token="t", bitbucket_username="u",
        )
        store.save_bitbucket(creds)
        store.delete_bitbucket()
        assert store.load_bitbucket() is None


# ─── Encryption ──────────────────────────────────────────────


class TestEncryption:
    def test_secrets_encrypted_at_rest(
        self, store: SqlAlchemyCredentialStore, tmp_path, master_key,
    ) -> None:
        store.save("github", "super_secret_xyz123")

        # Direct sqlite read — not via store
        db_path = tmp_path / "creds.db"
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT encrypted_secret FROM credentials_v2"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert b"super_secret_xyz123" not in rows[0][0]

    def test_wrong_key_raises(self, tmp_path) -> None:
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()
        url = f"sqlite:///{tmp_path / 'creds.db'}"

        s1 = SqlAlchemyCredentialStore(url, key1)
        s1.save("github", "token")

        s2 = SqlAlchemyCredentialStore(url, key2)
        with pytest.raises(CredentialStoreError, match="master key"):
            s2.load("github")


# ─── URL sanitization ────────────────────────────────────────


class TestUrlSanitize:
    def test_password_redacted_у_logs(self) -> None:
        url = "postgresql+psycopg://user:s3cr3t@db.example.com:5432/db"
        result = SqlAlchemyCredentialStore._sanitize_url(url)
        assert "s3cr3t" not in result
        assert "[REDACTED]" in result

    def test_no_password_leaves_unchanged(self) -> None:
        url = "sqlite:///path/to/db"
        assert SqlAlchemyCredentialStore._sanitize_url(url) == url


# ─── Factory selection ───────────────────────────────────────


class TestFactorySelection:
    def test_default_uses_sqlite_backend(
        self, tmp_path, monkeypatch, master_key,
    ) -> None:
        """Без env var → legacy sqlite3 CredentialStore."""
        monkeypatch.delenv("CREDENTIAL_STORE_URL", raising=False)
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("CREDENTIAL_MASTER_KEY", master_key.decode())

        import src.credentials.store as cs
        from src.config import get_settings
        get_settings.cache_clear()
        cs._default_store = None

        store = cs.get_credential_store()

        from src.credentials.store import CredentialStore
        assert isinstance(store, CredentialStore)

    def test_url_env_uses_sqlalchemy_backend(
        self, tmp_path, monkeypatch, master_key,
    ) -> None:
        """CREDENTIAL_STORE_URL → SqlAlchemyCredentialStore."""
        url = f"sqlite:///{tmp_path / 'sqlal.db'}"
        monkeypatch.setenv("CREDENTIAL_STORE_URL", url)
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("CREDENTIAL_MASTER_KEY", master_key.decode())

        import src.credentials.store as cs
        from src.config import get_settings
        get_settings.cache_clear()
        cs._default_store = None

        store = cs.get_credential_store()

        from src.credentials.sqlalchemy_store import SqlAlchemyCredentialStore
        assert isinstance(store, SqlAlchemyCredentialStore)
