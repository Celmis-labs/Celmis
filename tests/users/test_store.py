"""Tests для UserStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.users.models import User, UserAuthMethod
from src.users.password import hash_password
from src.users.store import (
    DEFAULT_USER_ID,
    UserExistsError,
    UserNotFoundError,
    UserStore,
)


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    return UserStore(tmp_path / "users.db")


def _make_user(
    id: str = "u-1",
    email: str = "alice@example.com",
    password: str = "P@ssw0rd!",
) -> User:
    return User(
        id=id,
        email=email,
        auth_method=UserAuthMethod.PASSWORD,
        password_hash=hash_password(password),
        name="Alice",
    )


# ─── CRUD ───────────────────────────────────────────────────────────


class TestCRUD:
    def test_create_and_get_by_id(self, store: UserStore) -> None:
        u = store.create(_make_user())
        assert u.id == "u-1"
        loaded = store.get_by_id("u-1")
        assert loaded is not None
        assert loaded.email == "alice@example.com"
        assert loaded.password_hash is not None

    def test_get_by_email_normalizes_case(self, store: UserStore) -> None:
        store.create(_make_user(email="Alice@Example.COM"))
        loaded = store.get_by_email("alice@example.com")
        assert loaded is not None
        assert loaded.email == "alice@example.com"  # stored lowercase

    def test_get_by_id_missing_returns_none(self, store: UserStore) -> None:
        assert store.get_by_id("ghost") is None

    def test_create_duplicate_email_raises(self, store: UserStore) -> None:
        store.create(_make_user(id="u-1", email="x@y.com"))
        with pytest.raises(UserExistsError, match="email"):
            store.create(_make_user(id="u-2", email="x@y.com"))

    def test_create_duplicate_id_raises(self, store: UserStore) -> None:
        store.create(_make_user(id="u-1", email="a@y.com"))
        with pytest.raises(UserExistsError):
            store.create(_make_user(id="u-1", email="b@y.com"))

    def test_create_duplicate_google_sub_raises(self, store: UserStore) -> None:
        u1 = User(
            id="u-1", email="a@y.com",
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="google-sub-12345",
        )
        u2 = User(
            id="u-2", email="b@y.com",
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="google-sub-12345",
        )
        store.create(u1)
        with pytest.raises(UserExistsError, match="google_sub"):
            store.create(u2)

    def test_get_by_google_sub(self, store: UserStore) -> None:
        u = User(
            id="u-1", email="a@y.com",
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="g-12345",
        )
        store.create(u)
        loaded = store.get_by_google_sub("g-12345")
        assert loaded is not None
        assert loaded.id == "u-1"

    def test_update(self, store: UserStore) -> None:
        u = store.create(_make_user())
        u.name = "Alice Updated"
        u.is_admin = True
        u.scopes = ["admin", "read:graph"]
        store.update(u)
        loaded = store.get_by_id("u-1")
        assert loaded.name == "Alice Updated"
        assert loaded.is_admin is True
        assert loaded.scopes == ["admin", "read:graph"]

    def test_update_missing_raises(self, store: UserStore) -> None:
        u = _make_user(id="ghost")
        with pytest.raises(UserNotFoundError):
            store.update(u)

    def test_delete(self, store: UserStore) -> None:
        store.create(_make_user())
        assert store.delete("u-1") is True
        assert store.delete("u-1") is False  # already gone
        assert store.get_by_id("u-1") is None

    def test_list_active_only(self, store: UserStore) -> None:
        u1 = _make_user(id="u-1", email="a@y.com")
        u2 = _make_user(id="u-2", email="b@y.com")
        u2.is_active = False
        store.create(u1)
        store.create(u2)

        active = store.list()
        assert len(active) == 1
        assert active[0].id == "u-1"

        all_ = store.list(active_only=False)
        assert len(all_) == 2

    def test_count(self, store: UserStore) -> None:
        assert store.count() == 0
        store.create(_make_user(id="u-1", email="a@y.com"))
        assert store.count() == 1


# ─── Default user ──────────────────────────────────────────────────


class TestDefaultUser:
    def test_ensure_default_creates_if_empty(self, store: UserStore) -> None:
        u = store.ensure_default_user()
        assert u.id == DEFAULT_USER_ID
        assert u.is_admin is True
        assert u.email == "default@local"
        assert "admin" in u.scopes

    def test_ensure_default_idempotent(self, store: UserStore) -> None:
        u1 = store.ensure_default_user()
        u2 = store.ensure_default_user()
        assert u1.id == u2.id
        assert store.count() == 1


# ─── Last login update ────────────────────────────────────────────


class TestLastLogin:
    def test_update_last_login(self, store: UserStore) -> None:
        u = store.create(_make_user())
        assert u.last_login_at is None
        store.update_last_login("u-1")
        loaded = store.get_by_id("u-1")
        assert loaded.last_login_at is not None
        # ISO format
        assert "T" in loaded.last_login_at


# ─── Auth methods ─────────────────────────────────────────────────


class TestAuthMethods:
    def test_google_only_user_no_password(self, store: UserStore) -> None:
        u = User(
            id="u-1", email="g@y.com",
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="google-sub",
            password_hash=None,
        )
        store.create(u)
        loaded = store.get_by_id("u-1")
        assert loaded.password_hash is None
        assert loaded.has_google is True
        assert loaded.has_password is False

    def test_dual_auth_user(self, store: UserStore) -> None:
        u = User(
            id="u-1", email="a@y.com",
            auth_method=UserAuthMethod.BOTH,
            google_sub="google-sub",
            password_hash=hash_password("pwd"),
        )
        store.create(u)
        loaded = store.get_by_id("u-1")
        assert loaded.has_password is True
        assert loaded.has_google is True
