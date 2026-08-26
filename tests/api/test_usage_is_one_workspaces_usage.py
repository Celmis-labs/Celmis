"""`/api/usage` answers about the caller's workspace, not the installation.

THE DEFECT. The three queries in `usage_summary` read `review_runs` filtered on
`started_at` alone, behind plain `get_current_user`. Every authenticated
account on the installation therefore saw the same run count, the same token
totals and the same dollar figure — including workspaces it has no membership
in. On a deployment with five tenants that is a cross-tenant read of
commercial data.

The docstring called it intentional. `review_runs.workspace_id` has existed
since the column was added; nothing was asking for it.
"""

from __future__ import annotations

import ast
import inspect


def _queries() -> list[str]:
    """Every SQL statement in the module that reads `review_runs`.

    Keyed on `FROM review_runs` rather than on the table name appearing
    anywhere: the first draft matched any string containing "review_runs" and
    duly failed on the module DOCSTRING, which mentions the table in prose.
    A test that cannot tell a query from a sentence about a query is keyed on
    the wrong thing.

    ast is used rather than a regex over the file so comments are gone before
    matching — a comment mentioning `workspace_id` must not satisfy these.
    """
    from src.api.routers import usage

    tree = ast.parse(inspect.getsource(usage))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "FROM review_runs" in node.value
    ]


def test_there_are_still_queries_to_check():
    """Guards the guard: an empty list would make every test below pass while
    asserting nothing."""
    assert len(_queries()) >= 3


def test_every_query_against_review_runs_is_workspace_scoped():
    unscoped = [q for q in _queries() if "workspace_id" not in q]

    assert not unscoped, (
        "these read every tenant's runs: "
        + "; ".join(" ".join(q.split())[:90] for q in unscoped)
    )


def test_the_endpoint_asks_for_the_callers_workspace():
    """The scope has to come from the request, not from a default argument."""
    from src.api.routers.usage import usage_summary

    params = inspect.signature(usage_summary).parameters
    assert "workspace_id" in params

    default = params["workspace_id"].default
    assert getattr(default, "dependency", None) is not None, (
        "workspace_id must be a Depends(...), not a literal"
    )


def test_the_note_that_called_this_intentional_is_gone():
    """It said shared-workspace mode returning one aggregate for everyone was
    deliberate. It was a cross-tenant read."""
    from src.api.routers.usage import usage_summary

    doc = (usage_summary.__doc__ or "").lower()
    assert "same aggregate for every user" not in doc
