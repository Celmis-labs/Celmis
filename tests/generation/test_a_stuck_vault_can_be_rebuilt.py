"""A half-built vault has a way out, and the way out is not a resume.

THE SHAPE OF BEING STUCK. A build writes markdown notes, every Qdrant upsert
is refused, and the failure is downgraded to a warning. Now:

  * the chat banner says "generate a vault";
  * the user presses it;
  * generation RESUMES — every note is already current, so nothing is
    regenerated, nothing is re-embedded, and no failure is recorded;
  * the job reports completed / attempts 1 / last_error null;
  * the banner still says "generate a vault".

Measured on production, twice in a row, with Qdrant at zero collections
throughout.

Two things were missing. `handle_generate_vault` has always read
`payload["force"]` and nothing could set it, so a rebuild was unrequestable.
And the guard that should have failed the second run required documents
generated in THIS run — a condition a resume never meets.
"""

from __future__ import annotations

from src.api.routers.repos import GenerateVaultIn
from src.generation.orchestrator import GenerationResult


def result(**kw) -> GenerationResult:
    base = dict(repo="acme/api", commit="a" * 40)
    base.update(kw)
    return GenerationResult(**base)


# ─── the way out ─────────────────────────────────────────────────────


def test_a_rebuild_can_be_requested():
    assert "force" in GenerateVaultIn.model_fields


def test_a_resume_is_still_the_default():
    """Forcing every build would re-spend the whole model budget of a large
    repository on every press."""
    assert GenerateVaultIn().force is False


def test_the_request_reaches_the_handler():
    """`force` was read by the queue handler for as long as it has existed;
    the gap was entirely on the way in."""
    import ast
    import inspect

    from src.api.routers import repos
    from src.sync import handlers

    # Read the string LITERALS, not the rendered source. `ast.unparse`
    # normalises quotes to single, so a check for `"force"` fails on code that
    # contains exactly that — testing the formatter instead of the code.
    literals = {
        node.value for node in ast.walk(ast.parse(inspect.getsource(handlers)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "force" in literals

    assert "payload.force" in inspect.getsource(repos)


def test_a_forced_rebuild_is_not_swallowed_by_a_pending_resume():
    """Same dedup key would mean the one request a stuck user finally reaches
    for is the one silently dropped."""
    import inspect

    from src.api.routers import repos

    src = inspect.getsource(repos)
    assert ":force" in src


# ─── the guard that let the second run pass ──────────────────────────


def test_a_resume_whose_upserts_failed_is_a_failure():
    r = result(modules_skipped=["auth"], notes_embedded=0,
               embedding_failures=["UnexpectedResponse: 404 Not Found"])

    assert r.embedded_nothing is True


def test_a_resume_with_nothing_to_do_is_not_a_failure():
    r = result(modules_skipped=["auth"], notes_embedded=0)

    assert r.embedded_nothing is False


def test_the_guard_no_longer_asks_what_was_generated_this_run():
    """That condition is what made it dead code for the stuck user."""
    import inspect

    from src.generation.orchestrator import GenerationResult as GR

    src = inspect.getsource(GR.embedded_nothing.fget)
    assert "modules_generated" not in src
