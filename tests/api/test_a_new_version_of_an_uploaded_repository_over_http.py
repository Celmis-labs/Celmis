"""Uploading the next version of an uploaded repository over HTTP.

Chunked upload as for the first one, then `complete` hands the archive to a job
(unpacking and indexing outlast a request) and the page polls the update. The
superadmin alone may do it; everybody else gets 403/404 and learns nothing; the
repository's identity, registry row and history survive; a bad archive is
reported and the old version stays.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.api.rbac_world import A_REPO, world

DENIED = ["gadmin", "owner_a", "admin_a", "editor_a", "member_a", "viewer_a", "loner"]
CHUNK = 2048


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


V1 = _zip({"shop/main.py": "def old_total(items):\n    return sum(items)\n",
           "shop/gone.py": "def gone():\n    return 1\n"})
V2 = _zip({"shop/main.py": "def new_total(items):\n    return sum(items) + 1\n"})


@pytest.fixture
async def vw(tmp_path, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_UPLOAD_CHUNK_BYTES", str(CHUNK))
    import src.api.routers.repos as repos_router
    import src.repos.indexing as indexing
    import src.sync.queue as queue

    jobs: list[dict] = []
    monkeypatch.setattr(indexing, "queue_index_if_needed",
                        lambda slug, **kw: indexing.INDEX_QUEUED)
    monkeypatch.setattr(repos_router, "record_action", lambda **kw: None)
    monkeypatch.setattr(queue, "enqueue", lambda **kw: jobs.append(kw) or f"job-{len(jobs)}")
    async with world(tmp_path, monkeypatch) as w:
        w.jobs = jobs
        w.settings = get_settings()
        # the repository, uploaded the ordinary way
        s = (await w.client.post("/api/repos/upload/sessions", headers=w.h("su", "ws-a"),
                                 json={"slug": "upload-shop", "name": "Shop",
                                       "filename": "code.zip", "size": len(V1)})).json()
        for n in range(s["parts"]):
            r = await w.client.put(
                f"/api/repos/upload/sessions/{s['session_id']}/parts/{n}",
                content=V1[n * s["chunk_size"]:(n + 1) * s["chunk_size"]],
                headers=w.h("su", "ws-a"))
            assert r.status_code == 204
        done = await w.client.post(f"/api/repos/upload/sessions/{s['session_id']}/complete",
                                   headers=w.h("su", "ws-a"))
        assert done.status_code == 201, done.text
        yield w


async def _send(w, data: bytes, who="su", slug="upload-shop", filename="next.zip"):
    r = await w.client.post(f"/api/repos/{slug}/versions/sessions", headers=w.h(who, "ws-a"),
                            json={"filename": filename, "size": len(data)})
    if r.status_code != 201:
        return r, None
    s = r.json()
    for n in range(s["parts"]):
        p = await w.client.put(
            f"/api/repos/upload/sessions/{s['session_id']}/parts/{n}",
            content=data[n * s["chunk_size"]:(n + 1) * s["chunk_size"]],
            headers=w.h(who, "ws-a"))
        assert p.status_code == 204
    return r, s


async def _apply(w, s, who="su", slug="upload-shop"):
    """complete + run the job the way the worker would."""
    from src.sync.handlers import handle_upload_version

    r = await w.client.post(f"/api/repos/{slug}/versions/sessions/{s['session_id']}/complete",
                            headers=w.h(who, "ws-a"))
    assert r.status_code == 202, r.text
    await handle_upload_version({"payload": w.jobs[-1]["payload"]})
    return (await w.client.get(f"/api/repos/{slug}/versions/updates/{r.json()['update_id']}",
                               headers=w.h("su", "ws-a"))).json()


async def test_a_new_version_is_applied_and_listed_with_what_changed(vw):
    before = (await vw.client.get("/api/repos", headers=vw.h("su", "ws-a"))).json()
    shop = next(r for r in before if r["slug"] == "upload-shop")
    assert shop["upload_version"] == 1

    _r, s = await _send(vw, V2)
    update = await _apply(vw, s)
    assert update["state"] == "done", update
    assert update["result"]["version"]["version"] == 2
    assert (update["result"]["removed"], update["result"]["changed"]) == (1, 1)
    assert not (vw.settings.repo_path("upload-shop") / "gone.py").exists()

    got = (await vw.client.get("/api/repos/upload-shop/versions", headers=vw.h("su", "ws-a"))).json()
    assert got["current"]["version"] == 2 and got["current"]["filename"] == "next.zip"
    assert [v["version"] for v in got["history"]] == [2, 1]
    after = next(r for r in (await vw.client.get("/api/repos", headers=vw.h("su", "ws-a"))).json()
                 if r["slug"] == "upload-shop")
    # same registry row, same name, new version number
    assert (after["provider"], after["full_name"], after["display_name"]) == (
        shop["provider"], shop["full_name"], "Shop")
    assert after["upload_version"] == 2


async def test_the_archive_already_served_is_refused_at_complete(vw):
    _r, s = await _send(vw, V2)
    assert (await _apply(vw, s))["state"] == "done"
    queued = len(vw.jobs)
    _r, s = await _send(vw, V2)
    r = await vw.client.post(
        f"/api/repos/upload-shop/versions/sessions/{s['session_id']}/complete",
        headers=vw.h("su", "ws-a"))
    assert r.status_code == 409, r.text
    assert len(vw.jobs) == queued


async def test_a_bad_archive_is_reported_and_the_old_version_stays(vw):
    _r, s = await _send(vw, b"not a zip at all" * 300)
    update = await _apply(vw, s)
    assert update["state"] == "failed" and update["error"]
    assert (vw.settings.repo_path("upload-shop") / "gone.py").is_file()
    got = (await vw.client.get("/api/repos/upload-shop/versions", headers=vw.h("su", "ws-a"))).json()
    assert got["current"]["version"] == 1


@pytest.mark.parametrize("who", DENIED)
async def test_nobody_but_the_superadmin_may_open_start_or_read(vw, who):
    r = await vw.client.post("/api/repos/upload-shop/versions/sessions", headers=vw.h(who, "ws-a"),
                             json={"filename": "n.zip", "size": 10})
    assert r.status_code in (403, 404), (who, r.status_code)
    r = await vw.client.get("/api/repos/upload-shop/versions", headers=vw.h(who, "ws-a"))
    assert r.status_code in (403, 404), (who, r.status_code)
    assert not vw.jobs


async def test_a_repository_from_a_git_host_has_no_versions_route(vw):
    r = await vw.client.post(f"/api/repos/{A_REPO}/versions/sessions", headers=vw.h("su", "ws-a"),
                             json={"filename": "n.zip", "size": 10})
    assert r.status_code == 404
    r = await vw.client.post("/api/repos/no-such-repo/versions/sessions",
                             headers=vw.h("su", "ws-a"),
                             json={"filename": "n.zip", "size": 10})
    assert r.status_code == 404


async def test_another_persons_session_and_a_first_upload_session_do_not_complete_here(vw):
    first = (await vw.client.post("/api/repos/upload/sessions", headers=vw.h("su", "ws-a"),
                                  json={"slug": "other", "filename": "a.zip",
                                        "size": len(V2)})).json()
    r = await vw.client.post(
        f"/api/repos/upload-shop/versions/sessions/{first['session_id']}/complete",
        headers=vw.h("su", "ws-a"))
    assert r.status_code == 404
    _r, s = await _send(vw, V2)
    r = await vw.client.post(f"/api/repos/upload/sessions/{s['session_id']}/complete",
                             headers=vw.h("su", "ws-a"))
    assert r.status_code == 404


async def test_an_update_status_is_not_readable_through_another_slug(vw):
    _r, s = await _send(vw, V2)
    r = await vw.client.post(f"/api/repos/upload-shop/versions/sessions/{s['session_id']}/complete",
                             headers=vw.h("su", "ws-a"))
    uid = r.json()["update_id"]
    other = await vw.client.get(f"/api/repos/{A_REPO}/versions/updates/{uid}",
                                headers=vw.h("su", "ws-a"))
    assert other.status_code == 404
