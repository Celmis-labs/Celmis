"""Reading the items of the visible packs stops at a hard cap, and says so.

The contract (src/knowledge/store.py `MAX_RANKED_ITEMS`; docs/CODEBASE.md "Search"):

  * the items are read in key order a batch at a time, so a pack is never left out
    whole because of where it sits: the scan reads up to the cap and no further;
  * reaching the cap with items left sets `truncated` (on the ranking, on
    `search`, on `visible_current_items`) and logs `pack_index_capped`; a corpus
    of exactly the cap's size is NOT truncated;
  * a corpus under the cap is never truncated, whatever the batch size.
"""

from __future__ import annotations

import logging

from src.knowledge import store
from tests.knowledge.pack_rig import A, upload


async def _packs(world, count: int = 4) -> int:
    """``count`` workspace packs, each with two notes that hold the word
    "unicorn" (and a README): ``2 * count`` notes in all. Returns the items."""
    for k in range(count):
        async with world.session() as s:
            view = await store.create_pack(s, world.principal("editor"), A, slug=f"p{k}",
                                           title=f"P{k}", audience="workspace")
            await store.add_version(s, view, upload(
                (f"notes/x{k}.md", f"# zebra{k} unicorn\nunicorn text {k}\n"),
                (f"notes/y{k}.md", f"# other{k} unicorn\nmore text\n")), "")
    async with world.session() as s:
        return len(await store.visible_current_items(s, world.principal("editor"), A))


async def test_a_corpus_under_the_cap_is_read_whole(world):
    total = await _packs(world)
    async with world.session() as s:
        scanned = await store.visible_current_items(s, world.principal("editor"), A)
        found = await store.search(s, world.principal("editor"), A, "unicorn", limit=50)
    assert len(scanned) == total and scanned.truncated is False
    assert len(found) == 8 and found.truncated is False


async def test_a_corpus_of_exactly_the_cap_is_not_truncated(world, monkeypatch):
    total = await _packs(world)
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", total)
    monkeypatch.setattr(store, "SCAN_BATCH", 3)
    async with world.session() as s:
        scanned = await store.visible_current_items(s, world.principal("editor"), A)
        found = await store.search(s, world.principal("editor"), A, "unicorn", limit=50)
    assert len(scanned) == total and scanned.truncated is False
    assert len(found) == 8 and found.truncated is False


async def test_a_corpus_over_the_cap_is_read_up_to_it_and_flagged_and_logged(
        world, monkeypatch, caplog):
    total = await _packs(world)
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", total - 3)
    monkeypatch.setattr(store, "SCAN_BATCH", 4)
    caplog.set_level(logging.WARNING, logger=store.logger.name)
    async with world.session() as s:
        scanned = await store.visible_current_items(s, world.principal("editor"), A)
        found = await store.search(s, world.principal("editor"), A, "unicorn", limit=50)
        ranking = await store.rank_current(s, world.principal("editor"), A, "unicorn", limit=50)
    assert len(scanned) == total - 3 and scanned.truncated is True
    assert found.truncated is True and ranking.truncated is True
    assert 0 < len(found) < 8                           # what was read is still ranked
    assert "pack_index_capped" in caplog.text


async def test_the_cap_applies_to_the_synchronous_readers_of_the_tools_too(world, monkeypatch):
    total = await _packs(world)
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", total - 1)
    principal = world.principal("editor")
    assert store.visible_current_items_sync(principal, A).truncated is True
    assert store.rank_current_sync(principal, A, "unicorn").truncated is True
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", total)
    assert store.visible_current_items_sync(principal, A).truncated is False
    assert store.rank_current_sync(principal, A, "unicorn").truncated is False
