"""Chunked archive upload: sessions, numbered parts, complete, abort, expiry.

Behind a proxy that caps request bodies (for example ~100 MB) an archive cannot
travel in one request, so the superadmin opens a session and sends parts. The
role tests are the point: every session route is the superadmin's alone.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile

import pytest

from tests.api.rbac_world import world

CHUNK = 2048
DENIED = ["gadmin", "owner_a", "admin_a", "editor_a", "member_a", "viewer_a", "loner"]


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("legacy-code/Модуль.bsl", "Процедура ОбработатьДокумент()\nКонецПроцедуры\n")
        zf.writestr("legacy-code/blob.bin", os.urandom(3 * CHUNK + 100))
    return buf.getvalue()


@pytest.fixture
async def cu(tmp_path, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_UPLOAD_CHUNK_BYTES", str(CHUNK))
    import src.api.routers.repos as repos_router
    import src.repos.indexing as indexing

    monkeypatch.setattr(indexing, "queue_index_if_needed",
                        lambda slug, **kw: indexing.INDEX_QUEUED)
    monkeypatch.setattr(repos_router, "record_action", lambda **kw: None)
    async with world(tmp_path, monkeypatch) as w:
        w.root = get_settings().repos_dir
        w.uploads = get_settings().workspace_dir / "uploads"
        w.data = _zip_bytes()
        yield w


async def _open(w, who="su", *, data=None, **over):
    data = w.data if data is None else data
    body = {"slug": "legacy-code", "name": "Legacy", "filename": "code.zip",
            "size": len(data), **over}
    return await w.client.post("/api/repos/upload/sessions", json=body, headers=w.h(who, "ws-a"))


def _part(data: bytes, session: dict, n: int) -> bytes:
    size = session["chunk_size"]
    return data[n * size:(n + 1) * size]


async def _put(w, sid, n, body, who="su"):
    return await w.client.put(f"/api/repos/upload/sessions/{sid}/parts/{n}", content=body,
                              headers=w.h(who, "ws-a"))


async def _complete(w, sid, who="su"):
    return await w.client.post(f"/api/repos/upload/sessions/{sid}/complete",
                               headers=w.h(who, "ws-a"))


async def _send_all(w, session, data, order=None):
    for n in order or range(session["parts"]):
        r = await _put(w, session["session_id"], n, _part(data, session, n))
        assert r.status_code == 204, r.text


async def test_the_happy_path_joins_the_parts_and_installs_the_repo(cu):
    r = await _open(cu, sha256=hashlib.sha256(cu.data).hexdigest())
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["chunk_size"] == CHUNK and s["parts"] == -(-len(cu.data) // CHUNK) > 2
    await _send_all(cu, s, cu.data)
    done = await _complete(cu, s["session_id"])
    assert done.status_code == 201, done.text
    assert done.json()["provider"] == "upload" and done.json()["display_name"] == "Legacy"
    assert (cu.root / "legacy-code" / "Модуль.bsl").is_file()
    assert not (cu.uploads / s["session_id"]).exists(), "temp files are gone"


async def test_parts_may_arrive_out_of_order_and_twice(cu):
    s = (await _open(cu)).json()
    order = list(reversed(range(s["parts"])))
    await _send_all(cu, s, cu.data, order)
    # a retry of a part that already arrived replaces it, harmlessly
    assert (await _put(cu, s["session_id"], 1, _part(cu.data, s, 1))).status_code == 204
    got = (await cu.client.get(f"/api/repos/upload/sessions/{s['session_id']}",
                               headers=cu.h("su", "ws-a"))).json()
    assert got["received"] == list(range(s["parts"]))
    assert (await _complete(cu, s["session_id"])).status_code == 201


async def test_completing_with_a_part_missing_is_refused_and_can_resume(cu):
    s = (await _open(cu)).json()
    await _send_all(cu, s, cu.data, [0, 2, 3])
    r = await _complete(cu, s["session_id"])
    assert r.status_code == 409 and "1" in r.json()["detail"]
    assert not (cu.root / "legacy-code").exists()
    await _send_all(cu, s, cu.data, [1])
    assert (await _complete(cu, s["session_id"])).status_code == 201


async def test_a_part_of_the_wrong_length_is_refused(cu):
    s = (await _open(cu)).json()
    short = await _put(cu, s["session_id"], 0, b"x" * (CHUNK - 1))
    assert short.status_code == 422
    long = await _put(cu, s["session_id"], 0, b"x" * (CHUNK + 1))
    assert long.status_code == 413
    assert (await _put(cu, s["session_id"], 99, b"x")).status_code == 422
    assert not list((cu.uploads / s["session_id"]).glob("part-*"))


async def test_a_declared_sha256_that_does_not_match_is_refused(cu):
    s = (await _open(cu, sha256="0" * 64)).json()
    await _send_all(cu, s, cu.data)
    r = await _complete(cu, s["session_id"])
    assert r.status_code == 422 and "sha256" in r.json()["detail"]
    assert not (cu.root / "legacy-code").exists()


async def test_a_declared_size_that_does_not_match_the_parts_is_refused(cu):
    s = (await _open(cu, size=len(cu.data) + 1)).json()  # one more byte declared
    for n in range(s["parts"]):
        body = cu.data[n * CHUNK:(n + 1) * CHUNK]
        r = await _put(cu, s["session_id"], n, body)
        if r.status_code != 204:  # the last part is now one byte too short
            assert r.status_code == 422
    assert (await _complete(cu, s["session_id"])).status_code == 409


async def test_everything_refusable_is_refused_before_any_byte(cu, monkeypatch):
    assert (await _open(cu, filename="code.rar")).status_code == 422
    assert (await _open(cu, slug="../x")).status_code == 422
    assert (await _open(cu, size=0)).status_code == 422
    monkeypatch.setenv("CELMIS_MAX_UPLOAD_BYTES", "1000")
    assert (await _open(cu)).status_code == 413
    monkeypatch.delenv("CELMIS_MAX_UPLOAD_BYTES")
    monkeypatch.setattr("shutil.disk_usage",
                        lambda p: type("U", (), {"free": len(cu.data) * 2 - 1})())
    low = await _open(cu)
    assert low.status_code == 507 and "disk" in low.json()["detail"].lower()


async def test_the_part_body_cap_is_the_chunk_not_the_archive():
    from src.api import middleware

    assert middleware._classify("/api/repos/upload/sessions/abc/parts/3") == "upload_part"
    assert middleware._classify("/api/repos/upload/sessions") == "default"
    assert middleware._classify("/api/repos/upload/sessions/abc/complete") == "default"
    caps = middleware._body_caps()
    assert caps["upload_part"] < 100 * 1000 * 1000
    assert caps["upload_part"] < caps["upload"]


async def test_abort_deletes_the_parts_and_the_session(cu):
    s = (await _open(cu)).json()
    await _send_all(cu, s, cu.data, [0, 1])
    assert (cu.uploads / s["session_id"]).is_dir()
    r = await cu.client.delete(f"/api/repos/upload/sessions/{s['session_id']}",
                               headers=cu.h("su", "ws-a"))
    assert r.status_code == 204 and not (cu.uploads / s["session_id"]).exists()
    assert (await _put(cu, s["session_id"], 0, _part(cu.data, s, 0))).status_code == 404


async def test_an_expired_session_is_refused_and_swept(cu):
    from src.repos import upload_sessions as us

    old = (await _open(cu)).json()
    meta = cu.uploads / old["session_id"] / "meta.json"
    import json

    m = json.loads(meta.read_text())
    m["expires_at"] = time.time() - 1
    meta.write_text(json.dumps(m))
    assert (await _put(cu, old["session_id"], 0, _part(cu.data, old, 0))).status_code == 404
    assert not (cu.uploads / old["session_id"]).exists()

    stale = (await _open(cu)).json()
    meta = cu.uploads / stale["session_id"] / "meta.json"
    m = json.loads(meta.read_text())
    m["expires_at"] = time.time() - 1
    meta.write_text(json.dumps(m))
    fresh = (await _open(cu)).json()  # opening a session sweeps the expired ones
    assert not (cu.uploads / stale["session_id"]).exists()
    assert (cu.uploads / fresh["session_id"]).is_dir()
    assert us.cleanup_expired() == 0


async def test_a_session_belongs_to_the_one_who_opened_it(cu):
    s = (await _open(cu)).json()
    r = await cu.client.get(f"/api/repos/upload/sessions/{s['session_id']}",
                            headers=cu.h("su", "ws-b"))
    assert r.status_code in (403, 404)
    assert (await cu.client.get("/api/repos/upload/sessions/not-an-id",
                                headers=cu.h("su", "ws-a"))).status_code == 404


@pytest.mark.parametrize("who", DENIED)
async def test_only_the_superadmin_touches_any_session_route(cu, who):
    s = (await _open(cu)).json()
    sid = s["session_id"]
    h = cu.h(who, "ws-a")
    assert (await _open(cu, who)).status_code == 403
    assert (await cu.client.get(f"/api/repos/upload/sessions/{sid}", headers=h)).status_code == 403
    assert (await _put(cu, sid, 0, _part(cu.data, s, 0), who)).status_code == 403
    assert (await _complete(cu, sid, who)).status_code == 403
    assert (await cu.client.delete(f"/api/repos/upload/sessions/{sid}", headers=h)).status_code == 403
    assert not list((cu.uploads / sid).glob("part-*"))
