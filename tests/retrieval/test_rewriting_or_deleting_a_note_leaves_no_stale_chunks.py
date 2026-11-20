"""Rewrites shrink the chunk set, deletes remove it, payload updates reach all."""

from __future__ import annotations

import uuid

from src.retrieval.note_chunks import chunk_point_ids
from src.vault.writer import VaultWriter
from tests.retrieval.test_the_tail_of_a_long_note_is_found_by_meaning import _write


def test_rewriting_a_note_shorter_removes_the_stale_chunks(chunk_world):
    _write(chunk_world)
    assert len(chunk_world.of("modules/big.md")) > 2
    _write(chunk_world, body="Now it is short.")
    payloads = chunk_world.of("modules/big.md")
    assert len(payloads) == 1 and payloads[0]["chunks"] == 1
    assert chunk_world.retriever().search("zebra") == []


def test_deleting_a_note_removes_every_chunk(chunk_world):
    _write(chunk_world)
    chunk_world.retriever().delete_by_note_path("modules/big.md", repo="repo-a")
    assert chunk_world.of("modules/big.md") == []


def test_the_chunk_ids_of_a_note_are_stable_across_rewrites(chunk_world):
    _write(chunk_world)
    before = sorted(str(p.id) for p in chunk_world.points())
    _write(chunk_world)
    assert sorted(str(p.id) for p in chunk_world.points()) == before
    assert set(before) <= {str(uuid.UUID(i)) for i in chunk_point_ids(VaultWriter.note_id("repo-a", "modules/big.md"))}


def test_a_computed_fields_update_reaches_every_chunk(chunk_world):
    _write(chunk_world)
    n = len(chunk_world.of("modules/big.md"))
    changed = chunk_world.writer().update_computed_fields(
        repo="repo-a", relative_path="modules/big.md",
        computed={"cross_refs": ["modules/other"]},
    )
    assert changed
    payloads = chunk_world.of("modules/big.md")
    assert len(payloads) == n
    assert all(p.get("cross_refs") == ["modules/other"] for p in payloads)
