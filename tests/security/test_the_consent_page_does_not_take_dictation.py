"""The API no longer renders a consent page, and the client endpoints answer to their owner.

The consent screen used to be an f-string of HTML on the API's origin that echoed
`state` and `code_challenge` from the query unescaped: a live element on the
operator's own origin. The page now lives in the web app (React escapes what it
renders) and the API builds no markup at all, which the first test pins so that
a page is not quietly added back.

The rest of this file is the pair of endpoints that made the client:
registration takes a workspace admin, while listing and deleting took a
platform admin, so whoever registered a client could not afterwards see it or
revoke it.
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.routers import oauth
from src.api.routers.oauth import delete_client, list_clients


def test_the_api_builds_no_html() -> None:
    import inspect

    source = inspect.getsource(oauth)
    assert "HTMLResponse" not in source
    assert "<form" not in source and "<html" not in source


# ─── who may see and revoke a client they registered ─────────────────


class _Row:
    def __init__(self, created_by: str) -> None:
        self.client_id = "ec_1"
        self.created_by = created_by
        self.name = "n"
        self.redirect_uris = []
        self.allowed_scopes = []


class _Session:
    def __init__(self, row) -> None:
        self._row = row
        self.deleted = []
        self.statements = []

    async def get(self, _model, _pk):
        return self._row

    async def scalars(self, statement):
        self.statements.append(statement)
        return type("R", (), {"all": lambda self: []})()

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        return None


class _User:
    def __init__(self, email: str, is_admin: bool = False) -> None:
        self.email = email
        self.id = email
        self.is_admin = is_admin


def test_the_creator_may_delete_their_own_client() -> None:
    session = _Session(_Row("dev@example.com"))
    asyncio.run(delete_client("ec_1", session, _User("dev@example.com")))
    assert session.deleted, "the person who registered it could not revoke it"


def test_a_stranger_may_not_delete_somebody_elses_client() -> None:
    from fastapi import HTTPException

    session = _Session(_Row("dev@example.com"))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(delete_client("ec_1", session, _User("other@example.com")))
    assert caught.value.status_code == 403
    assert not session.deleted


def test_a_platform_admin_may_delete_anything() -> None:
    session = _Session(_Row("dev@example.com"))
    asyncio.run(delete_client("ec_1", session, _User("root@example.com", True)))
    assert session.deleted


def test_a_missing_client_is_not_an_error() -> None:
    session = _Session(None)
    asyncio.run(delete_client("gone", session, _User("anyone@example.com")))
    assert not session.deleted


def test_listing_shows_a_non_admin_only_their_own() -> None:
    session = _Session(None)
    asyncio.run(list_clients(session, _User("dev@example.com")))
    where = session.statements[0].whereclause
    assert where is not None, "a non-admin was handed every client on the platform"
    assert "created_by" in str(where)
    assert where.right.value == "dev@example.com", (
        "the filter is there but does not name this user"
    )


def test_listing_shows_a_platform_admin_everything() -> None:
    session = _Session(None)
    asyncio.run(list_clients(session, _User("root@example.com", True)))
    assert session.statements[0].whereclause is None
