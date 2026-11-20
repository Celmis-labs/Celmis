"""Every chunk is filterable on the note's front matter, workspace included."""

from __future__ import annotations

from tests.retrieval.conftest import WS
from tests.retrieval.test_the_tail_of_a_long_note_is_found_by_meaning import _write


def test_every_chunk_payload_carries_the_notes_metadata(chunk_world):
    _write(chunk_world)
    payloads = chunk_world.of("modules/big.md")
    assert len(payloads) > 1
    for i, pl in enumerate(payloads):
        assert pl["chunk"] == i and pl["chunks"] == len(payloads)
        assert pl["fm"] == {"team": "ops"}
        assert pl["kb"]["status"] == "draft"
        assert (pl["type"], pl["repo"], pl["workspace_id"]) == ("module", "repo-a", WS)


def test_a_kb_filter_matches_a_tail_chunk(chunk_world):
    _write(chunk_world)
    r = chunk_world.retriever()
    assert [h.note_path for h in r.search("zebra", kb_filters={"status": "draft"})] == ["modules/big.md"]
    assert [h.note_path for h in r.search("zebra", kb_filters={"team": "ops"})] == ["modules/big.md"]
    assert r.search("zebra", kb_filters={"status": "verified"}) == []
