"""An MCP client connects with the URL alone: register, sign in, Allow, refresh.

The flow Claude Code and a claude.ai custom connector run, end to end, against a
real SQLite database through the same code the API runs (the FastAPI app, the
async session, the sync token store, the MCP token verifier). The browser is
played by `consent` below: it does what the web app's consent page does, with
the person's own API bearer token.

What is pinned:

  * anonymous registration: public clients only, loopback or allow-listed https;
  * the code is single use, bound to the client, the redirect URI (exact, with
    the loopback port the one thing that may vary) and the PKCE challenge, and a
    wrong verifier burns it;
  * the code and every token after it stay in the workspace consent was given in;
  * a refresh token rotates, and a reused one revokes its family;
  * revoking a connection, or the person leaving the workspace, ends the session
    at once, even for the access token already in the client's hands;
  * scopes are read-only unless a write scope is asked for AND the role allows it;
  * without an administrator's grant or CELMIS_MCP_SELF_SERVICE nobody connects.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite

WS_A, WS_B = "wsid-a", "wsid-b"
BASE = "https://celmis.example.com"
LOOPBACK = "http://localhost:53682/callback"
RESOURCE = f"{BASE}/backend/mcp"


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)


class World:
    """The database, the people, and a client that talks to the API."""

    def __init__(self, engine, http, tokens):  # noqa: ANN001
        self.engine, self.http, self.tokens = engine, http, tokens

    def headers(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    from src.access import resolver
    from src.api.main import app
    from src.db.models import Base, Workspace, WorkspaceMember
    from src.db.session import get_async_session
    from src.deployment import reset_mode_cache
    from src.users import User
    from src.users import store as users_mod
    from src.users.store import UserStore

    db = tmp_path / "celmis.db"
    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("CELMIS_MCP_SELF_SERVICE", "true")
    monkeypatch.delenv("OAUTH_ALLOWED_REDIRECT_HOSTS", raising=False)
    monkeypatch.setenv("CELMIS_RATE_LIMIT_ENABLED", "0")
    monkeypatch.delenv("MCP_OAUTH_ISSUER", raising=False)
    from src.api.routers import oauth as oauth_router
    oauth_router._RECENT_BY_IP.clear()
    # An earlier test in the same process may already have built the app's
    # middleware stack with the limiter on, and its counters outlive that test.
    # Rebuild it under this test's environment, and again afterwards.
    app.middleware_stack = None
    from src.config import get_settings
    get_settings.cache_clear()
    reset_mode_cache()

    engine = create_engine(f"sqlite:///{db}")
    event.listen(engine, "connect", _sqlite_booleans)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(resolver, "_ENGINE", engine)

    ustore = UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", ustore)
    people = {"ann": ("u-ann", False), "bob": ("u-bob", False), "eve": ("u-eve", False),
              "root": ("u-root", True)}
    from src.api.jwt_auth import issue_token
    tokens = {}
    for name, (uid, admin) in people.items():
        ustore.create(User(id=uid, email=f"{name}@acme.io", name=name, is_admin=admin))
        tokens[name], _ = issue_token(user_id=uid, email=f"{name}@acme.io")

    with Session(engine) as s:
        s.add_all([
            Workspace(id=WS_A, name="Alpha", slug="ws-a"),
            Workspace(id=WS_B, name="Bravo", slug="ws-b"),
            # ann: owner of A, member of B. bob: member of A. eve: member of B only.
            WorkspaceMember(workspace_id=WS_A, user_id="u-ann", role="owner"),
            WorkspaceMember(workspace_id=WS_B, user_id="u-ann", role="member"),
            WorkspaceMember(workspace_id=WS_A, user_id="u-bob", role="member"),
            WorkspaceMember(workspace_id=WS_B, user_id="u-eve", role="member"),
        ])
        s.commit()

    aengine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    maker = async_sessionmaker(aengine, expire_on_commit=False)

    async def _session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    http = TestClient(
        app, base_url=BASE, follow_redirects=False,
        headers={"X-Forwarded-Host": "celmis.example.com", "X-Forwarded-Proto": "https",
                 "X-Forwarded-Prefix": "/backend", "X-Forwarded-For": "203.0.113.9"})
    yield World(engine, http, tokens)
    app.middleware_stack = None
    app.dependency_overrides.pop(get_async_session, None)
    engine.dispose()
    asyncio.run(aengine.dispose())
    get_settings.cache_clear()
    reset_mode_cache()


# ─── what the client and the browser do ─────────────────────────────


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(world, redirect=LOOPBACK, **extra):
    body = {"client_name": "Acme Agent", "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none", **extra}
    return world.http.post("/oauth/register", json=body)


def _authorize_params(client_id, challenge, redirect=LOOPBACK, scope="", state="st-1", resource=RESOURCE):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    if scope:
        params["scope"] = scope
    if resource:
        params["resource"] = resource
    return params


def _consent(world, who, params, decision="allow", workspace=None):
    """The consent page's own call: the person's bearer token, JSON in, JSON out."""
    body = {k: params[k] for k in ("client_id", "redirect_uri", "code_challenge",
                                   "code_challenge_method")}
    body.update(scope=params.get("scope", ""), state=params.get("state", ""),
                resource=params.get("resource"), workspace=workspace, decision=decision)
    return world.http.post("/oauth/authorize/consent", json=body, headers=world.headers(who))


def _code_from(resp) -> str:
    assert resp.status_code == 200, resp.text
    query = parse_qs(urlsplit(resp.json()["redirect_to"]).query)
    return query["code"][0]


def _exchange(world, client_id, code, verifier, redirect=LOOPBACK, resource=RESOURCE):
    data = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": redirect, "code_verifier": verifier}
    if resource:
        data["resource"] = resource
    return world.http.post("/oauth/token", data=data)


def _refresh(world, client_id, refresh_token):
    return world.http.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token})


def connect(world, who="ann", scope="", workspace=None, redirect=LOOPBACK, resource=RESOURCE):
    """Register, authorize, Allow, exchange: the whole thing. Returns the token body and ids."""
    reg = _register(world, redirect)
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    params = _authorize_params(client_id, challenge, redirect, scope, resource=resource)
    answer = _consent(world, who, params, workspace=workspace)
    code = _code_from(answer)
    token = _exchange(world, client_id, code, verifier, redirect, resource)
    assert token.status_code == 200, token.text
    return token.json(), client_id


def _verify(token: str):
    from src.mcp_server.auth import JwtConfig, JwtTokenVerifier

    return asyncio.run(JwtTokenVerifier(JwtConfig.from_env()).verify_token(token))


def _claims(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


# ─── anonymous registration ─────────────────────────────────────────


@pytest.mark.parametrize("uri", [
    "http://localhost:53682/callback", "http://localhost/callback",
    "http://127.0.0.1:8123/cb", "http://[::1]:9000/cb",
    "https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
])
def test_an_anonymous_client_may_register_a_loopback_or_allowlisted_redirect(world, uri):
    r = _register(world, uri)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("ec_")
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [uri]
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("uri", [
    "http://example.com/callback",                  # http, not loopback
    "http://localhost.evil.example/cb",             # a lookalike host
    "https://evil.example/callback",                # https, not allowlisted
    "https://claude.ai.evil.example/cb",            # suffix trick
    "https://user:pw@claude.ai/cb",                 # credentials
    "https://claude.ai/cb#frag",                    # fragment
    "cursor://anysphere.cursor/oauth",              # custom scheme
    "javascript:alert(1)", "file:///etc/passwd", "",
    "http://localhost:99999999/cb",                 # not a port
    "https://claude.ai/cb\n.evil.example",          # control character
])
def test_anything_else_is_refused(world, uri):
    r = _register(world, uri)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_redirect_uri"


def test_a_confidential_registration_is_refused_without_an_administrator(world):
    r = _register(world, LOOPBACK, token_endpoint_auth_method="client_secret_post")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"
    assert "POST /oauth/clients" in r.json()["error_description"]


def test_registration_needs_redirect_uris_and_a_json_object(world):
    assert world.http.post("/oauth/register", json={"client_name": "x"}).status_code == 400
    assert world.http.post("/oauth/register", json=["x"]).status_code == 400
    assert _register(world, LOOPBACK, grant_types=["client_credentials"]).status_code == 400


def test_the_operator_allowlist_replaces_the_default(world, monkeypatch):
    monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_HOSTS", "apps.acme.example, *.corp.example")
    assert _register(world, "https://apps.acme.example/cb").status_code == 201
    assert _register(world, "https://x.corp.example/cb").status_code == 201
    assert _register(world, "https://corp.example/cb").status_code == 400
    assert _register(world, "https://claude.ai/api/mcp/auth_callback").status_code == 400
    assert _register(world, LOOPBACK).status_code == 201     # loopback is never an allowlist matter


def test_the_client_name_is_text_not_markup(world):
    r = world.http.post("/oauth/register", json={
        "client_name": "<img src=x onerror=alert(1)>\n   Evil", "redirect_uris": [LOOPBACK]})
    assert r.status_code == 201
    assert "<" not in r.json()["client_name"] and "\n" not in r.json()["client_name"]


def test_registration_shares_the_strictest_rate_limit_class():
    from src.api.middleware import _classify

    assert _classify("/oauth/register", "POST") == "auth"
    assert _classify("/oauth/token", "POST") == "auth"


def test_a_client_that_never_completed_a_grant_is_collected_after_a_day(world):
    from datetime import UTC, datetime, timedelta

    from src.db.models import OAuthClient, OAuthRefreshToken

    stale = _register(world).json()["client_id"]
    kept, _ = connect(world)
    fresh = _register(world).json()["client_id"]
    with Session(world.engine) as s:
        for row in s.query(OAuthClient).all():
            row.created_at = datetime.now(UTC) - timedelta(hours=30)
        s.get(OAuthClient, fresh).created_at = datetime.now(UTC) - timedelta(hours=1)
        s.add(OAuthClient(client_id="ec_admin", name="made by an admin", redirect_uris=[],
                          allowed_scopes=[], created_by="ann@acme.io",
                          created_at=datetime.now(UTC) - timedelta(days=9)))
        s.commit()
    assert _register(world).status_code == 201          # registering collects
    with Session(world.engine) as s:
        ids = {c.client_id for c in s.query(OAuthClient).all()}
        in_use = {r.client_id for r in s.query(OAuthRefreshToken).all()}
    assert stale not in ids, "an abandoned registration lives forever"
    assert fresh in ids and "ec_admin" in ids
    assert in_use and in_use <= ids, "a client that holds a session was collected"


# ─── authorize hands the browser to the web app ─────────────────────


def test_authorize_redirects_to_the_consent_page_on_the_web_origin(world):
    client_id = _register(world).json()["client_id"]
    _verifier, challenge = _pkce()
    r = world.http.get("/oauth/authorize", params=_authorize_params(client_id, challenge, scope="read:graph"))
    assert r.status_code == 302
    where = urlsplit(r.headers["location"])
    assert (where.scheme, where.netloc, where.path) == ("https", "celmis.example.com", "/oauth/consent")
    q = parse_qs(where.query)
    assert q["client_id"] == [client_id] and q["redirect_uri"] == [LOOPBACK]
    assert q["code_challenge"] == [challenge] and q["state"] == ["st-1"]
    assert q["resource"] == [RESOURCE] and q["scope"] == ["read:graph"]
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(("change", "error"), [
    ({"redirect_uri": "http://localhost:53682/other"}, "invalid_request"),
    ({"redirect_uri": "https://evil.example/cb"}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge": "short"}, "invalid_request"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"scope": "admin"}, "invalid_scope"),
    ({"resource": "https://other.example/backend/mcp"}, "invalid_target"),
    ({"resource": f"{BASE}/backend/somewhere-else"}, "invalid_target"),
    ({"client_id": "ec_nobody"}, "invalid_request"),
])
def test_authorize_refuses_what_it_cannot_honour_without_redirecting(world, change, error):
    client_id = _register(world).json()["client_id"]
    _verifier, challenge = _pkce()
    params = {**_authorize_params(client_id, challenge), **change}
    r = world.http.get("/oauth/authorize", params=params)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == error
    assert "location" not in r.headers


def test_a_loopback_client_may_use_another_port_but_not_another_path_or_host(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()

    def go(uri):
        return world.http.get("/oauth/authorize", params=_authorize_params(client_id, challenge, redirect=uri))

    assert go("http://localhost:61000/callback").status_code == 302
    assert go("http://localhost:61000/elsewhere").status_code == 400
    assert go("http://127.0.0.1:61000/callback").status_code == 400
    assert go("https://localhost:61000/callback").status_code == 400


def test_an_https_redirect_has_to_match_exactly(world):
    uri = "https://claude.ai/api/mcp/auth_callback"
    client_id = _register(world, uri).json()["client_id"]
    _v, challenge = _pkce()
    ok = world.http.get("/oauth/authorize", params=_authorize_params(client_id, challenge, redirect=uri))
    other = world.http.get("/oauth/authorize", params=_authorize_params(
        client_id, challenge, redirect="https://claude.ai:444/api/mcp/auth_callback"))
    assert ok.status_code == 302 and other.status_code == 400


# ─── consent ────────────────────────────────────────────────────────


def test_the_consent_page_is_told_who_asks_from_where_and_for_what(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge, scope="read:graph write:repos")
    info = world.http.get("/oauth/consent-info", params={k: v for k, v in params.items()
                                                         if k not in ("response_type", "state")},
                          headers=world.headers("ann")).json()
    assert info["client"]["name"] == "Acme Agent"
    assert info["client"]["redirect_host"] == "localhost"
    assert info["client"]["registered_by_administrator"] is False
    assert {w["slug"] for w in info["workspaces"]} == {"ws-a", "ws-b"}
    assert [s["scope"] for s in info["scopes"]] == ["read:graph", "write:repos"]
    assert info["blocked"] is None


def test_consent_requires_the_persons_own_bearer_token(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge)
    body = {k: params[k] for k in ("client_id", "redirect_uri", "code_challenge")} | {"decision": "allow"}
    assert world.http.post("/oauth/authorize/consent", json=body).status_code == 401
    assert world.http.post("/oauth/authorize/consent", json=body,
                           headers={"Authorization": "Bearer nonsense"}).status_code == 401
    # A browser form post (what the old page sent) is not a JSON body and is refused.
    assert world.http.post("/oauth/authorize/consent", data=body,
                           headers=world.headers("ann")).status_code in (400, 415, 422)


def test_deny_answers_access_denied_to_the_registered_redirect_only(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge)
    r = _consent(world, "ann", params, decision="deny")
    where = urlsplit(r.json()["redirect_to"])
    q = parse_qs(where.query)
    assert (where.scheme, where.netloc, where.path) == ("http", "localhost:53682", "/callback")
    assert q["error"] == ["access_denied"] and q["state"] == ["st-1"] and "code" not in q
    assert q["iss"] == [f"{BASE}/backend"]


def test_consent_never_redirects_anywhere_the_client_did_not_register(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge, redirect="https://evil.example/steal")
    for decision in ("allow", "deny"):
        r = _consent(world, "ann", params, decision=decision)
        assert r.status_code == 400 and "redirect_to" not in r.text


def test_state_is_returned_untouched_and_the_answer_names_the_issuer(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    state = 'a b&c=d"><script>'
    params = _authorize_params(client_id, challenge, state=state)
    q = parse_qs(urlsplit(_consent(world, "ann", params).json()["redirect_to"]).query)
    assert q["state"] == [state]
    assert q["iss"] == [f"{BASE}/backend"]


# ─── the code and the tokens ────────────────────────────────────────


def test_the_whole_flow_gives_an_mcp_token_the_server_accepts_and_a_refresh_token(world):
    body, client_id = connect(world, scope="read:graph read:reviews")
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    assert body["scope"] == "read:graph read:reviews"
    assert body["refresh_token"]
    claims = _claims(body["access_token"])
    assert claims["client_id"] == client_id
    assert claims["sub"] == "u-ann" and claims["workspace_id"] == WS_A
    assert claims["aud"] == ["mcp-code-analyzer", RESOURCE] and claims["resource"] == RESOURCE
    assert claims["grant"], "the token has to name the row it runs under"
    access = _verify(body["access_token"])
    assert access is not None and set(access.scopes) == {"read:graph", "read:reviews"}


def test_responses_from_the_token_endpoint_are_never_cached(world):
    reg = _register(world).json()["client_id"]
    r = world.http.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": reg,
                                              "code": "nope", "redirect_uri": LOOPBACK, "code_verifier": "x" * 50})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert r.headers["cache-control"] == "no-store"
    ok, _ = connect(world)
    assert world.http.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": _, "refresh_token": ok["refresh_token"]}
    ).headers["cache-control"] == "no-store"


def test_nothing_requested_is_a_read_only_token(world):
    body, _ = connect(world)
    scopes = set(body["scope"].split())
    assert scopes == {"read:graph", "read:groups", "read:reviews", "read:code"}
    assert not any(s.startswith("write:") for s in scopes)


def test_a_wrong_verifier_fails_and_burns_the_code(world):
    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    bad = _exchange(world, client_id, code, "not-the-verifier-" + "x" * 40)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # The thief gets one guess: the right verifier no longer redeems it either.
    assert _exchange(world, client_id, code, verifier).status_code == 400


def test_a_code_is_single_use(world):
    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    assert _exchange(world, client_id, code, verifier).status_code == 200
    again = _exchange(world, client_id, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_a_code_is_bound_to_its_client_and_its_redirect_uri(world):
    client_id = _register(world).json()["client_id"]
    other = _register(world).json()["client_id"]
    verifier, challenge = _pkce()

    def code():
        return _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))

    assert _exchange(world, other, code(), verifier).status_code == 400
    assert _exchange(world, client_id, code(), verifier, redirect="http://localhost:53682/x").status_code == 400
    assert _exchange(world, client_id, code(), verifier, redirect="http://localhost:1/callback").status_code == 400
    assert _exchange(world, client_id, code(), verifier).status_code == 200


def test_an_expired_code_is_refused(world):
    from datetime import UTC, datetime, timedelta

    from src.db.models import OAuthAuthCode

    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    with Session(world.engine) as s:
        s.get(OAuthAuthCode, code).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert _exchange(world, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_the_code_endpoint_needs_a_verifier(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    r = world.http.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
                                              "code": code, "redirect_uri": LOOPBACK})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


# ─── the resource the token is for ──────────────────────────────────


def test_a_resource_other_than_the_approved_one_is_refused_at_the_token_endpoint(world):
    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    r = _exchange(world, client_id, code, verifier, resource=f"{BASE}/backend/mcp/dev")
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"


def test_a_token_for_the_dev_endpoint_does_not_open_the_full_one(world):
    from src.mcp_server import auth

    body, _ = connect(world, resource=f"{BASE}/backend/mcp/dev/")
    claims = _claims(body["access_token"])
    assert claims["resource"] == f"{BASE}/backend/mcp/dev"          # stored without the slash

    def judged_at(path):
        reset = auth._REFUSAL.set({"path": path})
        try:
            return auth._resource_problem(claims)
        finally:
            auth._REFUSAL.reset(reset)

    assert judged_at("/mcp/dev/") is None
    assert judged_at("/mcp/dev") is None
    assert "different endpoint" in judged_at("/mcp/")
    assert "different endpoint" in judged_at("/mcp")


def test_a_token_for_the_full_endpoint_opens_it_and_what_is_below_it(world):
    from src.mcp_server import auth

    body, _ = connect(world)
    claims = _claims(body["access_token"])
    for path in ("/mcp/", "/mcp", "/mcp/dev/"):
        reset = auth._REFUSAL.set({"path": path})
        try:
            assert auth._resource_problem(claims) is None, path
        finally:
            auth._REFUSAL.reset(reset)


def test_a_client_that_names_no_resource_gets_a_token_with_no_binding(world):
    body, _ = connect(world, resource=None)
    claims = _claims(body["access_token"])
    assert "resource" not in claims and claims["aud"] == "mcp-code-analyzer"


# ─── workspaces ─────────────────────────────────────────────────────


def test_the_grant_follows_the_workspace_the_person_picked(world):
    body, _ = connect(world, who="ann", workspace="ws-b")
    assert _claims(body["access_token"])["workspace_id"] == WS_B
    body, _ = connect(world, who="ann", workspace="ws-a")
    assert _claims(body["access_token"])["workspace_id"] == WS_A


def test_a_person_cannot_pick_a_workspace_they_do_not_belong_to(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    r = _consent(world, "eve", _authorize_params(client_id, challenge), workspace="ws-a")
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    assert "redirect_to" not in r.text


def test_the_page_may_echo_the_workspace_by_id(world):
    """A person who is not a member of the active workspace (a platform operator) is shown it
    under its id; the page sends that back, and it must be accepted only as the active one."""
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge)
    assert _consent(world, "eve", params, workspace=WS_B).status_code == 200
    assert _consent(world, "eve", params, workspace=WS_A).status_code == 403


def test_without_a_pick_the_active_workspace_is_used(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge)
    headers = {**world.headers("ann"), "X-Workspace": "ws-b"}
    body = {k: params[k] for k in ("client_id", "redirect_uri", "code_challenge")} | {
        "resource": RESOURCE, "decision": "allow"}
    code = _code_from(world.http.post("/oauth/authorize/consent", json=body, headers=headers))
    verifier = None  # noqa: F841 — the verifier belongs to `_pkce` above, not needed here
    from src.api.routers.oauth import _bound_workspace

    assert _bound_workspace(code) == WS_B


def test_a_code_cannot_be_moved_to_another_workspace(world):
    """The workspace is part of the code the server issued; editing the code finds no row."""
    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge), workspace="ws-a"))
    opaque, _, _tag = code.rpartition(".")
    forged = f"{opaque}.{base64.urlsafe_b64encode(WS_B.encode()).rstrip(b'=').decode()}"
    assert _exchange(world, client_id, forged, verifier).status_code == 400
    assert _exchange(world, client_id, code, verifier).status_code == 200


def test_multi_tenant_a_token_never_reaches_another_workspace(world, monkeypatch):
    """Ann belongs to A and B. The token consent gave in A is judged against A only,
    and stops being good the moment she leaves A even though she is still in B."""
    from src.deployment import reset_mode_cache
    from src.mcp_server import auth

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()
    body, _ = connect(world, who="ann", workspace="ws-a")
    claims = _claims(body["access_token"])
    assert claims["workspace_id"] == WS_A
    assert auth._workspace_problem(claims) is None
    assert _verify(body["access_token"]) is not None

    from src.db.models import WorkspaceMember
    with Session(world.engine) as s:
        s.delete(s.get(WorkspaceMember, (WS_A, "u-ann")))
        s.commit()
    assert auth._workspace_problem(claims) is not None, "the token reached a workspace she left"
    assert _verify(body["access_token"]) is None
    # B's own token is unaffected: it is a different grant in a different workspace.
    other, _ = connect(world, who="ann", workspace="ws-b")
    assert _verify(other["access_token"]) is not None
    assert _claims(other["access_token"])["workspace_id"] == WS_B


def test_multi_tenant_the_resolved_caller_is_the_workspace_of_the_grant(world, monkeypatch):
    import types

    import mcp.server.auth.middleware.auth_context as ctx

    from src.deployment import reset_mode_cache
    from src.mcp_server import identity

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()
    body, _ = connect(world, who="ann", workspace="ws-b")
    monkeypatch.setattr(ctx, "get_access_token", lambda: types.SimpleNamespace(
        token=body["access_token"], client_id="c", scopes=[]))
    caller = identity.resolve_caller()
    assert caller.workspace_id == WS_B and not caller.refused
    # Ann ranks higher in A (owner) than in B (member): the old resolver would have said A.


# ─── refresh ────────────────────────────────────────────────────────


def test_a_refresh_token_rotates_and_the_new_access_token_works(world):
    first, client_id = connect(world)
    r = _refresh(world, client_id, first["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == first["scope"]
    claims = _claims(second["access_token"])
    assert claims["workspace_id"] == WS_A and claims["resource"] == RESOURCE
    assert _verify(second["access_token"]) is not None
    assert _refresh(world, client_id, second["refresh_token"]).status_code == 200


def test_a_reused_refresh_token_revokes_the_whole_family(world):
    first, client_id = connect(world)
    second = _refresh(world, client_id, first["refresh_token"]).json()
    replay = _refresh(world, client_id, first["refresh_token"])            # the old one again
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert "reuse" in replay.json()["error_description"]
    # The thief's replay burned the legitimate holder's newest token as well,
    assert _refresh(world, client_id, second["refresh_token"]).status_code == 400
    # and the access token still in somebody's hands stops working too: its grant is revoked.
    assert _verify(second["access_token"]) is None
    assert _verify(first["access_token"]) is None


def test_a_refresh_token_belongs_to_one_client(world):
    first, _client_id = connect(world)
    other = _register(world).json()["client_id"]
    assert _refresh(world, other, first["refresh_token"]).status_code == 400
    assert _refresh(world, "ec_nobody", first["refresh_token"]).status_code == 401


def test_a_refresh_cannot_widen_the_resource(world):
    first, client_id = connect(world)
    r = world.http.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": first["refresh_token"], "resource": f"{BASE}/backend/mcp/dev"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"


def test_an_expired_refresh_token_is_refused(world):
    from datetime import UTC, datetime, timedelta

    from src.db.models import OAuthRefreshToken

    first, client_id = connect(world)
    with Session(world.engine) as s:
        for row in s.query(OAuthRefreshToken).all():
            row.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        s.commit()
    assert _refresh(world, client_id, first["refresh_token"]).status_code == 400


def test_only_the_hash_of_a_refresh_token_is_stored(world):
    from src.db.models import OAuthRefreshToken

    first, _ = connect(world)
    with Session(world.engine) as s:
        stored = {r.token_hash for r in s.query(OAuthRefreshToken).all()}
    assert first["refresh_token"] not in stored
    assert hashlib.sha256(first["refresh_token"].encode()).hexdigest() in stored


def test_leaving_the_workspace_stops_the_refresh(world):
    from src.db.models import WorkspaceMember

    first, client_id = connect(world, who="bob", workspace="ws-a")
    with Session(world.engine) as s:
        s.delete(s.get(WorkspaceMember, (WS_A, "u-bob")))
        s.commit()
    r = _refresh(world, client_id, first["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert _verify(first["access_token"]) is None


def test_a_switched_off_account_stops_the_refresh(world):
    from src.users import get_user_store

    first, client_id = connect(world, who="bob")
    user = get_user_store().get_by_id("u-bob")
    user.is_active = False
    get_user_store().update(user)
    assert _refresh(world, client_id, first["refresh_token"]).status_code == 400


def test_deleting_the_client_ends_its_sessions(world):
    first, client_id = connect(world, who="ann")
    assert world.http.delete(f"/oauth/clients/{client_id}", headers=world.headers("root")).status_code == 204
    assert _refresh(world, client_id, first["refresh_token"]).status_code == 401
    assert _verify(first["access_token"]) is None


# ─── connected apps ─────────────────────────────────────────────────


def test_a_person_sees_and_revokes_their_own_connection(world):
    body, client_id = connect(world, who="bob")
    mine = world.http.get("/oauth/connections", headers=world.headers("bob")).json()
    assert len(mine) == 1
    item = mine[0]
    assert item["client_name"] == "Acme Agent" and item["redirect_host"] == "localhost"
    assert item["workspace_name"] == "Alpha" and item["mine"] is True and item["person"] is None
    assert set(item["scopes"]) == set(body["scope"].split())
    assert item["last_used_at"] and item["expires_at"]

    r = world.http.delete(f"/oauth/connections/{item['id']}", headers=world.headers("bob"))
    assert r.status_code == 204
    assert world.http.get("/oauth/connections", headers=world.headers("bob")).json() == []
    assert _refresh(world, client_id, body["refresh_token"]).status_code == 400, "revoke must stop refresh"
    assert _verify(body["access_token"]) is None, "revoke must stop the access token already issued"


def test_nobody_sees_or_revokes_a_stranger_connection(world):
    _body, _ = connect(world, who="bob", workspace="ws-a")
    item = world.http.get("/oauth/connections", headers=world.headers("bob")).json()[0]
    assert world.http.get("/oauth/connections", headers=world.headers("eve")).json() == []
    assert world.http.delete(f"/oauth/connections/{item['id']}",
                             headers=world.headers("eve")).status_code == 404
    assert len(world.http.get("/oauth/connections", headers=world.headers("bob")).json()) == 1


def test_a_workspace_administrator_sees_and_revokes_their_workspaces_connections(world):
    body, client_id = connect(world, who="bob", workspace="ws-a")      # ann owns A
    seen = world.http.get("/oauth/connections", headers=world.headers("ann")).json()
    assert [c["person"] for c in seen] == ["bob@acme.io"] and seen[0]["mine"] is False
    assert world.http.delete(f"/oauth/connections/{seen[0]['id']}",
                             headers=world.headers("ann")).status_code == 204
    assert _refresh(world, client_id, body["refresh_token"]).status_code == 400


def test_an_administrator_of_another_workspace_sees_nothing_of_it(world):
    connect(world, who="eve", workspace="ws-b")                        # ann is only a member of B
    assert world.http.get("/oauth/connections", headers=world.headers("ann")).json() == []


def test_revoking_requires_a_login(world):
    assert world.http.get("/oauth/connections").status_code == 401
    assert world.http.delete("/oauth/connections/x").status_code == 401


# ─── scopes by role ─────────────────────────────────────────────────


def test_a_write_scope_is_granted_to_a_role_that_may_use_it(world):
    body, _ = connect(world, who="ann", scope="read:graph write:repos")   # owner of A
    assert set(body["scope"].split()) == {"read:graph", "write:repos"}
    from src.db.models import McpToken
    claims = _claims(body["access_token"])
    with Session(world.engine) as s:
        row = s.get(McpToken, claims["grant"])
        assert row.allow_write is True and "write:repos" in row.scopes and row.kind == "self"
        assert "write:config" not in row.scopes, "only the write scopes that were granted"


def test_a_write_scope_is_withheld_from_a_role_that_may_not(world):
    body, _ = connect(world, who="bob", scope="read:graph write:repos write:config")   # a plain member
    assert body["scope"] == "read:graph"
    claims = _claims(body["access_token"])
    from src.db.models import McpToken
    with Session(world.engine) as s:
        assert s.get(McpToken, claims["grant"]).allow_write is False


def test_the_consent_page_says_which_scopes_are_withheld_and_why(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge, scope="read:graph write:repos")
    q = {k: v for k, v in params.items() if k not in ("response_type", "state")}
    info = world.http.get("/oauth/consent-info", params=q, headers=world.headers("bob")).json()
    by = {s["scope"]: s for s in info["scopes"]}
    assert by["read:graph"]["granted"] is True
    assert by["write:repos"]["granted"] is False and "roles" in by["write:repos"]["reason"]


def test_an_editor_may_write_reviews_but_not_config(world):
    from src.db.models import WorkspaceMember

    with Session(world.engine) as s:
        s.get(WorkspaceMember, (WS_A, "u-bob")).role = "editor"
        s.commit()
    body, _ = connect(world, who="bob", scope="write:reviews write:config")
    assert body["scope"] == "write:reviews"


def test_the_client_registered_by_an_administrator_keeps_to_its_allowed_scopes(world):
    r = world.http.post("/oauth/clients", headers=world.headers("root"), json={
        "name": "CI", "redirect_uris": [LOOPBACK], "allowed_scopes": ["read:graph"], "public": True})
    assert r.status_code == 201
    client_id = r.json()["client_id"]
    _v, challenge = _pkce()
    asked = world.http.get("/oauth/authorize", params=_authorize_params(
        client_id, challenge, scope="read:graph write:repos"))
    assert asked.status_code == 400 and asked.json()["error"] == "invalid_scope"


# ─── who may connect at all ─────────────────────────────────────────


def test_without_self_service_or_a_grant_nobody_but_the_superadmin_connects(world, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_SELF_SERVICE", "false")
    get_settings.cache_clear()
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge)
    q = {k: v for k, v in params.items() if k not in ("response_type", "state")}
    info = world.http.get("/oauth/consent-info", params=q, headers=world.headers("bob")).json()
    assert info["blocked"] and "administrator" in info["blocked"]
    r = _consent(world, "bob", params)
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    assert "redirect_to" not in r.text


def test_an_administrators_grant_opens_it_and_is_the_ceiling(world, monkeypatch):
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("CELMIS_MCP_SELF_SERVICE", "false")
    get_settings.cache_clear()
    with Session(world.engine) as s:
        grant = token_store.create_row(
            s, kind="oauth_grant", workspace_id=WS_A, user_id="u-bob", issued_by="u-root",
            label="bob", patterns=["acme/*"], profile="full", allow_write=False)
        grant_id = grant.id
    body, _ = connect(world, who="bob", scope="read:graph write:repos")
    claims = _claims(body["access_token"])
    assert claims["grant"] == grant_id, "the administrator's row is the one the session runs under"
    assert "write:repos" not in body["scope"]


def test_a_superadmin_may_connect_without_self_service(world, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_SELF_SERVICE", "false")
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@acme.io")
    get_settings.cache_clear()
    from src.users import get_user_store
    from src.users.roles import is_superadmin

    if not is_superadmin(get_user_store().get_by_id("u-root")):
        pytest.skip("this install configures its superadmin differently")
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    assert _consent(world, "root", _authorize_params(client_id, challenge), workspace="ws-a").status_code == 200


# ─── the discovery documents say what the server does ───────────────


def test_the_documents_advertise_what_a_client_needs(world):
    meta = world.http.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert set(meta["scopes_supported"]) >= {"read:graph", "write:repos", "write:config", "write:reviews"}
    assert "write:policies" not in meta["scopes_supported"], "a scope the grant model does not know"
    prm = world.http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert not any(s.startswith("write:") for s in prm["scopes_supported"]), (
        "a client that sends no scope asks for scopes_supported: that must never be a write scope")


# ─── a redirect is built from what was registered, never from the caller ───

HOSTILE_REDIRECTS = [
    "http://evil.example\\@localhost:53682/callback",       # a browser reads this as evil.example
    "http://localhost:53682\\@evil.example/callback",
    "http://user:pw@localhost:53682/callback",
    "http://evil.example@localhost:53682/callback",
    "http://localhost:53682/callback#frag",
    "http://localhost:53682/callback\t",
    "http://localhost:53682/call\nback",
    "http://localhost:53682/call%5cback",
    "http://localhost:53682/callback ",
    "http://localhost:0x35/callback",
]


@pytest.mark.parametrize("given", HOSTILE_REDIRECTS)
def test_a_loopback_redirect_with_a_second_reading_is_refused(world, given):
    from src.api import oauth_rules as rules

    assert not rules.redirect_matches([LOOPBACK], given)
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    params = _authorize_params(client_id, challenge, redirect=given)
    for decision in ("allow", "deny"):
        r = _consent(world, "ann", params, decision=decision)
        assert r.status_code == 400 and "redirect_to" not in r.text, given
    assert world.http.get("/oauth/authorize", params=params).status_code == 400


def test_the_answer_goes_to_the_registered_address_with_a_plain_port(world):
    client_id = _register(world).json()["client_id"]
    _v, challenge = _pkce()
    other = "http://localhost:61234/callback"
    r = _consent(world, "ann", _authorize_params(client_id, challenge, redirect=other))
    parts = urlsplit(r.json()["redirect_to"])
    assert (parts.scheme, parts.hostname, parts.port, parts.path) == ("http", "localhost", 61234, "/callback")
    assert parts.netloc == "localhost:61234" and not parts.fragment


def test_the_rules_keep_the_known_good_forms(world):
    from src.api import oauth_rules as rules

    assert rules.canonical_redirect([LOOPBACK], LOOPBACK) == LOOPBACK
    assert rules.canonical_redirect([LOOPBACK], "http://localhost:9/callback") == "http://localhost:9/callback"
    assert rules.canonical_redirect(["http://[::1]:1/cb"], "http://[::1]:7/cb") == "http://[::1]:7/cb"
    assert rules.canonical_redirect(["https://app.example.com/cb"], "https://app.example.com/cb")
    assert rules.canonical_redirect(["https://app.example.com/cb"], "https://app.example.com:444/cb") is None


# ─── the verifier must be a real RFC 7636 verifier ──────────────────

@pytest.mark.parametrize("verifier", ["a" * 42, "a" * 129, "a" * 43 + "=", "a" * 43 + " ", "é" * 50])
def test_a_verifier_outside_rfc_7636_is_refused_even_when_it_matches(world, verifier):
    from src.api.routers.oauth import _pkce_ok

    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert not _pkce_ok(verifier, challenge)
    good = "A" * 43
    assert _pkce_ok(good, base64.urlsafe_b64encode(hashlib.sha256(good.encode()).digest()).rstrip(b"=").decode())


# ─── a replayed code ends what it produced ──────────────────────────

def test_a_replayed_code_revokes_the_session_it_started(world):
    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    first = _exchange(world, client_id, code, verifier).json()
    assert _verify(first["access_token"]) is not None
    assert _exchange(world, client_id, code, verifier).status_code == 400      # the thief
    assert _refresh(world, client_id, first["refresh_token"]).status_code == 400
    assert _verify(first["access_token"]) is None


def test_another_client_replaying_a_code_ends_nothing(world):
    first, _client = connect(world)
    other = _register(world).json()["client_id"]
    from src.db.models import OAuthAuthCode

    with Session(world.engine) as s:
        code = s.query(OAuthAuthCode).first().code
    _exchange(world, other, code, "a" * 43)
    assert _verify(first["access_token"]) is not None


# ─── the cap on waiting registrations ───────────────────────────────

def test_clients_that_hold_a_session_do_not_use_up_the_cap(world, monkeypatch):
    from src.api.routers import oauth as oauth_router

    monkeypatch.setattr(oauth_router, "_ANONYMOUS_CLIENT_CAP", 2)
    connect(world)
    connect(world)
    assert _register(world).status_code == 201          # two in use, none waiting
    assert _register(world).status_code == 201
    refused = _register(world)                          # two waiting: full
    assert refused.status_code == 503 and refused.json()["error"] == "temporarily_unavailable"


def test_one_address_cannot_fill_the_registrations(world, monkeypatch):
    from src.api.routers import oauth as oauth_router

    monkeypatch.setattr(oauth_router, "_PER_IP_UNUSED_CAP", 3)
    monkeypatch.setenv("CELMIS_TRUST_PROXY", "1")        # behind the proxy the address is its last entry
    assert [_register(world).status_code for _ in range(3)] == [201, 201, 201]
    assert _register(world).status_code == 429
    other = world.http.post("/oauth/register", headers={"X-Forwarded-For": "198.51.100.4"}, json={
        "client_name": "Acme", "redirect_uris": [LOOPBACK], "token_endpoint_auth_method": "none"})
    assert other.status_code == 201, "the cap is per address, not global"


# ─── disconnecting an app stops its token, even under an admin grant ─

def test_disconnecting_stops_an_access_token_that_runs_under_an_administrators_grant(world, monkeypatch):
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("CELMIS_MCP_SELF_SERVICE", "false")
    get_settings.cache_clear()
    with Session(world.engine) as s:
        grant = token_store.create_row(
            s, kind="oauth_grant", workspace_id=WS_A, user_id="u-bob", issued_by="u-root",
            label="bob", patterns=["acme/*"], profile="full", allow_write=False)
        grant_id = grant.id
    one, _ = connect(world, who="bob")
    two, _ = connect(world, who="bob")
    assert _claims(one["access_token"])["grant"] == grant_id == _claims(two["access_token"])["grant"]
    assert _verify(one["access_token"]) is not None and _verify(two["access_token"]) is not None

    family = _claims(one["access_token"])["family"]
    r = world.http.delete(f"/oauth/connections/{family}", headers=world.headers("bob"))
    assert r.status_code == 204
    assert _verify(one["access_token"]) is None, "the disconnected app keeps working until its token expires"
    assert _verify(two["access_token"]) is not None, "the other app under the same grant must not be cut off"


# ─── the administrator's registration endpoint ──────────────────────

def test_an_administrator_registers_a_confidential_client_at_the_clients_endpoint(world):
    r = world.http.post("/oauth/clients", headers=world.headers("root"), json={
        "name": "CI", "redirect_uris": [], "allowed_scopes": ["read:graph"], "public": False})
    assert r.status_code == 201
    assert set(r.json()) == {"client_id", "client_secret", "name", "redirect_uris", "allowed_scopes"}
    assert r.json()["client_secret"]
    # The anonymous endpoint is not a way to the same thing.
    anon = world.http.post("/oauth/register", json={
        "client_name": "CI", "redirect_uris": [LOOPBACK], "token_endpoint_auth_method": "client_secret_basic"})
    assert anon.status_code == 400
    assert world.http.post("/oauth/clients", json={"name": "x"}).status_code in (401, 403)


def test_a_replay_does_not_touch_a_session_started_after_the_codes_span(world):
    from datetime import UTC, datetime, timedelta

    from src.db.models import OAuthRefreshToken

    client_id = _register(world).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_from(_consent(world, "ann", _authorize_params(client_id, challenge)))
    first = _exchange(world, client_id, code, verifier).json()
    # A later, unrelated session of the same client for the same person (another workspace).
    later = _consent(world, "ann", _authorize_params(client_id, challenge), workspace="ws-b")
    second = _exchange(world, client_id, _code_from(later), verifier).json()
    with Session(world.engine) as s:
        rows = {r.token_hash: r for r in s.query(OAuthRefreshToken).all()}
        for r in rows.values():
            if r.token_hash == hashlib.sha256(second["refresh_token"].encode()).hexdigest():
                r.issued_at = datetime.now(UTC) + timedelta(minutes=10)
        s.commit()
    assert _exchange(world, client_id, code, verifier).status_code == 400      # the replay
    assert _refresh(world, client_id, first["refresh_token"]).status_code == 400
    assert _refresh(world, client_id, second["refresh_token"]).status_code == 200


def test_registrations_from_one_ipv6_prefix_share_a_bucket(world):
    from src.api.routers.oauth import _address_key

    assert _address_key("2001:db8:1:2::1") == _address_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert _address_key("2001:db8:1:3::1") != _address_key("2001:db8:1:2::1")
    assert _address_key("203.0.113.9") == "203.0.113.9"
    assert _address_key("203.0.113.10") != _address_key("203.0.113.9")
    assert _address_key("::ffff:203.0.113.9") == "203.0.113.9"


def test_a_full_table_evicts_the_quietest_not_everyone(world, monkeypatch):
    from src.api.routers import oauth as oauth_router

    monkeypatch.setattr(oauth_router, "_RECENT_IPS_MAX", 10)
    for n in range(10):
        assert oauth_router._registration_allowed(f"198.51.100.{n}")
    assert oauth_router._registration_allowed("198.51.100.99")
    keys = set(oauth_router._RECENT_BY_IP)
    assert "198.51.100.99" in keys and "198.51.100.0" not in keys
    assert len(keys) >= 9, "a full table must not be emptied"
