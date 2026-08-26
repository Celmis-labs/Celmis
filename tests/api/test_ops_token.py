"""Ops debug token: unlocks the read-only ops endpoints and nothing else."""

import os

import pytest
from fastapi.testclient import TestClient

TOKEN = "ops-token-abcdefghijklmnop"


@pytest.fixture()
def client(monkeypatch):
    # A realistic value: the app now refuses to start on a shipped
    # placeholder or a one-character secret, and these tests are about
    # authorisation, not about secret strength.
    monkeypatch.setenv("CELMIS_JWT_SECRET", "e3f1a9c7d2b48065f1ac93de77208b4c")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345678")
    monkeypatch.setenv("CELMIS_OPS_TOKEN", TOKEN)
    from src.api.main import build_app
    return TestClient(build_app())


def test_ops_token_access(client, monkeypatch):
    c = client

    # 1. no credentials → 401
    r = c.get("/api/ops/logs")
    assert r.status_code == 401, r.status_code

    # 2. wrong token → 401
    r = c.get("/api/ops/logs", headers={"X-Ops-Token": "wrong-token-000000000"})
    assert r.status_code == 401, r.status_code

    # 3. correct token → 200 + payload shape
    r = c.get("/api/ops/logs?limit=5", headers={"X-Ops-Token": "ops-token-abcdefghijklmnop"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert "records" in r.json() and "stats" in r.json()

    # 4. plain-text tail works too
    r = c.get("/api/ops/logs.txt?limit=3", headers={"X-Ops-Token": "ops-token-abcdefghijklmnop"})
    assert r.status_code == 200, r.status_code

    # 5. token must NOT unlock non-ops admin endpoints
    r = c.get("/api/jobs", headers={"X-Ops-Token": "ops-token-abcdefghijklmnop"})
    assert r.status_code in (401, 403), ("jobs must stay closed", r.status_code)

    # 6. short/empty env token disables the path entirely (fail-closed)
    os.environ["CELMIS_OPS_TOKEN"] = "short"
    r = c.get("/api/ops/logs", headers={"X-Ops-Token": "short"})
    assert r.status_code == 401, ("short token must be refused", r.status_code)
    os.environ["CELMIS_OPS_TOKEN"] = ""
    r = c.get("/api/ops/logs", headers={"X-Ops-Token": ""})
    assert r.status_code == 401, ("empty token must be refused", r.status_code)


def test_ops_token_does_not_unlock_the_gateway_probe(client):
    """The token's value is that its blast radius is obvious.

    /api/ops/gateway is read-only and belongs to it; /api/ops/gateway/verify
    creates a team, registers deployments and mints a key — self-cleaning, but
    not read-only — so it must stay behind a global-admin session.
    """
    headers = {"X-Ops-Token": TOKEN}
    assert client.get("/api/ops/gateway", headers=headers).status_code == 200
    assert client.post("/api/ops/gateway/verify", headers=headers).status_code in (401, 403)
