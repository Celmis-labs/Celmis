"""Search and the brief read the current version of the packs the caller may see.

The contract (docs/CODEBASE.md, "Search and the brief"):

  * a search or a brief is built from the packs the caller can see and from their
    CURRENT version alone — a hidden pack, an older version, a deleted pack and
    another workspace's pack contribute nothing, not even a count;
  * a repository pointer in a note is quoted only to someone who may read that
    repository's code; for anyone else the note is quoted without it;
  * the answer to "nothing found" is the same text for a caller with no matching
    pack and for a caller whose matching packs are all hidden.
"""

from __future__ import annotations

import pytest

from tests.api.codebase_support import call, marker, seeded, upload_zip, zip_of
from tests.api.rbac_world import A_REPO, B_SECRET, world
from tests.api.test_the_codebase_routes_answer_by_role_and_audience import EXPECTED

NOTE = (f"---\nname: Invoice flow\ndescription: how invoices flow\nrepos: [{A_REPO}:src/billing.py:12]\n"
        "tags: [billing]\n---\n# Invoice flow\n\nInvoices are raised monthly by the invoicing job.\n")
SKILL = ("---\nname: billing\ndescription: Raise an invoice\n---\n# Billing\n\n"
         "Steps to raise an invoice.\n")


async def _invoices(w) -> str:
    r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                   json={"slug": "inv", "title": "Inv", "audience": "workspace"})
    pid = r.json()["id"]
    up = await upload_zip(w, "editor_a", pid, zip_of(
        {"README.md": "# Inv\n", "notes/flow.md": NOTE, "billing/SKILL.md": SKILL}))
    assert up.status_code == 201, up.text
    return pid


@pytest.mark.parametrize("who", sorted(EXPECTED))
async def test_search_finds_a_word_only_in_the_packs_the_caller_can_see(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        for slug in ("pw", "pt", "ptg", "pr", "prg"):
            r = await call(w, who, "GET", f"/api/codebase/search?q={marker(slug)}")
            assert r.status_code == 200, r.text
            found = [h["pack_slug"] for h in r.json()["hits"]]
            assert found == ([slug] if slug in EXPECTED[who][1] else []), (who, slug, found)


async def test_search_and_brief_never_mention_another_workspaces_pack(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        for who in ("su", "gadmin", "owner_a", "member_a", "both"):
            for query in (B_SECRET, "b-secret-pack", "description"):
                r = await call(w, who, "GET", f"/api/codebase/search?q={query}")
                assert B_SECRET.encode() not in r.content and b"b-secret-pack" not in r.content
            r = await call(w, who, "POST", "/api/codebase/brief", json={"task": f"{B_SECRET} pack description"})
            assert B_SECRET.encode() not in r.content


async def test_search_has_bounds_and_an_empty_query_finds_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        empty = await call(w, "editor_a", "GET", "/api/codebase/search?q=")
        assert empty.status_code == 200 and empty.json() == {"hits": []}
        for bad in ("limit=0", "limit=51", "limit=x", "q=" + "a" * 301):
            assert (await call(w, "editor_a", "GET", f"/api/codebase/search?{bad}")
                    ).status_code == 422, bad
        some = await call(w, "editor_a", "GET", "/api/codebase/search?q=recipe&limit=2")
        assert len(some.json()["hits"]) == 2
        assert set(some.json()["hits"][0]) == {"pack_id", "pack_slug", "pack_title", "item_id", "path",
                                               "kind", "skill", "name", "snippet", "score"}


async def test_only_the_current_version_is_searched_and_a_deleted_pack_disappears(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                       json={"slug": "ver", "title": "Ver", "audience": "workspace"})
        pid = r.json()["id"]
        base = f"/api/codebase/packs/{pid}"
        await upload_zip(w, "editor_a", pid, zip_of({"README.md": "# V\n", "notes/a.md": "# A\n\nzebrafish\n"}))
        await upload_zip(w, "editor_a", pid, zip_of({"README.md": "# V\n", "notes/a.md": "# A\n\naardvark\n"}))

        async def hits(q):
            return (await call(w, "member_a", "GET", f"/api/codebase/search?q={q}")).json()["hits"]

        assert await hits("zebrafish") == [] and len(await hits("aardvark")) == 1
        assert (await call(w, "editor_a", "POST", f"{base}/versions/1/activate")).status_code == 200
        assert len(await hits("zebrafish")) == 1 and await hits("aardvark") == []
        assert (await call(w, "editor_a", "DELETE", base)).status_code == 204
        assert await hits("zebrafish") == []
        brief = await call(w, "member_a", "POST", "/api/codebase/brief", json={"task": "zebrafish"})
        assert "zebrafish" not in brief.text


async def test_a_brief_quotes_a_repo_pointer_only_to_those_who_may_read_that_repo(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _invoices(w)
        readers = {"su", "gadmin", "owner_a", "admin_a", "admin2_a", "editor_a"}
        for who in ("su", "gadmin", "owner_a", "admin_a", "admin2_a", "editor_a", "member_a", "viewer_a", "both"):
            r = await call(w, who, "POST", "/api/codebase/brief", json={"task": "raise an invoice monthly"})
            assert r.status_code == 200, (who, r.text)
            text = r.json()["text"]
            assert "Invoice flow" in text and "[inv@v1]" in text, who          # the note itself is quoted
            assert (f"{A_REPO}:src/billing.py:12" in text) is (who in readers), who
            if who not in readers:
                assert A_REPO not in text and "billing.py" not in text, who
            assert {i["pack_slug"] for i in r.json()["items"]} == {"inv"}


async def test_a_brief_quotes_nothing_from_a_hidden_pack_and_says_so_the_same_way(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        nothing = (await call(w, "member_a", "POST", "/api/codebase/brief",
                              json={"task": "quantumturnip entirely unrelated"})).json()
        hidden = (await call(w, "member_a", "POST", "/api/codebase/brief",
                             json={"task": marker("pr")})).json()
        assert nothing == hidden and nothing["items"] == []
        assert marker("pr") not in hidden["text"]
        mine = (await call(w, "editor_a", "POST", "/api/codebase/brief",
                           json={"task": marker("pr")})).json()
        assert marker("pr") in mine["text"] and [i["pack_slug"] for i in mine["items"]] == ["pr"]


async def test_the_brief_input_is_bounded_and_strict(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        for body in ({"task": ""}, {"task": "x" * 8001}, {}, {"task": "ok", "extra": 1}):
            r = await call(w, "editor_a", "POST", "/api/codebase/brief", json=body)
            assert r.status_code == 422, body
