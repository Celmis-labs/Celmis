"""A users.db from before OIDC opens, gains the columns, and keeps its rows.

`CREATE TABLE IF NOT EXISTS` never touches an existing table, so without the
ALTER step every install that predates SSO would 500 on the first SELECT that
names `oidc_sub`.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.users import User, UserExistsError, UserStore
from src.users.models import UserAuthMethod

_OLD_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, auth_method TEXT NOT NULL,
    password_hash BLOB, google_sub TEXT UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
    scopes TEXT NOT NULL, created_at TEXT NOT NULL, last_login_at TEXT,
    name TEXT NOT NULL DEFAULT ''
);
INSERT INTO users (id, email, auth_method, scopes, created_at)
VALUES ('old-1', 'old@example.com', 'password', '[]', '2026-01-01T00:00:00+00:00');
"""


def test_an_old_database_is_migrated_in_place(tmp_path):
    db = tmp_path / "users.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(_OLD_SCHEMA)

    store = UserStore(db)
    old = store.get_by_id("old-1")
    assert old is not None and old.oidc_sub is None

    old.oidc_iss, old.oidc_sub = "https://idp", "sub-1"
    store.update(old)
    assert store.get_by_oidc("https://idp", "sub-1").id == "old-1"

    # Opening again is a no-op, not a "duplicate column" crash.
    UserStore(db)


def test_one_subject_cannot_be_linked_twice(tmp_path):
    store = UserStore(tmp_path / "users.db")
    store.create(User(id="a", email="a@example.com", auth_method=UserAuthMethod.OIDC,
                      oidc_iss="https://idp", oidc_sub="s"))
    with pytest.raises(UserExistsError):
        store.create(User(id="b", email="b@example.com", auth_method=UserAuthMethod.OIDC,
                          oidc_iss="https://idp", oidc_sub="s"))
