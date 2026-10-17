"""Switching an account off ends its MCP tokens, of every kind, at once.

The administrator's deactivate button (`seats.deactivate_person`) changes one
column, `is_active`. For that to be a real switch every token that speaks for
the person has to look at the column on every call — not when it was issued and
not after the grant cache expires:

  * the signed bearer of every kind and profile (`cli`, `pat`, `self`; `full`
    and `dev`) is refused by the verifier at the edge, before any tool runs, and
    by the identity resolver the tools ask;
  * the OAuth refresh grant gives a switched-off (or deleted) person no new access
    token, and the whole chain of refresh tokens ends with the same words as a
    withdrawn grant, so the answer does not say which it was;
  * the refusal is immediate: the grant cache (30 seconds at most) is not
    emptied by the deactivation, and nothing waits for it;
  * switching the account on again restores the same token — nothing was
    revoked, so nothing needs issuing;
  * a project token (`cmcp_`) speaks for no account at all: it names a project
    and nobody, and only the master account issues one, which cannot be switched
    off — so there is nothing for the button to end, and it is stated here
    rather than assumed.

The REST session, the refresh, the password and Google are covered in
tests/api/test_the_administrator_can_switch_an_account_off_and_on.py, single
sign-on in tests/ee/test_the_sso_allowance.py and the `cpk_` pack token in
tests/api/test_a_pack_token_publishes_one_pack_and_nothing_else.py.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.security.mcp_world import GRANTED, WS_A
from tests.security.test_mcp_token_lifecycle import (  # noqa: F401 — the flow fixture
    _exchange,
    oauth,
)

TOKENS = [
    pytest.param("cli", "full", id="cli-full"),
    pytest.param("cli", "dev", id="cli-dev"),
    pytest.param("pat", "full", id="pat-full"),
    pytest.param("pat", "dev", id="pat-dev"),
    pytest.param("self", "full", id="self-full"),
]


@pytest.fixture(autouse=True)
def _room_for_everybody():
    """The cast is more people than the community limit. These tests are about
    tokens, not seats: a licence without a seat limit lets reactivation work."""
    from src.users import seats

    seats.set_licence_limits(None, True)


def _verify(token: str):
    from src.mcp_server.auth import JwtTokenVerifier

    return asyncio.run(JwtTokenVerifier().verify_token(token))


def _off(world, who: str) -> None:
    from src.users import get_user_store, seats

    assert seats.deactivate_person(get_user_store(), world.users[who]) is True


def _on(world, who: str) -> None:
    from src.users import get_user_store, seats

    store = get_user_store()
    assert seats.reactivate_person(store, store.get_by_id(world.users[who])).is_active


@pytest.mark.parametrize(("kind", "profile"), TOKENS)
def test_a_signed_token_stops_at_the_edge_the_moment_its_person_is_switched_off(
        mcp_world, kind, profile):
    token, _view = mcp_world.issue("mn", [GRANTED], kind=kind, profile=profile)
    assert _verify(token) is not None, "the token should work before the switch"

    _off(mcp_world, "mn")

    assert _verify(token) is None


@pytest.mark.parametrize(("kind", "profile"), TOKENS)
def test_the_tools_see_a_switched_off_person_as_nobody(mcp_world, kind, profile):
    from src.mcp_server.identity import ACCOUNT_INACTIVE, caller_access

    # "mg" is in the team that holds a grant on the repository; a self-service
    # token carries the person's own access, so it asks for everything.
    token, _view = mcp_world.issue("mg", ["*"] if kind == "self" else [GRANTED],
                                   kind=kind, profile=profile)
    mcp_world.as_(token)
    _caller, before = caller_access([GRANTED])
    assert before[GRANTED].researchable, "the token should work before the switch"

    _off(mcp_world, "mg")

    caller, after = caller_access([GRANTED])
    assert caller.refused == ACCOUNT_INACTIVE
    assert not after[GRANTED].researchable and not after[GRANTED].code_visible


def test_switching_the_person_on_again_brings_the_same_token_back(mcp_world):
    token, _view = mcp_world.issue("mn", [GRANTED], kind="cli", profile="dev")
    _off(mcp_world, "mn")
    assert _verify(token) is None

    _on(mcp_world, "mn")

    assert _verify(token) is not None


def test_one_persons_switch_does_not_touch_somebody_elses_token(mcp_world):
    mine, _ = mcp_world.issue("mn", [GRANTED], kind="cli")
    theirs, _ = mcp_world.issue("mg", [GRANTED], kind="cli")

    _off(mcp_world, "mn")

    assert _verify(mine) is None
    assert _verify(theirs) is not None


def test_a_workspace_admin_s_token_is_ended_too(mcp_world):
    """Role is no shield: an administrator of the workspace is a person like any other."""
    token, _ = mcp_world.issue("admin", [GRANTED], kind="cli", profile="full", write=True)
    assert _verify(token) is not None

    _off(mcp_world, "admin")

    assert _verify(token) is None


def test_a_project_token_speaks_for_no_account_so_the_button_has_nothing_to_end(mcp_world):
    from sqlalchemy import inspect

    from src.db.models import McpProjectToken
    from src.mcp_server import project_tokens as pt
    from src.mcp_server.auth import JwtTokenVerifier
    from src.users import get_user_store, seats

    columns = {c.name for c in inspect(McpProjectToken).columns}
    assert "user_id" not in columns, (
        "a project token now names a person: it must be refused when they are "
        "switched off, and this test must say how")

    with mcp_world.session() as s:
        raw, _view = pt.mint(s, workspace_id=WS_A, project_id=str(uuid.uuid4()),
                             label="ci", ttl_seconds=86400, created_by=mcp_world.users["su"])
        s.commit()
    verifier = JwtTokenVerifier(accept_project_tokens=True)
    assert asyncio.run(verifier.verify_token(raw)) is not None

    store = get_user_store()
    for who in ("owner", "admin", "mg", "mr", "mn", "outsider"):
        seats.deactivate_person(store, mcp_world.users[who])

    assert asyncio.run(verifier.verify_token(raw)) is not None


# ─── the OAuth refresh grant ─────────────────────────────────────────


def _withdrawn(oauth_flow, refresh_token: str):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(oauth_flow.refresh(refresh_token))
    return exc.value


def test_a_switched_off_person_gets_no_new_access_token_from_a_refresh_token(mcp_world, oauth):  # noqa: F811
    mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))
    renewed = asyncio.run(oauth.refresh(issued["refresh_token"]))
    assert renewed["access_token"] and renewed["refresh_token"]

    _off(mcp_world, "mr")

    refused = _withdrawn(oauth, renewed["refresh_token"])
    assert (refused.status_code, refused.detail) == (400, "invalid_grant: access was withdrawn")
    # the chain is over: switching the person on again does not bring a spent chain back
    _on(mcp_world, "mr")
    again = _withdrawn(oauth, renewed["refresh_token"])
    assert again.status_code == 400


def test_the_refusal_for_a_switched_off_person_reads_like_a_withdrawn_grant(mcp_world, oauth):  # noqa: F811
    from src.mcp_server import token_store

    _, grant = mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    first = asyncio.run(_exchange(oauth, "mr"))
    with mcp_world.session() as s:
        token_store.revoke(s, grant.id, by=mcp_world.users["su"])
    by_grant = _withdrawn(oauth, first["refresh_token"])

    mcp_world.issue("mn", [GRANTED], kind="oauth_grant")
    second = asyncio.run(_exchange(oauth, "mn"))
    _off(mcp_world, "mn")
    by_account = _withdrawn(oauth, second["refresh_token"])

    assert (by_account.status_code, by_account.detail) == (by_grant.status_code, by_grant.detail)


def test_a_deleted_person_gets_no_new_access_token_from_a_refresh_token(mcp_world, oauth):  # noqa: F811
    from src.users import get_user_store

    mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))
    get_user_store().delete(mcp_world.users["mr"])

    assert _withdrawn(oauth, issued["refresh_token"]).status_code == 400


def test_a_lookup_that_fails_refuses_the_refresh(mcp_world, oauth, monkeypatch):  # noqa: F811
    mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))

    def broken():
        raise RuntimeError("user store unavailable")

    monkeypatch.setattr("src.users.get_user_store", broken)
    assert _withdrawn(oauth, issued["refresh_token"]).status_code == 400
