"""The pack limits are read from, and written to, the workspace.

The contract (docs/CODEBASE.md "Changing the limits", src/knowledge/limits.py):

  * `GET /api/codebase/limits` answers to any member: the limits in force, where each
    comes from (workspace | install | builtin), the overrides and the ceilings;
  * `PUT` is for owners and admins; anyone else is 403; a value above its ceiling, a
    zero or an unknown field is 422 and nothing is stored; null inherits;
  * the install default (`CELMIS_PACK_*`) is the layer under the workspace's own, and
    is itself held to the ceiling;
  * an upload is held to the limits of ITS workspace: over the limit it is refused with
    the code of the limit, under a raised limit it is kept, and another workspace is
    unaffected;
  * packs per workspace and versions kept follow the workspace's values.
"""

from __future__ import annotations

import hashlib

from tests.api.codebase_support import call, upload_zip, zip_of
from tests.api.rbac_world import world

BIG = "x" * 700 * 1024            # a file over the built-in 512 KB


async def _pack(w, slug="p", who="editor_a", ws="ws-a"):
    r = await call(w, who, "POST", "/api/codebase/packs", ws,
                   json={"slug": slug, "title": slug, "audience": "workspace"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _limits(w, who="member_a", ws="ws-a"):
    r = await call(w, who, "GET", "/api/codebase/limits", ws)
    assert r.status_code == 200, r.text
    return r.json()


async def test_every_member_reads_the_limits_in_force_with_their_source_and_the_ceilings(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        body = await _limits(w, "viewer_a")
        assert body["limits"] == {
            "archive_bytes": 5 * 1024 * 1024, "max_files": 400, "file_bytes": 512 * 1024,
            "total_bytes": 8 * 1024 * 1024, "packs_per_workspace": 200, "keep_versions": 20}
        assert set(body["sources"].values()) == {"builtin"}
        assert body["ceilings"]["archive_bytes"] == 5 * 1024 * 1024
        assert body["ceilings"]["max_files"] == 5000
        assert body["can_edit"] is False
        assert (await _limits(w, "admin_a"))["can_edit"] is True


async def test_an_install_default_sits_under_the_workspace_value_and_under_the_ceiling(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_MAX_FILES", "900")
    monkeypatch.setenv("CELMIS_PACK_MAX_FILE_BYTES", str(10 * 1024 * 1024 * 1024))   # silly
    async with world(tmp_path, monkeypatch) as w:
        body = await _limits(w)
        assert body["limits"]["max_files"] == 900 and body["sources"]["max_files"] == "install"
        assert body["limits"]["file_bytes"] == 4 * 1024 * 1024               # held to the ceiling
        r = await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": 1200})
        assert r.status_code == 200, r.text
        assert r.json()["limits"]["max_files"] == 1200
        assert r.json()["sources"]["max_files"] == "workspace"
        assert r.json()["defaults"]["max_files"] == 900


async def test_a_member_who_is_not_an_admin_cannot_change_the_limits(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        for who in ("editor_a", "member_a", "viewer_a"):
            r = await call(w, who, "PUT", "/api/codebase/limits", json={"max_files": 500})
            assert r.status_code == 403, (who, r.text)
        assert (await _limits(w))["limits"]["max_files"] == 400


async def test_a_value_above_its_ceiling_or_below_one_is_refused_and_nothing_is_stored(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        for body in ({"max_files": 5001}, {"file_bytes": 4 * 1024 * 1024 + 1},
                     {"archive_bytes": 5 * 1024 * 1024 + 1}, {"keep_versions": 101},
                     {"packs_per_workspace": 2001}, {"total_bytes": 200 * 1024 * 1024 + 1},
                     {"max_files": 0}, {"nonsense": 1}):
            r = await call(w, "admin_a", "PUT", "/api/codebase/limits", json=body)
            assert r.status_code == 422, (body, r.text)
        r = await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": 5000})
        assert r.status_code == 200, r.text
        assert (await _limits(w))["limits"]["max_files"] == 5000


async def test_null_gives_the_value_back_to_the_install(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": 50})
        assert (await _limits(w))["sources"]["max_files"] == "workspace"
        r = await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": None})
        assert r.json()["sources"]["max_files"] == "builtin" and r.json()["limits"]["max_files"] == 400


async def test_an_upload_over_the_workspace_limit_is_refused_and_one_under_a_raised_limit_is_kept(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        data = zip_of({"README.md": "# p\n", "notes/big.md": "# Big\n\n" + BIG})
        r = await upload_zip(w, "editor_a", pid, data)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "file_too_large", r.text
        r = await call(w, "admin_a", "PUT", "/api/codebase/limits",
                       json={"file_bytes": 1024 * 1024})
        assert r.status_code == 200, r.text
        r = await upload_zip(w, "editor_a", pid, data)
        assert r.status_code == 201, r.text


async def test_a_lowered_limit_refuses_what_the_built_in_one_allowed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": 2})
        three = zip_of({"README.md": "# p\n", "a.md": "# a\n", "b.md": "# b\n"})
        r = await upload_zip(w, "editor_a", pid, three)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_files", r.text
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"archive_bytes": 200})
        noise = "\n".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(40))
        r = await upload_zip(w, "editor_a", pid, zip_of({"README.md": noise}))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "archive_too_large", r.text


async def test_another_workspace_keeps_its_own_limits(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"max_files": 7})
        assert (await _limits(w, "admin_b", "ws-b"))["limits"]["max_files"] == 400


async def test_a_workspace_holds_as_many_packs_as_its_limit_says(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"packs_per_workspace": 2})
        await _pack(w, "one")
        await _pack(w, "two")
        r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                       json={"slug": "three", "title": "three", "audience": "workspace"})
        assert r.status_code == 409 and "at most 2 packs" in r.text, r.text


async def test_a_pack_keeps_as_many_versions_as_the_workspace_limit_says(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await call(w, "admin_a", "PUT", "/api/codebase/limits", json={"keep_versions": 2})
        pid = await _pack(w)
        for n in range(4):
            r = await upload_zip(w, "editor_a", pid, zip_of({"README.md": f"# v{n}\n"}))
            assert r.status_code == 201, r.text
        r = await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
        assert sorted(v["number"] for v in r.json()["versions"]) == [3, 4]
