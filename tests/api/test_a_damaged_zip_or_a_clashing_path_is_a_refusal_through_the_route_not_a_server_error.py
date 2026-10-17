"""An upload the archive reader cannot make sense of is `422`, never `500`.

The contract (docs/CODEBASE.md "Uploads"): whatever is wrong with the archive —
offsets that point outside the file, a short header, a pack with a path that is
both a file and a folder — the route answers `422 {"code","message","hits"}` with
a fixed sentence that repeats nothing from the archive, keeps nothing, and logs
no error. Random corruption of a good archive ends the same way, never in an
exception.
"""

from __future__ import annotations

import random
import struct

from tests.api.codebase_support import call, upload_zip, zip_of
from tests.api.rbac_world import world

SKILL = "---\nname: demo\ndescription: demo skill\n---\n# demo\n\nbody\n"


def skewed() -> bytes:
    data = bytearray(zip_of({"demo/SKILL.md": SKILL, "README.md": "# hi\n"}))
    end = data.rfind(b"PK\x05\x06")
    (offset,) = struct.unpack_from("<I", data, end + 16)
    struct.pack_into("<I", data, end + 16, offset + 6)
    return bytes(data)


async def _pack(w) -> str:
    r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                   json={"slug": "mine", "title": "Mine", "audience": "workspace"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _versions(w, pid: str) -> list:
    return (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")).json()["versions"]


async def test_an_archive_with_skewed_offsets_is_a_422_and_keeps_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        r = await upload_zip(w, "editor_a", pid, skewed())
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "bad_archive" and detail["hits"] == []
        assert "demo" not in r.text and "Traceback" not in r.text
        assert await _versions(w, pid) == []


async def test_a_path_that_is_a_file_and_a_folder_is_a_422_and_keeps_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        data = zip_of({"README.md": "# hi\n", "notes/a.md": "# a\n", "notes/a.md/b.md": "# b\n"})
        r = await upload_zip(w, "editor_a", pid, data)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "duplicate_path"
        assert "both a file and a folder" in r.json()["detail"]["message"]
        assert await _versions(w, pid) == []
        loose = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions",
                           data={"paths": ["a.md", "a.md/b.md"]},
                           files=[("files", ("a.md", b"# a\n", "text/markdown")),
                                  ("files", ("b.md", b"# b\n", "text/markdown"))])
        assert loose.status_code == 422 and loose.json()["detail"]["code"] == "duplicate_path"


async def test_random_corruption_of_a_good_archive_is_never_a_server_error(tmp_path, monkeypatch):
    good = zip_of({"demo/SKILL.md": SKILL * 3, "demo/notes/a.md": "# a\n\ntext\n" * 30, "x.json": "{}"})
    rng = random.Random(11)
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        for _ in range(60):
            data = bytearray(good)
            for _step in range(rng.randint(1, 5)):
                at = rng.randrange(len(data))
                data[at] = rng.randrange(256)
            r = await upload_zip(w, "editor_a", pid, bytes(data))
            assert r.status_code in (201, 422), (r.status_code, r.text[:200])
