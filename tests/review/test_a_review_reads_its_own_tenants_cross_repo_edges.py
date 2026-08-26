"""The review found cross-repo edges by globbing a directory.

`_cross_repo` built its file list as `workspace_dir/groups/*.fdblite`, which
was wrong in both directions at the same time:

  * it read EVERY tenant's cross-repo edge index into every review, and
    reported the other tenant's repositories as callers of this one — the
    disclosure the group scoping exists to prevent, arriving through the graph
    instead of through the group listing;
  * and the moment a scoped group's graph moved into its tenant directory
    beside the YAML — which is the fix for the first half — the flat glob
    stopped matching it, so cross-repo callers went to zero for exactly the
    installs that had the feature working.

I shipped the second half before noticing the first, which is how a fix for a
namespace becomes an outage for a feature: the writer moved and five readers
did not. `graph_path()` is now the one place that knows the layout.

Drift, two lines below in the orchestrator, already took `workspace_id`. The
graph context did not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import Settings
from src.groups.manager import GroupManager
from src.review.graph_context import _cross_repo

WS_A, WS_B = "ws-alpha", "ws-beta"
MINE = "github_acme-api"


@pytest.fixture
def world(tmp_path):
    settings = SimpleNamespace(workspace_dir=tmp_path)
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))
    return SimpleNamespace(settings=settings, mgr=mgr, tmp=tmp_path)


def _group_with_graph(world, name, ws, repos):
    g = world.mgr.create(name, workspace_id=ws)
    for r in repos:
        g.add_repo(r)
    world.mgr.save(g)
    path = world.mgr.graph_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return g, path


class _Store:
    """A graph that says one repo calls the file under review."""

    def __init__(self, caller_repo: str) -> None:
        self.caller_repo = caller_repo

    def query(self, cypher, params=None):
        if "count" in cypher.lower() or "RETURN" in cypher:
            return [{"repo": self.caller_repo, "edges": 3,
                     "file": "src/api.py", "from_repo": self.caller_repo,
                     "from_id": "x", "n": 3, "c": 3}]
        return []

    def close(self):
        pass


def test_the_review_finds_its_own_tenants_graph(world):
    _group_with_graph(world, "product", WS_A, ["github:acme/api"])
    opened: list = []

    _cross_repo(MINE, ["src/api.py"], world.settings,
                lambda p: (opened.append(p), _Store("github_acme-web"))[1],
                workspace_id=WS_A)

    assert len(opened) == 1, "the scoped group's graph was never opened"


def test_the_review_does_not_open_another_tenants_graph(world):
    _group_with_graph(world, "mine", WS_A, ["github:acme/api"])
    _, theirs = _group_with_graph(world, "theirs", WS_B, ["github:other/secret"])
    opened: list = []

    _cross_repo(MINE, ["src/api.py"], world.settings,
                lambda p: (opened.append(p), _Store("github_other-secret"))[1],
                workspace_id=WS_A)

    assert theirs not in opened, (
        "one tenant's review read another tenant's cross-repo edges"
    )


def test_a_flat_group_is_still_found(world):
    """Back-compat: a single-tenant install keeps working untouched."""
    _group_with_graph(world, "product", "default", ["github:acme/api"])
    opened: list = []

    _cross_repo(MINE, ["src/api.py"], world.settings,
                lambda p: (opened.append(p), _Store("github_acme-web"))[1],
                workspace_id="default")

    assert len(opened) == 1


def test_no_group_opens_nothing_and_does_not_raise(world):
    opened: list = []

    by_repo, per_file = _cross_repo(
        MINE, ["src/api.py"], world.settings,
        lambda p: (opened.append(p), _Store("x"))[1], workspace_id=WS_A)

    assert opened == []
    assert by_repo == {} and per_file == {}


def test_the_orchestrator_hands_the_tenant_down():
    """The workspace was already at the call site, on the very next line."""
    import ast
    import inspect

    import src.review.orchestrator as orch

    src = inspect.getsource(orch)
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_graph_context"
    ]
    assert calls, "build_graph_context is no longer called"
    for call in calls:
        assert any(kw.arg == "workspace_id" for kw in call.keywords), (
            "the review builds its graph context without a tenant"
        )
