"""A line accepted only because of `celmis:allow-secret` is reported, never hidden.

The contract (docs/CODEBASE.md "Secrets in skills"):

  * the pack page's upload and the publish upload of a pack token both answer with
    `allowed: [{"path", "line", "rule"}]` - where and which rule, never the text -
    and the audit row of the upload carries `allowed_count`;
  * a dry run reports the same lines and stores nothing;
  * an upload without a marked line answers `allowed: []` and `allowed_count: 0`;
  * the placeholders the scan waves through are not marked lines: they are not
    reported, and an unmarked real secret beside them is still a 422.
"""

from __future__ import annotations

from tests.api.codebase_support import call, upload_zip, zip_of
from tests.api.rbac_world import world
from tests.api.test_a_pack_token_publishes_one_pack_and_nothing_else import (
    bearer,
    make_pack,
    make_token,
)

# A generic value with the shape of a generated secret.
REAL = "aB3dE6gH9jK2mN5pQ8sT"
MARKED = f"Do not do this:\n\npassword = '{REAL}'  # celmis:allow-secret\n"


async def _pack(w) -> str:
    r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                   json={"slug": "mine", "title": "Mine", "audience": "workspace"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload_rows(w) -> list[dict]:
    return [a for a in w.audit if a["action"] == "codebase.version_uploaded"]


async def test_the_pack_page_upload_lists_a_marked_line_by_place_and_rule_and_counts_it_in_the_audit(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        r = await upload_zip(w, "editor_a", pid, zip_of({"README.md": "# Mine\n", "notes/bad.md": MARKED}))
        assert r.status_code == 201, r.text
        allowed = r.json()["allowed"]
        assert [(a["path"], a["line"]) for a in allowed] == [("notes/bad.md", 3)]
        assert all(set(a) == {"path", "line", "rule"} and a["rule"] for a in allowed)
        assert REAL not in r.text
        assert [row["detail"]["allowed_count"] for row in _upload_rows(w)] == [1]
        assert REAL not in repr(w.audit)


async def test_an_upload_without_a_marked_line_has_an_empty_list_and_a_zero_count(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        r = await upload_zip(w, "editor_a", pid, zip_of({
            "README.md": "# Mine\n\nexport SHOP_TOKEN=<paste your token here>\n"}))
        assert r.status_code == 201, r.text
        assert r.json()["allowed"] == []
        assert [row["detail"]["allowed_count"] for row in _upload_rows(w)] == [0]


async def test_a_real_secret_beside_a_placeholder_is_still_a_422_naming_only_its_line(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        text = f"export SHOP_API_KEY=<paste your key here>\nexport SHOP_TOKEN={REAL}\n"
        r = await upload_zip(w, "editor_a", pid, zip_of({"README.md": "# Mine\n", "setup.md": text}))
        assert r.status_code == 422, r.text
        hits = r.json()["detail"]["hits"]
        assert [(h["path"], h["line"]) for h in hits] == [("setup.md", 2)]
        assert REAL not in r.text


async def test_the_publish_upload_reports_marked_lines_in_a_dry_run_and_in_the_real_push(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        token, _info = await make_token(w, pid)
        archive = zip_of({"README.md": "# Ledger\n", "notes/bad.md": MARKED})
        files = {"archive": ("pack.zip", archive, "application/zip")}
        dry = await w.client.post("/api/codebase/publish", headers=bearer(token), files=files,
                                  data={"dry_run": "true"})
        assert dry.status_code == 200 and dry.json()["dry_run"] is True
        assert [(a["path"], a["line"]) for a in dry.json()["allowed"]] == [("notes/bad.md", 3)]
        assert not _upload_rows(w)
        pushed = await w.client.post("/api/codebase/publish", headers=bearer(token), files=files)
        assert pushed.status_code == 201, pushed.text
        assert [(a["path"], a["line"]) for a in pushed.json()["allowed"]] == [("notes/bad.md", 3)]
        assert REAL not in pushed.text and REAL not in dry.text
        rows = _upload_rows(w)
        assert [row["detail"]["allowed_count"] for row in rows] == [1] and rows[0]["detail"]["token_id"]
        again = await w.client.post("/api/codebase/publish", headers=bearer(token), files=files)
        assert again.status_code == 200 and again.json()["unchanged"] is True
        assert [(a["path"], a["line"]) for a in again.json()["allowed"]] == [("notes/bad.md", 3)]
