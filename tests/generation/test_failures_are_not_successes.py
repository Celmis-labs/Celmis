"""Four ways this session's code reported a failure as a success.

Each was found by running the code rather than reading it, and each has the
same shape: something went wrong, nothing raised, and the artefact that came
out looked exactly like a good one.

That shape matters more than the individual bugs. A vault of empty notes and a
vault of good notes were indistinguishable from the Jobs page, and resume mode
then pinned the emptiness across every later commit because the content hash
matched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


# ─── an empty document is not a document ─────────────────────────────


def test_the_api_engine_refuses_to_return_an_empty_document():
    from src.generation.engines import ApiEngine

    class _Empty:
        text = ""
        input_tokens = 0
        output_tokens = 0

    engine = ApiEngine("ws-1")
    import src.llm.client as client_mod

    original = client_mod.build_llm_client
    client_mod.build_llm_client = lambda *a, **k: type(
        "C", (), {"generate": lambda self, **kw: _Empty()})()
    try:
        with pytest.raises(RuntimeError, match="no text"):
            engine.generate(
                prompt="p", system_instruction="s", code_context="x",
                metadata_context=None, operation="generate_module_prd",
                repo="acme/api")
    finally:
        client_mod.build_llm_client = original


def test_the_agent_engine_checks_is_error():
    """The SDK reports a failed session — a refusal, a turn limit, an auth
    problem — on ResultMessage.is_error. The loop simply stopped on it and the
    caller received text="" as a normal result."""
    source = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")
    assert 'getattr(message, "is_error"' in source
    assert "raise RuntimeError" in source


def test_the_agent_engine_refuses_an_empty_result():
    source = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")
    assert "produced no text" in source


# ─── a build that produced nothing is not complete ───────────────────


def test_an_all_failed_build_says_so():
    from src.generation.orchestrator import GenerationResult

    r = GenerationResult(repo="a/b", commit="abc12345",
                         failures=["module:m1", "module:m2"])
    assert r.produced_nothing is True
    assert "produced nothing" in r.summary()
    assert "✅" not in r.summary()


def test_a_partial_build_is_neither_complete_nor_empty():
    from src.generation.orchestrator import GenerationResult

    r = GenerationResult(repo="a/b", commit="abc12345",
                         modules_generated=["m1"], failures=["module:m2"])
    assert r.produced_nothing is False
    assert "partly" in r.summary()


def test_a_resume_run_that_generated_nothing_is_still_a_success():
    """The distinction that makes this safe: a repository already current
    legitimately produces no documents, and calling that a failure would make
    every second build red."""
    from src.generation.orchestrator import GenerationResult

    r = GenerationResult(repo="a/b", commit="abc12345", modules_skipped=["m1"])
    assert r.produced_nothing is False
    assert "✅" in r.summary()


def test_the_worker_fails_a_job_that_produced_nothing():
    """The queue marks a job successful unless the handler raises. Without this
    the Jobs page said done, the vault was empty, and the retry that would have
    helped never happened."""
    handlers = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert "produced_nothing" in handlers
    assert "raise RuntimeError" in handlers


# ─── spend goes to the surface it came from ──────────────────────────


def test_spend_is_not_hardcoded_to_review():
    """Review was the only caller when the ledger was written. Documentation
    and Q&A then moved onto the same client and their spend arrived labelled
    "review" — worse than unlabelled, because a budget set on review would
    throttle a vault build."""
    client = (SRC / "llm" / "client.py").read_text(encoding="utf-8")
    assert "surface=getattr(self, \"_surface\", None)" in client, (
        "the spend ledger is hardcoded to one surface again"
    )


def test_each_caller_books_to_its_own_surface():
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert 'spend_surface="vault"' in engines
    qa = (SRC / "qa" / "orchestrator.py").read_text(encoding="utf-8")
    assert 'spend_surface="qa"' in qa


def test_the_profile_and_the_ledger_can_differ():
    """Documentation runs on the chat PROFILE — which model to use — while
    billing to the vault LEDGER. Collapsing the two would either put docs on
    the review model or bill a vault build to chat."""
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert 'surface="chat"' in engines and 'spend_surface="vault"' in engines


# ─── a partial save is not a full replace ────────────────────────────


def test_saving_one_setting_does_not_wipe_the_others():
    """The settings page saves the documentation language on its own. The
    handler was written as a full replace, so `payload.temperature` was the
    Pydantic default rather than the workspace's value — and choosing a
    language silently reset the provider, the model and the token limit."""
    llm = (SRC / "api" / "routers" / "llm.py").read_text(encoding="utf-8")
    assert "model_fields_set" in llm, (
        "the handler cannot tell 'not sent' from 'sent as the default' again"
    )
    assert 'def _keep(' in llm


def test_a_partial_payload_carries_only_what_was_sent():
    from src.api.routers.llm import LLMConfigIn

    partial = LLMConfigIn(docs_language="de")
    assert partial.model_fields_set == {"docs_language"}
    # The trap: these look like real values and are not.
    assert partial.temperature == 0.1
    assert partial.provider is None
