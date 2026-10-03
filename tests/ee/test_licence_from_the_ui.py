# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Entering the licence on /admin/health: PUT / GET / DELETE /api/license.

What is pinned, in the order it would hurt:

* a valid licence takes effect AT ONCE — the edition, the route table and the
  per-request guard all follow without a restart, and a restart reads the
  same stored token back;
* a licence that does not verify stores nothing and changes nothing;
* only a global admin may read or change it;
* the environment outranks the UI, and the UI says so (409) instead of
  storing something the variable would shadow;
* removing or replacing it takes features away at once;
* applying is idempotent — no route is ever mounted twice;
* the token is in no response, no log line and no audit row.

Signed by the suite's throwaway key (tests/ee/licensing.py), against a
throwaway credential store — never the developer's.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import get_current_user
from src.api.routers import capabilities as caps
from src.ee import license as lic
from src.ee import license_store, mount_enterprise, runtime
from src.users import User
from tests.ee.licensing import (
    CUSTOMER,
    isolate_license_store,
    mint_test_license,
    trust_test_key,
)

ADMIN = User(id="u-admin", email="admin@example.com", is_admin=True)
MEMBER = User(id="u-member", email="member@example.com", is_admin=False)


@pytest.fixture()
def store(monkeypatch, tmp_path):
    trust_test_key(monkeypatch)
    monkeypatch.delenv(lic.ENV_KEY, raising=False)
    monkeypatch.delenv(lic.ENV_FILE, raising=False)
    return isolate_license_store(monkeypatch, tmp_path)


@pytest.fixture()
def audit(monkeypatch):
    rows: list[dict] = []
    from src.security import audit as audit_module

    monkeypatch.setattr(audit_module, "record_action", lambda **kw: rows.append(kw))
    return rows


def _app(env: dict[str, str] | None = None, *, user: User = ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(caps.router)
    mount_enterprise(app, env={} if env is None else env)
    # Only the licence router's admin check is satisfied. The analytics route
    # keeps its own auth, so "mounted and licensed" reads as 401 there.
    app.state.test_user = user
    app.dependency_overrides[get_current_user] = lambda: app.state.test_user
    return app


def _analytics_status(app: FastAPI) -> int:
    """401 = routed, licence check passed, the endpoint's own auth refused;
    404 = not mounted; 403 = mounted but the licence refused."""
    from src.api.deps import current_workspace_id
    from src.db.session import get_async_session

    async def _no_db():
        yield None

    probe = TestClient(app)
    app.dependency_overrides[get_async_session] = _no_db
    # Nothing below the licence guard may succeed: the workspace dependency
    # is made to refuse, so a licensed route answers 401 deterministically.
    def _no_ws():
        raise HTTPException(status_code=401, detail="no workspace in this test")

    app.dependency_overrides[current_workspace_id] = _no_ws
    try:
        return probe.get("/api/analytics/summary").status_code
    finally:
        app.dependency_overrides.pop(current_workspace_id, None)
        app.dependency_overrides.pop(get_async_session, None)


def _raw_unknown_feature() -> str:
    """The mint script refuses unknown features, so this is signed by hand:
    a licence (from a newer build, say) granting nothing this build has."""
    import time

    import jwt

    from tests.ee.licensing import TEST_KEY

    now = int(time.time())
    return jwt.encode({"iss": lic.ISSUER, "sub": CUSTOMER, "features": ["sla-dashboard"],
                       "iat": now, "exp": now + 3600}, TEST_KEY, algorithm="EdDSA")


def _route_count(app: FastAPI) -> int:
    return len(app.router.routes)


def _capabilities(app: FastAPI):
    return caps.build_capabilities(app)


# ─── activation ──────────────────────────────────────────────────────


def test_a_valid_licence_takes_effect_without_a_restart(store, audit):
    app = _app()
    client = TestClient(app)
    assert _capabilities(app).edition == "community"
    assert _analytics_status(app) == 404

    token = mint_test_license(["sso", "analytics"])
    r = client.put("/api/license", json={"token": token})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["edition"] == "enterprise"
    assert body["source"] == "ui"
    assert body["managed_by_env"] is False
    assert body["license"]["customer"] == CUSTOMER
    assert body["license"]["features"] == ["analytics", "sso"]
    assert body["license"]["issued_at"] and body["license"]["expires_at"]

    doc = _capabilities(app)
    assert doc.edition == "enterprise"
    assert doc.features["review_analytics"].available is True
    assert doc.features["sso"].available is True
    assert doc.pages["/analytics"] is True
    assert _analytics_status(app) == 401, "mounted, licensed, then its own auth"
    assert "/api/auth/oidc" in caps.mounted_paths(app)

    assert license_store.read_token() == token
    assert [row["action"] for row in audit] == ["license.saved"]


def test_get_reports_what_is_in_force(store):
    app = _app()
    client = TestClient(app)
    body = client.get("/api/license").json()
    assert body == {
        "edition": "community", "source": None, "managed_by_env": False,
        "env_variable": None, "problem": None, "license": None,
    }
    client.put("/api/license", json={"token": mint_test_license(["analytics"])})
    body = client.get("/api/license").json()
    assert body["edition"] == "enterprise"
    assert body["license"]["features"] == ["analytics"]
    assert body["license"]["expired"] is False


@pytest.mark.parametrize("make, fragment", [
    pytest.param(lambda: mint_test_license(days=1, now=datetime.now(UTC) - timedelta(days=3)),
                 "expired", id="expired"),
    pytest.param(lambda: mint_test_license(key=Ed25519PrivateKey.generate()),
                 "not signed by Celmis", id="foreign-key"),
    pytest.param(lambda: "garbage", "not a licence key", id="garbage"),
    pytest.param(lambda: "", "Paste a licence key", id="empty"),
    pytest.param(lambda: "x" * 20_000, "too large", id="oversized"),
    pytest.param(lambda: _raw_unknown_feature(), "no feature", id="unknown-only"),
])
def test_a_licence_that_does_not_verify_is_refused_and_nothing_is_stored(
        store, audit, make, fragment):
    app = _app()
    before = _route_count(app)
    r = TestClient(app).put("/api/license", json={"token": make()})

    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str)
    assert fragment.lower() in r.json()["detail"].lower()
    assert license_store.read_token() is None
    assert _capabilities(app).edition == "community"
    assert _route_count(app) == before
    assert audit == []


def test_a_wrong_issuer_is_refused(store):
    import time

    import jwt

    from tests.ee.licensing import TEST_KEY

    now = int(time.time())
    token = jwt.encode({"iss": "somebody-else", "sub": CUSTOMER, "features": ["sso"],
                        "iat": now, "exp": now + 3600}, TEST_KEY, algorithm="EdDSA")
    r = TestClient(_app()).put("/api/license", json={"token": token})
    assert r.status_code == 422
    assert "issuer" in r.json()["detail"]
    assert license_store.read_token() is None


# ─── who may ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_only_a_global_admin_may_read_or_change_it(store, method):
    app = _app(user=MEMBER)
    client = TestClient(app)
    kwargs = {"json": {"token": mint_test_license()}} if method == "put" else {}
    r = getattr(client, method)("/api/license", **kwargs)
    assert r.status_code == 403
    assert license_store.read_token() is None
    assert _capabilities(app).edition == "community"


def test_without_a_session_it_is_401(store):
    app = FastAPI()
    mount_enterprise(app, env={})
    assert TestClient(app).get("/api/license").status_code == 401


# ─── the environment outranks the UI ─────────────────────────────────


def test_an_environment_licence_wins_and_the_ui_refuses_to_shadow_it(store, audit):
    env_token = mint_test_license(["sso"], customer="From The Environment")
    app = _app({lic.ENV_KEY: env_token})
    client = TestClient(app)

    state = client.get("/api/license").json()
    assert state["managed_by_env"] is True
    assert state["env_variable"] == lic.ENV_KEY
    assert state["source"] == "env_key"
    assert state["license"]["customer"] == "From The Environment"

    r = client.put("/api/license", json={"token": mint_test_license(["analytics"])})
    assert r.status_code == 409
    assert lic.ENV_KEY in r.json()["detail"]
    assert license_store.read_token() is None
    assert client.delete("/api/license").status_code == 409
    assert _capabilities(app).features["review_analytics"].available is False
    assert audit == []


def test_the_environment_wins_over_a_token_already_stored(store):
    license_store.save_token(mint_test_license(["analytics"], customer="From The UI"),
                             metadata={})
    app = _app({lic.ENV_KEY: mint_test_license(["sso"], customer="From The Environment")})
    assert app.state.celmis_license["customer"] == "From The Environment"
    assert runtime(app).source == "env_key"
    assert _analytics_status(app) == 404


def test_a_licence_file_also_manages_it(store, tmp_path):
    path = tmp_path / "celmis.license"
    path.write_text(mint_test_license(["sso"]))
    client = TestClient(_app({lic.ENV_FILE: str(path)}))
    assert client.get("/api/license").json()["env_variable"] == lic.ENV_FILE
    assert client.put("/api/license",
                      json={"token": mint_test_license()}).status_code == 409


# ─── taking it away ──────────────────────────────────────────────────


def test_removing_it_takes_the_features_away_at_once(store, audit):
    app = _app()
    client = TestClient(app)
    community_routes = _route_count(app)
    client.put("/api/license", json={"token": mint_test_license(["sso", "analytics"])})
    assert _analytics_status(app) == 401

    r = client.delete("/api/license")

    assert r.status_code == 200, r.text
    assert r.json()["edition"] == "community"
    assert r.json()["license"] is None
    doc = _capabilities(app)
    assert doc.edition == "community"
    assert doc.features["review_analytics"].available is False
    assert doc.features["sso"].available is False
    assert doc.pages["/analytics"] is False
    # Unmounted: the route table is back to the community one, and the
    # schema FastAPI memoised was dropped with it.
    assert _analytics_status(app) == 404
    assert _route_count(app) == community_routes
    assert "/api/analytics/summary" not in app.openapi()["paths"]
    assert license_store.read_token() is None
    assert [row["action"] for row in audit] == ["license.saved", "license.removed"]
    assert audit[1]["detail"]["previous_customer"] == CUSTOMER

    assert client.delete("/api/license").status_code == 404


def test_a_request_routed_before_the_removal_is_refused_by_the_guard(store):
    """The route table is one line of defence; the per-request guard is the
    other. A request already matched to the analytics route when the licence
    was removed meets the guard, which reads the licence in force NOW."""
    from src.ee import _still_licensed

    app = _app()
    client = TestClient(app)
    client.put("/api/license", json={"token": mint_test_license(["analytics"])})
    guard = _still_licensed("analytics")
    request = SimpleNamespace(app=app)
    guard(request)  # licensed: passes

    client.delete("/api/license")
    with pytest.raises(HTTPException) as refused:
        guard(request)
    assert refused.value.status_code == 403


def test_replacing_it_with_one_that_drops_a_feature_unmounts_that_feature(store, audit):
    app = _app()
    client = TestClient(app)
    client.put("/api/license", json={"token": mint_test_license(["sso", "analytics"])})
    assert _analytics_status(app) == 401

    r = client.put("/api/license", json={"token": mint_test_license(["sso"])})

    assert r.status_code == 200
    assert r.json()["license"]["features"] == ["sso"]
    assert _analytics_status(app) == 404
    assert _capabilities(app).features["review_analytics"].available is False
    assert _capabilities(app).features["sso"].available is True
    assert [row["action"] for row in audit] == ["license.saved", "license.replaced"]


# ─── idempotence ─────────────────────────────────────────────────────


def test_applying_the_same_licence_again_mounts_nothing_twice(store):
    app = _app()
    client = TestClient(app)
    token = mint_test_license(["sso", "analytics"])
    client.put("/api/license", json={"token": token})
    once = _route_count(app)
    paths_once = sorted(caps.mounted_paths(app))

    for _ in range(3):
        assert client.put("/api/license", json={"token": token}).status_code == 200
    from src.ee import reload_license

    reload_license(app)
    mount_enterprise(app, env={})

    assert _route_count(app) == once
    assert sorted(caps.mounted_paths(app)) == paths_once
    assert sum(1 for p in app.openapi()["paths"] if p == "/api/analytics/summary") == 1


def test_the_licence_router_is_mounted_once_and_always(store):
    app = _app()
    mount_enterprise(app, env={})
    assert sum(1 for p in caps.mounted_paths(app) if p == "/api/license") == 1
    assert _capabilities(app).features["license"].available is True


# ─── a restart reads it back ─────────────────────────────────────────


def test_a_restart_comes_back_as_the_enterprise_edition(store, monkeypatch):
    """`build_app()` — the real application — with only the stored token."""
    monkeypatch.setenv("CELMIS_JWT_SECRET", "test-licence-ui-secret-0123456789abcdef")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345678")
    license_store.save_token(mint_test_license(["analytics"]), metadata={})

    from src.api.main import build_app

    app = build_app()

    doc = caps.build_capabilities(app)
    assert doc.edition == "enterprise"
    assert doc.features["review_analytics"].available is True
    assert doc.features["sso"].available is False
    assert doc.features["license"].available is True
    assert runtime(app).source == "ui"


def test_an_expired_stored_licence_is_the_community_edition_and_says_why(store):
    license_store.save_token(
        mint_test_license(days=1, now=datetime.now(UTC) - timedelta(days=3)), metadata={})
    app = _app()
    body = TestClient(app).get("/api/license").json()
    assert body["edition"] == "community"
    assert body["source"] == "ui"
    assert body["problem"] == "expired"
    assert _analytics_status(app) == 404


# ─── the token stays put ─────────────────────────────────────────────


def test_the_token_is_in_no_response_no_log_and_no_audit_row(store, audit, caplog):
    token = mint_test_license(["sso", "analytics"])
    signature = token.split(".")[2]
    with caplog.at_level(logging.DEBUG):
        app = _app()
        client = TestClient(app)
        bodies = [
            client.put("/api/license", json={"token": token}).text,
            client.get("/api/license").text,
            client.get("/api/capabilities").text,
            client.put("/api/license", json={"token": token + "tampered"}).text,
            client.delete("/api/license").text,
        ]
        # Restart path: the stored token read back and logged about.
        license_store.save_token(token, metadata={})
        _app()

    for body in bodies:
        assert signature not in body
    for part in token.split("."):
        assert part not in caplog.text, "the token, or a piece of it, was logged"
    assert signature not in repr(audit)
    assert signature not in repr(vars(app.state))
    # And the stored metadata is the summary, not the token.
    stored = store.load(license_store.PROVIDER, user_id=license_store.SLOT,
                        update_last_used=False)
    assert signature not in repr(stored.metadata)
