"""Every LLM call that works on a repository says which one.

The Usage page grew a "by repository" breakdown and it showed documentation
runs and nothing else — every Q&A answer and every review agent landed in the
row keyed "—". Not a bug in the aggregation: the value simply never reached
the ledger, and in the Q&A case it had been three stack frames away the whole
time.

Two failure modes, and the second is worse than the first:

  Not recording it at all makes a breakdown that under-reports. A reader sees
  documentation cost $34 and review cost nothing per repository, and concludes
  review is cheap.

  Recording it in a DIFFERENT SPELLING splits one repository into two rows.
  `PullRequest.repo` is "owner/name"; everything else in this system keys on
  the slug `provider_owner-name`. Both spellings in one column is a table
  where the same repository appears twice and neither figure is right.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _spend_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `_record_spend(...)` in a parsed module.

    Parsed rather than grepped: a grep for "repo=" finds it in the comment
    explaining why it is absent, which has happened five times in this repo.
    """
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "_record_spend"
    ]


def _scopes_by_call(tree: ast.AST) -> dict[int, set[str]]:
    """For every `_record_spend` call, the names bound in its enclosing
    functions — keyed by line, because identity only holds within one parse.

    (The first version of this re-parsed the source to find the scope, so the
    call it was looking for was never the same object it had been given, and
    every lookup returned an empty set — a check that could only ever fail.)
    """
    out: dict[int, set[str]] = {}

    def walk(node, stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + [node]
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "_record_spend"):
            names: set[str] = set()
            for fn in stack:
                names.update(a.arg for a in fn.args.args + fn.args.kwonlyargs)
                for n in ast.walk(fn):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        names.add(n.id)
            out[node.lineno] = names
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    walk(tree, [])
    return out


# ─── the Q&A path ────────────────────────────────────────────────────


def test_an_answer_records_the_repository_it_answered_about():
    """The repo was a parameter of every answer method and was dropped on the
    way into the ledger, so "which codebase are we spending on" — the first
    question anybody asks a usage page — had no answer for Q&A at all."""
    src = (ROOT / "src" / "qa" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "_generate"]
    assert calls, "the orchestrator no longer generates anything"
    for call in calls:
        assert any(k.arg == "repo" for k in call.keywords), (
            f"_generate at line {call.lineno} records no repository"
        )


def test_the_synthesis_methods_are_given_it_rather_than_guessing():
    from src.qa.orchestrator import QAOrchestrator

    for name in ("_synthesize_overview", "_synthesize_technical"):
        sig = inspect.signature(getattr(QAOrchestrator, name))
        assert "repo" in sig.parameters, f"{name} cannot know the repository"


# ─── the review path ─────────────────────────────────────────────────


@pytest.mark.parametrize("module", [
    "src/review/agents/base.py", "src/review/agents/verifier.py",
])
def test_review_records_the_slug_and_not_the_display_name(module):
    """`PullRequest.repo` is "owner/name". Everything else keys on the slug.

    Writing the display name here does not merely mislabel a row — it creates
    a SECOND row for a repository that already has one, and splits its cost
    between them.
    """
    src = (ROOT / module).read_text(encoding="utf-8")
    tree = ast.parse(src)
    generates = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "generate"]
    repo_args = [k.value for call in generates for k in call.keywords
                 if k.arg == "repo"]
    assert repo_args, f"{module} sends no repository to the ledger"
    for value in repo_args:
        assert isinstance(value, ast.Attribute) and value.attr == "repo_slug", (
            f"{module} records the display name rather than the slug"
        )


def test_the_slug_property_is_the_one_the_rest_of_the_system_uses():
    from src.review.models import PullRequest

    pr = PullRequest(
        provider="github", repo="Acme-Dev/todo-app", number=1, title="t",
        description="", author="a", base_ref="main", base_sha="0" * 40,
        head_ref="f", head_sha="1" * 40, state="open",
    )
    assert pr.repo_slug == "github_Acme-Dev-todo-app"
    assert "/" not in pr.repo_slug, "a slug with a slash is a different key"


# ─── the streaming path ──────────────────────────────────────────────


def test_every_ledger_write_that_names_a_repo_can_actually_see_one():
    """Passing `repo=repo` from a closure that never bound it is a NameError
    on the first streamed answer — a fix that reads correct and fails at
    runtime, in a path with no test coverage because it has no live caller."""
    tree = ast.parse(
        (ROOT / "src" / "llm" / "gemini_client.py").read_text(encoding="utf-8"))
    scopes = _scopes_by_call(tree)
    checked = 0
    for call in _spend_calls(tree):
        if not any(k.arg == "repo" for k in call.keywords):
            continue
        checked += 1
        assert "repo" in scopes.get(call.lineno, set()), (
            f"_record_spend at line {call.lineno} passes a repo that is not "
            f"in scope"
        )
    assert checked, "no ledger write names a repository at all"


def test_embeddings_do_not_claim_a_repository():
    """They are workspace-shared by construction — one Qdrant collection. A
    repository attributed to them would be an invention, and the page already
    explains that "—" means workspace-wide rather than missing.

    This used to walk GeminiClient.embed/embed_batch; those moved to LiteLLM
    (`_litellm_embed` in src/llm/completion.py), so it walks the live path —
    and it counts what it found, because a test aimed at deleted functions
    passes vacuously forever."""
    src = (ROOT / "src" / "llm" / "completion.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    checked = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in ("embed", "embed_batch", "_litellm_embed"):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            callee = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if callee not in ("_record_litellm_spend", "record_spend", "_record_spend"):
                continue
            checked += 1
            assert not any(k.arg in ("repo", "repo_slug") for k in call.keywords), (
                f"{fn.name} attributes shared embeddings to one repository"
            )
    assert checked, (
        "the embed path no longer writes the ledger where this test looks — "
        "point it at the new write before trusting it"
    )


# ─── the model, not the door it went through ─────────────────────────


def test_the_ledger_records_the_model_that_ran_not_the_deployment():
    """Through the gateway every request goes to a per-workspace deployment
    named `litellm_proxy/celmis-<uuid>-<surface>`.

    Recording that as the model made "usage by model" — the breakdown people
    open Usage for — a list of opaque names that say which workspace and which
    surface and nothing whatsoever about which model. Two workspaces on the
    same model looked like two models; one workspace switching models looked
    like no change at all.
    """
    from src.llm.client import LLMClient

    sig = inspect.signature(LLMClient.__init__)
    assert "resolve_billing_model" in sig.parameters, (
        "there is no way to record anything but the name we dialled"
    )

    src = (ROOT / "src" / "llm" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "record_spend")
    model_arg = next(k.value for k in call.keywords if k.arg == "model")
    assert not isinstance(model_arg, ast.Name), (
        "the ledger is handed the model name verbatim, so a gateway "
        "deployment lands in the by-model breakdown"
    )


def test_the_mapping_leaves_direct_calls_alone():
    """A workspace on direct provider keys never sees a deployment name, and
    rewriting its model would be a fix inventing a problem."""
    src = (ROOT / "src" / "llm" / "client.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_billing_model")
    body = ast.dump(fn)
    assert "litellm_proxy/" in body, (
        "_billing_model rewrites every model rather than only gateway ones"
    )


# ─── and who asked ───────────────────────────────────────────────────


def test_the_main_client_records_who_asked():
    """"By user" showed one row, "—", holding 95% of the workspace's spend.

    The id was bound into the key resolver's closure and never reached the
    ledger, so the only breakdown about PEOPLE was about nobody. The native
    Gemini path recorded it all along, which is why the row that did have a
    name came from there.
    """
    import ast

    src = (ROOT / "src" / "llm" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "record_spend")
    assert any(k.arg == "user_id" for k in call.keywords), (
        "the ledger is written without the caller, so by-user is by-nobody"
    )

    from src.llm.client import LLMClient

    assert "user_id" in inspect.signature(LLMClient.__init__).parameters


def test_a_uuid_is_not_an_answer_to_who_asked():
    """The row keyed by a user id is drawn with the person's name.

    Resolved at read time rather than copied onto the ledger: a name changes,
    and a ledger holding a copy would show the old one forever.
    """
    import ast

    spend = (ROOT / "src" / "api" / "routers" / "spend.py").read_text(encoding="utf-8")
    tree = ast.parse(spend)

    row = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               and n.name == "GroupRow")
    assert any(isinstance(s, ast.AnnAssign) and s.target.id == "label"
               for s in row.body), "a group row cannot carry a display name"

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_name_users"), None)
    assert fn is not None, "nothing turns a user id into a name"
    body = ast.dump(fn)
    assert "get_by_id" in body
    # A store that is unavailable must not empty the table.
    assert any(isinstance(n, ast.ExceptHandler) for n in ast.walk(fn))


def test_the_empty_row_is_explained_by_the_column_it_is_in():
    """The footnote said "chat and embeddings span the workspace rather than
    one repository" under every breakdown — including By user, where the empty
    row means nobody was recorded. A confident explanation of the wrong thing
    is worse than none."""
    page = (ROOT / "web" / "app" / "(app)" / "admin" / "usage"
            / "page.tsx").read_text(encoding="utf-8")
    assert "nullHint" in page, "one footnote is reused for every dimension"
    assert "admin.usage.nullUser" in page

    en = json.loads(
        (ROOT / "web" / "lib" / "i18n" / "messages" / "en.json").read_text(
            encoding="utf-8"))
    assert "repositor" not in en["admin.usage.nullUser"].lower(), (
        "the by-user footnote still explains repositories"
    )
