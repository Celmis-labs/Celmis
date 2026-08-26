"""Reaching a row by its id must not skip the workspace boundary.

Found by testing production: a user of workspace B could read AND delete
workspace A's projects, and read A's chats in full, by asking for them by id.

    GET    /api/projects/{id}            -> 200, another tenant's project
    DELETE /api/projects/{id}            -> 204, gone
    GET    /api/chats/{id}               -> 200, the whole transcript

The tell that it was an oversight rather than a decision: the list endpoints on
the very same routers filter by workspace. Someone scoped the collection and
never came back for the members. `GET /api/notifications/bindings` had the same
shape and answered with every tenant's routing table.

These tests read the handlers rather than standing up Postgres and two
authenticated sessions. That makes them cheap enough to run on every commit,
and it pins the property that actually broke: a handler that takes an id from
the path must also take the workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROUTERS = Path(__file__).resolve().parents[2] / "src" / "api" / "routers"

#: Routers whose rows are per-workspace and are addressable by id.
SCOPED_ROUTERS = ["projects.py", "chats.py"]

#: Handlers that legitimately take no workspace: they are not per-tenant, or
#: they resolve the tenant themselves from something else in the path.
EXEMPT = {
    "resolve_active_workspace",
}


def _handlers(path: Path) -> list[tuple[str, list[str], str]]:
    """(name, argument names, decorator source) for every route handler."""
    tree = ast.parse(path.read_text())
    lines = path.read_text().splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = " ".join(
            "\n".join(lines[d.lineno - 1:(d.end_lineno or d.lineno)])
            for d in node.decorator_list
        )
        if "@router." not in decorators and "_router." not in decorators:
            continue
        args = [a.arg for a in node.args.args + node.args.kwonlyargs]
        out.append((node.name, args, decorators))
    return out


@pytest.mark.parametrize("router", SCOPED_ROUTERS)
def test_every_by_id_handler_takes_the_workspace(router: str):
    """A path id without a workspace is the whole bug, in one line."""
    offenders = []
    for name, args, decorators in _handlers(ROUTERS / router):
        if name in EXEMPT:
            continue
        takes_path_id = "{" in decorators and "_id" in decorators
        if takes_path_id and not any(a in ("ws_id", "workspace_id") for a in args):
            offenders.append(name)
    assert not offenders, (
        f"{router}: {offenders} address a row by id without scoping it to a "
        f"workspace — any signed-in user of any tenant can reach it"
    )


@pytest.mark.parametrize("router", SCOPED_ROUTERS)
def test_the_list_endpoint_was_scoped_all_along(router: str):
    """Pins the asymmetry that made this easy to miss.

    If a future change unscopes the list endpoint too, the test above stops
    being suspicious of anything — so the baseline is asserted as well.
    """
    listers = [
        args for name, args, _ in _handlers(ROUTERS / router)
        if name.startswith("list_")
    ]
    assert listers, f"{router} has no list handler"
    for args in listers:
        assert any(a in ("ws_id", "workspace_id") for a in args)


def test_notification_bindings_are_scoped_through_their_channel():
    """Bindings carry no workspace column; the channel they point at does.

    So the boundary is a join. Asserting on the query text is crude, but the
    alternative is a live database, and the failure being guarded against is
    precisely that the join is missing.
    """
    source = (ROUTERS / "intel.py").read_text()
    start = source.find("async def list_bindings")
    assert start > 0
    body = source[start:start + 1200]
    assert "NotificationChannel.workspace_id == ws_id" in body, (
        "list_bindings does not filter by the channel's workspace — it answers "
        "with every tenant's routing table"
    )
