"""A knowledge-pack upload may carry 5 MB, must declare its length, and is rate limited.

The contract (src/api/middleware.py, docs/CODEBASE.md "Uploads"):

  * only `POST /api/codebase/packs/{id}/versions` and `POST /api/codebase/publish`
    (a pack token's upload) are the pack-upload class; the GETs of the publish
    routes (`/api/codebase/publish`, `/api/codebase/publish/archive`: status and
    pull) are ordinary reads of the default class, so a pipeline that checks status
    around its pushes is not locked out by them; every other method and path stays
    in the class it was in;
  * its body cap is the LARGE cap (5 MB) — the multipart body is spooled before
    any gate runs, so the cap is the only thing between an anonymous client and
    the disk; a longer declared body is 413 and a missing length is 411;
  * it shares the archive upload's request rate, so a loop of uploads is slowed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import middleware

URL = "/api/codebase/packs/6b1f0c5e-0000-4000-8000-000000000001/versions"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CELMIS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.delenv("CELMIS_REDIS_URL", raising=False)
    monkeypatch.delenv("CELMIS_MAX_BODY_BYTES_LARGE", raising=False)
    monkeypatch.setenv("CELMIS_RL_UPLOAD", "3")
    app = FastAPI()
    app.add_middleware(middleware.RateLimitMiddleware)

    @app.post("/api/codebase/packs/{pack_id}/versions")
    def upload(pack_id: str) -> dict:
        return {"ok": True}

    @app.post("/api/codebase/packs")
    def create() -> dict:
        return {"ok": True}

    @app.api_route("/api/codebase/publish", methods=["GET", "POST"])
    @app.api_route("/api/codebase/publish/archive", methods=["GET"])
    def publish() -> dict:
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize("path,method,expected", [
    ("/api/codebase/packs/abc/versions", "POST", "pack_upload"),
    ("/api/codebase/packs/abc/versions", "GET", "default"),
    ("/api/codebase/packs/abc/versions", "", "default"),
    ("/api/codebase/packs/abc/versions/1/activate", "POST", "default"),
    ("/api/codebase/packs/abc/versions/extra/x", "POST", "default"),
    ("/api/codebase/packs//versions", "POST", "default"),
    ("/api/codebase/packs", "POST", "default"),
    ("/api/codebase/packs/a/b/versions", "POST", "default"),
    ("/api/codebase/brief", "POST", "default"),
    ("/api/repos/upload", "POST", "upload"),
    ("/api/codebase/publish", "POST", "pack_upload"),
    ("/api/codebase/publish", "GET", "default"),
    ("/api/codebase/publish/archive", "GET", "default"),
    ("/api/codebase/publish/archive", "POST", "default"),
    ("/api/codebase/publish/", "POST", "default"),
    ("/api/codebase/publish/other", "POST", "default"),
    ("/api/codebase/publish/archive/x", "GET", "default"),
    ("/api/codebase/publishing", "POST", "default"),
])
def test_only_the_versions_post_is_the_pack_upload_class(path, method, expected):
    assert middleware._classify(path, method) == expected


def test_the_cap_is_the_large_one_and_the_rate_is_the_upload_rate(monkeypatch):
    monkeypatch.delenv("CELMIS_MAX_BODY_BYTES_LARGE", raising=False)
    monkeypatch.setenv("CELMIS_RL_UPLOAD", "7")
    assert middleware._body_caps()["pack_upload"] == 5 * 1024 * 1024
    assert middleware._limits()["pack_upload"] == middleware._limits()["upload"] == 7


def test_a_declared_body_over_five_megabytes_is_413(client):
    r = client.post(URL, content=b"x", headers={"content-length": str(5 * 1024 * 1024 + 1)})
    assert r.status_code == 413


def test_a_body_under_the_cap_reaches_the_route(client):
    r = client.post(URL, content=b"x", headers={"content-length": str(5 * 1024 * 1024)})
    assert r.status_code == 200


def test_an_upload_without_a_declared_length_is_411(client):
    r = client.post(URL, content=iter([b"x", b"y"]))
    assert r.status_code == 411


def test_creating_a_pack_keeps_the_small_cap_and_needs_no_length(client):
    assert client.post("/api/codebase/packs", content=b"{}",
                       headers={"content-length": str(2 * 1024 * 1024)}).status_code == 413
    assert client.post("/api/codebase/packs", json={}).status_code == 200


def test_uploads_are_rate_limited(client):
    codes = [client.post(URL, content=b"x").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


PUBLISH = "/api/codebase/publish"


def test_the_publish_post_has_the_same_cap_and_the_same_demand_for_a_length(client):
    assert client.post(PUBLISH, content=b"x",
                       headers={"content-length": str(5 * 1024 * 1024 + 1)}).status_code == 413
    assert client.post(PUBLISH, content=b"x",
                       headers={"content-length": str(5 * 1024 * 1024)}).status_code == 200
    assert client.post(PUBLISH, content=iter([b"x", b"y"])).status_code == 411


def test_a_publish_read_needs_no_length_and_does_not_spend_the_upload_bucket(client):
    # the reads (status, pull) are far more than the upload budget of 3 and still answer ...
    assert [client.get(PUBLISH).status_code for _ in range(8)] == [200] * 8
    assert [client.get("/api/codebase/publish/archive").status_code for _ in range(4)] == [200] * 4
    # ... and they left the bucket the uploads and the publish POST share untouched
    assert [client.post(PUBLISH, content=b"x").status_code for _ in range(3)] == [200, 200, 200]
    assert client.post(PUBLISH, content=b"x").status_code == 429
    assert client.post(URL, content=b"x").status_code == 429


def test_a_refused_upload_names_the_seconds_to_wait(client):
    for _ in range(3):
        client.post(PUBLISH, content=b"x")
    refused = client.post(PUBLISH, content=b"x")
    assert refused.status_code == 429
    assert 0 < int(refused.headers["Retry-After"]) <= 60
