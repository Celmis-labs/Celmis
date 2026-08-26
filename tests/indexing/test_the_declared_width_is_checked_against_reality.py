"""One wrong digit in EMBEDDING_DIMENSIONS used to cost the whole index.

The bug, replayed: EMBEDDING_DIMENSIONS=1024 with a 768-wide model builds a
1024-wide Qdrant collection; the server keeps answering 768; every upsert is
rejected; the vault writer records each rejection as a warning — and the run
finishes green over an EMPTY index. `_dimensions_for` reported whatever came
back, so the declared number never met a real vector anywhere.

So the declared width is now checked against the first vector that actually
arrives — once per embedder instance — and a mismatch raises
EmbeddingConfigMismatch (the same type the gateway raises for the profile
drifting away from the collection: callers already treat it as "fix the
config or re-index"). Crucially it PROPAGATES out of the run instead of
being recorded per chunk: per-chunk isolation swallowed into warnings is the
exact mechanism that made the original failure silent.

Also proven here, because they shipped together:
  - the batch path — one POST carries up to EMBED_BATCH_SIZE documents where
    it used to be one POST per chunk (measured: 20 texts = 20 round-trips),
    order preserved even against a server that answers out of order;
  - the compose forwarding — the api container is the only place src/ runs,
    and an EMBEDDING_* variable compose does not forward is a setting that
    does not exist in the only supported deployment. Parsed as YAML data,
    never grepped: a variable named in a comment must not pass this test.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from src.config import Settings
from src.indexing.vectors.embedder import (
    EMBED_BATCH_SIZE,
    OpenAICompatibleEmbedder,
)
from src.llm.gateway import EmbeddingConfigMismatch
from src.security.audit import AuditLogger
from src.security.redactor import Redactor

#: What the fake server actually returns — the "reality" of the file name.
SERVER_DIMS = 8


# ─── a real embeddings server, on loopback ──────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """One vector per input, `dimensions` hint IGNORED — Ollama's behaviour,
    which is precisely the one that made the width check necessary. Vector i
    is [i, i, …] so a reordering bug shows up as wrong values, not just as a
    hunch. `shuffle` answers in reverse wire order with correct `index`
    fields, which the OpenAI schema explicitly allows."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "body": body})
        inputs = body.get("input") or [""]
        data = [
            {"object": "embedding", "index": i,
             "embedding": [float(i)] * self.server.width}
            for i in range(len(inputs))
        ]
        if self.server.shuffle:
            data = list(reversed(data))
        payload = json.dumps({
            "object": "list",
            "model": body.get("model"),
            "data": data,
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
    server.width = SERVER_DIMS
    server.shuffle = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def _settings(server, **overrides) -> Settings:
    host, port = server.server_address[0], server.server_address[1]
    base = {
        "embedding_provider": "openai_compatible",
        "embedding_base_url": f"http://{host}:{port}/v1",
        "embedding_model": "nomic-embed-text",
        "embedding_dimensions": SERVER_DIMS,
        "embedding_timeout_seconds": 5,
        "egress_allowed_hosts": [],
        "egress_allow_private_network": True,
    }
    base.update(overrides)
    return Settings(**base)


def _embedder(settings, audit) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(
        settings=settings, audit=audit, redactor=Redactor(fail_closed=True),
    )


# ─── 1. the declared width meets reality ────────────────────────────


def test_a_wrong_declared_width_raises_with_both_numbers(local_server, audit):
    """The replay of the original bug, with the new ending: the run STOPS.

    pytest.raises around list() is the point — an implementation that filed
    the mismatch into per-chunk `error` fields would hand the vault writer
    the same swallowable warnings that produced the empty index."""
    emb = _embedder(_settings(local_server, embedding_dimensions=1024), audit)

    with pytest.raises(EmbeddingConfigMismatch) as excinfo:
        list(emb.embed_documents([("c1", "def a(): pass"), ("c2", "def b(): pass")]))

    message = str(excinfo.value)
    assert "1024" in message, "the declared number, or it is not a diagnosis"
    assert str(SERVER_DIMS) in message, "the real number, or it is not a diagnosis"
    assert "EMBEDDING_DIMENSIONS" in message, "name the knob the operator must turn"


def test_a_mismatch_is_not_retried_and_not_isolated(local_server, audit):
    """Retrying configuration is 7 seconds of backoff arriving at the same
    number, and per-chunk fallback would ask 64 more times. One request,
    one loud answer."""
    emb = _embedder(_settings(local_server, embedding_dimensions=1024), audit)

    with pytest.raises(EmbeddingConfigMismatch):
        list(emb.embed_documents([("c1", "a"), ("c2", "b"), ("c3", "c")]))

    assert len(local_server.requests) == 1


def test_the_query_side_is_checked_too(local_server, audit):
    """A query embedded at the wrong width searches a collection it cannot
    match — retrieval would quietly return nothing, forever."""
    emb = _embedder(_settings(local_server, embedding_dimensions=1024), audit)

    with pytest.raises(EmbeddingConfigMismatch):
        emb.embed_query("how does login work")


def test_a_matching_width_passes(local_server, audit):
    results = list(_embedder(_settings(local_server), audit).embed_documents(
        [("c1", "def a(): pass"), ("c2", "def b(): pass")],
    ))

    assert [r.error for r in results] == [None, None]
    assert all(r.dimensions == SERVER_DIMS for r in results)


def test_zero_declared_dimensions_accepts_whatever_comes(local_server, audit):
    """0 means "whatever the server returns" — there is no declaration to
    contradict, so nothing to check (creating the collection is where 0 is
    refused, and that guard lives in the indexer, not here)."""
    settings = _settings(local_server, embedding_dimensions=0)
    results = list(_embedder(settings, audit).embed_documents([("c1", "x")]))

    assert results[0].error is None
    assert results[0].dimensions == SERVER_DIMS


def test_the_check_runs_once_per_instance(local_server, audit):
    """Configuration cannot change between chunk 12 and chunk 13, so the
    check is a flag, not a per-vector tax. Observable consequence: a server
    that changes width MID-RUN (a pathology no config knob causes) slips past
    a warmed instance — and is caught by the next fresh one."""
    emb = _embedder(_settings(local_server), audit)
    assert list(emb.embed_documents([("c1", "x")]))[0].error is None

    local_server.width = 4  # the pathology
    results = list(emb.embed_documents([("c2", "y")]))
    assert results[0].error is None, "warmed instance: the flag is set, no re-check"
    assert results[0].dimensions == 4

    with pytest.raises(EmbeddingConfigMismatch):
        list(_embedder(_settings(local_server), audit).embed_documents([("c3", "z")]))


# ─── 2. the batch path ──────────────────────────────────────────────


def test_twenty_documents_are_one_request(local_server, audit):
    """Measured before batching: 20 texts = 20 HTTP POSTs, against a schema
    that took a list all along."""
    chunks = [(f"c{i}", f"def f{i}(): pass") for i in range(20)]
    results = list(_embedder(_settings(local_server), audit).embed_documents(chunks))

    assert len(local_server.requests) == 1
    assert len(local_server.requests[0]["body"]["input"]) == 20
    assert [r.error for r in results] == [None] * 20


def test_order_is_preserved_even_when_the_server_answers_out_of_order(
    local_server, audit,
):
    """The OpenAI schema ties vectors to inputs by `index` and promises
    nothing about order. The server here answers REVERSED: a client that
    trusted wire order would hand every chunk its neighbour's vector — valid,
    wrong, and invisible until retrieval quality rots."""
    local_server.shuffle = True
    results = list(_embedder(_settings(local_server), audit).embed_documents(
        [(f"c{i}", f"text {i}") for i in range(5)],
    ))

    assert [r.chunk_id for r in results] == [f"c{i}" for i in range(5)]
    # Vector i is [i, i, …] by the fake's construction — the values prove the
    # pairing, not just the count.
    assert [r.vector[0] for r in results] == [float(i) for i in range(5)]


def test_more_than_one_window_splits_and_keeps_order(local_server, audit):
    """The split is still 64 + 3; only the ARRIVAL order stopped being a
    promise.

    This used to assert `sizes == [64, 3]` and it was really asserting that
    the two requests were made one after the other — which is the thing
    concurrency deliberately removed, and the short window regularly beats the
    long one to the socket. Sorted, because the claim worth keeping is that
    the windows are cut at `EMBED_BATCH_SIZE` and nothing is sent twice; the
    claim about ORDER belongs on the results, where the contract actually
    lives, and it is asserted below unchanged."""
    n = EMBED_BATCH_SIZE + 3
    chunks = [(f"c{i}", f"text {i}") for i in range(n)]
    results = list(_embedder(_settings(local_server), audit).embed_documents(chunks))

    sizes = sorted(len(r["body"]["input"]) for r in local_server.requests)
    assert sizes == [3, EMBED_BATCH_SIZE]
    assert [r.chunk_id for r in results] == [f"c{i}" for i in range(n)]
    assert all(r.error is None for r in results)


def test_prefixes_are_applied_to_every_document_in_a_batch(local_server, audit):
    """The side (document/query) is a property of the call, not of the batch
    size — nomic/e5-style models take it as a literal prefix on EACH text."""
    settings = _settings(
        local_server,
        embedding_document_prefix="search_document: ",
        embedding_query_prefix="search_query: ",
    )
    emb = _embedder(settings, audit)

    list(emb.embed_documents([("c1", "alpha"), ("c2", "beta")]))
    inputs = local_server.requests[0]["body"]["input"]
    assert inputs == ["search_document: alpha", "search_document: beta"]

    local_server.requests.clear()
    emb.embed_query("how does login work")
    assert local_server.requests[0]["body"]["input"] == \
        ["search_query: how does login work"]


def test_the_batch_still_writes_one_audit_record_per_text(local_server, audit):
    """The accounting contract (src/llm/completion._seam_embed documents it)
    counts texts. A per-request record would have shrunk a tenant's visible
    usage 64-fold the day batching landed."""
    list(_embedder(_settings(local_server), audit).embed_documents(
        [("c1", "x"), ("c2", "y"), ("c3", "z")], repo="test-repo",
    ))

    assert len(local_server.requests) == 1, "batched on the wire"
    lines = audit.log_path.read_text().splitlines()
    assert len(lines) == 3, "but still one audit record per text"


def test_the_mismatch_is_loud_through_the_product_entry_point(
    local_server, audit, monkeypatch,
):
    """End to end: `completion.embed_batch` is what indexing calls, and the
    mismatch must surface there as the exception itself — not as the generic
    "embedding failed" wrapper that per-chunk errors get."""
    from src.llm import completion

    settings = _settings(local_server, embedding_dimensions=1024)
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    monkeypatch.setattr(
        "src.indexing.vectors.embedder.get_audit_logger", lambda: audit,
    )

    with pytest.raises(EmbeddingConfigMismatch):
        completion.embed_batch(["def a(): pass", "def b(): pass"],
                               workspace_id="ws-acme")


# ─── 3. the deployment can actually switch any of this on ───────────

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

#: Every service that executes src/ — and can therefore reach
#: src/indexing/vectors — must receive the whole family. Today that is the
#: api container alone: the review poller and the vault writer run inside
#: its process, the sandbox is deliberately env-free, and postgres/qdrant/
#: litellm/web do not run this codebase. A new worker service belongs here.
_SERVICES_RUNNING_SRC = ("api",)

_FORWARDED = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_TIMEOUT_SECONDS",
    "EMBEDDING_DOCUMENT_PREFIX",
    "EMBEDDING_QUERY_PREFIX",
    "EGRESS_ALLOW_PRIVATE_NETWORK",
)


def _compose_environment(service: str) -> dict:
    compose = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    environment = compose["services"][service]["environment"]
    assert isinstance(environment, dict), f"{service}: expected mapping-style environment"
    return environment


def test_compose_forwards_the_embedding_family_to_every_src_service():
    """A Settings field the container never receives is a feature that does
    not exist in the shipped deployment — which is exactly how the local
    embedder shipped: correct, tested, and unreachable."""
    for service in _SERVICES_RUNNING_SRC:
        environment = _compose_environment(service)
        for var in _FORWARDED:
            assert var in environment, f"{service} does not forward {var}"
            assert f"${{{var}" in str(environment[var]), (
                f"{service}.{var} must interpolate the host value, "
                f"got {environment[var]!r}"
            )


def test_the_forwarded_defaults_do_not_brick_a_default_install(monkeypatch):
    """`VAR: "${VAR:-}"` writes an EMPTY STRING into the container env, and
    pydantic refuses "" for an int/bool field — Settings() would raise on
    every request and the api container would crash-loop on installs that
    never heard of local embeddings. So each compose default must construct
    a Settings equal to the vanilla one, verified by building Settings from
    exactly what a default install's container receives."""
    for var in _FORWARDED:
        monkeypatch.delenv(var, raising=False)
    environment = _compose_environment("api")
    container_env: dict[str, str] = {}
    for var in _FORWARDED:
        raw = str(environment[var])
        assert raw.startswith("${") and raw.endswith("}") and ":-" in raw, (
            f"{var}: expected '${{{var}:-<default>}}', got {raw!r}"
        )
        container_env[var.lower()] = raw[2:-1].split(":-", 1)[1]

    # _env_file=None on both: this asserts compose against Settings, not
    # against whatever a developer's local .env happens to say.
    from_compose = Settings(_env_file=None, **container_env)
    vanilla = Settings(_env_file=None)
    for var in _FORWARDED:
        field = var.lower()
        assert getattr(from_compose, field) == getattr(vanilla, field), (
            f"{var}: compose default changes behaviour "
            f"({getattr(from_compose, field)!r} != {getattr(vanilla, field)!r})"
        )
