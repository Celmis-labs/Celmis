"""An upload is checked in a worker thread, and one that breaks the checker is a 400.

The contract (src/api/routers/codebase.py `_ingest_upload`, docs/CODEBASE.md "Upload"):

  * unzipping, scanning and parsing a version run in a worker thread
    (`asyncio.to_thread`), so a large archive does not stall every other request
    on the event loop — for the pack page and for a publish token alike;
  * an upload the checker refuses on purpose stays the 422 it was
    (`ingest.IngestError`, with its code); any OTHER exception from the checker
    is the last resort: HTTP 400, code `bad_archive`, one fixed sentence, and
    nothing of the exception — no type, no message, no path;
  * nothing is stored for either.
"""

from __future__ import annotations

import threading

from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world

FIXED = {"code": "bad_archive", "message": "The upload could not be read as a pack.", "hits": []}


async def _pack(w, who="editor_a"):
    r = await call(w, who, "POST", "/api/codebase/packs", json={
        "slug": "mine", "title": "Mine", "audience": "workspace"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _versions(w, pid) -> list:
    return (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
            ).json()["versions"]


async def test_an_unexpected_failure_of_the_checker_is_a_400_that_says_nothing_about_it(
        tmp_path, monkeypatch):
    import src.knowledge.ingest as ingest

    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)

        def broken(*_a, **_k):
            raise ValueError("offset 17 in /srv/private/dir/archive-7f3c.zip is skewed")

        monkeypatch.setattr(ingest, "ingest_zip", broken)
        monkeypatch.setattr(ingest, "ingest_files", broken)
        r = await upload_zip(w, "editor_a", pid, note_zip("zz"))
        assert (r.status_code, r.json()["detail"]) == (400, FIXED)
        for fragment in ("ValueError", "offset", "/srv", "archive-7f3c", "Traceback"):
            assert fragment not in r.text, fragment
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions",
                       files=[("files", ("a.md", b"# a\n", "text/markdown"))])
        assert (r.status_code, r.json()["detail"]) == (400, FIXED)
        assert await _versions(w, pid) == []


async def test_a_refusal_on_purpose_is_still_a_422_with_its_code(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        r = await upload_zip(w, "editor_a", pid, b"this is not a zip")
        assert r.status_code == 422 and r.json()["detail"]["code"]
        assert r.json()["detail"]["message"] != FIXED["message"]      # its own reason, not the fixed one
        assert await _versions(w, pid) == []


async def test_the_checker_runs_in_a_worker_thread_not_on_the_event_loop(tmp_path, monkeypatch):
    import src.knowledge.ingest as ingest

    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_zip, real_files = ingest.ingest_zip, ingest.ingest_files

        def watch_zip(*a, **k):
            seen.append(threading.get_ident())
            return real_zip(*a, **k)

        def watch_files(*a, **k):
            seen.append(threading.get_ident())
            return real_files(*a, **k)

        monkeypatch.setattr(ingest, "ingest_zip", watch_zip)
        monkeypatch.setattr(ingest, "ingest_files", watch_files)
        r = await upload_zip(w, "editor_a", pid, note_zip("zz"))
        assert r.status_code == 201, r.text
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions",
                       files=[("files", ("b.md", b"# b\n", "text/markdown"))])
        assert r.status_code == 201, r.text
        assert len(seen) == 2 and loop_thread not in seen
