"""The embedding request must not drift from the vectors already in Qdrant.

Embeddings were the last native-SDK surface, kept as a documented exception
long after every completion moved to LiteLLM, because the failure mode is
unlike a completion's: a vector produced with a different model string, task
type or dimensionality is not wrong loudly — it lands in the same collection
and degrades every search that touches it, silently, until somebody pays for
a full re-index.

The exception was retired on evidence: the installed LiteLLM's ``gemini/``
route was captured at the wire posting the same ``batchEmbedContents`` body
the google-genai SDK posted — same ``models/<model>``, same ``taskType``,
same ``outputDimensionality``. These tests keep that evidence current:

  * what `completion.embed`/`embed_batch` HAND LiteLLM is pinned by exact
    dict equality, so a new field appears (or one disappears) only past a
    failing test;
  * what the installed LiteLLM PUTS ON THE WIRE for those exact kwargs is
    pinned against a captured HTTP body, so a LiteLLM upgrade that stops
    forwarding ``task_type`` or ``dimensions`` fails here instead of
    quietly flipping every install to symmetric, full-width embeddings;
  * the books the retired native branch used to keep — the spend row and
    the audit record — are asserted to still be written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.llm import completion
from src.llm.profiles import Profile

MODEL = "gemini-embedding-2"
DIMS = 3072
KEY = "AIzaSyTESTKEY-not-a-real-one"


def _direct_google() -> Profile:
    """The shape `_routed` returns for a direct-key Google workspace: no
    gateway fields, so `via_gateway` is False and `is_google` is True."""
    return Profile(
        surface="embeddings", provider="google", model=MODEL,
        api_key=KEY, raw_api_key=KEY, dimensions=DIMS,
    )


@pytest.fixture()
def books(monkeypatch, tmp_path):
    """Route the spend ledger into a list and the audit log into a tmp file,
    so the tests can read both without a database."""
    from src.llm import budget
    from src.security import audit as audit_mod

    rows: list[dict] = []
    monkeypatch.setattr(budget, "record_spend", lambda **kw: rows.append(kw))
    audit_path = tmp_path / "audit.jsonl"
    logger = audit_mod.AuditLogger(audit_path)
    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: logger)

    def records() -> list[dict]:
        if not audit_path.exists():
            return []
        return [json.loads(line) for line in
                audit_path.read_text(encoding="utf-8").splitlines()]

    return SimpleNamespace(spend=rows, audit=records)


def _capture_litellm(monkeypatch) -> list[dict]:
    """Swap `litellm.embedding` for a recorder that answers in shape."""
    import litellm

    calls: list[dict] = []

    def fake_embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[{"embedding": [float(i)] * DIMS}
                  for i in range(len(kwargs["input"]))],
            usage=SimpleNamespace(prompt_tokens=7),
        )

    monkeypatch.setattr(litellm, "embedding", fake_embedding)
    return calls


@pytest.fixture()
def direct_google(monkeypatch) -> Profile:
    p = _direct_google()
    monkeypatch.setattr(completion, "_routed", lambda surface, ws="default": p)
    # The seam must stay out of the way regardless of this machine's env —
    # these tests are about the profile path.
    monkeypatch.setattr(completion, "_configured_embedder", lambda: None)
    return p


# ─── what completion hands LiteLLM ───────────────────────────────────


def test_a_query_embed_sends_exactly_the_fields_the_collection_was_built_with(
    monkeypatch, books, direct_google,
):
    """Exact equality on purpose: a field that drifts, appears or disappears
    is a different embedding space wearing the same collection name."""
    calls = _capture_litellm(monkeypatch)
    s = get_settings()
    expected_task = (
        s.gemini_embedding_task_query if s.embedding_task_type_enabled
        else s.gemini_embedding_task_doc
    )

    vector = completion.embed("how does auth work")

    assert len(vector) == DIMS
    assert calls == [{
        "model": f"gemini/{MODEL}",
        "input": ["how does auth work"],
        "api_key": KEY,
        "task_type": expected_task,
        "dimensions": DIMS,
        # ≈ the native client's stop_after_attempt(5); a kwarg LiteLLM
        # consumes, never a wire field — the wire test below proves that.
        "num_retries": 4,
    }]


def test_a_document_batch_sends_the_document_side_and_keeps_order(
    monkeypatch, books, direct_google,
):
    calls = _capture_litellm(monkeypatch)
    s = get_settings()

    vectors = completion.embed_batch(["def a(): pass", "def b(): pass"])

    assert [v[0] for v in vectors] == [0.0, 1.0], "request order is the contract"
    assert calls == [{
        "model": f"gemini/{MODEL}",
        "input": ["def a(): pass", "def b(): pass"],
        "api_key": KEY,
        "task_type": s.gemini_embedding_task_doc,
        "dimensions": DIMS,
        "num_retries": 4,
    }]


def test_an_explicit_task_type_is_sent_verbatim(monkeypatch, books, direct_google):
    """The vault writes notes through `embed(..., task_type="RETRIEVAL_DOCUMENT")`
    — the caller's side of the asymmetric pair must survive the transport."""
    calls = _capture_litellm(monkeypatch)

    completion.embed("a vault note", task_type="RETRIEVAL_DOCUMENT",
                     operation="embed_note")

    assert calls[0]["task_type"] == "RETRIEVAL_DOCUMENT"


# ─── what the installed LiteLLM puts on the wire ─────────────────────


def test_the_installed_litellm_forwards_those_fields_to_the_wire(
    monkeypatch, books, direct_google,
):
    """Replay the EXACT kwargs `completion.embed_batch` produces through the
    real LiteLLM against a fake HTTP client, and read the body it would have
    posted. This is the half a monkeypatched `litellm.embedding` cannot see:
    the library was verified (wire capture, 2026-08) to map `task_type` →
    ``taskType`` and `dimensions` → ``outputDimensionality`` for ``gemini/``
    models — and an upgrade that stops doing either must fail HERE, not in
    the relevance of every search after the next index run.
    """
    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    with pytest.MonkeyPatch.context() as mp:
        captured_kwargs = _capture_litellm(mp)
        completion.embed_batch(["query one", "query two"])
    (kwargs,) = captured_kwargs

    wire: dict = {}

    class FakeClient(HTTPHandler):
        def post(self, url, *args, **kw):
            wire["url"] = url
            body = kw.get("json")
            if body is None and kw.get("data") is not None:
                body = json.loads(kw["data"])
            wire["body"] = body
            return httpx.Response(
                200,
                json={"embeddings": [{"values": [0.1] * 4}, {"values": [0.2] * 4}]},
                request=httpx.Request("POST", url),
            )

    resp = litellm.embedding(client=FakeClient(), **kwargs)

    assert wire["url"].endswith(f"models/{MODEL}:batchEmbedContents")
    requests = wire["body"]["requests"]
    assert [r["content"]["parts"][0]["text"] for r in requests] == \
        ["query one", "query two"]
    s = get_settings()
    for req in requests:
        # Exact key set: `num_retries` (or any future client kwarg) leaking
        # into the wire body would also be a changed embedding request.
        assert set(req) == {"model", "content", "taskType", "outputDimensionality"}
        assert req["model"] == f"models/{MODEL}"
        assert req["taskType"] == s.gemini_embedding_task_doc
        assert req["outputDimensionality"] == DIMS
    assert [d["embedding"][0] for d in resp.data] == [0.1, 0.2], \
        "LiteLLM must hand vectors back in request order"


# ─── the books the native branch used to keep ────────────────────────


def test_the_ledger_row_is_what_the_native_branch_wrote(
    monkeypatch, books, direct_google,
):
    """Surface "embeddings", the BARE model (pricing and the Usage page key on
    it), provider "google", the tenant — the row the Spend page reads."""
    _capture_litellm(monkeypatch)

    completion.embed("how does auth work", workspace_id="default")

    (row,) = books.spend
    assert row["surface"] == "embeddings"
    assert row["model"] == MODEL
    assert row["provider"] == "google"
    assert row["workspace_id"] == "default"
    assert row["tokens_in"] == 7 and row["tokens_out"] == 0


def test_a_direct_google_embed_still_writes_an_audit_record(
    monkeypatch, books, direct_google,
):
    """The native client wrote one per call; the transport change must not
    cost the audit trail its embedding records."""
    _capture_litellm(monkeypatch)

    completion.embed_batch(["a", "b"], operation="embed_notes_batch",
                           workspace_id="acme")

    (record,) = books.audit()
    assert record["mode"] == "embedding"
    assert record["model"] == MODEL
    assert record["operation"] == "embed_notes_batch"
    # "acme", not "default": normalize_workspace_id treats the literal
    # "default" as "caller never said" and stores None on purpose.
    assert record["workspace_id"] == "acme"
    assert record["input_tokens_estimated"] == 7
    assert record["extra"]["batch_size"] == 2
    assert record["extra"]["dimensions"] == DIMS


# ─── the behaviours that must NOT change with the transport ──────────


def test_task_type_is_not_sent_to_a_vendor_without_the_field(
    monkeypatch, books,
):
    """OpenAI answers 400 to an unknown embedding param — a workspace pointed
    at it must degrade to symmetric embeddings, not stop embedding."""
    p = Profile(surface="embeddings", provider="openai",
                model="text-embedding-3-large", api_key="sk-test",
                raw_api_key="sk-test", dimensions=3072)
    monkeypatch.setattr(completion, "_routed", lambda surface, ws="default": p)
    monkeypatch.setattr(completion, "_configured_embedder", lambda: None)
    calls = _capture_litellm(monkeypatch)

    completion.embed("how does auth work")

    assert "task_type" not in calls[0]
    assert "num_retries" not in calls[0], \
        "the retry budget mirrors the native GOOGLE client it replaced"
    assert books.audit() == [], \
        "non-Google direct embeds were never audited here; stay that way"


def test_a_google_workspace_without_a_key_fails_with_the_known_sentence(
    monkeypatch, books,
):
    """The native branch raised LLMCredentialError from its client factory
    before any request was formed. Same type, same sentence, same timing."""
    from src.llm.keys import LLMCredentialError

    p = Profile(surface="embeddings", provider="google", model=MODEL,
                api_key="", raw_api_key="", dimensions=DIMS)
    monkeypatch.setattr(completion, "_routed", lambda surface, ws="default": p)
    monkeypatch.setattr(completion, "_configured_embedder", lambda: None)
    calls = _capture_litellm(monkeypatch)

    with pytest.raises(LLMCredentialError, match="LLM Setup page"):
        completion.embed("anything")

    assert calls == [], "no key, no request — fail-closed"
