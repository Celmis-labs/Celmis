"""A search against a vault that does not exist cost money.

`VaultRetriever.search` embedded the question — a paid call — then asked
Qdrant, then took a 404 and reported "vault not generated". The multi-repo
chat path did the same. So the one state in which the user can do nothing but
try again was the state that billed on every attempt: an empty search page, a
chat that degrades to grep, and an embedding call behind each keystroke-driven
query.

The collection is checked first now. One metadata call, no vectors, no money.

AN UNREACHABLE QDRANT ANSWERS TRUE, and that asymmetry is the whole design.
"I could not ask" is not "it is not there": a probe that returned False on a
network blip would skip Tier 1 silently and tell the user to generate a vault
they already have. Answering True sends the query down the original path,
which fails on its own terms with the real error.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.retrieval.tier1_vault import CollectionMissing, VaultRetriever


class _Qdrant:
    """A Qdrant that has never had a collection created in it."""

    def __init__(self, *, missing=True, unreachable=False):
        self.missing = missing
        self.unreachable = unreachable
        self.queries = 0

    def get_collection(self, name):
        if self.unreachable:
            raise ConnectionError("connection refused")
        if self.missing:
            raise RuntimeError(
                f"Unexpected Response: 404 (Not Found) "
                f"Not found: Collection `{name}` doesn't exist!")
        return SimpleNamespace(config=None)

    def query_points(self, **kw):
        self.queries += 1
        if self.missing:
            raise RuntimeError("Not found: Collection doesn't exist!")
        return SimpleNamespace(points=[])


@pytest.fixture
def settings():
    from src.config import get_settings
    return get_settings()


def _retriever(settings, qdrant):
    return VaultRetriever(settings, workspace_id="ws-1", qdrant=qdrant)


def test_no_embedding_is_paid_for_when_the_collection_is_missing(settings):
    q = _Qdrant(missing=True)
    calls = []
    with (
        patch("src.llm.completion.embed", side_effect=lambda *a, **k: calls.append(a)),
        pytest.raises(CollectionMissing),
    ):
        _retriever(settings, q).search("how does settlement work")
    assert calls == [], "the question was embedded for a collection that does not exist"
    assert q.queries == 0


def test_the_error_names_the_state_not_the_vendor(settings):
    """The Qdrant body names an internal collection and reads as a crash. This
    exception is ours, so its message can say what is actually true."""
    q = _Qdrant(missing=True)
    with pytest.raises(CollectionMissing) as exc:
        _retriever(settings, q).search("anything")
    assert "has been indexed" in str(exc.value) or "does not exist" in str(exc.value)


def test_an_unreachable_qdrant_is_not_reported_as_an_empty_vault(settings):
    """The asymmetry. A blip must not become "generate a vault"."""
    q = _Qdrant(unreachable=True)
    r = _retriever(settings, q)
    assert r.collection_exists() is True


def test_a_present_collection_is_searched_normally(settings):
    q = _Qdrant(missing=False)
    with patch("src.llm.completion.embed", return_value=[0.1] * 8):
        hits = _retriever(settings, q).search("anything")
    assert hits == []
    assert q.queries == 1


# ─── the classifier knows the new exception by TYPE ──────────────────


def test_the_classifier_recognises_it_without_reading_the_message():
    """`classify_vector_store` matched `doesn'?t exist` on the message. This
    exception's message is ours to word, and tying our own wording to a regex
    written for a vendor's response body is how the sentence a user reads
    starts depending on an English contraction."""
    from src.llm.errors import classify_vector_store

    assert classify_vector_store(
        CollectionMissing("code_analysis_vault")).code == "vault_not_generated"


def test_the_vendor_shape_is_still_recognised():
    from src.llm.errors import classify_vector_store

    assert classify_vector_store(RuntimeError(
        "Not found: Collection `code_analysis_vault` doesn't exist!"
    )).code == "vault_not_generated"


# ─── single-repo QA degrades instead of raising ──────────────────────


def test_single_repo_qa_degrades_rather_than_failing(settings, monkeypatch):
    """The vault is an accelerator: retrieval falls back to grep + graph +
    source. The multi-repo retriever has degraded on a Qdrant failure since it
    was written; this path let the exception out, so a workspace with no vault
    got a 500 from chat instead of a slightly thinner answer."""
    from src.qa.orchestrator import QAOrchestrator

    orch = QAOrchestrator.__new__(QAOrchestrator)
    orch.vault_unavailable = None
    orch.vault_ret = SimpleNamespace(
        search=lambda *a, **k: (_ for _ in ()).throw(
            CollectionMissing("code_analysis_vault")))

    assert orch._vault_search("q", repo="acme/worker") == []
    assert orch.vault_unavailable == "vault_not_generated"
