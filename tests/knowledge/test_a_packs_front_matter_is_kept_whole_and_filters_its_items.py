"""The whole front matter of a markdown file is kept, and `where=` filters by it.

The contract (docs/CODEBASE.md "What a pack contains", "Search and the brief"):

  * every key of the YAML front matter is kept on the item, JSON-safe (a date is an ISO
    string) and bounded; `status` is also kept normalised (lower case);
  * `where='status=verified lang=uk'`: AND across keys, `a|b` is either, a list field
    matches by membership, keys and values are case-insensitive, a quoted value may hold
    spaces; a term that is not `key=value`, and `repos`, are refused;
  * the filter narrows the catalogue and the search alike; `repos` is neither shown nor
    filterable (pointers go through the repository gate).
"""

from __future__ import annotations

import json

import pytest

from src.knowledge import ingest, store, where
from tests.knowledge.pack_rig import A
from tests.knowledge.semantic_support import make_pack

CARD = ("---\ntype: process\nid: PR-7\nstatus: Verified\nlang: uk\ntags: [billing, month-end]\n"
        "reviewed: 2026-03-01\nlinks: {related: PR-9}\nrepos: [secret-repo]\n---\n"
        "# Close the month\n\nPost the accruals before the ledger closes.\n")
DRAFT = ("---\ntype: process\nid: PR-8\nstatus: draft\nlang: en\ntags: [billing]\n---\n"
         "# Close the quarter\n\nPost the accruals before the ledger closes.\n")
PLAIN = "# Plain\n\nPost the accruals before the ledger closes.\n"


def files(*pairs):
    return ingest.ingest_files(
        [("README.md", b"# pack\n"), *((p, t.encode()) for p, t in pairs)]).files


def test_the_whole_front_matter_is_kept_json_safe_with_a_normalised_status():
    [card] = [f for f in files(("notes/card.md", CARD)) if f.path == "notes/card.md"]
    assert card.meta["id"] == "PR-7" and card.meta["lang"] == "uk"
    assert card.meta["tags"] == ["billing", "month-end"]
    assert card.meta["reviewed"] == "2026-03-01"               # a date, as text
    assert card.meta["links"] == {"related": "PR-9"}
    assert card.status == "verified"
    json.dumps(card.meta)


def test_a_file_without_front_matter_has_none_and_a_huge_one_is_cut_to_the_bound():
    [plain] = [f for f in files(("notes/p.md", PLAIN)) if f.path == "notes/p.md"]
    assert plain.meta == {} and plain.status is None
    many = "".join(f"key{i}: {'v' * 60}\n" for i in range(120))          # ~8 KB: under the 8 KB bound
    [big] = [f for f in files(("notes/b.md", f"---\n{many}---\n# B\n")) if f.path == "notes/b.md"]
    assert len(json.dumps(big.meta)) <= ingest.MAX_FRONT_MATTER_CHARS


def test_where_is_and_across_keys_or_inside_one_and_membership_for_lists():
    meta = {"Status": "Verified", "lang": "uk", "tags": ["billing", "month-end"], "n": 3}

    def ok(text):
        return where.matches(meta, "verified", where.parse(text))

    assert ok("status=verified lang=uk")
    assert ok("STATUS=VERIFIED")
    assert ok("lang=en|uk")
    assert ok("tags=month-end")
    assert ok("n=3")
    assert not ok("status=verified lang=en")
    assert not ok("tags=ledger")
    assert not ok("missing=x")
    assert where.matches({"title": "Close the month"}, None, where.parse('title="close the month"'))


@pytest.mark.parametrize("text", ["status", "=x", "status=", "repos=secret-repo", 'a="b'])
def test_a_bad_where_is_refused(text):
    with pytest.raises(where.WhereError):
        where.parse(text)


@pytest.fixture
async def filled(world):
    await make_pack(world, "cards", ("notes/close-month.md", CARD), ("notes/close-quarter.md", DRAFT),
                    ("notes/plain.md", PLAIN))
    return world


async def test_the_store_keeps_the_front_matter_and_the_catalogue_can_be_filtered(filled):
    p = filled.principal("member")
    everything = store.visible_current_items_sync(p, A)
    by_path = {i.path: i for _pk, _n, i in everything}
    assert by_path["notes/close-month.md"].meta["id"] == "PR-7"
    assert by_path["notes/close-month.md"].status == "verified"
    kept = store.visible_current_items_sync(p, A, where="status=verified lang=uk")
    assert [i.path for _pk, _n, i in kept] == ["notes/close-month.md"]
    assert [i.path for _pk, _n, i in store.visible_current_items_sync(p, A, where="tags=billing")
            ] == ["notes/close-month.md", "notes/close-quarter.md"]


async def test_a_search_is_narrowed_by_where_and_unchanged_without_it(filled):
    p = filled.principal("member")
    query = "post accruals ledger"
    plain = {h.item.path for h in store.rank_current_sync(p, A, query).hits}
    assert plain == {"notes/close-month.md", "notes/close-quarter.md", "notes/plain.md"}
    only = store.rank_current_sync(p, A, query, where="status=verified")
    assert [h.item.path for h in only.hits] == ["notes/close-month.md"]
    none = store.rank_current_sync(p, A, query, where="status=archived")
    assert none.hits == []
    async with filled.session() as s:
        hits = await store.search(s, filled.principal("member"), A, query, 10, where="lang=en")
    assert [h.item.path for h in hits] == ["notes/close-quarter.md"]
