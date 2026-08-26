"""A vault job that embedded nothing does not report success.

THE DEFECT. A vault has two halves — markdown notes on disk and vectors in
Qdrant — and only one of them was ever checked. `batched_qdrant()` downgrades
an upsert failure to a `logger.warning`, and the job-level guard tests
`produced_nothing`, which counts DOCUMENTS. Documents existed, so the job
passed.

On production all three generation jobs reported `status: completed`,
`attempts: 1`, `last_error: null`, in 64s / 64s / 14s. Afterwards:

    readyz → qdrant.collections = 0
    /api/qa/available-repos → []
    every answer's meta → vault_unavailable: "vault_not_generated"

The chat banner tells the user to generate a vault. They do. The Jobs page says
done. The banner still says generate a vault. There is no run that clears it —
a loop with no exit, and every trip through it costs real model spend.

The notes stay on disk either way. What changes is that the job says so.
"""

from __future__ import annotations

from src.generation.orchestrator import GenerationResult


def result(**kw) -> GenerationResult:
    base = dict(repo="acme/api", commit="a" * 40)
    base.update(kw)
    return GenerationResult(**base)


def test_documents_without_vectors_is_a_failure():
    r = result(modules_generated=["auth"], notes_embedded=0,
               embedding_failures=["ConnectError: qdrant unreachable"])

    assert r.embedded_nothing is True


def test_documents_with_vectors_is_a_success():
    r = result(modules_generated=["auth"], notes_embedded=12)

    assert r.embedded_nothing is False


def test_a_resume_run_that_wrote_nothing_is_not_a_failure():
    """Everything already current: nothing to embed, no failures recorded,
    nothing wrong. Same distinction `produced_nothing` already makes."""
    r = result(modules_skipped=["auth"], notes_embedded=0)

    assert r.embedded_nothing is False
    assert r.produced_nothing is False


def test_a_resume_run_whose_upserts_failed_IS_a_failure():
    """The hole the first version of this guard had. It required documents
    generated in THIS run, and a resume run generates none — so for the exact
    user stuck in the loop, the check could not fire however many upserts were
    refused. Measured: `completed / attempts 1 / last_error null`, twice, with
    Qdrant at zero collections throughout."""
    r = result(modules_skipped=["auth"], notes_embedded=0,
               embedding_failures=["UnexpectedResponse: 404 Not Found"])

    assert r.embedded_nothing is True


def test_a_silent_zero_is_not_enough_to_fail():
    """No failures recorded and no embeddings means nothing was attempted —
    a repo with no notes to embed. Failing that would be a false alarm."""
    r = result(modules_generated=["auth"], notes_embedded=0,
               embedding_failures=[])

    assert r.embedded_nothing is False


def test_the_two_failure_modes_stay_distinct():
    """One is a generation failure to retry, the other is the vector half
    being down while the text half worked. Different remedies."""
    gen_failed = result(failures=["auth.md: 429"])
    embed_failed = result(modules_generated=["auth"], notes_embedded=0,
                          embedding_failures=["ConnectError"])

    assert gen_failed.produced_nothing is True
    assert gen_failed.embedded_nothing is False
    assert embed_failed.produced_nothing is False
    assert embed_failed.embedded_nothing is True


def test_the_summary_says_semantic_search_will_stay_empty():
    """The person reading the Jobs page needs to know what the consequence is,
    not just that a stage failed."""
    text = result(modules_generated=["auth"], notes_embedded=0,
                  embedding_failures=["ConnectError"]).summary()

    assert "NOTHING embedded" in text
    assert "semantic search will stay empty" in text


def test_the_job_handler_checks_the_embedding_half():
    """The guard lives in the queue handler, and the whole defect was that it
    only looked at documents."""
    import ast
    import inspect

    from src.sync import handlers

    tree = ast.parse(inspect.getsource(handlers))
    body = ast.unparse(tree)
    assert "embedded_nothing" in body
    assert "produced_nothing" in body
