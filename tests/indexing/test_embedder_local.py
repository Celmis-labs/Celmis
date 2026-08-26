"""The proof, which matters more than the abstraction.

The claim being sold is not "we support Ollama". It is: install this, empty
the egress allowlist, and your source code does not leave the machine. A claim
like that is worth exactly as much as the test behind it, so this file tries to
break it rather than to demonstrate it.

The shape of an honest proof here is three assertions, not one:

  1. With `egress_allowed_hosts` EMPTY, the local backend still embeds.
  2. Under the same config with the private-network switch OFF, it is BLOCKED —
     which is what proves assertion 1 was subject to the check rather than
     bypassing it. Without this, a client that never consulted the allowlist at
     all would pass test 1 and the suite would be lying.
  3. A public host is still refused with the switch ON — the switch opens the
     LAN, not the internet.

What the test cannot prove, stated rather than papered over: `is_private_destination`
resolves the hostname, and on a box whose resolver is off-machine that DNS query
is itself a packet leaving. A literal IP (127.0.0.1) has no lookup, which is why
this test uses one and why the settings docs recommend one.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import Settings
from src.indexing.vectors.embedder import (
    Embedder,
    OpenAICompatibleEmbedder,
    get_embedder,
)
from src.llm.gateway import EmbeddingConfigMismatch
from src.security.audit import AuditLogger
from src.security.egress import EgressBlockedError, is_private_destination
from src.security.redactor import Redactor

DIMS = 8


# ─── a real embeddings server, on loopback ──────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Answers POST /v1/embeddings the way Ollama, vLLM and TEI do."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "body": body,
                                     "headers": dict(self.headers)})
        inputs = body.get("input") or [""]
        payload = json.dumps({
            "object": "list",
            "model": body.get("model"),
            "data": [{"object": "embedding", "index": i,
                      "embedding": [0.125] * DIMS}
                     for i in range(len(inputs))],
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
    """A real HTTP server on 127.0.0.1 — not a mock.

    Mocking the transport would test the mock. The point of this file is that
    a socket really opens and really stays on the machine.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _settings(server, **overrides) -> Settings:
    """The exact configuration a no-egress install runs."""
    host, port = server.server_address[0], server.server_address[1]
    base = {
        "embedding_provider": "openai_compatible",
        "embedding_base_url": f"http://{host}:{port}/v1",
        "embedding_model": "nomic-embed-text",
        "embedding_dimensions": DIMS,
        "embedding_timeout_seconds": 5,
        # THE POINT: not one host on the internet is reachable.
        "egress_allowed_hosts": [],
        "egress_allow_private_network": True,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def _embedder(settings, audit) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(
        settings=settings, audit=audit, redactor=Redactor(fail_closed=True),
    )


# ─── 1. it embeds with the allowlist emptied ────────────────────────


def test_local_embedder_works_with_an_empty_egress_allowlist(local_server, audit):
    """No allowed host, anywhere — and indexing still produces vectors."""
    settings = _settings(local_server)
    assert settings.egress_allowed_hosts == []

    results = list(_embedder(settings, audit).embed_documents([("c1", "def foo(): pass")]))

    assert len(results) == 1
    assert results[0].error is None
    assert results[0].vector == [0.125] * DIMS
    assert results[0].dimensions == DIMS
    assert results[0].model == "nomic-embed-text"
    # The request really went to the loopback server, and to the OpenAI route.
    assert len(local_server.requests) == 1
    assert local_server.requests[0]["path"] == "/v1/embeddings"


def test_query_side_also_embeds_with_nothing_allowed(local_server, audit):
    """Retrieval is the other half; an install where indexing works offline and
    search does not is not an offline install."""
    vector = _embedder(_settings(local_server), audit).embed_query("how does login work")
    assert vector == [0.125] * DIMS


def test_no_outbound_host_is_reachable_under_that_config(local_server):
    """The same client, asked for the internet, refuses — including the vendor
    this product used to be nailed to."""
    settings = _settings(local_server)
    from src.security.egress import build_http_client

    client = build_http_client(
        allowed_hosts=settings.egress_allowed_hosts, timeout=2.0,
        allow_private_network=settings.egress_allow_private_network,
    )
    try:
        for url in (
            "https://generativelanguage.googleapis.com/v1beta/models",
            "https://api.openai.com/v1/embeddings",
            "https://api.github.com/zen",
        ):
            with pytest.raises(EgressBlockedError):
                client.get(url)
    finally:
        client.close()


# ─── 2. the check is genuinely in the path ──────────────────────────


def test_the_whitelist_transport_is_really_in_the_embedder_path(local_server, audit):
    """Turn the private-network switch OFF and the SAME call must fail.

    This is the assertion that makes test 1 mean something. An implementation
    that built a plain httpx.Client and never consulted egress policy would
    sail through test 1 and fail here.
    """
    settings = _settings(local_server, egress_allow_private_network=False)

    results = list(_embedder(settings, audit).embed_documents([("c1", "code")]))

    assert results[0].vector == []
    assert "EgressBlockedError" in (results[0].error or "")
    # And nothing reached the server.
    assert local_server.requests == []


def test_a_refusal_is_not_retried(local_server, audit):
    """A policy decision does not become allowed by asking three times; the
    backoff would only make an offline misconfiguration slow to diagnose."""
    settings = _settings(local_server, egress_allow_private_network=False)
    emb = _embedder(settings, audit)

    calls = {"n": 0}
    original = emb._embed_one

    def counting(text, task_type):
        calls["n"] += 1
        return original(text, task_type)

    emb._embed_one = counting
    list(emb.embed_documents([("c1", "code")], max_retries=3))
    assert calls["n"] == 1


# ─── 3. the switch opens the LAN, not the internet ──────────────────


def test_public_base_url_is_blocked_even_with_private_network_on(audit, local_server):
    """Someone pointing `embedding_base_url` at a hosted endpoint does not get
    a quiet exemption because the local backend is selected."""
    settings = _settings(
        local_server, embedding_base_url="https://api.openai.com/v1",
    )
    results = list(_embedder(settings, audit).embed_documents([("c1", "code")]))
    assert "EgressBlockedError" in (results[0].error or "")


def test_cloud_metadata_address_is_not_private(local_server, audit):
    """169.254.169.254 is `is_private == True` by `ipaddress`'s reckoning, and
    is the one address an SSRF wants. The switch must not hand it over."""
    assert is_private_destination("169.254.169.254") is False

    settings = _settings(local_server, embedding_base_url="http://169.254.169.254/v1")
    results = list(_embedder(settings, audit).embed_documents([("c1", "code")]))
    assert "EgressBlockedError" in (results[0].error or "")


def test_private_classification_is_by_address_not_by_name():
    assert is_private_destination("127.0.0.1") is True
    assert is_private_destination("10.0.0.7") is True
    assert is_private_destination("192.168.1.10") is True
    assert is_private_destination("8.8.8.8") is False
    assert is_private_destination("") is False
    # Unresolvable is unknown, and unknown is not private.
    assert is_private_destination("no-such-host.invalid") is False


# ─── the Google SDK is not merely unused — it is absent ─────────────


def test_the_local_path_runs_without_google_genai_installed(local_server, audit):
    """An air-gapped install will not have `google-genai` on disk at all.

    A module-scope `from google import genai` made that install impossible to
    even import, which is a different failure from "sends data to Google" and
    just as fatal. So: block the package outright, reload the module, embed.
    """
    class _Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "google" or fullname.startswith("google."):
                raise ImportError(f"{fullname} is not installed (simulated air gap)")
            return None

    # Execute the module source fresh under a throwaway name rather than
    # reloading the canonical one: `importlib.reload` would rebind the classes
    # every other test in this process already holds, and a proof that breaks
    # its neighbours is a proof nobody keeps.
    source = Path(sys.modules["src.indexing.vectors.embedder"].__file__)
    saved = {k: v for k, v in sys.modules.items()
             if k == "google" or k.startswith("google.")}
    for name in saved:
        del sys.modules[name]
    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(ImportError):
            importlib.import_module("google.genai")   # the air gap is real

        spec = importlib.util.spec_from_file_location("embedder_airgapped", source)
        fresh = importlib.util.module_from_spec(spec)
        # @dataclass resolves annotations through sys.modules[cls.__module__].
        sys.modules[spec.name] = fresh
        try:
            spec.loader.exec_module(fresh)           # raises on a module-scope import
        except BaseException:
            del sys.modules[spec.name]
            raise

        emb = fresh.OpenAICompatibleEmbedder(
            settings=_settings(local_server), audit=audit,
            redactor=Redactor(fail_closed=True),
        )
        results = list(emb.embed_documents([("c1", "def foo(): pass")]))
        assert results[0].error is None
        assert results[0].vector == [0.125] * DIMS
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.pop("embedder_airgapped", None)
        sys.modules.update(saved)


# ─── the guarantees that must not differ between backends ───────────


def test_secrets_are_redacted_before_they_reach_the_local_server(local_server, audit):
    """Local does not mean trusted. The same redaction runs, because "it never
    leaves the box" is not a reason to write an AWS key into a vector store."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    results = list(_embedder(_settings(local_server), audit).embed_documents(
        [("c1", f"const KEY = '{secret}'")],
    ))

    sent = local_server.requests[0]["body"]["input"][0]
    assert secret not in sent, f"SECRET LEAKED: {sent!r}"
    assert "[REDACTED:" in sent
    assert results[0].redaction_stats["secrets_found"] >= 1


def test_redaction_failure_blocks_the_send(local_server, audit):
    emb = _embedder(_settings(local_server), audit)
    emb.redactor.redact = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))

    results = list(emb.embed_documents([("c1", "code")]))

    assert "redaction_failed" in (results[0].error or "")
    assert local_server.requests == []


def test_one_bad_chunk_does_not_stop_the_run(local_server, audit):
    """Same isolation the Gemini path has: indexing a large repo must not lose
    9,000 chunks because chunk 12 failed.

    Since batching, three chunks travel as ONE request and a batch fails as a
    unit — so the guarantee now rests on the fallback: a failed batch is
    retried chunk by chunk through the single-text path, and only the culprit
    records an error."""
    emb = _embedder(_settings(local_server), audit)
    original = emb._embed_one
    seen = {"n": 0}

    def flaky(text, task_type):
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("server hiccup")
        return original(text, task_type)

    def batch_rejected(texts, task_type):
        raise RuntimeError("batch rejected")

    emb._embed_one = flaky
    emb._embed_many = batch_rejected
    results = list(emb.embed_documents(
        [("c1", "a"), ("c2", "b"), ("c3", "c")], max_retries=1,
    ))

    assert len(results) == 3
    assert results[0].error is None
    assert results[1].error is not None
    assert results[2].error is None


def test_every_embed_writes_an_audit_record(local_server, audit):
    list(_embedder(_settings(local_server), audit).embed_documents(
        [("c1", "x"), ("c2", "y")], repo="test-repo",
    ))
    lines = audit.log_path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert record["mode"] == "embedding"
        assert record["repo"] == "test-repo"
        assert record["model"] == "nomic-embed-text"


# ─── wiring ─────────────────────────────────────────────────────────


def test_the_implementation_satisfies_the_protocol(local_server):
    assert isinstance(_embedder(_settings(local_server), None), Embedder)


def test_the_default_install_does_not_come_through_this_module_at_all():
    """This used to assert that default settings select GeminiEmbedder, and it
    passed while being untrue of every install: `completion._configured_embedder()`
    returns None for "gemini" and never calls `get_embedder`, so the default
    value has always meant "ask the profile/gateway path instead". The class it
    named is gone; the selector now says so rather than answering."""
    assert Settings().embedding_provider == "gemini"
    with pytest.raises(ValueError, match="src.llm.completion.embed"):
        get_embedder()


def test_selection_follows_settings(local_server):
    assert isinstance(get_embedder(_settings(local_server)), OpenAICompatibleEmbedder)


def test_misconfiguration_is_refused_loudly():
    """Selecting the local backend without a URL must fail at construction, not
    produce vectors from somewhere else."""
    with pytest.raises(ValueError, match="EMBEDDING_BASE_URL"):
        OpenAICompatibleEmbedder(settings=Settings(embedding_provider="openai_compatible"))
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        OpenAICompatibleEmbedder(settings=Settings(
            embedding_provider="openai_compatible",
            embedding_base_url="http://127.0.0.1:11434/v1",
        ))
    with pytest.raises(ValueError, match="embedding_provider"):
        Settings(embedding_provider="ollamma")


def test_auth_header_only_when_a_key_is_configured(local_server, audit):
    """Ollama wants no token; vLLM/TEI can be started with one. Sending an
    empty Bearer to a server that checks is a 401 nobody can read."""
    list(_embedder(_settings(local_server), audit).embed_documents([("c1", "x")]))
    assert "Authorization" not in local_server.requests[0]["headers"]

    local_server.requests.clear()
    settings = _settings(local_server, embedding_api_key="s3cret")
    list(_embedder(settings, audit).embed_documents([("c1", "x")]))
    assert local_server.requests[0]["headers"]["Authorization"] == "Bearer s3cret"


def test_asymmetric_prefixes_are_applied_per_side(local_server, audit):
    """nomic-embed-text needs `search_document: ` / `search_query: `; getting it
    wrong costs recall silently, so it is configuration with a test on it."""
    settings = _settings(
        local_server,
        embedding_document_prefix="search_document: ",
        embedding_query_prefix="search_query: ",
    )
    emb = _embedder(settings, audit)

    list(emb.embed_documents([("c1", "def foo(): pass")]))
    assert local_server.requests[0]["body"]["input"][0].startswith("search_document: ")

    local_server.requests.clear()
    emb.embed_query("how does login work")
    assert local_server.requests[0]["body"]["input"][0].startswith("search_query: ")


def test_dimensions_are_only_sent_when_configured(local_server, audit):
    """`dimensions` is honoured by vLLM, ignored by Ollama and rejected by the
    strict ones — so it goes on the wire only when somebody asked for it."""
    settings = _settings(local_server, embedding_dimensions=0)
    results = list(_embedder(settings, audit).embed_documents([("c1", "x")]))
    assert "dimensions" not in local_server.requests[0]["body"]
    # Width reported is what the server actually returned.
    assert results[0].dimensions == DIMS

    local_server.requests.clear()
    # The hint still goes on the wire — and this fake, like Ollama, ignores it
    # and answers 8-wide anyway. That contradiction is now a loud refusal
    # instead of a mis-sized point (the request is recorded BEFORE the check,
    # which is what this assertion is about; the refusal itself is proven in
    # test_the_declared_width_is_checked_against_reality.py).
    with pytest.raises(EmbeddingConfigMismatch):
        list(_embedder(_settings(local_server, embedding_dimensions=256), audit)
             .embed_documents([("c1", "x")]))
    assert local_server.requests[0]["body"]["dimensions"] == 256
