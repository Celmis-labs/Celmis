"""A world for the pack semantic-search tests - not a test module.

Switches the feature on, runs the background indexing inline, puts a real in-memory
Qdrant behind the pack collection and replaces the embedder with one that costs
nothing and has a MEANING: every word belongs to a concept (``MEANINGS``), and a text's
vector is the count of its concepts. Two texts that share no word but share a concept
are close; that is what lets a test say "found only by meaning".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pytest

from tests.knowledge.pack_rig import A, upload

MEANINGS: dict[str, tuple[str, ...]] = {
    "money-back": ("refund", "reimburse", "reimbursement", "chargeback", "repay"),
    "billing": ("invoice", "bill", "billing", "payment", "receipt"),
    "shipping": ("shipping", "parcel", "courier", "delivery", "carrier"),
    "security": ("password", "credential", "login", "token"),
}
AXES = list(MEANINGS)
_WORD = re.compile(r"[a-z]+")


def vector(text: str) -> list[float]:
    counts = [0.0] * (len(AXES) + 1)             # the last axis: no concept at all
    for word in _WORD.findall(text.lower()):
        for i, name in enumerate(AXES):
            if word in MEANINGS[name]:
                counts[i] += 1.0
    if not any(counts):
        counts[-1] = 1.0
    norm = math.sqrt(sum(c * c for c in counts))
    return [c / norm for c in counts]


@dataclass
class Semantic:
    client: object
    queries: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    signature: str = "fake:meanings:4"
    broken: bool = False

    def points(self) -> list[dict]:
        from src.knowledge import semantic

        if not self.client.collection_exists(semantic.collection()):
            return []
        rows, _ = self.client.scroll(semantic.collection(), limit=1000, with_payload=True)
        return [p.payload for p in rows]


@pytest.fixture
def semantic_on(monkeypatch):
    from qdrant_client import QdrantClient

    from src.knowledge import semantic
    from src.llm import completion, profiles

    client = QdrantClient(":memory:")
    state = Semantic(client)
    monkeypatch.setenv("CELMIS_PACK_SEMANTIC", "1")
    monkeypatch.setattr("src.retrieval.tier1_vault._build_qdrant_client", lambda settings: client)
    monkeypatch.setattr(semantic, "_spawn", lambda fn: fn())
    monkeypatch.setattr(profiles, "embeddings_signature", lambda: state.signature)

    def embed(text, *a, **k):
        if state.broken:
            raise RuntimeError("embedder down")
        state.queries.append(text)
        return vector(text)

    def embed_batch(texts, *a, **k):
        if state.broken:
            raise RuntimeError("embedder down")
        state.documents.extend(texts)
        return [vector(t) for t in texts]

    monkeypatch.setattr(completion, "embed", embed, raising=False)
    monkeypatch.setattr(completion, "embed_batch", embed_batch, raising=False)
    return state


async def make_pack(world, slug, *pairs, who="editor", ws=A, audience="workspace"):
    """A pack with one version made of (path, text) pairs; its id."""
    from src.knowledge import store

    async with world.session() as s:
        view = await store.create_pack(s, world.principal(who, ws), ws, slug=slug,
                                       title=slug.title(), audience=audience)
        pid = view.pack.id
    await add(world, pid, *pairs, who=who, ws=ws)
    return pid


async def add(world, pid, *pairs, who="editor", ws=A):
    from src.knowledge import store

    async with world.session() as s:
        view = await store.get_pack(s, world.principal(who, ws), ws, pid)
        return await store.add_version(s, view, upload(*pairs))
