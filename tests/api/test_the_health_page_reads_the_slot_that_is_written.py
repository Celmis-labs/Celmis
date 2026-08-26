"""The integrations health card looks where connections are actually saved.

THE DEFECT. `integrations_health` read the credential store with
`user_id=user.id` — a slot nothing writes to. Every git connection saved
through the UI lands in the workspace slot `ws:{workspace_id}`, which is what
`connections._slot_for` returns and what `/api/ops/diag` shows for all five
stored connections on production.

So the card was structurally incapable of ever saying "connected". Observed:
`/api/connections` returned github connected as `celmis-codereviewer` while
`/api/health/integrations` called the same provider `not_configured` with
detail "no token saved", in the same second. Bitbucket, connected since
2026-08-10, reported the same.

A health page that reports a working dependency as unconfigured trains its
reader to ignore it.
"""

from __future__ import annotations

import ast
import inspect


def _source() -> str:
    from src.api.routers import search
    return inspect.getsource(search.integrations_health)


def _code_only() -> str:
    """The function with comments and docstrings removed, so a comment about
    the workspace slot cannot satisfy a test about reading it."""
    tree = ast.parse(inspect.getsource(inspect.getmodule(_target())))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "integrations_health":
            return ast.unparse(node)
    raise AssertionError("integrations_health not found")


def _target():
    from src.api.routers import search
    return search.integrations_health


def test_the_git_cards_read_the_workspace_slot():
    body = _code_only()

    assert "git_workspace_slot" in body


def test_the_workspace_slot_is_tried_before_the_user_slot():
    """The user slot survives only as a back-compat fallback for tokens saved
    before connections moved. Reading it FIRST, or only, is the defect."""
    body = _code_only()

    assert body.index("user_id=slot") < body.index("user_id=user.id")


def test_both_card_kinds_use_the_workspace_slot():
    """`_slot_for` puts git AND llm providers in the same workspace slot, and
    the llm block had the identical defect."""
    body = _code_only()

    assert body.count("user_id=slot") >= 2


def test_the_slot_helper_agrees_with_the_writer():
    """Both sides must derive the slot the same way, or this comes back."""
    from src.api.routers.connections import _slot_for
    from src.credentials.git_keys import git_workspace_slot

    assert _slot_for("github", "ws-1") == git_workspace_slot("ws-1")


def test_the_endpoint_still_takes_a_workspace():
    from src.api.routers.search import integrations_health

    assert "ws_id" in inspect.signature(integrations_health).parameters
