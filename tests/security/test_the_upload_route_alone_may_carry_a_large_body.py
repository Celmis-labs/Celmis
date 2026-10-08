"""Request size caps: the archive upload route gets 250 MB, nothing else does."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import middleware


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CELMIS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.delenv("CELMIS_REDIS_URL", raising=False)
    monkeypatch.delenv("CELMIS_MAX_UPLOAD_BYTES", raising=False)
    app = FastAPI()
    app.add_middleware(middleware.RateLimitMiddleware)

    @app.post("/api/repos/upload")
    def upload() -> dict:
        return {"ok": True}

    @app.post("/api/repos")
    def add() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_the_upload_path_is_its_own_class():
    assert middleware._classify("/api/repos/upload") == "upload"
    assert middleware._classify("/api/repos/upload/extra") != "upload"
    assert middleware._classify("/api/repos") == "default"


def test_the_upload_cap_is_250_mb_plus_the_multipart_envelope():
    caps = middleware._body_caps()
    assert caps["upload"] == 250 * 1024 * 1024 + 1024 * 1024
    assert caps["default"] == 1024 * 1024 and caps["mcp"] == 5 * 1024 * 1024


def test_a_declared_body_over_the_cap_is_413_on_the_upload_route(client):
    over = str(250 * 1024 * 1024 + 2 * 1024 * 1024)
    r = client.post("/api/repos/upload", content=b"x", headers={"content-length": over})
    assert r.status_code == 413


def test_a_large_declared_body_on_any_other_route_is_still_413(client):
    r = client.post("/api/repos", content=b"x",
                    headers={"content-length": str(50 * 1024 * 1024)})
    assert r.status_code == 413


def test_a_body_under_the_cap_reaches_the_upload_route(client):
    r = client.post("/api/repos/upload", content=b"x",
                    headers={"content-length": str(100 * 1024 * 1024)})
    assert r.status_code != 413


def test_an_upload_without_a_declared_length_is_411(client):
    """The multipart body is spooled before auth runs: no length, no cap, no entry."""
    r = client.post("/api/repos/upload", content=iter([b"x", b"y"]))
    assert r.status_code == 411
