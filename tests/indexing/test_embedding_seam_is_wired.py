"""The seam is reachable, or it is decoration.

`src/indexing/vectors/embedder.py` was correct, tested, and called from nowhere
in `src/`: every real embedding went through `src.llm.completion.embed` /
`embed_batch`, which knew only about Gemini and LiteLLM. "Air-gapped" was
therefore a capability with no way to switch it on — the kind of feature that
passes its own tests forever.

So this file asserts the wiring rather than the implementation:

  1. the default install still takes the Gemini path, byte for byte;
  2. `EMBEDDING_PROVIDER=openai_compatible` makes the product's own embedding
     entry points post to a loopback server instead — with no profile, no
     gateway and no provider key involved;
  3. the document and query sides stay different sides through that entry
     point, because asymmetry that survives only in the layer below it is
     asymmetry the product does not have;
  4. the audit record that comes out names the tenant and the operation;
  5. the vector width the collection is built from follows the same switch,
     and refuses to be guessed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import Settings
from src.security.audit import AuditLogger

DIMS = 8
TENANT = "ws-acme"


# ─── a real embeddings server, on loopback ──────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Answers POST /v1/embeddings the way Ollama, vLLM and TEI do."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(body)
        inputs = body.get("input") or [""]
        payload = json.dumps({
            "object": "list",
            "model": body.get("model"),
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.125] * DIMS}
                for i in range(len(inputs))
            ],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the stderr spam
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _local_settings(server, **overrides) -> Settings:
    host, port = server.server_address[0], server.server_address[1]
    base = {
        "embedding_provider": "openai_compatible",
        "embedding_base_url": f"http://{host}:{port}/v1",
        "embedding_model": "nomic-embed-text",
        "embedding_dimensions": DIMS,
        "embedding_timeout_seconds": 5,
        "embedding_document_prefix": "search_document: ",
        "embedding_query_prefix": "search_query: ",
        # THE POINT: not one host on the internet is reachable.
        "egress_allowed_hosts": [],
        "egress_allow_private_network": True,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def use_settings(monkeypatch):
    """Install a Settings object as the one `completion` reads."""
    def _install(settings: Settings) -> Settings:
        monkeypatch.setattr("src.config.get_settings", lambda: settings)
        return settings
    return _install


@pytest.fixture
def audit(tmp_path: Path, monkeypatch) -> AuditLogger:
    log = AuditLogger(tmp_path / "audit.jsonl")
    # Patched where it is USED: embedder.py imported the name at module load,
    # so rebinding it in src.security.audit would rebind nothing.
    monkeypatch.setattr(
        "src.indexing.vectors.embedder.get_audit_logger", lambda: log,
    )
    return log


# ─── 1. the default install is untouched ────────────────────────────


def test_the_default_install_does_not_go_near_the_seam():
    """Three live workspaces embed through Gemini today. The seam must be
    invisible to them — not "equivalent", absent."""
    from src.llm.completion import _configured_embedder

    assert Settings().embedding_provider == "gemini"
    assert _configured_embedder() is None


def test_a_configured_provider_produces_a_local_embedder(local_server, use_settings):
    from src.indexing.vectors.embedder import OpenAICompatibleEmbedder
    from src.llm.completion import _configured_embedder

    use_settings(_local_settings(local_server))
    assert isinstance(_configured_embedder(), OpenAICompatibleEmbedder)


# ─── 2. the product's own entry points reach it ─────────────────────


def test_embed_batch_posts_to_the_local_server(local_server, use_settings, audit):
    """`embed_batch` is what indexing and the vault writer call. With the
    provider configured it must resolve no profile, mint no virtual key and
    open no socket to anybody's cloud."""
    from src.llm import completion

    use_settings(_local_settings(local_server))
    vectors = completion.embed_batch(
        ["def a(): pass", "def b(): pass"],
        operation="embed_notes_batch", workspace_id=TENANT,
    )

    assert vectors == [[0.125] * DIMS, [0.125] * DIMS]
    # ONE POST for the whole batch. It used to be one per chunk, which made
    # indexing pay a full HTTP round-trip per text against a schema that took
    # a list all along (measured: 20 texts = 20 POSTs).
    assert len(local_server.requests) == 1, "one POST per batch"
    request = local_server.requests[0]
    assert request["model"] == "nomic-embed-text"
    assert [t.removeprefix("search_document: ") for t in request["input"]] == \
        ["def a(): pass", "def b(): pass"], "order and prefix survive batching"


def test_embed_query_posts_to_the_local_server(local_server, use_settings, audit):
    from src.llm import completion

    use_settings(_local_settings(local_server))
    vector = completion.embed("how does login work", workspace_id=TENANT)

    assert vector == [0.125] * DIMS
    assert len(local_server.requests) == 1


def test_the_seam_is_not_overridable_by_the_workspace_profile(
    local_server, use_settings, audit, monkeypatch,
):
    """EMBEDDING_PROVIDER is an installation decision — "the code does not
    leave this box". A UI-selected profile that could override it would make
    the guarantee unenforceable, so the profile is never even resolved."""
    from src.llm import completion

    use_settings(_local_settings(local_server))

    def _explode(*a, **k):  # pragma: no cover — the assertion is that it is not called
        raise AssertionError("the embeddings profile was resolved anyway")

    monkeypatch.setattr(completion, "_routed", _explode)
    monkeypatch.setattr("src.llm.profiles.resolve_profile", _explode)

    completion.embed_batch(["x"], workspace_id=TENANT)
    assert len(local_server.requests) == 1


# ─── 3. the two sides stay two sides ────────────────────────────────


def test_document_and_query_sides_survive_the_entry_point(
    local_server, use_settings, audit,
):
    """The prefixes are how an OpenAI-shaped server expresses what `task_type`
    expresses on Gemini. If `completion.embed*` collapsed the sides, the layer
    below could not tell them apart and every search would be symmetric."""
    from src.llm import completion

    use_settings(_local_settings(local_server))

    completion.embed_batch(["def foo(): pass"], workspace_id=TENANT)
    assert local_server.requests[0]["input"][0].startswith("search_document: ")

    local_server.requests.clear()
    completion.embed("how does login work", workspace_id=TENANT)
    assert local_server.requests[0]["input"][0].startswith("search_query: ")


def test_an_explicit_document_task_type_is_still_a_document(
    local_server, use_settings, audit,
):
    """`embed()` defaults to the query side, but the vault writer calls it with
    RETRIEVAL_DOCUMENT for a note it is storing. That note must be embedded as
    a document on every provider, not just on the one with the enum."""
    from src.llm import completion

    use_settings(_local_settings(local_server))
    completion.embed("a note", task_type="RETRIEVAL_DOCUMENT",
                     operation="embed_note", workspace_id=TENANT)
    assert local_server.requests[0]["input"][0].startswith("search_document: ")


# ─── task_type, end to end and on by default ────────────────────────


def test_task_type_is_on_by_default_and_reads_the_configured_values():
    from src.llm.completion import _effective_task_type

    s = Settings()
    assert s.embedding_task_type_enabled is True
    assert _effective_task_type(None, query=True) == s.gemini_embedding_task_query
    assert _effective_task_type(None, query=False) == s.gemini_embedding_task_doc
    assert _effective_task_type(None, query=True) != _effective_task_type(None, query=False)


def test_turning_it_off_means_symmetric_not_empty(use_settings):
    """An empty task_type is a 400 from Gemini, not a symmetric embedding. Off
    has to mean a real value used on both sides — and it has to beat an
    explicit argument, or the switch is one a caller can route around."""
    from src.llm.completion import _effective_task_type

    s = use_settings(Settings(embedding_task_type_enabled=False))
    assert _effective_task_type(None, query=True) == s.gemini_embedding_task_doc
    assert _effective_task_type(None, query=False) == s.gemini_embedding_task_doc
    assert _effective_task_type("CODE_RETRIEVAL_QUERY", query=True) == \
        s.gemini_embedding_task_doc


def test_a_provider_without_the_field_degrades_to_symmetric():
    """LiteLLM forwards an unknown embedding param in `extra_body`, and OpenAI
    answers 400 for an argument it does not know. Sending `task_type` to a
    non-Google deployment would not cost the asymmetry — it would cost the
    embedding."""
    from src.llm.completion import _embedding_kwargs, _expressible_task_type
    from src.llm.profiles import Profile

    def _routed_profile(provider: str) -> Profile:
        return Profile(
            surface="embeddings", provider=provider, model="m", api_key="sk-virtual",
            dimensions=1536, gateway_model="celmis-acme-embed",
            gateway_url="http://litellm:4000", gateway_underlying=f"{provider}/m",
        )

    google = _routed_profile("google")
    openai = _routed_profile("openai")

    assert _expressible_task_type(google, "RETRIEVAL_DOCUMENT") == "RETRIEVAL_DOCUMENT"
    assert _expressible_task_type(openai, "RETRIEVAL_DOCUMENT") == ""
    assert _embedding_kwargs(google, ["x"], "RETRIEVAL_DOCUMENT")["task_type"] == \
        "RETRIEVAL_DOCUMENT"
    assert "task_type" not in _embedding_kwargs(openai, ["x"], "RETRIEVAL_DOCUMENT")


def test_the_side_is_read_from_the_task_type_not_from_the_call_site():
    from src.llm.completion import _is_query_side

    assert _is_query_side("RETRIEVAL_QUERY")
    assert _is_query_side("CODE_RETRIEVAL_QUERY")
    assert _is_query_side("QUESTION_ANSWERING")
    assert not _is_query_side("RETRIEVAL_DOCUMENT")
    # Unknown → document. A query embedded as a document costs one search; a
    # document embedded as a query is written into the index.
    assert not _is_query_side("SOMETHING_NEW")
    assert not _is_query_side("")


# ─── 4. the record says whose call it was ───────────────────────────


def test_the_local_path_still_writes_an_attributed_audit_record(
    local_server, use_settings, audit,
):
    """Switching provider must not drop embedding calls out of their owner's
    audit page — an unattributed record is global-admin-only by design."""
    from src.llm import completion

    use_settings(_local_settings(local_server))
    completion.embed_batch(["x"], operation="embed_notes_batch", workspace_id=TENANT)

    records = [json.loads(line) for line in audit.log_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["workspace_id"] == TENANT
    assert records[0]["mode"] == "embedding"
    assert records[0]["operation"] == "embed_notes_batch"
    assert records[0]["model"] == "nomic-embed-text"


def test_a_failing_chunk_fails_the_batch_rather_than_returning_a_hole(
    local_server, use_settings, audit, monkeypatch,
):
    """The seam isolates per-chunk errors so an index run survives a bad file.
    `embed_batch` promises a vector per text, in order, to a caller that zips
    them into Qdrant points — an empty vector there is a point nothing will
    ever retrieve."""
    from src.indexing.vectors import embedder as embedder_mod
    from src.llm import completion

    use_settings(_local_settings(local_server))

    def _down(self, text, task_type):
        raise RuntimeError("server down")

    monkeypatch.setattr(embedder_mod.OpenAICompatibleEmbedder, "_embed_one", _down)
    monkeypatch.setattr(embedder_mod.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="embedding failed"):
        completion.embed_batch(["x"], workspace_id=TENANT)


# ─── 5. the width follows the same switch ───────────────────────────


def test_the_collection_width_comes_from_the_configured_provider(
    local_server, use_settings,
):
    from src.llm.completion import embedding_dimensions

    use_settings(_local_settings(local_server))
    assert embedding_dimensions() == DIMS


def test_an_unknown_width_is_refused_not_guessed(local_server, use_settings):
    """`embedding_dimensions=0` means "whatever the server returns", which is
    an answer for a request and no answer at all for a collection. Falling back
    to Gemini's 3072 would build a collection that rejects every vector this
    install can produce — after a full index run."""
    from src.llm.completion import embedding_dimensions

    use_settings(_local_settings(local_server, embedding_dimensions=0))
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSIONS"):
        embedding_dimensions()


# ─── 6. the width guard on an existing collection ───────────────────
#
# THESE USED TO POINT AT THE WRONG IMPLEMENTATION. There were two width
# guards: one on `qdrant_indexer.QdrantIndexer` for the code-chunk collection,
# and one on `VaultRetriever` for the vault. The three tests here covered the
# first — a module the running product never reached, since deleted — and the
# LIVE guard, the one every vault build passes through, had no test at all.
#
# So they are re-pointed rather than removed. The vault writes an UNNAMED
# vector, and its width comes from the embedding just produced rather than
# from config: `embedding_dimensions` defaults to 0 ("whatever the server
# returns"), so config cannot answer and the vectors in hand can. That is why
# `ensure_collection` takes the width as an argument here — and why the third
# old test, which checked that a misconfigured provider could not fall back to
# the Gemini width, has no equivalent: the live method never resolves the
# width itself. That concern is covered directly by
# `test_a_local_provider_must_state_its_width` above.


def _memory_client():
    from qdrant_client import QdrantClient

    return QdrantClient(":memory:")


def _vault_collection_with_width(client, name: str, width: int) -> None:
    """A vault-shaped collection: one unnamed vector, no sparse companion."""
    from qdrant_client import models

    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=width, distance=models.Distance.COSINE,
        ),
    )


def _vault(settings, client):
    from src.retrieval.tier1_vault import VaultRetriever

    return VaultRetriever(settings, client, workspace_id="ws-width")


def test_ensure_collection_accepts_a_collection_of_the_right_width(
    local_server, use_settings,
):
    settings = use_settings(_local_settings(local_server))
    client = _memory_client()
    try:
        _vault_collection_with_width(client, settings.qdrant_collection, DIMS)
        assert _vault(settings, client).ensure_collection(DIMS) is True
    finally:
        client.close()


def test_ensure_collection_creates_the_collection_when_there_is_none(
    local_server, use_settings,
):
    """Nothing created it. The vault's own collection had no creator anywhere
    in the codebase, so on a deployment where it had never been made by hand
    every upsert died with a 404 that `batched_qdrant` downgraded to a
    warning — and `readyz` reported `qdrant: {ok: true, collections: 0}` while
    every vault build wrote markdown and no vectors."""
    settings = use_settings(_local_settings(local_server))
    client = _memory_client()
    try:
        assert not client.collection_exists(settings.qdrant_collection)
        assert _vault(settings, client).ensure_collection(DIMS) is True
        assert client.collection_exists(settings.qdrant_collection)
    finally:
        client.close()


def test_ensure_collection_refuses_a_collection_of_the_wrong_width(
    local_server, use_settings,
):
    """It used to return the moment the collection existed. Switch the model
    and every upsert then fails with Qdrant's own message, which names neither
    number and does not say that a re-index is the fix."""
    from src.retrieval.tier1_vault import CollectionWidthMismatch

    settings = use_settings(_local_settings(local_server))
    client = _memory_client()
    try:
        _vault_collection_with_width(client, settings.qdrant_collection, DIMS * 4)
        with pytest.raises(CollectionWidthMismatch) as excinfo:
            _vault(settings, client).ensure_collection(DIMS)
    finally:
        client.close()

    message = str(excinfo.value)
    assert str(DIMS * 4) in message and str(DIMS) in message, (
        "both numbers or it is not a diagnosis"
    )
    assert "searched" in message, "it has to say what is lost, not just that it refused"
