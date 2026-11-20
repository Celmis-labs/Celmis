"""A long note is several points; the part past the first 6000 characters is
searchable, and the note still comes back once."""

from __future__ import annotations

import uuid

from src.retrieval.note_chunks import MAX_CHUNKS
from src.vault.writer import NoteMetadata, VaultWriter
from tests.retrieval.conftest import WS, long_body

KB = {"id": "c1", "status": "draft", "effective_status": "draft", "origin": "import", "objects": []}


def _meta():
    return NoteMetadata(type="module", repo="repo-a", module="big",
                        extra={"title": "Big note", "fm": {"team": "ops"}, "kb": dict(KB)})


def _write(world, path="modules/big.md", body=None):
    world.writer().write(repo="repo-a", relative_path=path,
                         content=body if body is not None else long_body(), metadata=_meta())


def test_the_tail_of_a_long_note_is_found_by_meaning(chunk_world):
    _write(chunk_world)
    assert len(chunk_world.of("modules/big.md")) > 1
    hits = chunk_world.retriever().search("where is the zebra configured")
    assert [h.note_path for h in hits] == ["modules/big.md"]
    assert hits[0].score > 0.9
    assert "zebra" in hits[0].content.lower()


def test_a_note_found_by_several_chunks_appears_once_with_its_best_score(chunk_world):
    body = long_body(tail="zebra one") + "\n\n" + long_body(tail="zebra two")
    _write(chunk_world, body=body)
    hits = chunk_world.retriever().search("zebra", top_k=5)
    assert len(hits) == 1


def test_a_short_note_stays_one_point_with_the_same_embed_text(chunk_world):
    _write(chunk_world, path="modules/small.md", body="Short note about routines.")
    (payload,) = chunk_world.of("modules/small.md")
    assert (payload["chunk"], payload["chunks"]) == (0, 1)
    expected = VaultWriter._make_embed_text("modules/small.md", "Short note about routines.", _meta())
    assert payload["content"] == expected


def test_chunk_zero_is_exactly_the_old_embed_text_and_keeps_the_note_id(chunk_world):
    _write(chunk_world)
    expected = VaultWriter._make_embed_text("modules/big.md", long_body(), _meta())
    ids = {str(p.id): p.payload for p in chunk_world.points()}
    assert ids[str(uuid.UUID(VaultWriter.note_id("repo-a", "modules/big.md")))]["content"] == expected


def test_a_very_long_note_is_capped(chunk_world):
    _write(chunk_world, body=long_body(paragraphs=2000))
    payloads = chunk_world.of("modules/big.md")
    assert len(payloads) == MAX_CHUNKS
    assert {p["chunks"] for p in payloads} == {MAX_CHUNKS}


def test_workspace_isolation_holds_for_tail_chunks(chunk_world):
    _write(chunk_world)
    assert chunk_world.retriever("ws-other").search("zebra") == []
    assert chunk_world.retriever(WS).search("zebra")
