"""The embedder seam holds no provider SDK, and its machinery still works.

This file used to be "Tests for GeminiEmbedder": a `GeminiEmbedder` fixture,
`patch.object(embedder, "_get_client")`, and a mocked `models.embed_content`.
The class is gone. It built its own `genai.Client` inside this module, which is
the exact shape the no-native-SDK policy bans, and it was unreachable on every
install there is — `completion._configured_embedder()` returns None, not a
Gemini embedder, for the default `embedding_provider="gemini"`, so
`get_embedder` was only ever called with the other value. See the module
docstring of src/indexing/vectors/embedder.py.

The old file's real subject was never Gemini, though. It was `_BaseEmbedder`:
redact before sending, retry a transient failure, isolate a chunk that keeps
failing, write an audit record either way. Gemini was just the concrete class
that happened to be handy for exercising it. So that coverage moves here onto a
stub with no transport at all, which is what it should always have been — the
base class is provider-agnostic, and testing it through a provider is how a
deleted provider takes working tests down with it.

The other half is the deletion itself, pinned so it cannot come back by
accident: nothing in this package may import the Google SDK.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.indexing.vectors import embedder as embedder_mod
from src.indexing.vectors.embedder import _BaseEmbedder, get_embedder
from src.security.audit import AuditLogger
from src.security.redactor import Redactor

DIMS = 8


# ─── the deletion ───────────────────────────────────────────────────


def test_the_gemini_embedder_is_gone():
    """A second way to send the customer's source code to a vendor.

    It carried none of what the real Gemini embedding path carries — no
    workspace profile, no per-workspace key, no gateway route, no spend ledger
    row — and it would have produced perfectly plausible vectors anyway, which
    is the failure that never announces itself.
    """
    assert not hasattr(embedder_mod, "GeminiEmbedder")
    from src.indexing import vectors
    assert not hasattr(vectors, "GeminiEmbedder")
    assert "GeminiEmbedder" not in vectors.__all__


def test_this_package_imports_no_provider_sdk():
    """Parsed as data, not grepped: the module docstring and the comments in
    `get_embedder` both say "Gemini" and both must keep saying it.
    """
    import ast

    source = Path(embedder_mod.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = sorted(
        name for name in imported
        if name.split(".")[0] in {"google", "anthropic", "mistralai", "openai"}
    )
    assert not offenders, f"a provider SDK is back in the seam: {offenders}"


def test_asking_this_module_for_gemini_refuses_instead_of_improvising():
    """Fail-closed. The one caller never asks — `_configured_embedder()`
    returns None for "gemini" — so a call that gets here is a caller that
    skipped the door where the profile, key, route and ledger are applied.
    Handing back *something* would hide that behind vectors that look fine.
    """
    with pytest.raises(ValueError) as exc:
        get_embedder(Settings(embedding_provider="gemini"))
    message = str(exc.value)
    assert "src.llm.completion.embed" in message, (
        "the refusal has to say where Gemini embeddings actually go, or the "
        "next person reads it as 'unsupported'"
    )


# ─── the base machinery, with no provider involved ──────────────────


class _StubEmbedder(_BaseEmbedder):
    """One text in, one vector out — and a script of what the transport does.

    `script` is consumed per call: a vector to return, or an exception to
    raise. Nothing here is a mock of a vendor; `_embed_one` is the entire
    provider surface `_BaseEmbedder` knows about.
    """

    def __init__(self, script: list, **kw) -> None:
        super().__init__(**kw)
        self.script = list(script)
        self.sent: list[str] = []

    @property
    def model(self) -> str:
        return "stub-embed"

    @property
    def declared_dimensions(self) -> int:
        return DIMS

    @property
    def task_document(self) -> str:
        return "DOC"

    @property
    def task_query(self) -> str:
        return "QUERY"

    def _embed_one(self, text: str, task_type: str) -> list[float]:
        self.sent.append(text)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def _stub(script: list, audit: AuditLogger) -> _StubEmbedder:
    return _StubEmbedder(
        script, settings=Settings(), audit=audit,
        redactor=Redactor(fail_closed=True),
    )


def test_a_chunk_becomes_one_result_in_input_order(audit):
    emb = _stub([[0.1] * DIMS, [0.2] * DIMS], audit)
    results = list(emb.embed_documents([("c1", "def a(): pass"), ("c2", "def b(): pass")]))

    assert [r.chunk_id for r in results] == ["c1", "c2"]
    assert results[0].vector == [0.1] * DIMS
    assert all(r.error is None for r in results)
    assert results[0].dimensions == DIMS


def test_the_two_sides_carry_different_task_types(audit):
    """Asymmetric retrieval: a question and the chunk answering it go into
    different corners of the same space, and the side is decided here."""
    seen: list[str] = []

    class _Recording(_StubEmbedder):
        def _embed_one(self, text: str, task_type: str) -> list[float]:
            seen.append(task_type)
            return super()._embed_one(text, task_type)

    emb = _Recording([[0.0] * DIMS] * 2, settings=Settings(), audit=audit,
                     redactor=Redactor(fail_closed=True))
    list(emb.embed_documents([("c1", "code")]))
    emb.embed_query("how does login work")

    assert seen == ["DOC", "QUERY"]


def test_secrets_are_redacted_before_the_transport_sees_them(audit):
    """The security property, and the reason redaction lives in the base class
    rather than in each implementation."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    emb = _stub([[0.1] * DIMS], audit)
    results = list(emb.embed_documents([("c1", f"const KEY = '{secret}'")]))

    assert secret not in emb.sent[0], f"SECRET LEAKED: {emb.sent[0]!r}"
    assert "[REDACTED:" in emb.sent[0]
    assert results[0].redaction_stats["secrets_found"] >= 1


def test_a_redaction_failure_sends_nothing_at_all(audit, monkeypatch):
    """Fail-closed: unredacted text is not a degraded send, it is the thing
    redaction exists to prevent."""
    emb = _stub([[0.1] * DIMS], audit)
    monkeypatch.setattr(
        emb.redactor, "redact",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    results = list(emb.embed_documents([("c1", "code")]))

    assert emb.sent == [], "text reached the transport after redaction failed"
    assert results[0].error is not None and "redaction_failed" in results[0].error


def test_a_transient_failure_is_retried_until_it_works(audit, monkeypatch):
    monkeypatch.setattr(embedder_mod.time, "sleep", lambda *_: None)
    emb = _stub(
        [RuntimeError("rate_limit"), RuntimeError("rate_limit"), [0.9] * DIMS], audit,
    )
    results = list(emb.embed_documents([("c1", "text")], max_retries=3))

    assert results[0].vector == [0.9] * DIMS
    assert results[0].error is None
    assert len(emb.sent) == 3


def test_a_chunk_that_keeps_failing_is_recorded_not_raised(audit, monkeypatch):
    """Per-chunk isolation: indexing a large repository must not lose 9,000
    good chunks to one bad file."""
    monkeypatch.setattr(embedder_mod.time, "sleep", lambda *_: None)
    emb = _stub(
        [[0.1] * DIMS,
         RuntimeError("err"), RuntimeError("err"), RuntimeError("err"),
         [0.1] * DIMS],
        audit,
    )
    results = list(emb.embed_documents(
        [("c1", "a"), ("c2", "b"), ("c3", "c")], max_retries=3,
    ))

    assert [r.error is None for r in results] == [True, False, True]
    assert results[1].vector == []


def test_every_call_writes_one_audit_record_success_or_failure(audit, monkeypatch):
    monkeypatch.setattr(embedder_mod.time, "sleep", lambda *_: None)
    emb = _stub([[0.1] * DIMS, RuntimeError("api_down"), RuntimeError("api_down")], audit)
    list(emb.embed_documents([("c1", "x"), ("c2", "y")], repo="test-repo", max_retries=2))

    records = [json.loads(line) for line in audit.log_path.read_text().splitlines()]
    assert len(records) == 2
    assert [r["mode"] for r in records] == ["embedding", "embedding"]
    assert [r["repo"] for r in records] == ["test-repo", "test-repo"]
    assert all(r["model"] == "stub-embed" and "redaction" in r for r in records)
    assert records[0]["error"] is None
    assert records[1]["error"] is not None and "api_down" in records[1]["error"]
