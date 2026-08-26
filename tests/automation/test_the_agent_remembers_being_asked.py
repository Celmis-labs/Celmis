"""What was asked of the Celmis agent outlives the page it was asked on.

The complaint that produced this: "чати з celmis agent зупиняються та не
зберігаються якшо вийти з цієї сторінки". Half of it was a misreading and the
other half was true, and the true half made the misreading inevitable.

The work never stopped. `/execute` queues jobs server-side and returns 202, so
documentation generation for twenty repositories carries on regardless of what
the browser does. But the question, the plan and the fact that anything had
been started lived in React state and nowhere else — so leaving the page showed
an empty form, which reads exactly like the work stopped.

So a row is written when the sentence is READ, before anything is confirmed,
and the outcome lands on that same row.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTER = (ROOT / "src" / "api" / "routers" / "automation.py").read_text(encoding="utf-8")
CHAT = (ROOT / "src" / "automation" / "chat.py").read_text(encoding="utf-8")
PAGE = (ROOT / "web" / "app" / "(app)" / "automation" / "page.tsx").read_text(encoding="utf-8")
MESSAGES = ROOT / "web" / "lib" / "i18n" / "messages"
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))

HISTORY_KEYS = [k for k in EN if k.startswith("automation.history")
                or k.startswith("automation.status.")
                or k == "automation.queuedCount"]


def _calls(source: str, func: str) -> list[ast.Call]:
    """Every call to `func` in the module, as AST.

    Parsed rather than grepped. A test that greps for a token finds it in the
    comment explaining why it is absent, and passes on a file that does the
    opposite of what it claims.
    """
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", None)
        if name == func:
            out.append(node)
    return out


# ─── the row ─────────────────────────────────────────────────────────


def test_the_row_exists_before_the_reading_does():
    """A plan that was never approved is still the answer to "what did I ask".

    Written before the model is even called, because the reading now happens
    on the queue: if the row waited for the answer, the seconds somebody is
    most likely to navigate away in would be the seconds with nothing stored.
    """
    tree = ast.parse(ROUTER)
    plan_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "plan")
    row = next((c for c in ast.walk(plan_fn)
                if isinstance(c, ast.Call)
                and getattr(c.func, "id", None) == "AutomationRun"), None)
    assert row is not None, "/plan writes no row"
    status = next((k.value for k in row.keywords if k.arg == "status"), None)
    assert status is not None and ast.literal_eval(status) == "reading", (
        "the row does not record that the answer has not arrived yet"
    )


def test_the_outcome_lands_on_the_same_row():
    assert "plan_id" in ROUTER, "execute cannot find the row it belongs to"
    assert "_record_outcome" in ROUTER


def test_a_quoted_uuid_cannot_reach_another_tenants_row():
    """The id comes from the client. Fetching by id alone would let anyone who
    knows a uuid overwrite a row in a workspace they are not in."""
    fn = next(n for n in ast.walk(ast.parse(ROUTER))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_record_outcome")
    src = ast.dump(fn)
    assert "workspace_id" in src, "_record_outcome does not scope by workspace"


def test_bookkeeping_never_fails_the_request():
    """By the time the outcome is written the jobs are queued. An exception
    here would answer "nothing started" to a person whose twenty jobs just
    did."""
    fn = next(n for n in ast.walk(ast.parse(ROUTER))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_record_outcome")
    assert any(isinstance(n, ast.ExceptHandler) for n in ast.walk(fn)), (
        "a failed write of the history row can take down a started run"
    )


def test_history_is_readable_and_scoped_to_the_workspace():
    fn = next((n for n in ast.walk(ast.parse(ROUTER))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "history"), None)
    assert fn is not None, "there is no way to read back what was asked"
    src = ast.dump(fn)
    assert "workspace_id" in src, "history is not workspace-scoped"


def test_the_model_exists_with_the_columns_the_router_writes():
    from src.db.models import AutomationRun

    for column in ("workspace_id", "message", "action", "arguments",
                   "resolved_repos", "status", "result", "executed_at"):
        assert hasattr(AutomationRun, column), f"AutomationRun has no {column}"


def test_there_is_a_migration_for_it():
    versions = ROOT / "alembic" / "versions"
    assert any("automation_runs" in p.read_text(encoding="utf-8")
               for p in versions.glob("*.py")), (
        "the table exists in the model and not in any migration — it will be "
        "missing on every deployed database"
    )


# ─── the wait ────────────────────────────────────────────────────────


def test_the_planner_does_not_inherit_the_architect_timeout():
    """Measured on production: 0.9 s typical, and 200 s when the upstream
    stalled and the retry ladder ran to the end.

    The client defaults — 120 s, three retries — are sized for an architect
    call carrying a whole diff. Inherited by a surface with a person watching
    a spinner they mean up to eight minutes of "Reading…".
    """
    call = next(c for c in _calls(CHAT, "generate"))
    kwargs = {k.arg: k.value for k in call.keywords}
    assert "timeout" in kwargs, "the planner inherits the 120 s default"
    timeout = ast.literal_eval(kwargs["timeout"])
    retries = ast.literal_eval(kwargs["num_retries"])
    assert timeout <= 30, f"{timeout}s is a long time to watch a spinner"
    # The ceiling is the product, not the timeout: retries multiply it.
    assert timeout * (1 + retries) <= 60, (
        f"worst case is still {timeout * (1 + retries)}s"
    )


def test_the_reading_happens_off_the_request():
    """Leaving the page used to throw the reading away: the answer was
    computed and dropped, and pressing again paid for the same model call
    twice. It is a queue job now — which also makes it the only thing here
    that can be told to stop."""
    tree = ast.parse(ROUTER)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "plan")
    src = ast.dump(fn)
    assert "enqueue" in src, "the reading still runs inside the request"
    assert "interpret" not in src, "the model is still called in the request"

    import src.sync.handlers as handlers

    assert hasattr(handlers, "handle_automation_plan"), (
        "nothing on the worker picks the job up, so it is queued forever"
    )
    import inspect as _inspect

    import src.sync.worker as worker
    assert "KIND_AUTOMATION_PLAN" in _inspect.getsource(worker.start_worker), (
        "the handler exists but is never registered"
    )


def test_a_reading_can_be_stopped():
    """A person who typed the wrong thing should not have to wait for the
    model to finish being wrong."""
    fn = next((n for n in ast.walk(ast.parse(ROUTER))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "stop_run"), None)
    assert fn is not None, "there is no way to stop a reading"
    src = ast.dump(fn)
    assert "workspace_id" in src, "one tenant can stop another's reading"
    assert "request_cancel" in src or "mark_cancelled" in src


def test_stopping_does_not_pretend_to_unqueue_started_work():
    """Approved work is a set of jobs owned by Monitoring. Half-cancelling a
    documentation sweep from a chat box leaves a state nobody can describe."""
    fn_src = ROUTER[ROUTER.index("async def stop_run("):]
    fn_src = fn_src[:fn_src.index("@router.post(\"/execute\"")]
    assert 'row.status != "reading"' in fn_src, (
        "stop is offered for runs whose jobs it cannot actually stop"
    )


def test_a_question_is_not_put_behind_a_confirm_button():
    """The complaint was that the chat could do almost nothing. It could not
    answer anything at all: every verb queued work, so every question came
    back as "I have no command for that"."""
    from src.automation.chat import CATALOGUE

    reads = [n for n, spec in CATALOGUE.items() if spec.get("reads")]
    assert reads, "the catalogue is still write-only"

    handlers = (ROOT / "src" / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert "reads_only" in handlers, (
        "a question is still stored as a plan waiting for approval"
    )


# ─── the page ────────────────────────────────────────────────────────


def test_the_page_reads_the_history_back():
    assert "/api/automation/history" in PAGE, "nothing on screen shows the record"
    assert "plan_id" in PAGE, "the confirm does not name the row it belongs to"


def test_the_history_refreshes_after_a_reading_not_only_after_a_run():
    """A plan that is read and never confirmed must appear too — otherwise the
    list silently omits the case it exists for."""
    assert PAGE.count("automation-history") >= 3, (
        "the history is fetched but never invalidated on both paths"
    )


def test_the_question_is_kept_verbatim():
    """The plan is a reading of the sentence and can be wrong. Without the
    sentence beside it there is no way to see that it was misread."""
    assert re.search(r"\{h\.message\}", PAGE), "history shows the plan, not the ask"


# ─── the strings ─────────────────────────────────────────────────────


def test_there_are_strings_for_it():
    assert len(HISTORY_KEYS) >= 7, f"only {len(HISTORY_KEYS)} history keys"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_carries_them(locale):
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [k for k in HISTORY_KEYS if k not in data]
    assert not missing, f"{locale} renders raw keys for {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_no_locale_falls_back_to_english(locale):
    if locale == "en":
        pytest.skip("it is the English")
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    same = [k for k in HISTORY_KEYS if data.get(k) == EN[k]]
    assert not same, f"{locale} still shows English for {same}"


def test_no_russian_reached_the_new_strings():
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for key in HISTORY_KEYS:
        assert not set("ыэъё") & set(uk[key].lower()), f"{key}: {uk[key]}"


def test_a_new_surface_does_not_break_a_provisioned_workspace():
    """Adding the "agent" profile took the agent down in production.

    Every workspace had been provisioned on the gateway for three surfaces.
    Asking for a fourth resolved to a direct-key profile, so the key resolver
    went looking for a raw `gemini` credential that a gateway tenant does not
    hold, and the reading died with LLMCredentialError before sending a token.

    Two guards, because either alone would have prevented it:
      the planner only asks for the agent route when the workspace actually
      chose one, and the client provisions a surface it has no deployment for
      instead of silently falling back to direct keys.
    """
    assert "is_configured" in CHAT, (
        "the planner asks for a route the workspace never configured"
    )
    client = (ROOT / "src" / "llm" / "client.py").read_text(encoding="utf-8")
    assert "_routed" in client, (
        "build_llm_client resolves profiles without provisioning, so any "
        "surface added later is direct-key on an already-provisioned tenant"
    )
