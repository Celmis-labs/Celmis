"""The item route filters the repository pointers the way the brief does.

The contract (docs/CODEBASE.md "Brief", src/api/routers/codebase.py `read_item`):

  * `repos` of `GET /packs/{id}/items/{item_id}` holds the pointers (`slug`,
    `slug:path`, `slug:path:line`) of the repositories the CALLER may read as code,
    in the pack's own order; a pointer to any other repository is left out without
    a trace — no placeholder, no count — and its name is nowhere in the answer;
  * `content` is the file exactly as its authors wrote it, front matter included
    (it is the pack's own text, shown to everyone who may open the pack), so the
    filter is about the derived `repos` field and every other field of the answer;
  * a caller who may read none gets `"repos": []`.
"""

from __future__ import annotations

import json

from tests.api.codebase_support import call, upload_zip, zip_of
from tests.api.rbac_world import world

NOTE = ("---\ndescription: Where billing lives\n"
        "repos: [shop-api:app/db.py:12, payments-core:src/x.py, ghost-repo, shop-api]\n"
        "---\n# Billing\n\nThe billing code sits next to the shop.\n")


async def _item(w):
    r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
        "slug": "billing", "title": "Billing", "audience": "workspace"})
    pid = r.json()["id"]
    r = await upload_zip(w, "editor_a", pid, zip_of({"README.md": "# b\n", "notes/billing.md": NOTE}))
    assert r.status_code == 201, r.text
    hits = (await call(w, "editor_a", "GET", "/api/codebase/search?q=billing")).json()["hits"]
    return pid, next(h["item_id"] for h in hits if h["path"] == "notes/billing.md")


def _may_read(monkeypatch, *slugs: str) -> None:
    from src.api.routers import codebase

    async def readable(_user, _ws, wanted):
        return {s for s in wanted if s in slugs}

    monkeypatch.setattr(codebase, "code_readable_repo_slugs", readable)


async def test_the_item_names_the_pointers_of_readable_repositories_and_no_others(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, item = await _item(w)
        _may_read(monkeypatch, "shop-api")
        r = await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}/items/{item}")
        assert r.status_code == 200, r.text
        assert r.json()["repos"] == ["shop-api:app/db.py:12", "shop-api"]
        derived = json.dumps({k: v for k, v in r.json().items() if k != "content"})
        for hidden in ("payments-core", "ghost-repo"):
            assert hidden not in derived, hidden
        assert "The billing code sits next to the shop." in r.json()["content"]


async def test_a_caller_who_may_read_no_repository_gets_an_empty_list(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, item = await _item(w)
        _may_read(monkeypatch)
        r = await call(w, "viewer_a", "GET", f"/api/codebase/packs/{pid}/items/{item}")
        assert r.status_code == 200, r.text
        assert r.json()["repos"] == []
        derived = json.dumps({k: v for k, v in r.json().items() if k != "content"})
        for hidden in ("shop-api", "payments-core", "ghost-repo"):
            assert hidden not in derived, hidden
