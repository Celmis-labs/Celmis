"""Three ways one workspace could reach another's vectors, and none of them
was a permission check.

The collection is shared: every workspace's code lives in one Qdrant
collection, and the only thing separating tenants was that each caller
happened to pass the right repository list. Partitioning the payload fixed
the reads. These are the three paths that partitioning alone did not close,
found by reading the diff for what it did NOT touch.

  A list endpoint scrolled the whole collection with no filter and returned
  every tenant's repository slugs — the access resolver behind it falls open
  for any repository with no rules configured, which is every repository
  until somebody writes one.

  A purge deleted by repository slug alone. A slug is
  `{provider}_{owner}-{name}` and is not unique across tenants, so purging
  one workspace's repository could take another's vectors with it. The read
  hole discloses; this one destroys.

  A settings page dropped and recreated the entire shared collection to
  change the embedding width — wiping every workspace, on behalf of a person
  who was changing their own model.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fn(module_path: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module_path} — check this test")


# ─── the read ────────────────────────────────────────────────────────


def test_listing_repositories_is_scoped_to_a_workspace():
    """It returned every tenant's slugs, with point counts, to any signed-in
    user."""
    fn = _fn("src/api/routers/qa.py", "available_repos")
    body = ast.dump(fn)
    assert "scroll_filter" in body, (
        "the scroll runs with no filter, so it pages the whole installation"
    )
    assert "must_conditions" in body, (
        "the filter is not the workspace scope every other read uses"
    )


# ─── the delete ──────────────────────────────────────────────────────


def test_purging_a_repository_cannot_take_another_tenants_vectors():
    from src.repos.purge import purge_repo

    assert "workspace_id" in inspect.signature(purge_repo).parameters, (
        "purge_repo cannot know whose repository it is deleting"
    )
    fn = _fn("src/repos/purge.py", "_purge_qdrant")
    body = ast.dump(fn)
    assert "must_conditions" in body, (
        "the delete filter is the slug alone, and a slug is not unique "
        "across tenants"
    )


def test_the_http_purge_passes_the_tenant_rather_than_defaulting():
    """A default would make the endpoint compile and the isolation
    theoretical."""
    src = (ROOT / "src" / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert "workspace_id=workspace_id" in src


# ─── the recreate ────────────────────────────────────────────────────


def test_changing_the_embedding_width_cannot_wipe_every_workspace():
    src = (ROOT / "src" / "api" / "routers" / "llm.py").read_text(encoding="utf-8")
    idx = src.index("qc.delete_collection(coll)")
    guard = src[max(0, idx - 1200):idx]
    assert "_is_single_tenant" in guard, (
        "the shared collection is dropped with no check on who else is in it"
    )


def test_the_single_tenant_check_fails_closed():
    """It answers "is this installation safe to wipe". Unknown must mean no:
    a check that says yes when the database is unreachable is a check that
    says yes exactly when something is already wrong."""
    from src.api.routers.llm import _is_single_tenant

    # No database is reachable in the test environment, so this exercises the
    # unknown branch rather than the happy one.
    assert _is_single_tenant() is False
