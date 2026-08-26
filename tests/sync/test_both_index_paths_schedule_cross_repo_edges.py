"""Cross-repo edges were built by the webhook path and by nothing else.

`cross_repo_materialize` had exactly one producer: `run_index` in
src/sync/incremental.py, the INCREMENTAL pass. The full pass — the per-repo
Index button, `POST /api/repos/index-all`, and the `index_repo_full` job that
both of them queue — ran the entire pipeline through `index_repo_sync` and
scheduled nothing.

So an installation that added its repositories through the UI and never
received a push had no cross-repo edges at all. Nothing reported it. A review
of such a repo said it found no cross-repo callers, which is indistinguishable
from the truthful answer that nothing calls it — the failure wore the shape of
a correct result.

Verified on prod at 0.1.0+145e463: `index-all?force=true` completed both
`index_repo_full` jobs and produced zero `cross_repo_materialize` rows, as a
global admin with no tenant filter.

Both paths now go through one hook, so they cannot drift apart again.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from src.config import Settings
from src.groups.indexer import enqueue_materialize_for_repo
from src.groups.manager import GroupManager

WS_A, WS_B = "ws-alpha", "ws-beta"
PROBE = "github:celmis-codereviewer/celmis-e2e-probe"
PROBE_SLUG = "github_celmis-codereviewer-celmis-e2e-probe"
SIBLING = "github:celmis-codereviewer/celmis-e2e-sibling"
SIBLING_SLUG = "github_celmis-codereviewer-celmis-e2e-sibling"


class FakeQueue:
    """Honours dedup the way the real table does: a pending row blocks."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, *, kind, payload, dedup_key=None, enqueued_by=None, **kw):
        if dedup_key and any(r["dedup_key"] == dedup_key for r in self.rows):
            return None
        self.rows.append({"kind": kind, "payload": payload,
                          "dedup_key": dedup_key, "enqueued_by": enqueued_by})
        return f"job-{len(self.rows)}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))
    queue = FakeQueue()
    monkeypatch.setattr("src.sync.queue.enqueue", queue.enqueue)
    monkeypatch.setattr("src.groups.get_group_manager", lambda: mgr)
    return mgr, queue


def _group(mgr, name, ws, repos):
    g = mgr.create(name, workspace_id=ws)
    g.workspace_id = ws
    for r in repos:
        g.add_repo(r)
    mgr.save(g)
    return g


# ─── the hook itself ─────────────────────────────────────────────────


def test_indexing_a_repo_schedules_its_group(world):
    mgr, queue = world
    _group(mgr, "product", WS_A, [PROBE, SIBLING])

    ids = enqueue_materialize_for_repo(PROBE_SLUG, enqueued_by="test")

    assert len(ids) == 1
    assert queue.rows[0]["kind"] == "cross_repo_materialize"
    assert queue.rows[0]["payload"]["group_name"] == "product"


def test_the_tenant_travels_in_the_payload_and_the_key(world):
    mgr, queue = world
    _group(mgr, "product", WS_A, [PROBE])

    enqueue_materialize_for_repo(PROBE_SLUG, enqueued_by="test")

    row = queue.rows[0]
    assert row["payload"]["workspace_id"] == WS_A
    assert WS_A in row["dedup_key"], (
        "two tenants owning a group of the same name would collapse into one job"
    )


def test_two_tenants_with_the_same_group_name_get_two_jobs(world):
    mgr, queue = world
    _group(mgr, "product", WS_A, [PROBE])
    _group(mgr, "product", WS_B, [PROBE])

    enqueue_materialize_for_repo(PROBE_SLUG, enqueued_by="test")

    assert len(queue.rows) == 2
    assert {r["payload"]["workspace_id"] for r in queue.rows} == {WS_A, WS_B}


def test_indexing_every_repo_of_one_group_coalesces(world):
    mgr, queue = world
    _group(mgr, "product", WS_A, [PROBE, SIBLING])

    first = enqueue_materialize_for_repo(PROBE_SLUG, enqueued_by="test")
    second = enqueue_materialize_for_repo(SIBLING_SLUG, enqueued_by="test")

    assert len(first) == 1 and second == []
    assert len(queue.rows) == 1, "index-all over a group must not fan out"


def test_a_repo_in_no_group_schedules_nothing(world):
    mgr, queue = world
    _group(mgr, "product", WS_A, [SIBLING])

    assert enqueue_materialize_for_repo(PROBE_SLUG, enqueued_by="test") == []
    assert queue.rows == []


# ─── both call sites ─────────────────────────────────────────────────
#
# Keyed on the AST call node, not on the source text: a docstring or a comment
# naming the hook must not satisfy this. (A test that greps for a word passes
# on the comment explaining the word's absence — a mistake made here before.)


def _calls_in(path: str, func: str) -> set[str]:
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func),
        None,
    )
    assert target is not None, f"{func} not found in {path}"
    return {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(target) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name | ast.Attribute)
    }


@pytest.mark.parametrize("path,func", [
    ("src/repos/indexing.py", "index_repo_sync"),      # the path that never did
    ("src/sync/incremental.py", "run_index"),          # the path that always did
])
def test_the_path_schedules_cross_repo_edges(path, func):
    assert "enqueue_materialize_for_repo" in _calls_in(path, func)


def test_neither_path_enqueues_the_job_itself(world):
    """One hook, so the tenant rule and the dedup key cannot drift apart."""
    for path in ("src/repos/indexing.py", "src/sync/incremental.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "KIND_CROSS_REPO_MATERIALIZE" not in names, (
            f"{path} builds the job itself instead of using the shared hook"
        )
