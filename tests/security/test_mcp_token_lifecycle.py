"""A token's life: issued, narrowed, revoked, expired — and how that is audited.

These are the acceptance criteria of the access lane written as behaviour:

  * a revoked token is refused on the next call after the cache window (<=30 s);
  * a PATCH to the repo list takes effect without reissuing the token;
  * a person outside the workspace cannot hold a token for it;
  * OAuth needs a grant row, is limited to its repos, and its refresh dies
    with the grant;
  * a token minted before grants existed is refused (or, by choice, read-only);
  * every call writes one audit row, and no row holds a value that was asked.

Real tokens, real JWT verification, real identity resolution, SQLite.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time

import pytest

from tests.security.mcp_world import (
    FOREIGN,
    GRANTED,
    RULED,
    WILD,
    WS_A,
    set_env,
)


def _verify(token: str):
    from src.mcp_server.auth import JwtTokenVerifier

    return asyncio.run(JwtTokenVerifier().verify_token(token))


def _readable(world, token: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(token)
    _caller, access = caller_access([GRANTED, RULED, WILD, FOREIGN])
    return {s for s, d in access.items() if d.researchable}


# ─── revoke, expire, narrow ──────────────────────────────────────────


def test_the_cache_window_is_at_most_thirty_seconds():
    from src.mcp_server import token_store

    assert token_store.CACHE_TTL_SECONDS <= 30


def test_a_revoked_token_is_refused_on_the_next_call(mcp_world):
    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD])
    assert _verify(token) is not None
    with mcp_world.session() as s:
        token_store.revoke(s, view.id, by=mcp_world.users["su"])
    assert _verify(token) is None
    assert _readable(mcp_world, token) == set()


def test_a_revocation_made_elsewhere_lands_within_the_cache_window(mcp_world, monkeypatch):
    """Another process writes the revocation: this one learns it when its cached
    answer expires, and never later than the window."""
    from datetime import UTC, datetime

    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD])
    assert _verify(token) is not None                      # cached as active
    with mcp_world.session() as s:                          # no invalidate(): another process
        row = s.get(type(mcp_world.row(view.id)), view.id)
        row.revoked_at = datetime.now(UTC)
        s.commit()
    assert _verify(token) is not None, "still inside the window: served from the cache"
    real = time.monotonic
    monkeypatch.setattr(token_store.time, "monotonic",
                        lambda: real() + token_store.CACHE_TTL_SECONDS + 1)
    assert _verify(token) is None, "after the window the row is read again"


def test_an_expired_token_is_refused(mcp_world):
    from datetime import UTC, datetime, timedelta

    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD])
    with mcp_world.session() as s:
        row = s.get(type(mcp_world.row(view.id)), view.id)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    token_store.invalidate(view.id)
    assert _verify(token) is None
    assert _readable(mcp_world, token) == set()


def test_a_token_is_refused_for_somebody_else(mcp_world):
    """The row names its holder: the same signed value under another subject
    is not the same grant."""
    import jwt as pyjwt

    token, _ = mcp_world.issue("mn", [WILD])
    claims = pyjwt.decode(token, options={"verify_signature": False})
    forged = pyjwt.encode({**claims, "sub": mcp_world.users["mg"]}, mcp_world.secret,
                          algorithm="HS256")
    assert _verify(forged) is None


def test_patching_the_repo_list_applies_without_reissuing(mcp_world):
    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD])
    assert _readable(mcp_world, token) == {WILD}
    with mcp_world.session() as s:
        token_store.update_row(s, view.id, patterns=[GRANTED, RULED])
    assert _readable(mcp_world, token) == {GRANTED, RULED}, "same token, new list"
    with mcp_world.session() as s:
        token_store.update_row(s, view.id, patterns=["github_aco-w*"])
    assert _readable(mcp_world, token) == {WILD}


def test_a_patch_can_neither_write_into_a_dev_token_nor_empty_the_list(mcp_world):
    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD], profile="dev")
    with mcp_world.session() as s:
        with pytest.raises(token_store.TokenError):
            token_store.update_row(s, view.id, allow_write=True)
        with pytest.raises(token_store.TokenError):
            token_store.update_row(s, view.id, patterns=[])
    assert _readable(mcp_world, token) == {WILD}


def test_the_token_list_cannot_name_another_workspaces_repo_into_reach(mcp_world):
    """Patterns are matched against the TOKEN'S workspace registry, so a pattern
    that names another tenant's repository matches nothing."""
    token, _ = mcp_world.issue("owner", [FOREIGN, "github_bco-*"])
    assert _readable(mcp_world, token) == set()


def test_a_token_for_a_workspace_the_holder_is_not_in_is_refused(mcp_world):
    token, _ = mcp_world.issue("outsider", ["*"], workspace=WS_A)
    from src.mcp_server.identity import resolve_caller

    mcp_world.as_(token)
    caller = resolve_caller()
    assert caller.refused and not caller.is_admin
    assert _readable(mcp_world, token) == set()
    assert _verify(token) is None


def test_removing_the_holder_from_the_workspace_ends_the_token(mcp_world):
    from sqlalchemy import delete

    from src.db.models import WorkspaceMember

    token, _ = mcp_world.issue("mg", ["*"], kind="self")
    assert _readable(mcp_world, token) == {GRANTED}
    with mcp_world.session() as s:
        s.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == mcp_world.users["mg"]))
        s.commit()
    assert _readable(mcp_world, token) == set()


def test_a_write_token_writes_and_a_read_token_does_not(mcp_world):
    from src.automation.actions import ActionError
    from src.mcp_server.identity import actor_for

    read, _ = mcp_world.issue("admin", ["*"])
    mcp_world.as_(read)
    with pytest.raises(ActionError, match="read-only"):
        actor_for("mcp", writing=True)
    write, _ = mcp_world.issue("admin", ["*"], write=True)
    mcp_world.as_(write)
    assert actor_for("mcp", writing=True).workspace_id == WS_A


# ─── legacy tokens ───────────────────────────────────────────────────


def test_a_token_from_before_grants_is_refused_by_default(mcp_world):
    from src.mcp_server.identity import LEGACY_REFUSAL, resolve_caller

    legacy = mcp_world.legacy_token("owner")
    assert _verify(legacy) is None
    mcp_world.as_(legacy)
    assert resolve_caller().refused == LEGACY_REFUSAL
    assert _readable(mcp_world, legacy) == set()


def test_accept_mode_lets_a_legacy_token_read_only_with_the_holders_own_access(
        mcp_world, monkeypatch, caplog):
    from src.automation.actions import ActionError
    from src.mcp_server.identity import actor_for, resolve_caller

    set_env(monkeypatch, CELMIS_MCP_LEGACY_TOKENS="accept")
    legacy = mcp_world.legacy_token("mg")
    assert _verify(legacy) is not None
    with caplog.at_level(logging.WARNING):
        assert _readable(mcp_world, legacy) == {GRANTED}, "the person's own access, nothing more"
    mcp_world.as_(legacy)
    caller = resolve_caller()
    assert caller.kind == "legacy" and not caller.allow_write
    with pytest.raises(ActionError, match="read-only"):
        actor_for("mcp", writing=True)


def test_accept_mode_gives_no_unruled_repo_to_a_legacy_member(mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_MCP_LEGACY_TOKENS="accept")
    assert _readable(mcp_world, mcp_world.legacy_token("mn")) == set()


# ─── OAuth ───────────────────────────────────────────────────────────


@pytest.fixture
def oauth(mcp_world, tmp_path, monkeypatch):
    """The consent and token endpoints, called as functions, on the world's DB."""
    from datetime import UTC
    from datetime import datetime as _dt

    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.api.routers import oauth as oauth_mod
    from src.db.models import OAuthClient
    from src.users import get_user_store
    from tests.security.mcp_world import _sqlite_booleans

    class _NaiveUtc(_dt):
        """SQLite drops tzinfo on the way back; compare like with like."""

        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return _dt.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr(oauth_mod, "datetime", _NaiveUtc)
    engine = create_async_engine(f"sqlite+aiosqlite:///{mcp_world.engine.url.database}")
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _client():
        async with factory() as s:
            s.add(OAuthClient(client_id="ec_pub", client_secret_hash=None, name="Editor",
                              redirect_uris=["http://127.0.0.1/cb"],
                              allowed_scopes=["read:graph"], created_by="su@acme.io"))
            await s.commit()

    asyncio.run(_client())

    class Flow:
        verifier = secrets.token_urlsafe(48)

        def __init__(self) -> None:
            self.challenge = base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()

        async def consent(self, who: str):
            user = get_user_store().get_by_id(mcp_world.users[who])
            async with factory() as s:
                return await oauth_mod.authorize_consent(
                    request=None, client_id="ec_pub", redirect_uri="http://127.0.0.1/cb",
                    code_challenge=self.challenge, code_challenge_method="S256",
                    scope="read:graph", state="", session=s, user=user, workspace_id=WS_A)

        async def exchange(self, code: str):
            async with factory() as s:
                return await oauth_mod.token_exchange(
                    grant_type="authorization_code", code=code,
                    redirect_uri="http://127.0.0.1/cb", client_id="ec_pub",
                    client_secret=None, code_verifier=self.verifier, refresh_token=None,
                    session=s)

        async def refresh(self, refresh_token: str):
            async with factory() as s:
                return await oauth_mod.token_exchange(
                    grant_type="refresh_token", code=None, redirect_uri=None,
                    client_id="ec_pub", client_secret=None, code_verifier=None,
                    refresh_token=refresh_token, session=s)

    yield Flow()
    asyncio.run(engine.dispose())


def _code(redirect) -> str:
    return redirect.headers["location"].split("code=", 1)[1].split("&", 1)[0]


def test_oauth_consent_without_a_grant_is_refused(mcp_world, oauth):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(oauth.consent("mn"))
    assert exc.value.status_code == 403
    assert "superadmin" in str(exc.value.detail)


def test_oauth_cannot_exceed_the_grant(mcp_world, oauth):
    import jwt as pyjwt

    _, grant = mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))
    claims = pyjwt.decode(issued["access_token"], options={"verify_signature": False})
    assert claims["grant"] == grant.id and claims["workspace_id"] == WS_A
    assert _verify(issued["access_token"]) is not None
    # mr's own rights reach RULED; the grant is what limits the OAuth client
    assert _readable(mcp_world, issued["access_token"]) == {GRANTED}


def test_narrowing_the_grant_narrows_the_oauth_token_at_once(mcp_world, oauth):
    from src.mcp_server import token_store

    _, grant = mcp_world.issue("mr", [GRANTED, RULED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))
    assert _readable(mcp_world, issued["access_token"]) == {GRANTED, RULED}
    with mcp_world.session() as s:
        token_store.update_row(s, grant.id, patterns=[RULED])
    assert _readable(mcp_world, issued["access_token"]) == {RULED}


def test_refresh_fails_after_the_grant_is_revoked(mcp_world, oauth):
    from fastapi import HTTPException

    from src.mcp_server import token_store

    _, grant = mcp_world.issue("mr", [GRANTED], kind="oauth_grant")
    issued = asyncio.run(_exchange(oauth, "mr"))
    renewed = asyncio.run(oauth.refresh(issued["refresh_token"]))
    assert renewed["access_token"] and renewed["refresh_token"]
    with mcp_world.session() as s:
        token_store.revoke(s, grant.id, by=mcp_world.users["su"])
    # the access token already issued is refused at once ...
    assert _verify(renewed["access_token"]) is None
    # ... and the refresh token no longer buys a new one
    with pytest.raises(HTTPException) as exc:
        asyncio.run(oauth.refresh(renewed["refresh_token"]))
    assert exc.value.status_code == 400


async def _exchange(flow, who: str) -> dict:
    return await flow.exchange(_code(await flow.consent(who)))


# ─── the audit ───────────────────────────────────────────────────────


NEEDLE = "NEEDLE-4f9a-value-that-must-not-be-logged"


def _enveloped_call(mcp, name: str, arguments: dict):
    import mcp.types as types

    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments))
    return asyncio.run(handler(req))


def _log_rows(world) -> list:
    from sqlalchemy import select

    from src.db.models import McpCallLog
    from src.mcp_server import audit

    assert audit.flush(10.0), "the audit queue did not drain"
    with world.session() as s:
        return list(s.execute(select(McpCallLog).order_by(McpCallLog.ts)).scalars())


def _servers():
    from src.mcp_server import http_app, server

    return {"stdio": server.build_server, "http": http_app._build_mcp}


@pytest.mark.parametrize("name", ["stdio", "http"])
def test_every_tool_of_every_server_sits_behind_the_audit_envelope(name, monkeypatch):
    """The envelope wraps the call handler of the whole server, so a tool added
    tomorrow is covered without anybody remembering to. A server that does not
    install it, or an SDK that stops exposing the handler, fails here."""
    from src.mcp_server import call_envelope, guard

    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    mcp = _servers()[name]()
    assert call_envelope.is_enveloped(mcp), f"{name}: calls are not audited"
    tools = mcp._tool_manager._tools
    assert tools, name
    unguarded = [n for n, t in tools.items() if not guard.is_guarded(t.fn)]
    assert not unguarded, f"{name}: no refusal guard on {unguarded}"


def test_a_call_writes_one_row_naming_who_which_token_which_tool(mcp_world):
    from src.mcp_server import server

    token, view = mcp_world.issue("mn", [WILD])
    mcp_world.as_(token)
    mcp = server.build_server()
    _enveloped_call(mcp, "list_repos", {})
    rows = _log_rows(mcp_world)
    assert len(rows) == 1
    [row] = rows
    assert (row.tool, row.user_id, row.token_id, row.workspace_id) == (
        "list_repos", mcp_world.users["mn"], view.id, WS_A)
    assert row.kind == "cli" and row.status == "ok"
    assert row.result_bytes > 0 and row.duration_ms >= 0
    assert isinstance(row.repos, list)


def test_the_audit_holds_no_value_that_was_asked(mcp_world):
    from src.mcp_server import audit, server
    from src.mcp_server.call_envelope import _args_hash

    token, _ = mcp_world.issue("mn", [WILD])
    mcp_world.as_(token)
    mcp = server.build_server()
    args = {"name": NEEDLE, "repo_slug": GHOST_SLUG}
    _enveloped_call(mcp, "find_symbol", args)
    [row] = _log_rows(mcp_world)
    everything = json.dumps({c.name: str(getattr(row, c.name))
                             for c in row.__table__.columns}, default=str)
    assert NEEDLE not in everything and token not in everything
    assert row.args_hash == _args_hash(args) and len(row.args_hash) == 16
    assert audit.flush()


GHOST_SLUG = "github_nobody-nothing"


def test_two_calls_differ_by_hash_but_neither_shows_its_arguments(mcp_world):
    from src.mcp_server import server

    token, _ = mcp_world.issue("mn", [WILD])
    mcp_world.as_(token)
    mcp = server.build_server()
    _enveloped_call(mcp, "find_symbol", {"name": "alpha", "repo_slug": GHOST_SLUG})
    _enveloped_call(mcp, "find_symbol", {"name": "bravo", "repo_slug": GHOST_SLUG})
    first, second = _log_rows(mcp_world)
    assert first.args_hash != second.args_hash
    assert "alpha" not in repr(first.__dict__) and "bravo" not in repr(second.__dict__)


def test_a_refused_credential_is_audited_as_denied(mcp_world):
    from src.mcp_server import server

    mcp_world.as_(mcp_world.legacy_token("owner"))     # refused by default
    mcp = server.build_server()
    result = _enveloped_call(mcp, "list_repos", {})
    assert result.root.isError
    [row] = _log_rows(mcp_world)
    assert row.status == "denied"


def test_an_unknown_tool_is_audited_too(mcp_world):
    from src.mcp_server import server

    token, _ = mcp_world.issue("mn", [WILD])
    mcp_world.as_(token)
    _enveloped_call(server.build_server(), "no_such_tool", {})
    [row] = _log_rows(mcp_world)
    assert row.tool == "no_such_tool" and row.status in ("error", "denied")


def test_the_audit_never_breaks_a_call(mcp_world, monkeypatch):
    from src.mcp_server import audit, server

    def boom(_rec):
        raise RuntimeError("the audit table is gone")

    token, _ = mcp_world.issue("mn", [WILD])
    mcp_world.as_(token)
    monkeypatch.setattr(audit, "_enqueue", lambda row: boom(row))
    result = _enveloped_call(server.build_server(), "list_repos", {})
    assert not result.root.isError


def test_retention_deletes_only_rows_past_the_window(mcp_world):
    from datetime import UTC, datetime, timedelta

    from src.db.models import McpCallLog
    from src.mcp_server import audit

    def row(days: int) -> McpCallLog:
        return McpCallLog(
            ts=datetime.now(UTC) - timedelta(days=days), workspace_id=WS_A, user_id="u",
            token_id=None, kind="pat", client_id="", tool="t", profile="dev", repos=[],
            status="ok", result_bytes=0, result_items=0, duration_ms=0, args_hash="x")

    with mcp_world.session() as s:
        s.add_all([row(1), row(audit.retention_days() + 5)])
        s.commit()
        assert audit.purge(s) == 1
    assert len(_log_rows(mcp_world)) == 1
