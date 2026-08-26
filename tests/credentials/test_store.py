"""Tests для multi-provider credential store + v1→v2 migration."""

from __future__ import annotations

import sqlite3

import pytest
from cryptography.fernet import Fernet

from src.credentials.store import CredentialStore, CredentialStoreError
from src.sync.bitbucket_api import BitbucketCredentials


@pytest.fixture
def master_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture
def store(tmp_path, master_key):
    db_path = tmp_path / "credentials.db"
    return CredentialStore(db_path, master_key)


# ─── Generic API ────────────────────────────────────────────────────


class TestGenericAPI:
    def test_save_and_load(self, store: CredentialStore) -> None:
        store.save("github", "ghp_abc123", metadata={"username": "konst"})
        result = store.load("github")
        assert result is not None
        assert result.provider == "github"
        assert result.account_label == "default"
        assert result.user_id == "default"
        assert result.secret == "ghp_abc123"
        assert result.metadata == {"username": "konst"}

    def test_load_missing_returns_none(self, store: CredentialStore) -> None:
        assert store.load("github") is None

    def test_overwrite_updates_secret(self, store: CredentialStore) -> None:
        store.save("github", "old_token", metadata={"username": "konst"})
        store.save("github", "new_token", metadata={"username": "konst"})
        result = store.load("github")
        assert result is not None
        assert result.secret == "new_token"

    def test_delete_removes_row(self, store: CredentialStore) -> None:
        store.save("github", "ghp_abc")
        assert store.delete("github") is True
        assert store.load("github") is None

    def test_delete_nonexistent_returns_false(self, store: CredentialStore) -> None:
        assert store.delete("github") is False

    def test_is_authenticated(self, store: CredentialStore) -> None:
        assert store.is_authenticated("github") is False
        store.save("github", "token")
        assert store.is_authenticated("github") is True

    def test_multiple_accounts_per_provider(self, store: CredentialStore) -> None:
        """User може мати github_personal + github_work."""
        store.save("github", "personal_token", account_label="personal")
        store.save("github", "work_token", account_label="work")

        personal = store.load("github", account_label="personal")
        work = store.load("github", account_label="work")
        assert personal is not None and personal.secret == "personal_token"
        assert work is not None and work.secret == "work_token"

    def test_multiple_providers_coexist(self, store: CredentialStore) -> None:
        store.save("bitbucket", "bb_token")
        store.save("github", "gh_token")
        store.save("gitlab", "gl_token")

        assert store.load("bitbucket").secret == "bb_token"
        assert store.load("github").secret == "gh_token"
        assert store.load("gitlab").secret == "gl_token"

    def test_list_all_credentials(self, store: CredentialStore) -> None:
        store.save("bitbucket", "t1", metadata={"username": "a"})
        store.save("github", "t2", metadata={"username": "b"})
        store.save("github", "t3", account_label="work", metadata={"username": "c"})

        items = store.list()
        assert len(items) == 3
        # Sorted by provider, label
        providers = [i["provider"] for i in items]
        assert providers == ["bitbucket", "github", "github"]
        # Жоден елемент не повинен містити secret
        for item in items:
            assert "secret" not in item
            assert "encrypted_secret" not in item

    def test_list_filter_by_provider(self, store: CredentialStore) -> None:
        store.save("bitbucket", "t1")
        store.save("github", "t2")
        store.save("github", "t3", account_label="work")

        github_only = store.list(provider="github")
        assert len(github_only) == 2
        assert all(i["provider"] == "github" for i in github_only)

    def test_user_id_isolation(self, store: CredentialStore) -> None:
        """user_a і user_b мають окремі credentials."""
        store.save("github", "tokenA", user_id="user_a")
        store.save("github", "tokenB", user_id="user_b")

        a = store.load("github", user_id="user_a")
        b = store.load("github", user_id="user_b")
        assert a is not None and a.secret == "tokenA"
        assert b is not None and b.secret == "tokenB"

    def test_last_used_at_updated_on_load(self, store: CredentialStore) -> None:
        store.save("github", "token")
        first = store.load("github")
        assert first is not None
        # Перший load має update last_used_at — другий load бачить це
        second = store.load("github", update_last_used=False)
        assert second is not None
        assert second.last_used_at is not None


# ─── Backward-compat Bitbucket API ──────────────────────────────────


class TestBitbucketBackwardCompat:
    def test_save_load_bitbucket(self, store: CredentialStore) -> None:
        creds = BitbucketCredentials(
            atlassian_email="user@example.com",
            api_token="ATATT3xFfGF0_test_token",
            bitbucket_username="userX",
        )
        store.save_bitbucket(creds)
        loaded = store.load_bitbucket()
        assert loaded is not None
        assert loaded.atlassian_email == "user@example.com"
        assert loaded.api_token == "ATATT3xFfGF0_test_token"
        assert loaded.bitbucket_username == "userX"

    def test_is_bitbucket_authenticated(self, store: CredentialStore) -> None:
        assert store.is_bitbucket_authenticated() is False
        creds = BitbucketCredentials(
            atlassian_email="u@e.com", api_token="t", bitbucket_username="u",
        )
        store.save_bitbucket(creds)
        assert store.is_bitbucket_authenticated() is True

    def test_delete_bitbucket(self, store: CredentialStore) -> None:
        creds = BitbucketCredentials(
            atlassian_email="u@e.com", api_token="t", bitbucket_username="u",
        )
        store.save_bitbucket(creds)
        store.delete_bitbucket()
        assert store.load_bitbucket() is None


# ─── Encryption ─────────────────────────────────────────────────────


class TestEncryption:
    def test_secrets_encrypted_at_rest(self, store: CredentialStore, tmp_path) -> None:
        """Прямий read SQLite файлу — жодного plaintext token."""
        store.save("github", "super_secret_token_xyz123")
        # Прямий read SQLite — без Fernet
        conn = sqlite3.connect(store.db_path)
        rows = conn.execute("SELECT encrypted_secret FROM credentials_v2").fetchall()
        conn.close()
        assert len(rows) == 1
        encrypted_blob = rows[0][0]
        assert b"super_secret_token_xyz123" not in encrypted_blob
        assert b"super_secret" not in encrypted_blob

    def test_wrong_key_raises(self, tmp_path) -> None:
        """Decrypt з іншим master key → CredentialStoreError."""
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()
        db_path = tmp_path / "creds.db"

        store1 = CredentialStore(db_path, key1)
        store1.save("github", "token")

        store2 = CredentialStore(db_path, key2)
        with pytest.raises(CredentialStoreError, match="master key"):
            store2.load("github")


# ─── v1 → v2 migration ─────────────────────────────────────────────


class TestMigrationV1toV2:
    def test_migrate_existing_v1_data(self, tmp_path, master_key) -> None:
        """v1 schema з даними → v2 (default user, default label)."""
        db_path = tmp_path / "credentials.db"

        # Manually create v1 schema + data
        f = Fernet(master_key)
        encrypted = f.encrypt(b"old_token_value")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE credentials (
                provider TEXT PRIMARY KEY,
                metadata TEXT NOT NULL,
                encrypted_secret BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )
        """)
        conn.execute(
            """INSERT INTO credentials VALUES (?, ?, ?, ?, ?, ?)""",
            ("bitbucket", '{"atlassian_email": "old@example.com"}',
             encrypted, "2026-01-01", "2026-01-01", None),
        )
        conn.commit()
        conn.close()

        # CredentialStore init should auto-migrate
        store = CredentialStore(db_path, master_key)

        # Стара таблиця має бути видалена
        conn = sqlite3.connect(db_path)
        old_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='credentials'"
        ).fetchone()
        conn.close()
        assert old_table is None

        # Дані доступні через нову API
        creds = store.load("bitbucket")
        assert creds is not None
        assert creds.user_id == "default"
        assert creds.account_label == "default"
        assert creds.secret == "old_token_value"
        assert creds.metadata.get("atlassian_email") == "old@example.com"

    def test_migrate_empty_v1_schema(self, tmp_path, master_key) -> None:
        """v1 schema без даних — просто видалити стару таблицю."""
        db_path = tmp_path / "credentials.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE credentials (
                provider TEXT PRIMARY KEY,
                metadata TEXT NOT NULL,
                encrypted_secret BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )
        """)
        conn.commit()
        conn.close()

        store = CredentialStore(db_path, master_key)
        # Жодного error, store працює
        store.save("github", "test_token")
        assert store.load("github").secret == "test_token"

    def test_no_migration_for_fresh_db(self, store: CredentialStore) -> None:
        """Чиста DB — ніякої міграції не відбувається."""
        # Просто ensure store працює напряму
        store.save("github", "token")
        assert store.load("github").secret == "token"
