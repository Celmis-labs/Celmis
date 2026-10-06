"""The MCP graph tools answered for every tenant's graph.

Graph files live flat at ``<data_dir>/<repo_slug>/graph.fdblite``. The tools
``find_symbol``, ``get_symbol``, ``find_callers``, ``find_callees`` and
``query_graph`` took a ``repo_slug``, checked the ``read:graph`` scope, and
opened whatever file that slug named. Under multi_tenant any token with
``read:graph`` therefore read another tenant's symbol graph — and ran
arbitrary read-only Cypher against it — by spelling its slug. And because
``repo_data_path`` was ``data_dir / repo_slug`` with no check, ``../`` reached
any graph file on the box, registered or not.

These tests attack both: a caller of workspace A against workspace B's repo
(must read exactly like a repo that does not exist), and traversal slugs
against the path helper and against every tool.
"""

from __future__ import annotations

import pytest

from src.config import InvalidRepoSlug, Settings
from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store

WS_A, WS_B = "ws-alpha", "ws-beta"
SLUG_A, SLUG_B = "github_alpha-api", "github_beta-secret"
TRAVERSAL = [
    "../github_beta-secret",
    "..%2fgithub_beta-secret",
    "..",
    ".",
    "",
    "/etc",
    "a/b",
    "a\\b",
    "github_beta-secret\x00",
    "x..y",
]

GRAPH_TOOLS = ("find_symbol", "get_symbol", "find_callers", "find_callees",
               "query_graph")


def _build_graph(settings: Settings, slug: str, prefix: str) -> None:
    db = settings.repo_graph_path(slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = make_graph_store(db)
    try:
        store.add_symbols_batch([
            SymbolInfo(id=f"src/{prefix}.py::caller", name="caller",
                       kind="function", file=f"src/{prefix}.py", start_line=1,
                       language="python", is_exported=True),
            SymbolInfo(id="secrets/keys.py::secret_fn", name="secret_fn",
                       kind="function", file="secrets/keys.py", start_line=3,
                       language="python", is_exported=True),
        ])
        store.add_edges_batch([
            EdgeInfo(from_id=f"src/{prefix}.py::caller",
                     to_id="secrets/keys.py::secret_fn",
                     kind="CALLS", confidence="strong"),
        ])
        store.commit()
    finally:
        store.close()


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    root = tmp_path_factory.mktemp("celmis")
    settings = Settings(workspace_dir=root)
    _build_graph(settings, SLUG_A, "alpha")
    _build_graph(settings, SLUG_B, "beta")
    return root


@pytest.fixture
def env(graphs, monkeypatch):
    """Two tenants, one repo each, a caller of A, multi_tenant on."""
    from src.api.auto_review import AutoReviewStore, RepoConfig
    from src.config import get_settings
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("WORKSPACE_DIR", str(graphs))
    get_settings.cache_clear()

    store = AutoReviewStore(graphs / "secrets" / "auto_review.db")
    for ws, slug in ((WS_A, SLUG_A), (WS_B, SLUG_B)):
        if store.get_in_workspace(ws, slug) is None:
            store.upsert(RepoConfig(
                user_id=f"user-{ws}", repo_slug=slug, provider="github",
                full_name=slug.split("_", 1)[1].replace("-", "/", 1),
                url=f"https://github.com/{slug}", workspace_id=ws,
            ))
    monkeypatch.setattr("src.api.auto_review._default_store", store)

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()

    from src.mcp_server.identity import McpCaller
    state = {"caller": McpCaller("user-a", False, WS_A, ("read:graph",),
                                 authenticated=True)}
    monkeypatch.setattr("src.mcp_server.identity.resolve_caller",
                        lambda: state["caller"])

    # The research-access rules need Postgres; here every repo the binding
    # lets through is fully readable unless a test says otherwise.
    from src.access import RepoAccessDecision
    state["decide"] = RepoAccessDecision.full
    monkeypatch.setattr(
        "src.access.resolve_access",
        lambda *, user_id, is_admin, workspace_id, repos:  # noqa: ARG005
            {r: state["decide"](r) for r in repos},
    )
    monkeypatch.setattr(
        "src.access.effective.effective_access",
        lambda principal, workspace_id, repos=None:  # noqa: ARG005
            {r: state["decide"](r) for r in (repos or [])},
    )
    yield state
    monkeypatch.delenv("CELMIS_DEPLOYMENT_MODE")
    reset_mode_cache()
    get_settings.cache_clear()


@pytest.fixture
def mcp_tools():
    from src.mcp_server import build_server

    mcp = build_server()
    return {name: t.fn for name, t in mcp._tool_manager._tools.items()}


def _call(tools, name: str, slug: str):
    fn = tools[name]
    if name == "find_symbol":
        return fn(name="secret_fn", repo_slug=slug)
    if name == "get_symbol":
        return fn(symbol_id="secrets/keys.py::secret_fn", repo_slug=slug)
    if name in ("find_callers", "find_callees"):
        sym = ("secrets/keys.py::secret_fn" if name == "find_callers"
               else f"src/{'alpha' if slug == SLUG_A else 'beta'}.py::caller")
        return fn(symbol_id=sym, repo_slug=slug)
    return fn(cypher="MATCH (s:Symbol) RETURN s.name AS name", repo_slug=slug)


def _found_something(name: str, out) -> bool:  # noqa: ANN001
    if name == "find_symbol":
        return out["count"] > 0
    if name == "get_symbol":
        return out is not None
    if name == "find_callers":
        return bool(out.get("callers"))
    if name == "find_callees":
        return bool(out.get("callees"))
    return bool(out.get("ok")) and bool(out.get("rows"))


# ─── the path helper ────────────────────────────────────────────────


@pytest.mark.parametrize("slug", TRAVERSAL)
def test_repo_data_path_refuses_anything_but_one_segment(slug, tmp_path):
    settings = Settings(workspace_dir=tmp_path)
    for helper in (settings.repo_data_path, settings.repo_graph_path,
                   settings.repo_path, settings.repo_vault_path):
        with pytest.raises(InvalidRepoSlug):
            helper(slug)


def test_an_absolute_path_is_not_a_slug(tmp_path):
    settings = Settings(workspace_dir=tmp_path)
    with pytest.raises(InvalidRepoSlug):
        settings.repo_data_path(str(tmp_path / "data" / SLUG_B))


@pytest.mark.parametrize("slug", [
    "github_owner-repo", "gitlab_group-sub-name", "acme-frontend",
    "github_my.org-repo.js", "bitbucket_A_B-c_d",
])
def test_real_slugs_still_resolve_under_data_dir(slug, tmp_path):
    settings = Settings(workspace_dir=tmp_path)
    assert settings.repo_data_path(slug).parent == settings.data_dir


# ─── cross-tenant ───────────────────────────────────────────────────


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_caller_reads_its_own_tenants_graph(env, mcp_tools, tool):
    assert _found_something(tool, _call(mcp_tools, tool, SLUG_A))


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_caller_cannot_read_another_tenants_graph(env, mcp_tools, tool):
    out = _call(mcp_tools, tool, SLUG_B)
    assert not _found_something(tool, out), (
        f"{tool}: workspace A read workspace B's graph"
    )


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_foreign_reads_exactly_like_missing(env, mcp_tools, tool):
    """Not an existence oracle: a foreign slug and an unknown one answer alike."""
    foreign = _call(mcp_tools, tool, SLUG_B)
    missing = _call(mcp_tools, tool, "github_nobody-nothing")
    if isinstance(foreign, dict):
        for d in (foreign, missing):
            for k in ("repo", "repo_slug"):
                d.pop(k, None)
    assert foreign == missing


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
@pytest.mark.parametrize("slug", ["../" + SLUG_B, "..%2f" + SLUG_B, "/etc",
                                  SLUG_B + "\x00", ".."])
def test_traversal_slugs_find_nothing(env, mcp_tools, tool, slug):
    assert not _found_something(tool, _call(mcp_tools, tool, slug))


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_caller_with_no_workspace_reads_nothing(env, mcp_tools, tool):
    from src.mcp_server.identity import McpCaller

    env["caller"] = McpCaller("client:x", False, "default", ("read:graph",),
                              authenticated=True, workspace_resolved=False)
    assert not _found_something(tool, _call(mcp_tools, tool, SLUG_A))


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_global_admin_is_still_held_to_the_token_workspace(env, mcp_tools, tool):
    from src.mcp_server.identity import McpCaller

    env["caller"] = McpCaller("root", True, WS_A, (), authenticated=True)
    assert not _found_something(tool, _call(mcp_tools, tool, SLUG_B))
    assert _found_something(tool, _call(mcp_tools, tool, SLUG_A))


def test_a_research_rule_denying_the_repo_hides_it(env, mcp_tools):
    from src.access import RepoAccessDecision

    env["decide"] = RepoAccessDecision.denied
    for tool in GRAPH_TOOLS:
        assert not _found_something(tool, _call(mcp_tools, tool, SLUG_A)), tool


def test_deny_globs_filter_symbols_and_refuse_raw_cypher(env, mcp_tools):
    from src.access.resolver import RepoAccessDecision, _RuleView

    def restricted(slug):
        return RepoAccessDecision(
            repo_slug=slug, visibility="code", open_default=False,
            rules=(_RuleView("code", (), ("secrets/**",), ()),),
            deny_globs=("secrets/**",),
        )

    env["decide"] = restricted
    assert mcp_tools["find_symbol"](name="secret_fn", repo_slug=SLUG_A)["count"] == 0
    assert mcp_tools["get_symbol"](
        symbol_id="secrets/keys.py::secret_fn", repo_slug=SLUG_A) is None
    callees = mcp_tools["find_callees"](
        symbol_id="src/alpha.py::caller", repo_slug=SLUG_A)["callees"]
    assert all(not c["file"].startswith("secrets/") for c in callees)
    # Raw Cypher cannot be filtered by path, so a restricted repo refuses it.
    out = mcp_tools["query_graph"](
        cypher="MATCH (s:Symbol) RETURN s.file AS f", repo_slug=SLUG_A)
    assert out["ok"] is False and not out["rows"]
    # The non-secret symbol is still there.
    assert mcp_tools["find_symbol"](name="caller", repo_slug=SLUG_A)["count"] == 1


def test_list_repos_lists_only_the_callers_repos(env, mcp_tools, monkeypatch, graphs):
    from src.config import get_settings

    for slug in (SLUG_A, SLUG_B):
        (get_settings().repos_dir / slug / ".git").mkdir(parents=True, exist_ok=True)
    slugs = {r["slug"] for r in mcp_tools["list_repos"]()["repos"]}
    assert SLUG_B not in slugs
    assert SLUG_A in slugs


# ─── single_tenant is unchanged ─────────────────────────────────────


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_single_tenant_still_reads_every_graph(env, mcp_tools, tool, monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
    reset_mode_cache()
    assert _found_something(tool, _call(mcp_tools, tool, SLUG_B))
    assert _found_something(tool, _call(mcp_tools, tool, SLUG_A))


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_single_tenant_still_refuses_traversal(env, mcp_tools, tool, monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
    reset_mode_cache()
    assert not _found_something(tool, _call(mcp_tools, tool, "../" + SLUG_B))


def test_pure_tools_refuse_traversal_without_raising(env):
    from src.mcp_server import tools

    bad = "../" + SLUG_B
    assert tools.find_symbol("secret_fn", bad) == []
    assert tools.get_symbol("secrets/keys.py::secret_fn", bad) is None
    assert tools.find_callers("secret_fn", bad)["callers"] == []
    assert tools.find_callees("secret_fn", bad)["callees"] == []
    assert tools.query_graph("MATCH (n) RETURN n", repo_slug=bad)["ok"] is False
    assert tools.query_graph("MATCH (n) RETURN n",
                             group_name="../../data/x")["ok"] is False


# ─── groups ─────────────────────────────────────────────────────────


@pytest.fixture
def groups(env, monkeypatch):
    import src.groups.manager as gm
    from src.groups.manager import GroupManager

    monkeypatch.setattr(GroupManager, "_project_groups", lambda self, ws=None: [])
    monkeypatch.setattr(gm, "_default_manager", None)
    mgr = gm.get_group_manager()
    for ws, name, slug in ((WS_A, "alpha-g", SLUG_A), (WS_B, "beta-g", SLUG_B)):
        if not any(g.name == name for _p, g in mgr.iter_groups(ws)):
            group = mgr.create(name, workspace_id=ws)
            group.repos = [f"github:{slug.split('_', 1)[1].replace('-', '/', 1)}"]
            mgr.save(group)
        group = mgr.load(name, ws)
        path = mgr.graph_path(group)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            store = make_graph_store(path)
            try:
                store.add_symbols_batch([SymbolInfo(
                    id=f"{name}::x", name="x", kind="function", file="x.py",
                    start_line=1, language="python", is_exported=True)])
                store.commit()
            finally:
                store.close()
    return mgr


def test_group_tools_are_one_tenants(groups, mcp_tools):
    q = "MATCH (s:Symbol) RETURN s.name AS name"
    assert mcp_tools["query_graph"](cypher=q, group_name="alpha-g")["ok"] is True
    foreign = mcp_tools["query_graph"](cypher=q, group_name="beta-g")
    missing = mcp_tools["query_graph"](cypher=q, group_name="nobody-g")
    assert foreign == missing and foreign["ok"] is False

    names = {g["name"] for g in mcp_tools["list_groups"]()["groups"]}
    assert names == {"alpha-g"}
    assert mcp_tools["list_repos"](group_name="beta-g")["repos"] == []
    assert mcp_tools["cross_repo_edges"](group_name="beta-g")["edges"] == []


def test_raw_group_reads_refuse_a_member_with_path_restrictions(groups, mcp_tools, env):
    """Group graph ids and `file` carry member-repo paths that cannot be
    filtered by a member's deny globs, so raw Cypher and the raw edge list
    need every member unrestricted — as repo-scoped query_graph does."""
    from src.access.resolver import RepoAccessDecision, _RuleView

    q = "MATCH (s:Symbol) RETURN s.id AS id, s.file AS f"
    assert mcp_tools["query_graph"](cypher=q, group_name="alpha-g")["ok"] is True

    env["decide"] = lambda slug: RepoAccessDecision(
        repo_slug=slug, visibility="code", open_default=False,
        rules=(_RuleView("code", (), ("secrets/**",), ()),),
        deny_globs=("secrets/**",),
    )
    out = mcp_tools["query_graph"](cypher=q, group_name="alpha-g")
    missing = mcp_tools["query_graph"](cypher=q, group_name="nobody-g")
    assert out == missing and out["ok"] is False and not out["rows"]
    assert mcp_tools["cross_repo_edges"](group_name="alpha-g")["edges"] == []
    # The group itself is still the caller's: it stays listed.
    assert {g["name"] for g in mcp_tools["list_groups"]()["groups"]} == {"alpha-g"}
