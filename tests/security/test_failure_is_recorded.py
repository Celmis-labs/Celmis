"""A failure that leaves no trace is the one nobody can debug.

From the 14 Aug production report, three findings that share one shape — the
system knew it had failed and then stored or delivered something that said
otherwise:

  * a message whose entire body is "(generation failed)" carried meta.error =
    False, because the `error` variable was declared and never assigned;
  * a budget-blocked question was answered with an SSE error event and then
    `return`ed before the persistence block, so the transcript kept the
    question with nothing after it and the UI showed "…" forever;
  * a missing Qdrant collection reached the browser and the message metadata as
    Qdrant's raw 404 body, naming an internal collection and reading as a crash
    — when it actually means "nobody has generated documentation yet" and the
    answer came from source code, which is fine.

Text-level assertions on purpose: the streaming generator needs a database, a
retriever and a provider to execute, and a test that needs all three would not
be the test that runs.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QA = (ROOT / "src" / "api" / "routers" / "qa.py").read_text()


def test_the_error_flag_is_actually_assigned():
    """`error: str | None = None` with no assignment anywhere makes
    meta["error"] permanently False — including on "(generation failed)"."""
    assert 'error: str | None = None' in QA, "the declaration moved; re-check this"
    assignments = re.findall(r"^\s+error = (?!None)", QA, re.M)
    assert len(assignments) >= 2, (
        f"found {len(assignments)} assignments to `error` — the budget path and "
        f"the generic failure path must both set it"
    )


def test_the_failure_code_travels_with_the_flag():
    """"it failed" sends someone to the logs; "quota_exhausted" sends them to
    the provider."""
    assert '"error": bool(error),' in QA
    assert '"error_code": error,' in QA


def test_no_failure_path_returns_before_persistence():
    """A bare `return` inside the stream generator skips the block that writes
    the assistant message. That is what left two questions with no answer row
    at all."""
    start = QA.find("async def _ask_stream")
    if start < 0:
        start = QA.find("def _ask_stream")
    assert start > 0, "could not locate the stream generator"
    end = QA.find("\n@router", start)
    body = QA[start:end if end > 0 else len(QA)]
    # Anchored on the CALL that writes the row, not on the comment above it.
    # It used to look for the words "Persist assistant message" and broke the
    # moment that comment was reworded — a security guard that a translation
    # can silently switch off is not a guard.
    persist = body.find("repo.append_message(")
    assert persist > 0, (
        "the assistant message is no longer persisted through "
        "repo.append_message() — this guard has lost its anchor"
    )
    before = body[:persist]
    bare_returns = re.findall(r"^\s+return\s*$", before, re.M)
    assert not bare_returns, (
        f"{len(bare_returns)} bare return(s) before the persistence block — a "
        f"failure reported to the browser and forgotten by the database"
    )


def test_the_budget_path_reports_once_and_still_persists():
    """It yields its own error event, so the generic handler must not classify
    and yield a second one — but the stream must still fall through."""
    assert "raise _AlreadyReported" in QA
    assert "except _AlreadyReported:" in QA
    reported = QA.find("except _AlreadyReported:")
    generic = QA.find("except Exception as exc:", QA.find("except asyncio.CancelledError:"))
    assert 0 < reported < generic, (
        "_AlreadyReported must be caught BEFORE the generic handler, or the "
        "client gets two error events"
    )


def test_the_vault_error_reaches_the_user_as_a_code():
    """Qdrant's raw body names the internal collection and reads as a crash."""
    retriever = (ROOT / "src" / "qa" / "multi_repo_retriever.py").read_text()
    assert "self.vault_unavailable = str(exc)" not in retriever, (
        "the raw exception string is back on the wire"
    )
    assert "classify_vector_store(exc).code" in retriever


def test_the_vector_store_classifier_never_returns_a_provider_body():
    from src.llm.errors import classify_vector_store

    class _Qdrant404(Exception):
        status_code = 404

    raw = ('Unexpected Response: 404 (Not Found) Raw response content: '
           'b\'{"status":{"error":"Not found: Collection code_analysis_vault '
           'doesn\'t exist!"}}\'')
    failure = classify_vector_store(_Qdrant404(raw))
    assert failure.code == "vault_not_generated"
    assert "code_analysis_vault" not in failure.hint
    assert "Raw response" not in failure.hint


def test_a_missing_collection_is_recognised_without_a_status_code():
    """Some clients raise a plain exception whose text is the only signal."""
    from src.llm.errors import classify_vector_store

    failure = classify_vector_store(
        Exception("Not found: Collection code_analysis_vault doesn't exist!"))
    assert failure.code == "vault_not_generated"


def test_an_unreachable_store_is_distinguished_from_a_missing_one():
    """"generate documentation" is useless advice when the store is down."""
    from src.llm.errors import classify_vector_store

    failure = classify_vector_store(ConnectionError("connection refused to qdrant:6333"))
    assert failure.code == "vault_unavailable"
    assert "qdrant:6333" not in failure.hint, "the hint leaks internal topology"


# ─── delivery must not cross a tenant ────────────────────────────────


def test_notify_requires_a_workspace_with_no_default():
    """The binding query filtered on enabled/event/repo_slug and nothing else,
    while NotificationChannel.name is globally unique — so a workspace-wide
    binding matched EVERY tenant's events and delivered one workspace's PR
    title and review summary into another tenant's chat room.

    Required and defaultless: a default is how the next caller reopens it.
    """
    from src.notifications.dispatch import notify

    params = inspect.signature(notify).parameters
    assert "workspace_id" in params
    assert params["workspace_id"].default is inspect.Parameter.empty
    assert params["workspace_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_binding_query_filters_on_the_channel_workspace():
    """Tenancy lives on the channel, not the binding — the join is the
    boundary, the same way the /bindings listing does it."""
    dispatch = (ROOT / "src" / "notifications" / "dispatch.py").read_text()
    assert "NotificationChannel.workspace_id == workspace_id" in dispatch


def test_every_caller_passes_one():
    """A missing argument is now a TypeError rather than a silent cross-tenant
    delivery — but only if every call site was updated."""
    for path in ("src/review/orchestrator.py", "src/review/breaking_change.py"):
        source = (ROOT / path).read_text()
        idx = source.find("            notify(")
        assert idx > 0, f"notify( call not found in {path}"
        call = source[idx:idx + 200]
        assert "workspace_id=" in call, f"{path} calls notify() without a workspace"
