"""An MCP token answers for the workspace it was minted in, and nowhere else.

The MCP identity resolver ignored the token's workspace and used the person's
highest-ranked membership instead — usually their personal workspace, where
they are owner. So a token issued inside a neighbouring team's workspace, for
exploring THAT team's code, read a different tenant; and a person who ranked
higher in B than in A could not hold an A-only token at all.

The attacks here:

  * a token minted in A, held by somebody who is owner of B, must never read
    B's repositories — even ones B's rules would let them read;
  * once that person is removed from A, the same token is refused, with a
    sentence that says why — at the HTTP edge (403, before any tool runs) and
    inside the resolver (every repository denied, writes refused);
  * a claim naming a workspace the subject never belonged to, a deleted
    account, a database that cannot be asked — all refused, never re-homed;
  * a token from the time before the claim keeps the old resolution, and
    says so in the log once.

The database is a real SQLite file read through the same sync engine the
resolver uses in production; the JWTs are signed and verified for real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import types

import jwt as pyjwt
import pytest
from sqlalchemy import create_engine, event

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite

WS_A, WS_B = "wsid-a", "wsid-b"
REPO_A, REPO_B = "github_alpha-api", "github_beta-api"


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two workspaces. `dana` is a member of A and the OWNER of B, so the old
    resolver always picked B for her. Both repos carry a rule granting her
    team full code, so only the workspace binding stands between her A-token
    and B's code."""
    from src.access import resolver
    from src.db.models import (
        Base,
        RepoAccessRule,
        Team,
        TeamMember,
        Workspace,
        WorkspaceMember,
    )
    from src.deployment import reset_mode_cache
    from src.users import User
    from src.users import store as users_mod
    from src.users.store import UserStore

    secret = secrets.token_urlsafe(48)
    monkeypatch.setenv("MCP_JWT_SECRET", secret)
    monkeypatch.delenv("MCP_JWT_SECRET_PREVIOUS", raising=False)
    monkeypatch.delenv("CELMIS_JWT_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(resolver, "_ENGINE", engine)

    ustore = UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", ustore)
    for uid, email, admin in (("u-dana", "dana@acme.io", False),
                              ("u-root", "root@acme.io", True)):
        ustore.create(User(id=uid, email=email, name=uid, is_admin=admin))

    from sqlalchemy.orm import Session
    with Session(engine) as s:
        s.add_all([
            Workspace(id=WS_A, name="Alpha", slug="ws-a"),
            Workspace(id=WS_B, name="Bravo", slug="ws-b"),
            WorkspaceMember(workspace_id=WS_A, user_id="u-dana", role="member"),
            WorkspaceMember(workspace_id=WS_B, user_id="u-dana", role="owner"),
            Team(id="t-a", name="alpha-team", workspace_id=WS_A),
            Team(id="t-b", name="bravo-team", workspace_id=WS_B),
            TeamMember(team_id="t-a", user_id="u-dana"),
            TeamMember(team_id="t-b", user_id="u-dana"),
            RepoAccessRule(id="r-a", workspace_id=WS_A, team_id="t-a",
                           repo_slug=REPO_A, visibility="code"),
            RepoAccessRule(id="r-b", workspace_id=WS_B, team_id="t-b",
                           repo_slug=REPO_B, visibility="code"),
        ])
        s.commit()

    from src.api.auto_review import AutoReviewStore, RepoConfig
    ar = AutoReviewStore(tmp_path / "ar.db")
    for ws, slug in ((WS_A, REPO_A), (WS_B, REPO_B)):
        ar.upsert(RepoConfig(user_id="u-dana", repo_slug=slug, provider="github",
                             full_name=slug.split("_", 1)[1].replace("-", "/", 1),
                             url=f"https://github.com/{slug}", workspace_id=ws))
    monkeypatch.setattr("src.api.auto_review._default_store", ar)

    state = types.SimpleNamespace(engine=engine, secret=secret, token=None)

    import mcp.server.auth.middleware.auth_context as ctx
    monkeypatch.setattr(
        ctx, "get_access_token",
        lambda: (types.SimpleNamespace(token=state.token, client_id="c", scopes=[])
                 if state.token else None),
    )
    from src.mcp_server import identity
    monkeypatch.setattr(identity, "_LEGACY_LOGGED", set())
    yield state
    engine.dispose()
    monkeypatch.delenv("CELMIS_DEPLOYMENT_MODE")
    reset_mode_cache()


def _mint(sub: str, workspace: str | None = None, **extra) -> str:
    from src.mcp_server.auth import JwtConfig, issue_token

    claims = dict(extra)
    if workspace is not None:
        claims["workspace_id"] = workspace
    return issue_token(JwtConfig.from_env(), subject=sub, scopes=["read:graph"],
                       extra_claims=claims or None)


def _remove(world, ws: str, user: str) -> None:
    from sqlalchemy.orm import Session

    from src.db.models import WorkspaceMember
    with Session(world.engine) as s:
        s.delete(s.get(WorkspaceMember, (ws, user)))
        s.commit()


# ─── which workspace ────────────────────────────────────────────────


def test_a_token_minted_in_a_answers_for_a_though_she_ranks_higher_in_b(world):
    from src.mcp_server.identity import resolve_caller

    world.token = _mint("u-dana", WS_A)
    caller = resolve_caller()
    assert caller.workspace_id == WS_A
    assert caller.workspace_resolved is True
    assert not caller.refused


def test_a_token_minted_in_a_never_reads_b(world):
    """The tenant-isolation property itself: B's repo is denied to the A token
    although B's own rule grants Dana full code there."""
    from src.mcp_server.identity import caller_access

    world.token = _mint("u-dana", WS_A)
    _caller, access = caller_access([REPO_A, REPO_B])
    assert access[REPO_A].code_visible is True
    assert access[REPO_B].researchable is False
    assert access[REPO_B].path_visible("src/app.py") is False


def test_the_b_token_reads_b_and_not_a(world):
    from src.mcp_server.identity import caller_access

    world.token = _mint("u-dana", WS_B)
    caller, access = caller_access([REPO_A, REPO_B])
    assert caller.workspace_id == WS_B
    assert access[REPO_B].code_visible is True
    assert access[REPO_A].researchable is False


def test_the_short_claim_spelling_is_not_treated_as_legacy(world):
    from src.mcp_server.identity import resolve_caller

    world.token = _mint("u-dana", None, ws=WS_A)
    assert resolve_caller().workspace_id == WS_A


# ─── refusal ────────────────────────────────────────────────────────


def test_a_removed_member_is_refused_with_a_reason(world):
    from src.mcp_server.identity import caller_access, resolve_caller

    world.token = _mint("u-dana", WS_A)
    _remove(world, WS_A, "u-dana")

    caller = resolve_caller()
    assert caller.refused and WS_A in caller.refused
    assert "no longer a member" in caller.refused
    # Not re-homed into B, where she is still owner.
    assert caller.workspace_id == ""
    assert caller.is_admin is False
    _caller, access = caller_access([REPO_A, REPO_B])
    assert not any(d.researchable for d in access.values())


def test_a_claim_for_a_workspace_never_joined_is_refused(world):
    """A token naming somebody else's workspace — say, one minted by hand with
    a leaked secret's older sibling — is not honoured just because it is
    signed."""
    from sqlalchemy.orm import Session

    from src.db.models import Workspace
    from src.mcp_server.identity import caller_access

    with Session(world.engine) as s:
        s.add(Workspace(id="wsid-c", name="Charlie", slug="ws-c"))
        s.commit()
    world.token = _mint("u-dana", "wsid-c")
    caller, access = caller_access([REPO_A, REPO_B])
    assert caller.refused
    assert not any(d.researchable for d in access.values())


def test_a_deleted_account_with_a_claim_is_refused(world):
    from src.mcp_server.identity import resolve_caller

    world.token = _mint("u-ghost", WS_A)
    assert resolve_caller().refused


def test_an_unreachable_database_fails_closed(world, monkeypatch):
    from src.access import resolver
    from src.mcp_server.identity import caller_access

    def _boom():
        raise RuntimeError("db down")

    world.token = _mint("u-dana", WS_A)
    monkeypatch.setattr(resolver, "_sync_engine", _boom)
    caller, access = caller_access([REPO_A])
    assert caller.refused
    assert access[REPO_A].researchable is False


def test_a_global_admin_may_hold_a_token_for_an_existing_workspace(world):
    from src.mcp_server.identity import resolve_caller

    world.token = _mint("u-root", WS_B)
    caller = resolve_caller()
    assert caller.workspace_id == WS_B and caller.is_admin and not caller.refused

    world.token = _mint("u-root", "wsid-nowhere")
    assert resolve_caller().refused


def test_a_refused_caller_cannot_write(world):
    """The write tools resolve an Actor first; a refused token must stop there
    rather than land the write in a workspace nobody chose."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "mcp_server"
    for name in ("server.py", "http_app.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        actor = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_actor")
        refused_checks = [
            n for n in ast.walk(actor)
            if isinstance(n, ast.If) and isinstance(n.test, ast.Attribute)
            and n.test.attr == "refused"
        ]
        assert refused_checks, f"{name}:_actor does not stop a refused caller"
        assert any(isinstance(x, ast.Raise) for c in refused_checks
                   for x in ast.walk(c)), name


# ─── the HTTP edge ──────────────────────────────────────────────────


def test_the_verifier_refuses_a_removed_member_before_any_tool_runs(world):
    from src.mcp_server.auth import _REFUSAL, JwtTokenVerifier

    verifier = JwtTokenVerifier()
    token = _mint("u-dana", WS_A)
    assert asyncio.run(verifier.verify_token(token)) is not None

    _remove(world, WS_A, "u-dana")
    holder: dict = {}
    reset = _REFUSAL.set(holder)
    try:
        assert asyncio.run(verifier.verify_token(token)) is None
    finally:
        _REFUSAL.reset(reset)
    assert "no longer a member" in holder.get("reason", "")


def test_the_verifier_still_accepts_a_legacy_token(world):
    from src.mcp_server.auth import JwtTokenVerifier

    assert asyncio.run(JwtTokenVerifier().verify_token(_mint("u-dana"))) is not None


def test_a_client_token_carrying_a_claim_is_refused(world):
    """client_credentials tokens are minted without a claim. One that carries
    a workspace anyway names nobody whose membership could be checked."""
    from src.mcp_server.auth import JwtTokenVerifier

    token = _mint("client:ec_x", WS_A)
    assert asyncio.run(JwtTokenVerifier().verify_token(token)) is None


async def _through_the_wrapper(token: str | None):
    """The real wrapper around a stand-in for the SDK: authenticate with the
    real verifier, answer 401 exactly as RequireAuthMiddleware does."""
    from src.mcp_server.auth import JwtTokenVerifier
    from src.mcp_server.http_app import _ExplainRefusal

    verifier = JwtTokenVerifier()

    async def sdk(scope, receive, send):
        ok = await verifier.verify_token(token) if token else None
        if ok is None:
            body = json.dumps({"error": "invalid_token",
                               "error_description": "Authentication required"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"tools"})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _ExplainRefusal(sdk)({"type": "http", "headers": []}, None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


def test_a_removed_member_reads_403_and_a_sentence(world):
    token = _mint("u-dana", WS_A)
    assert asyncio.run(_through_the_wrapper(token))[0] == 200

    _remove(world, WS_A, "u-dana")
    status, body = asyncio.run(_through_the_wrapper(token))
    assert status == 403
    detail = json.loads(body)
    assert detail["error"] == "access_denied"
    assert "no longer a member" in detail["error_description"]
    assert b"tools" not in body


def test_a_forged_token_still_reads_as_a_plain_401(world):
    """The wrapper explains OUR refusal only. A token signed with the wrong
    key gets the SDK's answer, unchanged — no oracle about memberships."""
    forged = pyjwt.encode({"sub": "u-dana", "workspace_id": WS_A,
                           "aud": "mcp-code-analyzer", "iss": "code-analyzer"},
                          "not-the-secret-" + "x" * 40, algorithm="HS256")
    status, body = asyncio.run(_through_the_wrapper(forged))
    assert status == 401
    assert b"no longer a member" not in body


def test_the_wrapper_is_mounted():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "mcp_server"
           / "http_app.py").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(src)
    wrapped = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_ExplainRefusal"
    ]
    assert wrapped, "_ExplainRefusal is defined but never wraps the MCP app"


# ─── legacy tokens ──────────────────────────────────────────────────


def test_a_legacy_token_keeps_the_old_resolution_and_is_logged_once(world, caplog):
    from src.mcp_server.identity import resolve_caller

    world.token = _mint("u-dana")
    with caplog.at_level(logging.WARNING, logger="src.mcp_server.identity"):
        first = resolve_caller()
        second = resolve_caller()
    assert first.workspace_id == WS_B == second.workspace_id  # best-ranked
    assert not first.refused
    lines = [r for r in caplog.records if "mcp_token_without_workspace" in r.getMessage()]
    assert len(lines) == 1


# ─── minting ────────────────────────────────────────────────────────


def test_the_oauth_binding_round_trips_and_is_absent_on_old_values():
    from src.api.routers.oauth import _bind_workspace, _bound_workspace

    code = _bind_workspace(secrets.token_urlsafe(32), WS_A)
    assert _bound_workspace(code) == WS_A
    assert _bound_workspace(secrets.token_urlsafe(32)) is None   # legacy code
    assert _bound_workspace(secrets.token_hex(16)) is None       # legacy family
    assert _bind_workspace("abc", None) == "abc"
    assert _bound_workspace("abc.%%%") is None


def test_the_oauth_flow_carries_the_consent_workspace_into_every_token(
        world, tmp_path, monkeypatch):
    """Consent in A → code → access token with A → refresh → still A."""
    import base64
    import hashlib
    from datetime import UTC
    from datetime import datetime as _dt

    class _NaiveUtc(_dt):
        """SQLite drops tzinfo on the way back; compare like with like."""

        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return _dt.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr("src.api.routers.oauth.datetime", _NaiveUtc)

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.api.routers import oauth
    from src.db.models import OAuthClient
    from src.users import get_user_store

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine.sync_engine, "connect", _sqlite_booleans)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as s:
                s.add(OAuthClient(client_id="ec_pub", client_secret_hash=None,
                                  name="Claude", redirect_uris=["http://127.0.0.1/cb"],
                                  allowed_scopes=["read:graph"], created_by="dana@acme.io"))
                await s.commit()
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            user = get_user_store().get_by_id("u-dana")
            async with factory() as s:
                redirect = await oauth.authorize_consent(
                    request=None, client_id="ec_pub",
                    redirect_uri="http://127.0.0.1/cb", code_challenge=challenge,
                    code_challenge_method="S256", scope="read:graph", state="",
                    session=s, user=user, workspace_id=WS_A,
                )
            location = redirect.headers["location"]
            code = location.split("code=", 1)[1].split("&", 1)[0]
            async with factory() as s:
                issued = await oauth.token_exchange(
                    grant_type="authorization_code", code=code,
                    redirect_uri="http://127.0.0.1/cb", client_id="ec_pub",
                    client_secret=None, code_verifier=verifier, refresh_token=None,
                    session=s,
                )
            async with factory() as s:
                refreshed = await oauth.token_exchange(
                    grant_type="refresh_token", code=None, redirect_uri=None,
                    client_id="ec_pub", client_secret=None, code_verifier=None,
                    refresh_token=issued["refresh_token"], session=s,
                )
            return issued, refreshed
        finally:
            await engine.dispose()

    issued, refreshed = asyncio.run(run())
    for tok in (issued["access_token"], refreshed["access_token"]):
        claims = pyjwt.decode(tok, world.secret, algorithms=["HS256"],
                              options={"verify_aud": False})
        assert claims["workspace_id"] == WS_A
    # The scope the client sees is untouched by the binding.
    assert issued["scope"] == "read:graph" == refreshed["scope"]


def test_the_cli_can_bind_a_token_to_a_workspace():
    import inspect

    from src.cli import mcp_issue_token_cmd

    assert "workspace" in inspect.signature(mcp_issue_token_cmd).parameters
