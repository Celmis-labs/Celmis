"""A version arrives as one zip or as loose files, is checked before it is kept,
and is read back as a diff, an item or a download.

The contract (docs/CODEBASE.md, "Uploads"):

  * `POST /api/codebase/packs/{id}/versions` takes ONE zip (`archive`) or a list
    of `files`, never both and never neither; a maintainer is asked BEFORE a
    single byte of the body is read, so a reader's upload is refused and parsed
    by nobody;
  * an upload the checks refuse is `422 {"code","message","hits"}` where each hit
    is `{path, line, label}` — the place, never the value — and nothing is kept;
  * a good upload becomes the current version; an older one can be made current
    again; the diff, the item and the archive of a version are what that version
    held, byte for byte, and are as hidden as the pack itself.
"""

from __future__ import annotations

import io
import secrets
import zipfile

from tests.api.codebase_support import call, note_zip, upload_zip, zip_of
from tests.api.rbac_world import world

SKILL = "---\nname: billing\ndescription: How invoices are raised\n---\n# Billing\n\nRaise it.\n"


async def _pack(w, who="editor_a", slug="mine", audience="workspace") -> str:
    r = await call(w, who, "POST", "/api/codebase/packs",
                   json={"slug": slug, "title": "Mine", "audience": audience})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_a_zip_becomes_the_current_version_and_is_listed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        data = zip_of({"README.md": "# Mine\n", "billing/SKILL.md": SKILL,
                       "notes/why.md": "# Why\n\nBecause.\n"})
        r = await upload_zip(w, "editor_a", pid, data, note="first")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["version"]["number"] == 1 and body["version"]["current"] is True
        assert body["version"]["note"] == "first" and body["skills"] == ["billing"]
        listed = await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}/versions")
        assert [(v["number"], v["current"]) for v in listed.json()["versions"]] == [(1, True)]
        detail = (await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}")).json()
        assert detail["current_version"]["number"] == 1
        assert {i["path"] for i in detail["items"]} >= {"billing/SKILL.md", "notes/why.md"}


async def test_loose_files_are_named_by_their_filename_or_by_paths(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions",
                       files=[("files", ("README.md", b"# Mine\n", "text/markdown")),
                              ("files", ("notes/a.md", b"# A\n\nalpha\n", "text/markdown"))])
        assert r.status_code == 201, r.text
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions",
                       files=[("files", ("blob1", b"# Mine\n", "text/markdown")),
                              ("files", ("blob2", b"# B\n\nbeta\n", "text/markdown"))],
                       data={"paths": ["README.md", "notes/b.md"]})
        assert r.status_code == 201, r.text
        detail = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).json()
        assert detail["current_version"]["number"] == 2
        assert "notes/b.md" in {i["path"] for i in detail["items"]}


async def test_neither_both_nor_mismatched_parts_are_a_clear_422_and_keep_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        url = f"/api/codebase/packs/{pid}/versions"
        both = await call(w, "editor_a", "POST", url, data={"note": "x"}, files=[
            ("archive", ("p.zip", note_zip("x"), "application/zip")),
            ("files", ("README.md", b"# x\n", "text/markdown"))])
        none = await call(w, "editor_a", "POST", url, data={"note": "x"})
        skew = await call(w, "editor_a", "POST", url, data={"paths": ["a.md", "b.md"]},
                          files=[("files", ("a.md", b"# a\n", "text/markdown"))])
        for r in (both, none, skew):
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["code"] == "bad_upload" and r.json()["detail"]["hits"] == []
        assert (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
                ).json()["versions"] == []


async def test_a_refused_upload_names_the_place_of_a_secret_and_never_its_value(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        canary = secrets.token_urlsafe(14)
        data = zip_of({"README.md": "# Mine\n",
                       "api/setup.md": f"# Setup\n\nCall it like this:\n\npassword = '{canary}'\n"})
        r = await upload_zip(w, "editor_a", pid, data)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "secrets_found" and detail["hits"]
        assert all(set(h) == {"path", "line", "label"} and h["path"] == "api/setup.md"
                   for h in detail["hits"]), detail["hits"]
        assert canary not in r.text
        assert (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
                ).json()["versions"] == []
        assert not [a for a in w.audit if a["action"] == "codebase.version_uploaded"]


async def test_hostile_archives_are_refused_without_a_stack_trace(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        escape = io.BytesIO()
        with zipfile.ZipFile(escape, "w") as zf:
            zf.writestr("README.md", "# x\n")
            zf.writestr("../../etc/passwd", "root")
        for blob in (b"this is not a zip", b"", escape.getvalue()):
            r = await upload_zip(w, "editor_a", pid, blob)
            assert r.status_code == 422, (blob[:12], r.status_code, r.text[:200])
            assert r.json()["detail"]["code"]
            assert "Traceback" not in r.text and "File \"" not in r.text
        assert (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
                ).json()["versions"] == []


async def test_a_reader_is_refused_before_the_body_is_read(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        import src.knowledge.ingest as ingest

        def never(*a, **k):
            raise AssertionError("the body of a non-maintainer's upload was parsed")

        monkeypatch.setattr(ingest, "ingest_zip", never)
        monkeypatch.setattr(ingest, "ingest_files", never)
        for who in ("member_a", "viewer_a"):
            r = await upload_zip(w, who, pid, note_zip("zz"))
            assert (r.status_code, r.content) == (403, b'{"detail":"Requires a pack maintainer"}')
        # nor does a stranger learn the pack exists
        r = await upload_zip(w, "member_b", pid, note_zip("zz"), ws="ws-b")
        assert r.status_code == 404


async def test_an_older_version_can_be_made_current_again_and_diffs_say_what_changed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        v1 = {"README.md": "# Mine\n", "notes/keep.md": "# Keep\n\nsame\n", "notes/gone.md": "# Gone\n\nold\n"}
        v2 = {"README.md": "# Mine\n", "notes/keep.md": "# Keep\n\nchanged\n", "notes/new.md": "# New\n\nfresh\n"}
        assert (await upload_zip(w, "editor_a", pid, zip_of(v1))).status_code == 201
        assert (await upload_zip(w, "editor_a", pid, zip_of(v2))).status_code == 201
        base = f"/api/codebase/packs/{pid}"

        diff = (await call(w, "member_a", "GET", f"{base}/versions/2/diff")).json()
        assert diff == {"added": ["notes/new.md"], "removed": ["notes/gone.md"],
                        "changed": ["notes/keep.md"]}
        same = (await call(w, "member_a", "GET", f"{base}/versions/2/diff?against=2")).json()
        assert same == {"added": [], "removed": [], "changed": []}

        # making v1 current again is a maintainer's act
        denied = await call(w, "member_a", "POST", f"{base}/versions/1/activate")
        assert denied.status_code == 403
        ok = await call(w, "editor_a", "POST", f"{base}/versions/1/activate")
        assert ok.status_code == 200 and ok.json()["current_version"]["number"] == 1
        paths = {i["path"] for i in (await call(w, "member_a", "GET", base)).json()["items"]}
        assert "notes/gone.md" in paths and "notes/new.md" not in paths

        for bad in ("/versions/9/activate",):
            r = await call(w, "editor_a", "POST", base + bad)
            assert r.status_code == 404
        for bad in ("/versions/9/diff", "/versions/1/diff?against=9", "/versions/9/archive"):
            assert (await call(w, "editor_a", "GET", base + bad)).status_code == 404
        assert (await call(w, "editor_a", "GET", f"{base}/versions/0/diff")).status_code == 422


async def test_an_item_and_an_archive_hold_exactly_what_was_uploaded(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        files = {"README.md": "# Mine\n", "billing/SKILL.md": SKILL, "notes/why.md": "# Why\n\nBecause.\n"}
        assert (await upload_zip(w, "editor_a", pid, zip_of(files))).status_code == 201
        base = f"/api/codebase/packs/{pid}"
        items = (await call(w, "member_a", "GET", base)).json()["items"]
        why = next(i for i in items if i["path"] == "notes/why.md")
        got = (await call(w, "member_a", "GET", f"{base}/items/{why['id']}")).json()
        assert got["content"] == files["notes/why.md"] and got["version"] == 1
        assert got["sha"] == why["sha"] and got["kind"] == "note"

        for bad in ("nope", "00000000-0000-4000-8000-000000000000", B_ITEM):
            r = await call(w, "member_a", "GET", f"{base}/items/{bad}")
            assert r.status_code == 404, bad

        arch = await call(w, "member_a", "GET", f"{base}/versions/1/archive")
        assert arch.status_code == 200 and arch.headers["content-type"] == "application/zip"
        assert arch.headers["content-disposition"] == 'attachment; filename="mine-v1.zip"'
        with zipfile.ZipFile(io.BytesIO(arch.content)) as zf:
            assert {n: zf.read(n).decode() for n in zf.namelist()} == files
        again = await call(w, "member_a", "GET", f"{base}/versions/1/archive")
        assert again.content == arch.content                       # deterministic


B_ITEM = "bbbbbbbb-0000-4000-8000-000000000003"


async def test_a_hidden_packs_items_diffs_and_archives_are_not_readable(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, slug="quiet", audience="restricted")
        assert (await upload_zip(w, "editor_a", pid, note_zip("quiet"))).status_code == 201
        item = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).json()["items"][0]["id"]
        for who in ("member_a", "admin_a", "gadmin"):
            for path in (f"/items/{item}", "/versions/1/diff", "/versions/1/archive", "/versions"):
                r = await call(w, who, "GET", f"/api/codebase/packs/{pid}{path}")
                assert (r.status_code, r.content) == (404, b'{"detail":"pack not found or not accessible"}')
                assert b"quiet" not in r.content
