"""`GET /api/codebase/search?where=` and the fields of an opened item.

The contract (docs/CODEBASE.md "Search and the brief"): `where` keeps hits whose front
matter matches (AND across keys, list fields by membership); a malformed `where` is 422;
`GET /packs/{id}/items/{item_id}` returns the front matter as `meta` (and `status`),
without `repos`, whose pointers go through the repository gate.
"""

from __future__ import annotations

from tests.api.codebase_support import call, upload_zip, zip_of
from tests.api.rbac_world import world

CARD = ("---\ntype: process\nstatus: Verified\nlang: uk\ntags: [billing, month-end]\n"
        "repos: [secret-repo]\n---\n# Close the month\n\nPost the zebracorn accruals.\n")
DRAFT = "---\nstatus: draft\nlang: en\n---\n# Close the quarter\n\nPost the zebracorn accruals.\n"


async def _pack(w):
    r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
        "slug": "cards", "title": "Cards", "audience": "workspace"})
    pid = r.json()["id"]
    r = await upload_zip(w, "editor_a", pid, zip_of({
        "README.md": "# cards\n", "notes/month.md": CARD, "notes/quarter.md": DRAFT}))
    assert r.status_code == 201, r.text
    return pid


async def _paths(w, where=""):
    r = await call(w, "member_a", "GET", "/api/codebase/search", params={
        "q": "zebracorn accruals", "where": where})
    assert r.status_code == 200, r.text
    return sorted(h["path"] for h in r.json()["hits"])


async def test_where_narrows_the_hits_and_no_where_changes_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _pack(w)
        assert await _paths(w) == ["notes/month.md", "notes/quarter.md"]
        assert await _paths(w, "status=verified") == ["notes/month.md"]
        assert await _paths(w, "status=verified lang=en") == []
        assert await _paths(w, "tags=month-end") == ["notes/month.md"]
        assert await _paths(w, "lang=uk|en") == ["notes/month.md", "notes/quarter.md"]


async def test_a_malformed_where_is_a_422_and_repos_cannot_be_probed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _pack(w)
        for bad in ("status", "repos=secret-repo", 'a="b'):
            r = await call(w, "member_a", "GET", "/api/codebase/search",
                           params={"q": "zebracorn", "where": bad})
            assert r.status_code == 422, (bad, r.text)
            assert r.json()["detail"]["code"] == "bad_where"


async def test_an_opened_item_carries_its_front_matter_without_the_repository_pointers(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w)
        hits = (await call(w, "member_a", "GET", "/api/codebase/search?q=zebracorn")).json()["hits"]
        month = next(h for h in hits if h["path"] == "notes/month.md")
        assert month["status"] == "verified"
        r = await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}/items/{month['item_id']}")
        body = r.json()
        assert body["status"] == "verified"
        assert body["meta"]["lang"] == "uk" and body["meta"]["tags"] == ["billing", "month-end"]
        assert "repos" not in body["meta"] and "secret-repo" not in str(body["meta"])
