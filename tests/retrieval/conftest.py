"""A scratch vault, a real in-memory Qdrant and an embedder that reads topics.

Texts embed by what they mention: the word ZEBRA points one way, everything
else another, so a test can make a question match ONLY a chunk of a note's tail.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

WS = "ws-chunks"
OTHER_WS = "ws-other"


def _vec(text: str) -> list[float]:
    if "zebra" in text.lower():
        return [1.0, 0.0, 0.01, 0.0]
    return [0.0, 1.0, 0.01, 0.0]


@dataclass
class ChunkWorld:
    qdrant: object

    def points(self) -> list:
        from src.config import get_settings

        name = get_settings().qdrant_collection
        if not self.qdrant.collection_exists(name):
            return []
        pts, _ = self.qdrant.scroll(name, limit=1000, with_payload=True)
        return pts

    def of(self, note_path: str) -> list[dict]:
        found = [p.payload for p in self.points() if p.payload.get("note_path") == note_path]
        return sorted(found, key=lambda pl: pl.get("chunk", 0))

    def writer(self, workspace_id: str = WS):
        from src.vault.writer import VaultWriter

        return VaultWriter(workspace_id=workspace_id)

    def retriever(self, workspace_id: str = WS):
        from src.retrieval.tier1_vault import VaultRetriever

        return VaultRetriever(workspace_id=workspace_id)


@pytest.fixture
def chunk_world(tmp_path, monkeypatch):
    from qdrant_client import QdrantClient

    from src.config import get_settings
    from src.llm import completion

    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()
    client = QdrantClient(":memory:")
    monkeypatch.setattr("src.retrieval.tier1_vault._build_qdrant_client", lambda settings: client)
    monkeypatch.setattr(completion, "embed", lambda text, *a, **k: _vec(text), raising=False)
    monkeypatch.setattr(completion, "embed_batch",
                        lambda texts, *a, **k: [_vec(t) for t in texts], raising=False)
    yield ChunkWorld(client)
    get_settings.cache_clear()


def long_body(*, tail: str = "The zebra crossing is configured here.", paragraphs: int = 60) -> str:
    """About 15k characters of filler with a distinctive paragraph at the end."""
    filler = [f"Paragraph {i}: " + ("routine description of the module. " * 7) for i in range(paragraphs)]
    return "\n\n".join([*filler, tail])
