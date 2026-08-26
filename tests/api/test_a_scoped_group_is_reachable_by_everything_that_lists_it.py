"""Four call sites kept the old bargain after the group namespace changed.

Groups moved from `groups/{name}.yaml` to `groups/<tenant>/{name}.yaml`, and
`load()` stopped answering to a bare name. Four places had not been told:

  * DELETE /api/repos/groups/{name} checked ownership WITH the tenant and then
    deleted WITHOUT it — so it unlinked whichever group sat at the shared flat
    address, somebody else's, and left its own in place while returning 204.
  * `purge` listed every name on the installation and opened each flat, so a
    tenant-scoped group kept the purged repository and the report said done.
  * the cross-repo edge index was `groups/{name}.fdblite` — the YAML got a
    tenant directory, the graph file beside it did not, and two tenants
    holding a group called "product" read each other's edges.
  * the MCP `list_groups` tool listed names installation-wide, then dropped
    every scoped group behind a warning when the open failed.

One shape underneath all four: enumerate through one addressing scheme, open
through another. `iter_groups()` exists so the loop carries the path.
"""

from __future__ import annotations

import yaml

from src.config import Settings
from src.groups.manager import GroupManager, _ws_dir

WS_A, WS_B = "ws-alpha", "ws-beta"


def _mgr(tmp_path) -> GroupManager:
    return GroupManager(settings=Settings(workspace_dir=tmp_path))


def _flat_group(tmp_path, name, ws, repos):
    d = tmp_path / "groups"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.yaml"
    f.write_text(yaml.safe_dump({"name": name, "description": "", "repos": repos,
                                 "workspace_id": ws}), encoding="utf-8")
    return f


def _scoped_group(mgr, name, ws, repos):
    g = mgr.create(name, workspace_id=ws)
    for r in repos:
        g.add_repo(r)
    mgr.save(g)
    return g


# ─── delete ──────────────────────────────────────────────────────────


def test_delete_refuses_a_group_it_does_not_own(tmp_path):
    victim = _flat_group(tmp_path, "product", WS_A, ["github:acme/a"])
    mgr = _mgr(tmp_path)

    assert mgr.delete("product", WS_B) is False
    assert victim.exists(), "one tenant deleted another tenant's group"


def test_delete_removes_the_callers_own_scoped_group(tmp_path):
    mgr = _mgr(tmp_path)
    _scoped_group(mgr, "product", WS_B, ["github:acme/b"])

    assert mgr.delete("product", WS_B) is True
    assert mgr.list(WS_B) == []


def test_deleting_leaves_the_other_tenants_group_alone(tmp_path):
    mgr = _mgr(tmp_path)
    _scoped_group(mgr, "product", WS_A, ["github:acme/a"])
    _scoped_group(mgr, "product", WS_B, ["github:acme/b"])

    mgr.delete("product", WS_B)

    assert mgr.list(WS_A) == ["product"]
    assert mgr.load("product", WS_A).repos == ["github:acme/a"]


def test_an_untenanted_delete_still_works_for_the_cli(tmp_path):
    """`analyzer group delete` passes no workspace and must keep working."""
    flat = _flat_group(tmp_path, "product", "default", ["github:acme/a"])
    mgr = _mgr(tmp_path)

    assert mgr.delete("product") is True
    assert not flat.exists()


# ─── purge ───────────────────────────────────────────────────────────


def test_purge_reaches_a_tenant_scoped_group(tmp_path, monkeypatch):
    """`purge` builds its own GroupManager(), so point the settings at tmp."""
    from src.config import get_settings
    from src.repos.purge import PurgeReport, _purge_groups

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        mgr = _mgr(tmp_path)
        _scoped_group(mgr, "product", WS_A,
                      ["github:acme/keep", "github:acme/doomed"])

        report = PurgeReport(slug="github_acme-doomed")
        _purge_groups("github_acme-doomed", report)

        assert report.errors == []
        assert report.group_memberships_removed == 1
        assert mgr.load("product", WS_A).repos == ["github:acme/keep"]
    finally:
        get_settings.cache_clear()


def test_purge_still_reaches_a_flat_group(tmp_path, monkeypatch):
    from src.config import get_settings
    from src.repos.purge import PurgeReport, _purge_groups

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        _flat_group(tmp_path, "legacy", "default",
                    ["github:acme/keep", "github:acme/doomed"])

        report = PurgeReport(slug="github_acme-doomed")
        _purge_groups("github_acme-doomed", report)

        assert report.group_memberships_removed == 1
        assert _mgr(tmp_path).load("legacy").repos == ["github:acme/keep"]
    finally:
        get_settings.cache_clear()


# ─── the cross-repo graph file ───────────────────────────────────────


def test_two_tenants_do_not_share_one_cross_repo_graph(tmp_path):
    mgr = _mgr(tmp_path)
    a = _scoped_group(mgr, "product", WS_A, ["github:acme/a"])
    b = _scoped_group(mgr, "product", WS_B, ["github:acme/b"])

    assert mgr.graph_path(a) != mgr.graph_path(b), (
        "both tenants' cross-repo edges landed in one file"
    )
    assert _ws_dir(WS_A) in str(mgr.graph_path(a))


def test_the_graph_sits_beside_the_group_it_belongs_to(tmp_path):
    mgr = _mgr(tmp_path)
    g = _scoped_group(mgr, "product", WS_A, ["github:acme/a"])

    path = mgr.graph_path(g)
    assert path.suffix == ".fdblite"
    assert path.parent == (tmp_path / "groups" / _ws_dir(WS_A))


def test_a_flat_group_keeps_its_flat_graph(tmp_path):
    """Back-compat: nothing on an existing single-tenant install moves."""
    _flat_group(tmp_path, "product", "default", ["github:acme/a"])
    mgr = _mgr(tmp_path)

    g = mgr.load("product", "default")
    assert mgr.graph_path(g) == tmp_path / "groups" / "product.fdblite"


def test_the_indexer_uses_the_same_address(tmp_path):
    from src.groups.indexer import GroupIndexer

    mgr = _mgr(tmp_path)
    g = _scoped_group(mgr, "product", WS_A, ["github:acme/a"])
    indexer = GroupIndexer(g, Settings(workspace_dir=tmp_path))

    assert indexer._cross_repo_graph_path() == mgr.graph_path(g)


# ─── iter_groups, the shared fix ─────────────────────────────────────


def test_iter_groups_opens_everything_it_lists(tmp_path):
    mgr = _mgr(tmp_path)
    _scoped_group(mgr, "alpha", WS_A, ["github:acme/a"])
    _scoped_group(mgr, "beta", WS_B, ["github:acme/b"])
    _flat_group(tmp_path, "legacy", "default", ["github:acme/c"])

    seen = {g.name for _p, g in mgr.iter_groups()}

    assert seen == set(mgr.list()), "listed a group it could not open"


def test_iter_groups_scopes_to_one_tenant(tmp_path):
    mgr = _mgr(tmp_path)
    _scoped_group(mgr, "alpha", WS_A, ["github:acme/a"])
    _scoped_group(mgr, "beta", WS_B, ["github:acme/b"])

    assert [g.name for _p, g in mgr.iter_groups(WS_A)] == ["alpha"]


def test_a_corrupt_file_does_not_stop_the_sweep(tmp_path):
    mgr = _mgr(tmp_path)
    _scoped_group(mgr, "alpha", WS_A, ["github:acme/a"])
    (tmp_path / "groups" / _ws_dir(WS_A) / "broken.yaml").write_text(
        "{{{ not yaml", encoding="utf-8")

    assert [g.name for _p, g in mgr.iter_groups(WS_A)] == ["alpha"]


# ─── the MCP tool ────────────────────────────────────────────────────


def test_mcp_list_groups_sees_a_scoped_group(tmp_path, monkeypatch):
    """It listed the name, failed to open it, and logged a warning instead."""
    from src.config import get_settings
    from src.mcp_server import tools

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        mgr = _mgr(tmp_path)
        _scoped_group(mgr, "product", WS_A, ["github:acme/a"])
        monkeypatch.setattr(tools, "get_group_manager", lambda: mgr)

        names = [g.name for g in tools.list_groups()]

        assert names == ["product"]
    finally:
        get_settings.cache_clear()


def test_mcp_list_groups_scopes_when_asked(tmp_path, monkeypatch):
    from src.config import get_settings
    from src.mcp_server import tools

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        mgr = _mgr(tmp_path)
        _scoped_group(mgr, "alpha", WS_A, ["github:acme/a"])
        _scoped_group(mgr, "beta", WS_B, ["github:acme/b"])
        monkeypatch.setattr(tools, "get_group_manager", lambda: mgr)

        assert [g.name for g in tools.list_groups(workspace_id=WS_A)] == ["alpha"]
    finally:
        get_settings.cache_clear()


def test_mcp_counts_edges_from_the_groups_own_graph(tmp_path, monkeypatch):
    """Two tenants, one group name: the edge count must not come from a file
    they share."""
    from src.config import get_settings
    from src.mcp_server import tools

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        mgr = _mgr(tmp_path)
        a = _scoped_group(mgr, "product", WS_A, ["github:acme/a"])
        _scoped_group(mgr, "product", WS_B, ["github:acme/b"])
        mgr.graph_path(a).write_bytes(b"")          # A has an index, B does not
        monkeypatch.setattr(tools, "get_group_manager", lambda: mgr)

        by_ws = {
            ws: tools.list_groups(workspace_id=ws)[0].cross_repo_indexed
            for ws in (WS_A, WS_B)
        }

        assert by_ws == {WS_A: True, WS_B: False}
    finally:
        get_settings.cache_clear()


# ─── the route, not just the manager ─────────────────────────────────
#
# The manager guard alone did not catch this: reverting the route to
# `delete(name)` left every manager test green, because a call with no tenant
# skips the ownership check by design — that is how the CLI deletes. The route
# HAS a tenant and has to pass it.


def _route_world(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    for target in ("src.groups.get_group_manager",
                   "src.groups.manager.get_group_manager"):
        monkeypatch.setattr(target, lambda: mgr, raising=False)
    monkeypatch.setattr("src.groups.manager._default_manager", mgr, raising=False)
    return mgr


def test_the_route_deletes_its_own_group_not_the_flat_one(tmp_path, monkeypatch):
    from src.api.routers.groups import delete_group

    mgr = _route_world(tmp_path, monkeypatch)
    stranger = _flat_group(tmp_path, "product", WS_A, ["github:acme/a"])
    _scoped_group(mgr, "product", WS_B, ["github:acme/b"])
    user = type("U", (), {"email": "b@example.com", "id": "u-b"})()

    delete_group("product", user=user, workspace_id=WS_B)

    assert stranger.exists(), "the route deleted another tenant's group"
    assert mgr.list(WS_B) == [], "the route reported success and deleted nothing"


def test_the_route_still_deletes_a_flat_group_for_its_owner(tmp_path, monkeypatch):
    from src.api.routers.groups import delete_group

    _route_world(tmp_path, monkeypatch)
    flat = _flat_group(tmp_path, "product", "default", ["github:acme/a"])
    user = type("U", (), {"email": "a@example.com", "id": "u-a"})()

    delete_group("product", user=user, workspace_id="default")

    assert not flat.exists()
