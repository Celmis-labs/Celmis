"""One operation, one implementation, whatever door it came through.

"Run a dependency audit" existed twice: once in the HTTP route and once in
src/automation/actions.py, both ending at the same run_audit() and everything
before it written separately. They had already diverged — the fifty-repository
cap and the "is this repository even yours" check lived in the action and
nowhere else, so an audit over two hundred repositories was refused when an
agent asked through MCP and accepted when a person asked through the web.

The duplication is not the problem; the divergence of invariants is. The
mechanism is always the same: you are working on one surface, you add a check,
and the other one is in a different file and did not break. Three months later
nobody remembers there were two.

What makes it worth stopping for is which invariant goes next. This repository
has a commit named "an id was enough to reach another tenant's data". A tenancy
fix applied to one path and not the other is that bug with a longer life. And
the audit-log entry — the thing the evidence pack is assembled from — would
have been the next thing to exist on one side only, which produces a pack with
holes: worse than no pack, because it invites confidence.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ROUTE = (SRC / "api" / "routers" / "deps.py").read_text(encoding="utf-8")


def test_the_route_delegates_rather_than_reimplements():
    assert "from src.automation.actions import" in ROUTE
    assert "start_dep_audit(" in ROUTE


def test_the_route_no_longer_builds_the_run_itself():
    """The tell that a second implementation is back: the route constructing
    the row and enqueueing the job on its own."""
    tree = ast.parse(ROUTE)
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "start_audit"
    )
    body = ast.unparse(handler)
    assert "DepAuditRun(id=" not in body, "the route creates its own run again"
    assert "KIND_DEPS_AUDIT" not in body, "the route enqueues its own job again"


def test_both_surfaces_inherit_the_repository_cap():
    """The specific divergence that started this. It was enforced for MCP and
    not for the web, so the same request meant two different things."""
    from src.automation.actions import MAX_AUDIT_REPOS

    assert MAX_AUDIT_REPOS > 0
    actions = (SRC / "automation" / "actions.py").read_text(encoding="utf-8")
    assert "MAX_AUDIT_REPOS" in actions
    # And it is reachable from the web now, because the web goes through here.
    assert "start_dep_audit(" in ROUTE


def test_the_action_kept_what_only_the_route_had():
    """Converging must not quietly drop a capability. `force` — supersede a run
    that stopped reporting progress, and self-heal a row whose queue job
    vanished — existed only in the route."""
    from src.automation.actions import start_dep_audit

    assert "force" in inspect.signature(start_dep_audit).parameters
    actions = (SRC / "automation" / "actions.py").read_text(encoding="utf-8")
    assert "orphaned (queue job was deleted or lost)" in actions
    assert "mark_cancelled" in actions


def test_a_refusal_reaches_the_caller_as_a_sentence():
    """The action refuses with something a person can act on. A 500 would turn
    "at most 50 repositories" into a stack trace."""
    assert "ActionError" in ROUTE
    assert "status_code=409" in ROUTE


# ─── sets: the thing a sentence answers well and a form does not ─────


def test_documentation_can_be_asked_for_by_condition():
    """"Every service that has no documentation" is a set defined by a
    condition. Covering it through the single-repository button meant finding
    them among forty and pressing it forty times."""
    from src.automation.actions import generate_docs

    params = inspect.signature(generate_docs).parameters
    assert "missing_only" in params
    assert "owner" in params, "no way to say 'everything under acme'"
    assert "repo_slugs" in params


def test_a_bulk_request_reports_what_it_refused():
    """A bulk action that silently covers nine of ten is the failure this
    surface exists to avoid, so every repository comes back in one list or the
    other."""
    actions = (SRC / "automation" / "actions.py").read_text(encoding="utf-8")
    src = actions[actions.index("async def generate_docs("):]
    assert '"skipped"' in src and '"queued"' in src
    assert "not indexed yet" in src, (
        "an unindexed repository would be documented from filenames"
    )
    assert "already has documentation" in src


def test_the_bulk_build_is_capped_too():
    """Lower than the audit's, because each repository is one model call per
    module rather than one in total."""
    from src.automation.actions import MAX_AUDIT_REPOS, MAX_VAULT_REPOS

    assert MAX_VAULT_REPOS < MAX_AUDIT_REPOS


def test_bulk_generation_goes_through_the_action_too():
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    assert "from src.automation.actions import ActionError, Actor, generate_docs" in docs


# ─── the whole set, downloadable ────────────────────────────────────


def test_the_archive_covers_every_repository():
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    src = docs[docs.index("def export_all_docs("):]
    assert "list_for_workspace(workspace_id)" in src, "not workspace-scoped"
    assert "zipfile" in src


def test_the_archive_names_what_is_missing_from_it():
    """A download that silently covers six of nine services looks complete.
    The gap is named inside the archive rather than left to be noticed by
    counting folders."""
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    assert "MISSING.txt" in docs
    assert "have no documentation yet" in docs


def test_documents_in_the_archive_keep_their_marks():
    """A note lifted out of the zip still has to say what produced it — the
    same rule as the single-repository export."""
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    src = docs[docs.index("def export_all_docs("):]
    assert "as_footer" in src


def test_the_static_routes_are_declared_before_the_parameterised_one():
    """FastAPI matches in declaration order. Declared after, GET
    /api/docs/export-all is read as a repository whose slug is "export-all" —
    a 404 that looks like a missing repo rather than a shadowed route."""
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    parameterised = docs.index('@router.get("/{slug}"')
    assert docs.index('@router.get("/export-all")') < parameterised
    assert docs.index('@router.post("/generate"') < parameterised


# ─── the set operations reached the interface too ────────────────────


def test_the_documentation_page_offers_the_set_not_just_the_repository():
    """Everything on that page acted on the repository in the picker. "The
    documentation" for a platform of nine services meant nine downloads."""
    page = (ROOT / "web" / "app" / "(app)" / "docs" / "page.tsx").read_text(
        encoding="utf-8")
    assert "docs.generateMissing" in page
    assert "docs.exportAll" in page
    assert "missing_only: true" in page, (
        "the button regenerates everything instead of filling the gaps"
    )


def test_the_bulk_button_reports_a_no_op():
    """Every repository skipped is the common case once the gaps are filled —
    a spinner that stops and a list that does not change is not an answer."""
    page = (ROOT / "web" / "app" / "(app)" / "docs" / "page.tsx").read_text(
        encoding="utf-8")
    assert "docs.generateNothing" in page
    assert "!r.queued.length" in page


def test_the_archive_download_is_authenticated():
    """Every export endpoint reads the Authorization header and nothing else,
    so a bare anchor downloads a 401 body — which this codebase shipped once
    already, under a button labelled Download SBOM."""
    page = (ROOT / "web" / "app" / "(app)" / "docs" / "page.tsx").read_text(
        encoding="utf-8")
    assert "downloadWithAuth" in page
