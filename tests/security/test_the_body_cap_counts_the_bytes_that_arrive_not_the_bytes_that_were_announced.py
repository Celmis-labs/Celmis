"""The request-size cap holds for the bytes actually received, whatever the headers say.

The contract (src/api/middleware.py "Request size", docs/CODEBASE.md "Uploads"):

  * every body is counted as the application reads it; once the running total
    passes the limit of its path class the body is cut off and the answer is ONE
    413 — a missing, forged or small Content-Length changes nothing, and neither
    does a parser that would rather say 400;
  * the application is not fed the whole body: reading stops within one chunk of
    the cap;
  * the one-request archive upload and the pack upload also refuse
    Transfer-Encoding (411), so their declared length is the real one; a body
    under the cap, with or without a header that understates it, goes through;
  * routes that never carried a large body keep their small cap, and a request
    with no body, or a websocket, is untouched.

Driven with raw ASGI messages, because a test client cannot send a lying header.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, File, Request, UploadFile

from src.api import middleware

MB = 1024 * 1024
CHUNK = 64 * 1024
PACK_UPLOAD = "/api/codebase/packs/6b1f0c5e-0000-4000-8000-000000000001/versions"
CREATE_PACK = "/api/codebase/packs"
REPO_UPLOAD = "/api/repos/upload"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("CELMIS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("CELMIS_RL_UPLOAD", "1000")
    monkeypatch.setenv("CELMIS_RL_DEFAULT", "1000")
    monkeypatch.delenv("CELMIS_REDIS_URL", raising=False)
    monkeypatch.delenv("CELMIS_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("CELMIS_MAX_BODY_BYTES_LARGE", raising=False)
    api = FastAPI()
    api.add_middleware(middleware.RateLimitMiddleware)

    @api.post("/api/codebase/packs/{pack_id}/versions")
    async def upload(pack_id: str, archive: UploadFile = File(...)) -> dict:
        return {"size": len(await archive.read())}

    @api.post(REPO_UPLOAD)
    async def repo_upload(request: Request) -> dict:
        return {"size": len(await request.body())}

    @api.post(CREATE_PACK)
    async def create(request: Request) -> dict:
        return {"size": len(await request.body())}

    return api


def multipart(size: int) -> tuple[bytes, str]:
    boundary = "----testboundary"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"archive\"; "
            f"filename=\"pack.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + b"x" * size + tail, f"multipart/form-data; boundary={boundary}"


class Wire:
    """What a server hands the application, and what the application sends back."""

    def __init__(self, body: bytes, chunk: int = CHUNK):
        self.chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]
        self.delivered = 0
        self.sent: list[dict] = []

    async def receive(self):
        if not self.chunks:
            return {"type": "http.disconnect"}
        body = self.chunks.pop(0)
        self.delivered += len(body)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def send(self, message):
        self.sent.append(message)

    @property
    def status(self) -> int | None:
        starts = [m["status"] for m in self.sent if m["type"] == "http.response.start"]
        assert len(starts) <= 1, f"{len(starts)} responses were started"
        return starts[0] if starts else None

    @property
    def text(self) -> str:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body").decode()


async def post(app, path, body, *, headers=(), method="POST", chunk=CHUNK) -> Wire:
    wire = Wire(body, chunk)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "server": ("test", 80), "client": ("203.0.113.9", 5000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    await app(scope, wire.receive, wire.send)
    return wire


def lying(length: int, content_type: str = "application/octet-stream"):
    return [("content-length", str(length)), ("content-type", content_type)]


async def test_a_pack_upload_that_announces_ten_bytes_and_sends_twelve_megabytes_is_413(app):
    body, ctype = multipart(12 * MB)
    wire = await post(app, PACK_UPLOAD, body, headers=lying(10, ctype))
    assert wire.status == 413
    assert "too large" in wire.text and str(5 * MB) in wire.text


async def test_the_application_is_fed_no_more_than_one_chunk_past_the_cap(app):
    body, ctype = multipart(12 * MB)
    wire = await post(app, PACK_UPLOAD, body, headers=lying(10, ctype))
    assert wire.status == 413
    assert wire.delivered <= 5 * MB + CHUNK


async def test_one_413_is_sent_and_nothing_follows_it(app):
    body, ctype = multipart(7 * MB)
    wire = await post(app, PACK_UPLOAD, body, headers=lying(10, ctype))
    kinds = [m["type"] for m in wire.sent]
    assert kinds == ["http.response.start", "http.response.body"]
    assert json.loads(wire.text)["detail"].startswith("request body too large")


async def test_a_body_with_no_length_on_a_small_route_is_cut_at_one_megabyte(app):
    wire = await post(app, CREATE_PACK, b"x" * (3 * MB), headers=[])
    assert wire.status == 413
    assert wire.delivered <= 1 * MB + CHUNK


async def test_a_chunked_body_on_a_small_route_is_cut_at_one_megabyte(app):
    wire = await post(app, CREATE_PACK, b"x" * (3 * MB), headers=[("transfer-encoding", "chunked")])
    assert wire.status == 413


async def test_a_lying_small_length_on_a_small_route_is_cut_at_one_megabyte(app):
    wire = await post(app, CREATE_PACK, b"x" * (3 * MB), headers=lying(10))
    assert wire.status == 413


async def test_a_body_under_the_cap_goes_through_even_when_the_header_understates_it(app):
    wire = await post(app, CREATE_PACK, b"x" * (MB // 2), headers=lying(10))
    assert wire.status == 200 and json.loads(wire.text) == {"size": MB // 2}


async def test_a_body_of_exactly_the_cap_goes_through_and_one_byte_more_does_not(app):
    assert (await post(app, CREATE_PACK, b"x" * MB, headers=lying(10))).status == 200
    assert (await post(app, CREATE_PACK, b"x" * (MB + 1), headers=lying(10))).status == 413


async def test_an_honest_pack_upload_under_the_cap_reaches_the_route(app):
    body, ctype = multipart(2 * MB)
    wire = await post(app, PACK_UPLOAD, body, headers=lying(len(body), ctype))
    assert wire.status == 200 and json.loads(wire.text) == {"size": 2 * MB}


@pytest.mark.parametrize("path", [PACK_UPLOAD, REPO_UPLOAD, "/api/codebase/publish"])
async def test_an_upload_that_is_chunked_is_refused_with_411_even_beside_a_length(app, path):
    headers = [("content-length", "10"), ("transfer-encoding", "chunked")]
    wire = await post(app, path, b"x" * (6 * MB), headers=headers)
    assert wire.status == 411
    assert wire.delivered == 0                              # refused before a byte was read
    assert "Transfer-Encoding" in wire.text


async def test_an_upload_with_no_length_and_no_encoding_is_still_411(app):
    assert (await post(app, PACK_UPLOAD, b"x", headers=[])).status == 411


async def test_a_small_route_may_still_be_chunked_under_its_cap(app):
    wire = await post(app, CREATE_PACK, b"x" * 1000, headers=[("transfer-encoding", "chunked")])
    assert wire.status == 200


async def test_the_repository_archive_upload_keeps_its_own_much_larger_cap(app):
    wire = await post(app, REPO_UPLOAD, b"x" * (6 * MB), headers=lying(6 * MB))
    assert wire.status == 200 and json.loads(wire.text) == {"size": 6 * MB}


async def test_a_request_without_a_body_is_untouched(app):
    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True}

    wire = await post(app, "/api/ping", b"", method="GET")
    assert wire.status == 200 and json.loads(wire.text) == {"ok": True}


async def test_nothing_is_counted_when_the_limiter_is_switched_off(app, monkeypatch):
    monkeypatch.setenv("CELMIS_RATE_LIMIT_ENABLED", "0")
    api = FastAPI()
    api.add_middleware(middleware.RateLimitMiddleware)

    @api.post(CREATE_PACK)
    async def create(request: Request) -> dict:
        return {"size": len(await request.body())}

    wire = await post(api, CREATE_PACK, b"x" * (3 * MB), headers=lying(10))
    assert wire.status == 200
