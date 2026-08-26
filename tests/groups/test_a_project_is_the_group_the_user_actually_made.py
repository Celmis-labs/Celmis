"""The product had one concept and the code had two.

A user groups repositories by making a **Project** in the web interface. Every
cross-repo capability — drift detection, the cross-repo edge graph, the radius
a review reports — keys on a **Group**, which only the CLI and a single HTTP
route can create and which the web has no page for at all.

So a workspace set up the normal way had the concept and not the capability,
and the product never said so: the review reported no cross-repo findings,
which is precisely what the truthful answer looks like. The two objects even
share a docstring — "a logical group of repos" and "group of repositories for
cross-repo analysis" — and share no code whatsoever: neither `src/review/` nor
`src/groups/` imports `Project`.

A project is now READ as a group, in `GroupManager` alone, because after the
addressing work drift, materialisation, the review graph context, purge and
the MCP tools all reach groups through `iter_groups()`. One seam, six callers,
and not one of them had to learn a second kind of grouping — which is the
whole test below.

Read-only in that direction: the project is the source of truth, and a YAML
file beside it would be a second one, free to disagree from the first edit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import Settings
from src.groups.manager import GroupManager, GroupValidationError

WS_A, WS_B = "ws-alpha", "ws-beta"


def _cfg(slug, provider, full_name):
    return SimpleNamespace(repo_slug=slug, provider=provider, full_name=full_name)


REGISTRY = {
    WS_A: [_cfg("github_acme-api", "github", "acme/api"),
           _cfg("github_acme-web", "github", "acme/web"),
           _cfg("github_acme-solo", "github", "acme/solo")],
    WS_B: [_cfg("github_other-x", "github", "other/x"),
           _cfg("github_other-y", "github", "other/y")],
}


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    m = GroupManager(settings=Settings(workspace_dir=tmp_path))
    monkeypatch.setattr(
        "src.api.auto_review.get_auto_review_store",
        lambda: SimpleNamespace(list_for_workspace=lambda ws: REGISTRY.get(ws, [])),
    )
    return m


def _projects(monkeypatch, mgr, rows):
    """Stand in for the database read, which the manager keeps private."""
    def fake(workspace_id=None):
        return [g for g in rows
                if workspace_id is None or g.workspace_id == workspace_id]
    monkeypatch.setattr(mgr, "_project_groups", fake)


from src.groups.models import RepoGroup  # noqa: E402


def _project_group(name, ws, repos, pid="p-1"):
    return RepoGroup(name=name, repos=repos, workspace_id=ws, project_id=pid)


# ─── a project reaches the seam every consumer uses ──────────────────


def test_a_project_appears_among_the_groups(mgr, monkeypatch):
    _projects(monkeypatch, mgr, [
        _project_group("Acme Platform", WS_A, ["github:acme/api", "github:acme/web"]),
    ])

    assert [g.name for _p, g in mgr.iter_groups(WS_A)] == ["Acme Platform"]


def test_the_indexer_finds_a_project_for_a_repository(mgr, monkeypatch):
    """`groups_containing` is what enqueues cross-repo materialisation."""
    _projects(monkeypatch, mgr, [
        _project_group("Acme Platform", WS_A, ["github:acme/api", "github:acme/web"]),
    ])

    found = mgr.groups_containing("github_acme-api", WS_A)

    assert [g.name for g in found] == ["Acme Platform"]
    assert found[0].project_id == "p-1"


def test_another_tenants_project_is_not_visible(mgr, monkeypatch):
    _projects(monkeypatch, mgr, [
        _project_group("Acme Platform", WS_A, ["github:acme/api", "github:acme/web"]),
        _project_group("Theirs", WS_B, ["github:other/x", "github:other/y"], "p-2"),
    ])

    assert [g.name for _p, g in mgr.iter_groups(WS_A)] == ["Acme Platform"]


def test_a_hand_written_group_wins_over_a_project_of_the_same_name(mgr, monkeypatch):
    """A file somebody wrote is not shadowed by a view.

    The name here has no space, because a YAML group's name becomes a path and
    `_validate_name` refuses one — which is the asymmetry that makes projects
    the better-named half of this pair: "Acme Platform" is a project name and
    can never be a group name.
    """
    g = mgr.create("platform", workspace_id=WS_A)
    g.add_repo("github:acme/api")
    mgr.save(g)
    _projects(monkeypatch, mgr, [
        _project_group("platform", WS_A, ["github:acme/api", "github:acme/web"]),
    ])

    groups = [g for _p, g in mgr.iter_groups(WS_A)]

    assert len(groups) == 1
    assert groups[0].project_id is None


def test_the_materialize_handler_can_open_it_by_name(mgr, monkeypatch):
    """The job carries a name; a listing whose loader cannot open what it
    listed is the defect this codebase has already shipped twice."""
    _projects(monkeypatch, mgr, [
        _project_group("Acme Platform", WS_A, ["github:acme/api", "github:acme/web"]),
    ])

    assert mgr.load("Acme Platform", WS_A).project_id == "p-1"


# ─── it is a view, not a copy ────────────────────────────────────────


def test_a_project_group_refuses_to_be_written(mgr, monkeypatch):
    g = _project_group("Acme Platform", WS_A, ["github:acme/api", "github:acme/web"])

    with pytest.raises(GroupValidationError, match="view of a project"):
        mgr.save(g)


def test_its_edges_are_keyed_on_the_id_not_the_name(mgr):
    """A project can be renamed, and two tenants can hold one name."""
    a = _project_group("Platform", WS_A, ["github:acme/api", "github:acme/web"], "p-1")
    renamed = _project_group("Renamed", WS_A, a.repos, "p-1")
    other = _project_group("Platform", WS_B, ["github:other/x", "github:other/y"], "p-2")

    assert mgr.graph_path(a) == mgr.graph_path(renamed)
    assert mgr.graph_path(a) != mgr.graph_path(other)


# ─── the seam, which is the point ────────────────────────────────────


def test_no_consumer_had_to_learn_about_projects():
    """The whole design: one seam, six callers, none of them changed.

    Keyed on imports rather than prose — a consumer that started reading the
    database itself would show up here.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    consumers = [
        "src/review/cross_repo_drift.py", "src/review/graph_context.py",
        "src/sync/incremental.py",
        "src/sync/handlers.py", "src/mcp_server/tools.py",
    ]
    # purge is deliberately excluded: it is the one place that legitimately
    # knows both, because it deletes `project_repos` rows AND strips group
    # membership. Everything else must see only groups.
    offenders = []
    for rel in consumers:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and node.module == "src.db.models"
                    and any(a.name in {"Project", "ProjectRepo"} for a in node.names)):
                offenders.append(rel)

    assert not offenders, offenders
