"""Search inside packs fuses BM25 with a search by meaning.

The contract (docs/CODEBASE.md "Search and the brief", src/knowledge/semantic.py):

  * an item that shares no word with the query but shares its meaning is found, and shows
    its best chunk as the snippet;
  * with no collection, with nothing indexed, with a failing embedder or the feature off,
    search is BM25 alone and never an error; a query is embedded only when something the
    caller may read is indexed;
  * an item of a pack the caller may not read never comes back, whatever its vector says;
  * the index follows the current version: points of an older version, of a version that
    was pruned and of a deleted pack are removed;
  * packs that were there before the feature are indexed by the backfill.
"""

from __future__ import annotations

import pytest

from src.db.models import KnowledgePackVersion
from src.knowledge import limits, semantic, store
from tests.knowledge.pack_rig import A
from tests.knowledge.semantic_support import add, make_pack

REFUND = ("notes/money-back.md",
          "# Money back\n\nWe reimburse a customer after a chargeback is confirmed.\n")
BILLING = ("notes/billing.md", "# Billing\n\nEvery invoice carries a receipt and a payment date.\n")
SHIPPING = ("notes/shipping.md", "# Shipping\n\nThe courier collects each parcel at noon.\n")


def ranked(world, who, query, ws=A, **kw):
    return store.rank_current_sync(world.principal(who, ws), ws, query, limit=20, **kw)


def paths(ranking):
    return [h.item.path for h in ranking.hits]


async def test_an_item_that_shares_no_word_with_the_query_is_found_by_its_meaning(world, semantic_on):
    await make_pack(world, "support", REFUND, BILLING, SHIPPING)
    assert semantic_on.points(), "the current version was indexed after the upload"
    found = ranked(world, "member", "refund")
    assert "notes/money-back.md" in paths(found)
    assert "notes/shipping.md" not in paths(found)
    # BM25 alone would have found nothing: no word of the query is in the item
    semantic_on.broken = True
    assert paths(ranked(world, "member", "refund")) == []


async def test_a_hit_found_only_by_meaning_carries_its_best_chunk_as_the_snippet(world, semantic_on):
    await make_pack(world, "support", REFUND, SHIPPING)
    async with world.session() as s:
        hits = await store.search(s, world.principal("member"), A, "refund", 10)
    [hit] = [h for h in hits if h.item.path == "notes/money-back.md"]
    assert "chargeback" in hit.snippet


async def test_a_word_match_and_a_meaning_match_are_fused_into_one_ranking(world, semantic_on):
    await make_pack(world, "support", REFUND, BILLING, SHIPPING)
    found = paths(ranked(world, "member", "invoice receipt"))
    assert found[0] == "notes/billing.md"           # both orders agree: first
    assert len(found) == len(set(found))


async def test_a_workspace_with_no_collection_gets_the_word_ranking_and_no_error(world, semantic_on):
    await make_pack(world, "support", REFUND, BILLING)
    semantic_on.client.delete_collection(semantic.collection())
    assert paths(ranked(world, "member", "invoice")) == ["notes/billing.md"]
    assert paths(ranked(world, "member", "refund")) == []


async def test_no_query_is_embedded_while_nothing_is_indexed(world, semantic_on, monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_SEMANTIC", "0")
    await make_pack(world, "support", REFUND, BILLING)
    monkeypatch.setenv("CELMIS_PACK_SEMANTIC", "1")
    assert semantic_on.points() == [] and semantic_on.documents == []
    assert paths(ranked(world, "member", "invoice")) == ["notes/billing.md"]
    assert semantic_on.queries == []                # nothing indexed: nothing to pay for


async def test_a_failing_embedder_leaves_the_word_ranking(world, semantic_on):
    await make_pack(world, "support", REFUND, BILLING)
    semantic_on.broken = True
    assert paths(ranked(world, "member", "invoice")) == ["notes/billing.md"]


async def test_an_item_of_a_pack_the_caller_may_not_read_is_never_returned(world, semantic_on):
    await make_pack(world, "open", BILLING)
    await make_pack(world, "private", REFUND, audience="restricted")
    assert any(p["path"] == "notes/money-back.md" for p in semantic_on.points())
    assert "notes/money-back.md" not in paths(ranked(world, "viewer", "refund"))
    assert "notes/money-back.md" in paths(ranked(world, "editor", "refund"))
    async with world.session() as s:
        hits = await store.search(s, world.principal("viewer"), A, "refund", 10)
    assert hits == []


async def test_an_item_of_another_workspace_is_never_returned(world, semantic_on):
    from tests.knowledge.pack_rig import B

    await make_pack(world, "abroad", REFUND, who="admin_b", ws=B)
    assert paths(ranked(world, "admin_b", "refund", ws=B)) == ["notes/money-back.md"]
    await make_pack(world, "home", BILLING)
    assert paths(ranked(world, "member", "refund")) == []


async def test_the_points_of_an_old_version_are_removed_when_a_new_one_is_current(world, semantic_on):
    pid = await make_pack(world, "support", REFUND, BILLING)
    first = {p["version_id"] for p in semantic_on.points()}
    version = await add(world, pid, REFUND, SHIPPING)
    now = semantic_on.points()
    assert {p["version_id"] for p in now} == {version.id} and version.id not in first
    assert {p["path"] for p in now} == {"README.md", "notes/money-back.md", "notes/shipping.md"}
    assert all(p["pack_id"] == pid and p["workspace_id"] == A for p in now)


async def test_rolling_back_re_indexes_the_older_version(world, semantic_on):
    pid = await make_pack(world, "support", REFUND)
    await add(world, pid, SHIPPING)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.activate_version(s, view, 1)
    assert {p["path"] for p in semantic_on.points()} == {"README.md", "notes/money-back.md"}
    assert "notes/money-back.md" in paths(ranked(world, "member", "refund"))


async def test_a_pruned_version_leaves_no_points(world, semantic_on, monkeypatch):
    import dataclasses

    monkeypatch.setattr(limits, "BUILTIN", dataclasses.replace(limits.BUILTIN, keep_versions=1))
    pid = await make_pack(world, "support", REFUND)
    version = await add(world, pid, BILLING)
    assert await world.count(KnowledgePackVersion) == 1
    assert {p["version_id"] for p in semantic_on.points()} == {version.id}


async def test_deleting_a_pack_removes_its_points(world, semantic_on):
    keep = await make_pack(world, "keep", BILLING)
    gone = await make_pack(world, "gone", REFUND)
    async with world.session() as s:
        await store.delete_pack(s, await store.get_pack(s, world.principal("editor"), A, gone))
    assert {p["pack_id"] for p in semantic_on.points()} == {keep}


async def test_the_backfill_indexes_a_pack_that_was_there_before(world, semantic_on, monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_SEMANTIC", "0")
    pid = await make_pack(world, "old", REFUND, BILLING)
    monkeypatch.setenv("CELMIS_PACK_SEMANTIC", "1")
    assert semantic_on.points() == []
    done = semantic.backfill()
    assert done.versions == 1 and done.chunks == len(semantic_on.points()) > 0
    assert "notes/money-back.md" in paths(ranked(world, "member", "refund"))
    assert semantic.backfill().versions == 0         # up to date: nothing to redo
    assert {p["pack_id"] for p in semantic_on.points()} == {pid}


async def test_a_changed_embeddings_model_re_indexes(world, semantic_on, monkeypatch):
    await make_pack(world, "support", REFUND)
    semantic_on.documents.clear()
    semantic_on.signature = "fake:other-model:4"
    assert semantic.backfill().versions == 1 and semantic_on.documents


@pytest.mark.parametrize("size", [100, 5000, 20000])
def test_a_long_item_is_cut_into_bounded_chunks_by_heading(size):
    text = "# Top\n\n" + "\n\n".join(f"## Part {i}\n\n" + "word " * (size // 40) for i in range(8))
    chunks = semantic.chunks_of("Doc", "", text)
    assert 1 <= len(chunks) <= semantic.MAX_CHUNKS_PER_ITEM
    assert all(len(c) <= semantic.CHUNK_CHARS * 2 + 200 for c in chunks)
    assert all(c.startswith("Doc\n") for c in chunks)
