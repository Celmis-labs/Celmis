"""Ranking a workspace's packs batch by batch gives the answer `search.rank` gives.

The contract (src/knowledge/store.py `_rank_core`, src/knowledge/search.py `rank`):

  * BM25 needs the whole corpus (document count, average length, how many items
    hold each term), so the store counts those while the items stream past in
    batches and keeps only the figures of items that match; the best items come
    out in the same order with the same scores as `search.rank` over every item;
  * the batch size changes nothing; memory is one batch plus the matching items;
  * `limit` bounds what is returned, and `per_kind` adds the best N of each kind
    (the brief fills a section per kind) to the best `limit` overall;
  * if the formula in `search.py` ever changes without the store following, this
    test is the one that fails.
"""

from __future__ import annotations

import random

import pytest

from src.knowledge import search as search_mod
from src.knowledge import store
from tests.knowledge.pack_rig import A, upload

WORDS = ("invoice", "refund", "ledger", "tariff", "carrier", "pallet", "barcode", "audit",
         "rollout", "backup", "queue", "cache", "token", "retry", "export", "report")
QUERIES = ("invoice", "refund ledger", "tariff carrier pallet", "backup queue cache retry",
           "unseen-word", "invoice unseen-word", "audit")


def _text(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def _files(rng: random.Random, pack: int) -> list[tuple[str, str]]:
    files = []
    for i in range(7):
        folder = rng.choice(("notes", "checklists"))
        body = f"---\ndescription: {_text(rng, 3)}\n---\n# {_text(rng, 2)}\n\n{_text(rng, rng.randint(5, 60))}\n"
        files.append((f"{folder}/p{pack}-{i}-{rng.choice(WORDS)}.md", body))
    files.append((f"skill{pack}/SKILL.md",
                  f"---\nname: skill{pack}\ndescription: {_text(rng, 4)}\n---\n# s\n\n{_text(rng, 30)}\n"))
    return files


async def _corpus(world, seed: int = 7) -> None:
    rng = random.Random(seed)
    for pack in range(3):
        async with world.session() as s:
            view = await store.create_pack(s, world.principal("editor"), A, slug=f"p{pack}",
                                           title=f"P{pack}", audience="workspace")
            await store.add_version(s, view, upload(*_files(rng, pack)), "")


async def _plain(world, query: str, limit: int, per_kind: dict[str, int] | None):
    async with world.session() as s:
        scanned = await store.visible_current_items(s, world.principal("editor"), A)
    everything = search_mod.rank([item for _ref, _number, item in scanned], query, 10_000)
    keep = {h.item.id for h in everything[:limit]}
    for kind, count in (per_kind or {}).items():
        keep |= {h.item.id for h in [h for h in everything if h.item.kind == kind][:count]}
    return [(h.item.id, h.score) for h in everything if h.item.id in keep]


async def _streamed(world, query: str, limit: int, per_kind: dict[str, int] | None):
    async with world.session() as s:
        ranking = await store.rank_current(s, world.principal("editor"), A, query, limit=limit,
                                           per_kind=per_kind)
    assert ranking.truncated is False
    return [(h.item.id, h.score) for h in ranking.hits]


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("limit", [1, 5, 50])
async def test_the_best_items_and_their_scores_are_those_of_the_plain_ranker(world, query, limit):
    await _corpus(world)
    assert await _streamed(world, query, limit, None) == await _plain(world, query, limit, None)


@pytest.mark.parametrize("query", QUERIES)
async def test_the_best_of_each_kind_is_added_to_the_best_overall(world, query):
    await _corpus(world)
    per_kind = {"skill": 1, "checklist": 2, "note": 2}
    assert await _streamed(world, query, 3, per_kind) == await _plain(world, query, 3, per_kind)


async def test_the_size_of_a_batch_changes_nothing(world, monkeypatch):
    await _corpus(world)
    whole = await _streamed(world, "invoice refund", 10, {"note": 3})
    monkeypatch.setattr(store, "SCAN_BATCH", 3)
    assert await _streamed(world, "invoice refund", 10, {"note": 3}) == whole
    monkeypatch.setattr(store, "SCAN_BATCH", 1)
    assert await _streamed(world, "invoice refund", 10, {"note": 3}) == whole


async def test_a_query_without_a_word_or_a_limit_ranks_nothing(world):
    await _corpus(world)
    assert await _streamed(world, "", 5, None) == []
    assert await _streamed(world, "!!!", 5, None) == []
    assert await _streamed(world, "invoice", 0, None) == []


async def test_the_test_corpus_is_one_the_queries_find_something_in(world):
    await _corpus(world)
    assert len(await _plain(world, "invoice refund", 5, None)) == 5
    assert await _plain(world, "unseen-word", 5, None) == []
    async with world.session() as session:
        scanned = await store.visible_current_items(session, world.principal("editor"), A)
    assert {"skill", "checklist", "note"} <= {item.kind for _ref, _number, item in scanned}
