# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The licence gate: what verifies, what does not, and what gets mounted.

Each refusal is attacked with a token that fails exactly that check and
passes the rest, signed by a throwaway key the embedded public key is pointed
at for the test (tests/ee/licensing.py). The one test that does NOT re-point
it is the one that matters most: a token from any key but the owner's is the
community edition.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ee import license as lic
from src.ee import mount_enterprise
from tests.ee.licensing import CUSTOMER, TEST_KEY, mint_test_license, trust_test_key


@pytest.fixture()
def trusted(monkeypatch):
    trust_test_key(monkeypatch)
    monkeypatch.delenv(lic.ENV_KEY, raising=False)
    monkeypatch.delenv(lic.ENV_FILE, raising=False)


def _paths(app: FastAPI) -> set[str]:
    from src.api.routers.capabilities import mounted_paths

    return mounted_paths(app)


# ─── the key ─────────────────────────────────────────────────────────


def test_the_embedded_key_is_the_owners_ed25519_key():
    """Pinned by value: a changed key is a different licensing authority, and
    that must be a reviewed diff to this test, not a side effect."""
    assert "MCowBQYDK2VwAyEAu7TmMc2m1o3FT2UEXgZoQ6c+DE9gInyHyp0xDEfsYbs=" in lic.PUBLIC_KEY_PEM
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    assert isinstance(lic._public_key(), Ed25519PublicKey)


def test_a_test_signed_licence_does_not_verify_against_the_real_key(monkeypatch):
    """No re-pointing here. If this ever passes verification, the suite's
    throwaway key and the owner's key have become interchangeable."""
    monkeypatch.delenv(lic.ENV_KEY, raising=False)
    with pytest.raises(lic.LicenseError, match="signature"):
        lic.verify(mint_test_license())


def test_the_mint_script_and_the_verifier_agree_on_the_features():
    from scripts import ee_mint_license

    assert ee_mint_license.KNOWN_FEATURES == lic.KNOWN_FEATURES
    assert ee_mint_license.ISSUER == lic.ISSUER


# ─── verification ────────────────────────────────────────────────────


def test_a_valid_licence_is_read(trusted):
    got = lic.verify(mint_test_license(["analytics"], days=10))

    assert got.customer == CUSTOMER
    assert got.features == frozenset({"analytics"})
    assert got.grants("analytics") and not got.grants("sso")
    assert got.expires_at - got.issued_at == timedelta(days=10)


def _raw(claims: dict, *, key=TEST_KEY, alg="EdDSA") -> str:
    now = int(time.time())
    base = {"iss": lic.ISSUER, "sub": CUSTOMER, "features": ["sso"],
            "iat": now, "exp": now + 3600}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, key, algorithm=alg)


@pytest.mark.parametrize("claims, reason", [
    pytest.param({"exp": int(time.time()) - 3600}, "expired", id="expired"),
    pytest.param({"nbf": int(time.time()) + 86400}, "not valid yet", id="not-yet"),
    pytest.param({"iss": "somebody-else"}, "issuer", id="wrong-issuer"),
    pytest.param({"exp": None}, "invalid", id="no-exp"),
    pytest.param({"iat": None}, "invalid", id="no-iat"),
    pytest.param({"sub": None}, "invalid", id="no-customer"),
    pytest.param({"features": None}, "features", id="no-features"),
    pytest.param({"features": "sso"}, "features", id="features-not-a-list"),
    pytest.param({"features": []}, "no feature", id="empty-features"),
    pytest.param({"features": ["sla-dashboard"]}, "no feature", id="only-unknown"),
])
def test_a_licence_failing_one_check_is_refused(trusted, claims, reason):
    with pytest.raises(lic.LicenseError, match=reason):
        lic.verify(_raw(claims))


def test_an_unknown_feature_beside_a_known_one_is_ignored(trusted):
    """A newer licence on an older build grants what the build has."""
    got = lic.verify(_raw({"features": ["sso", "from-the-future"]}))
    assert got.features == frozenset({"sso"})


def test_a_key_the_verifier_was_not_given_is_refused(trusted):
    with pytest.raises(lic.LicenseError, match="signature"):
        lic.verify(_raw({}, key=Ed25519PrivateKey.generate()))


def test_a_tampered_payload_is_refused(trusted):
    """Granting yourself a feature by editing the token's middle part."""
    import base64
    import json

    header, payload, sig = mint_test_license(["sso"]).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["features"] = ["sso", "analytics"]
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(lic.LicenseError):
        lic.verify(f"{header}.{forged_payload}.{sig}")


@pytest.mark.parametrize("alg", ["HS256", "none"])
def test_another_algorithm_is_refused_before_any_key_is_tried(trusted, alg):
    """HS256 'signed' with the public key, and alg=none: the classic two."""
    # PyJWT refuses to HMAC with a PEM, so a dummy secret stands in: the
    # header is what is under test, and it must be refused before any key.
    key = "a-dummy-hmac-secret-of-sufficient-length" if alg == "HS256" else None
    forged = jwt.encode({"iss": lic.ISSUER, "sub": "x", "features": ["sso"],
                         "iat": int(time.time()), "exp": int(time.time()) + 60},
                        key, algorithm=alg)
    with pytest.raises(lic.LicenseError, match="not accepted"):
        lic.verify(forged)


@pytest.mark.parametrize("junk", ["", "   ", "not-a-jwt", "a.b.c", "x" * 20000])
def test_junk_is_refused_not_raised(trusted, junk):
    with pytest.raises(lic.LicenseError):
        lic.verify(junk)


# ─── where the licence comes from ────────────────────────────────────


def test_the_variable_is_read(trusted, monkeypatch):
    monkeypatch.setenv(lic.ENV_KEY, mint_test_license(["sso"]))
    got = lic.load_license()
    assert got is not None and got.features == frozenset({"sso"})


def test_the_file_is_read(trusted, monkeypatch, tmp_path):
    path = tmp_path / "celmis.license"
    path.write_text(mint_test_license(["analytics"]) + "\n")
    monkeypatch.setenv(lic.ENV_FILE, str(path))
    got = lic.load_license()
    assert got is not None and got.features == frozenset({"analytics"})


def test_the_variable_outranks_a_file_left_in_a_volume(trusted, monkeypatch, tmp_path):
    path = tmp_path / "old.license"
    path.write_text(mint_test_license(["analytics"]))
    monkeypatch.setenv(lic.ENV_FILE, str(path))
    monkeypatch.setenv(lic.ENV_KEY, mint_test_license(["sso"]))
    assert lic.load_license().features == frozenset({"sso"})


def test_a_missing_file_is_the_community_edition(trusted, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(lic.ENV_FILE, str(tmp_path / "nope"))
    with caplog.at_level(logging.WARNING, logger=lic.__name__):
        assert lic.load_license() is None
    assert "license_file_unreadable" in caplog.text


def test_no_licence_is_the_community_edition_and_says_so(trusted, caplog):
    with caplog.at_level(logging.INFO, logger=lic.__name__):
        assert lic.load_license() is None
    assert "community edition" in caplog.text


def test_an_invalid_licence_warns_without_printing_the_token(trusted, monkeypatch, caplog):
    token = mint_test_license(["sso"], key=Ed25519PrivateKey.generate())
    monkeypatch.setenv(lic.ENV_KEY, token)
    with caplog.at_level(logging.DEBUG):
        assert lic.load_license() is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("license_invalid" in r.getMessage() for r in warnings)
    assert "NOT mounted" in caplog.text
    for part in token.split("."):
        assert part not in caplog.text, "the token, or a piece of it, was logged"


# ─── what gets mounted ───────────────────────────────────────────────


def _mounted(env: dict[str, str]) -> FastAPI:
    app = FastAPI()
    mount_enterprise(app, env=env)
    return app


def test_nothing_is_mounted_without_a_licence(trusted):
    app = _mounted({})
    assert app.state.celmis_license is None
    paths = _paths(app)
    assert not any(p.startswith("/api/analytics") for p in paths)
    assert "/api/auth/oidc" not in paths


def test_only_the_licensed_feature_is_mounted(trusted):
    app = _mounted({lic.ENV_KEY: mint_test_license(["analytics"])})
    paths = _paths(app)
    assert "/api/analytics/summary" in paths
    assert "/api/auth/oidc" not in paths

    app = _mounted({lic.ENV_KEY: mint_test_license(["sso"])})
    paths = _paths(app)
    assert "/api/auth/oidc" in paths
    assert not any(p.startswith("/api/analytics") for p in paths)


def test_an_expired_licence_mounts_nothing(trusted):
    expired = mint_test_license(days=1, now=datetime.now(UTC) - timedelta(days=3))
    app = _mounted({lic.ENV_KEY: expired})
    assert app.state.celmis_license is None
    assert "/api/auth/oidc" not in _paths(app)


def test_the_app_is_told_the_summary_and_never_the_token(trusted):
    token = mint_test_license(["sso", "analytics"])
    app = _mounted({lic.ENV_KEY: token})
    summary = app.state.celmis_license

    assert summary["edition"] == "enterprise"
    assert summary["customer"] == CUSTOMER
    assert summary["features"] == ["analytics", "sso"]
    assert datetime.fromisoformat(summary["expires_at"]) > datetime.now(UTC)
    assert token not in repr(summary)
    assert token.split(".")[2] not in repr(vars(app.state))


def test_a_licence_that_lapses_while_the_process_runs_stops_serving(trusted, monkeypatch):
    """Verified at start-up only would mean a process started on day 364
    serves the feature until somebody restarts it."""
    from src.db.session import get_async_session

    app = _mounted({lic.ENV_KEY: mint_test_license(["analytics"])})

    async def _no_db():
        yield None

    app.dependency_overrides[get_async_session] = _no_db
    client = TestClient(app)
    # No auth dependencies are satisfied here, so a live licence answers 401 —
    # the request got past the licence check and reached the endpoint's own.
    assert client.get("/api/analytics/summary").status_code == 401

    monkeypatch.setattr(lic.License, "expired", lambda self, now=None: True)
    r = client.get("/api/analytics/summary")
    assert r.status_code == 403
    assert "expired" in r.json()["detail"]
