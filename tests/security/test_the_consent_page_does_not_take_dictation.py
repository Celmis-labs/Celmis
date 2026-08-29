"""The OAuth consent screen echoed the query string into HTML unescaped.

`_render_consent` is an f-string building a page, and four of its values come
straight from the query. `client_id`, `redirect_uri` and `scope` are checked
against the registered client first — but `state` and `code_challenge` are
not checked against anything, because they cannot be: they are the caller's
own opaque data. Both land inside value="...".

Measured against a running box with `state='"><b id=probe>PWNED</b>'`:

    <input type="hidden" name="state" value=""><b id=probe>PWNED</b>">

a live element on the API's origin, which is also the web app's origin, so a
script there runs as the signed-in operator. Reaching it needs a client_id and
one of its redirect_uris; a client_id is not a secret — it is in the MCP
configuration people paste around.

The second half of this file is the pair of endpoints that made the client:
registration takes a workspace admin, while listing and deleting took a
platform admin, so whoever registered a client could not afterwards see it or
revoke it.
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.routers.oauth import _render_consent, delete_client, list_clients

BREAKOUT = '"><b id=celmis-probe>PWNED</b>'

FIELDS = [
    "client_name", "client_id", "redirect_uri",
    "code_challenge", "code_challenge_method", "scope", "state",
]


def _render(**overrides) -> str:
    base = {
        "client_name": "ACME CI",
        "client_id": "ec_0123456789abcdef",
        "redirect_uri": "http://127.0.0.1:9000/cb",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
        "scope": "read:reviews",
        "state": "opaque-state",
    }
    base.update(overrides)
    return _render_consent(**base)


@pytest.mark.parametrize("field", FIELDS)
def test_no_field_can_open_a_tag(field: str) -> None:
    page = _render(**{field: BREAKOUT})
    assert "<b id=celmis-probe>" not in page, (
        f"{field} reached the page as markup — a script in its place runs on "
        f"the operator's origin"
    )
    assert "&lt;b id=celmis-probe&gt;" in page, (
        f"{field} did not arrive at all; the test is no longer looking at it"
    )


@pytest.mark.parametrize("field", FIELDS)
def test_no_field_can_close_an_attribute(field: str) -> None:
    """The quote is the boundary; escaping it is what keeps the value a value."""
    page = _render(**{field: 'a" onmouseover="steal()'})
    assert 'onmouseover="steal()' not in page, (
        f"{field} escaped its attribute and became one"
    )


def test_the_page_still_says_what_it_is_for() -> None:
    page = _render()
    assert "Grant access to Celmis?" in page
    assert "ACME CI" in page
    assert "read:reviews" in page
    assert 'name="state" value="opaque-state"' in page


def test_an_empty_scope_still_reads_as_a_sentence() -> None:
    assert "(no scopes requested)" in _render(scope="")


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
