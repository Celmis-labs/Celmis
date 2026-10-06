"""Under single_tenant the MCP graph tools answer through the access rules.

Production runs single_tenant. There, ``tenancy.authorize_repo`` returned a
full decision for every valid slug, so the research-access rules that Q&A,
search and the docs apply were skipped by ``find_symbol``, ``get_symbol``,
``find_callers``, ``find_callees``, ``query_graph``, ``list_repos`` and the
group tools: a neighbouring team limited to "Metadata only", or denied
``secrets/**``, read symbol names, signatures, files and call edges — and ran
arbitrary Cypher — by asking MCP instead.

The cast, all in one workspace of a single_tenant install:

  * ``neighbour`` — visibility ``metadata``, deny ``secrets/**``;
  * ``coder``     — visibility ``code``, deny ``secrets/**``;
  * ``full``      — visibility ``code``, no globs;
  * ``outsider``  — no team; a rule exists for the repository → ``none``;
  * an unruled repository, which everybody keeps reading in full;
  * and no bearer identity at all (stdio), which keeps full access.

Then the refusal guard: with a refused caller, every tool on both servers
fails with the reason, in-process, without the HTTP verifier in front.

The resolver is the real one over a SQLite file; the graphs are real.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest
from sqlalchemy import create_engine, event

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite
from src.config import Settings
from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store

WS = "ws-one"
RULED, UNRULED = "github_acme-core", "github_acme-open"
SECRET_ID = "secrets/keys.py::load_key"
SECRET_TWIN = "secrets/keys.py::load"
OPEN_TWIN = "src/app.py::load"
HANDLER = "src/app.py::handler"
HELPER = "src/util.py::helper"
GRAPH_TOOLS = ("find_symbol", "get_symbol", "find_callers", "find_callees",
               "query_graph")


def _sym(sid: str) -> SymbolInfo:
    file, name = sid.split("::")
    return SymbolInfo(id=sid, name=name, kind="function", file=file, start_line=1,
                      language="python", is_exported=True,
                      signature=f"def {name}()")


def _build(settings: Settings, slug: str) -> None:
    db = settings.repo_graph_path(slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = make_graph_store(db)
    try:
        store.add_symbols_batch([_sym(s) for s in
                                 (SECRET_ID, SECRET_TWIN, OPEN_TWIN, HANDLER, HELPER)])
        store.add_edges_batch([
            EdgeInfo(from_id=HANDLER, to_id=SECRET_ID, kind="CALLS", confidence="strong"),
            EdgeInfo(from_id=HANDLER, to_id=HELPER, kind="CALLS", confidence="strong"),
            EdgeInfo(from_id=SECRET_ID, to_id=HELPER, kind="CALLS", confidence="strong"),
            EdgeInfo(from_id=SECRET_TWIN, to_id=HELPER, kind="CALLS", confidence="strong"),
            EdgeInfo(from_id=OPEN_TWIN, to_id=HANDLER, kind="CALLS", confidence="strong"),
        ])
        store.commit()
    finally:
        store.close()


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    root = tmp_path_factory.mktemp("celmis")
    settings = Settings(workspace_dir=root)
    for slug in (RULED, UNRULED):
        _build(settings, slug)
        (settings.repos_dir / slug / ".git").mkdir(parents=True, exist_ok=True)
    return root


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)


@pytest.fixture
def env(graphs, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session

    from src.access import resolver
    from src.config import get_settings
    from src.db.models import Base, RepoAccessRule, Team, TeamMember
    from src.deployment import reset_mode_cache
    from src.mcp_server.identity import McpCaller

    monkeypatch.setenv("WORKSPACE_DIR", str(graphs))
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
    get_settings.cache_clear()
    reset_mode_cache()

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    with Session(engine) as s:
        for team, user, vis, deny in (
            ("t-neigh", "neighbour", "metadata", ["secrets/**"]),
            ("t-code", "coder", "code", ["secrets/**"]),
            ("t-full", "full", "code", []),
        ):
            s.add(Team(id=team, name=team, workspace_id=WS))
            s.add(TeamMember(team_id=team, user_id=user))
            s.add(RepoAccessRule(id=f"r-{team}", workspace_id=WS, team_id=team,
                                 repo_slug=RULED, visibility=vis, deny_globs=deny))
        s.commit()

    # Only repositories registered in the caller's workspace exist for the
    # resolver, so the two repos are registered the way the app does it.
    from src.api.auto_review import AutoReviewStore, RepoConfig
    store = AutoReviewStore(graphs / "secrets" / "auto_review.db")
    for slug in (RULED, UNRULED):
        if store.get_in_workspace(WS, slug) is None:
            store.upsert(RepoConfig(
                user_id="owner", repo_slug=slug, provider="github",
                full_name=slug.split("_", 1)[1].replace("-", "/", 1),
                url=f"https://github.com/{slug}", workspace_id=WS))
    monkeypatch.setattr("src.api.auto_review._default_store", store)

    state = {"caller": None}

    def as_(user: str | None, **kw):
        state["caller"] = (None if user is None else
                           McpCaller(user, False, WS, ("read:graph", "read:groups"),
                                     authenticated=True, **kw))

    import src.mcp_server.identity as identity
    real = identity.resolve_caller
    monkeypatch.setattr(identity, "resolve_caller",
                        lambda: state["caller"] if state["caller"] else real())
    yield as_
    engine.dispose()
    get_settings.cache_clear()
    monkeypatch.delenv("CELMIS_DEPLOYMENT_MODE")
    reset_mode_cache()


@pytest.fixture
def mcp_tools():
    from src.mcp_server import build_server

    mcp = build_server()
    return {name: t.fn for name, t in mcp._tool_manager._tools.items()}


def _call(tools, name: str, slug: str, sid: str = SECRET_ID):
    fn = tools[name]
    if name == "find_symbol":
        return fn(name=sid.split("::")[1], repo_slug=slug)
    if name == "get_symbol":
        return fn(symbol_id=sid, repo_slug=slug)
    if name == "find_callers":
        return fn(symbol_id=HELPER, repo_slug=slug)
    if name == "find_callees":
        return fn(symbol_id=sid, repo_slug=slug)
    return fn(cypher="MATCH (s:Symbol) RETURN s.name AS name, s.file AS file",
              repo_slug=slug)


def _text(out) -> str:  # noqa: ANN001
    return repr(out)


def _payload(tool: str, out) -> str:  # noqa: ANN001
    """What the tool answered — without the echo of what it was asked."""
    if out is None:
        return ""
    if tool == "find_symbol":
        return repr(out["matches"])
    if tool == "get_symbol":
        return repr(out)
    if tool in ("find_callers", "find_callees"):
        return repr([out.get("callers"), out.get("callees"), out.get("resolved_ids")])
    return repr(out.get("rows"))


# ─── visibility none ────────────────────────────────────────────────


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_team_without_a_rule_reads_nothing(env, mcp_tools, tool):
    env("outsider")
    out = _payload(tool, _call(mcp_tools, tool, RULED, HANDLER))
    assert "handler" not in out and "load_key" not in out and "helper" not in out


def test_list_repos_hides_a_repository_the_rules_hide(env, mcp_tools):
    env("outsider")
    slugs = {r["slug"] for r in mcp_tools["list_repos"]()["repos"]}
    assert slugs == set()   # the unruled one is closed too, not just the ruled one
    env("neighbour")
    # Metadata is research access: the repository is listed, its code is not.
    slugs = {r["slug"] for r in mcp_tools["list_repos"]()["repos"]}
    assert slugs == {RULED}


# ─── metadata ───────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_metadata_only_gets_no_symbol_sources(env, mcp_tools, tool):
    env("neighbour")
    out = _payload(tool, _call(mcp_tools, tool, RULED, HANDLER))
    for leak in ("handler", "helper", "load_key", "src/app.py", "def "):
        assert leak not in out, f"{tool} leaked {leak!r} at metadata level"


# ─── deny globs at code level ───────────────────────────────────────


def test_denied_symbols_are_not_found_by_name_or_id(env, mcp_tools):
    env("coder")
    assert mcp_tools["find_symbol"](name="load_key", repo_slug=RULED)["count"] == 0
    assert mcp_tools["get_symbol"](symbol_id=SECRET_ID, repo_slug=RULED) is None
    assert mcp_tools["find_symbol"](name="handler", repo_slug=RULED)["count"] == 1


def test_callers_drop_rows_in_denied_files(env, mcp_tools):
    env("coder")
    out = mcp_tools["find_callers"](symbol_id=HELPER, repo_slug=RULED, depth=1)
    files = {r["file"] for r in out["callers"]}
    assert files == {"src/app.py"}
    assert "secrets/" not in _text(out)


def test_the_edges_of_a_denied_symbol_are_not_walked(env, mcp_tools):
    """Callees of a concealed function describe its body."""
    env("coder")
    out = mcp_tools["find_callees"](symbol_id=SECRET_ID, repo_slug=RULED)
    assert out["callees"] == [] and "secrets/" not in _text(out)
    out = mcp_tools["find_callers"](symbol_id=SECRET_ID, repo_slug=RULED)
    assert out["callers"] == []


def test_a_bare_name_does_not_smuggle_a_denied_twin(env, mcp_tools):
    """`load` resolves to src/app.py::load AND secrets/keys.py::load. The walk
    merged both, so the denied twin's edges arrived under the visible name."""
    env("coder")
    out = mcp_tools["find_callees"](symbol_id="load", repo_slug=RULED, depth=1)
    assert out["resolved_ids"] == [OPEN_TWIN]
    assert {r["id"] for r in out["callees"]} == {HANDLER}   # not helper via the twin
    assert "secrets/" not in _text(out)


@pytest.mark.parametrize("who", ["coder", "neighbour", "outsider"])
def test_raw_cypher_is_refused_where_anything_is_concealed(env, mcp_tools, who):
    env(who)
    out = mcp_tools["query_graph"](
        cypher="MATCH (s:Symbol) RETURN s.name AS name", repo_slug=RULED)
    assert out.get("ok") is False and out.get("rows") == []


def test_raw_cypher_is_allowed_at_full_code_with_no_globs(env, mcp_tools):
    env("full")
    out = mcp_tools["query_graph"](
        cypher="MATCH (s:Symbol) RETURN s.name AS name", repo_slug=RULED)
    assert out.get("ok") is not False and len(out["rows"]) == 5


# ─── unchanged ──────────────────────────────────────────────────────


@pytest.fixture
def unruled_open(monkeypatch):
    """The operator's escape hatch: the pre-upgrade behaviour, single_tenant only."""
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_UNRULED_REPO_ACCESS", "open")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
@pytest.mark.parametrize("who", ["outsider", "coder"])
def test_an_unruled_repository_is_closed_to_a_member_by_default(env, mcp_tools, tool, who):
    env(who)
    out = _payload(tool, _call(mcp_tools, tool, UNRULED))
    assert "load_key" not in out and "helper" not in out and "handler" not in out, (
        tool, who, out)


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
@pytest.mark.parametrize("who", ["outsider", "coder", None])
def test_an_unruled_repository_is_readable_when_the_operator_opens_it(
        env, unruled_open, mcp_tools, tool, who):
    env(who)
    out = _payload(tool, _call(mcp_tools, tool, UNRULED))
    assert "load_key" in out or "helper" in out or "name" in out, (tool, who, out)


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_a_stdio_caller_with_no_identity_still_reads_an_unruled_repository(
        env, mcp_tools, tool):
    """The subprocess boundary is the trust boundary on stdio, as before."""
    env(None)
    out = _payload(tool, _call(mcp_tools, tool, UNRULED))
    assert "load_key" in out or "helper" in out or "name" in out, (tool, out)


@pytest.mark.parametrize("tool", GRAPH_TOOLS)
def test_no_bearer_identity_keeps_full_access(env, mcp_tools, tool):
    """stdio: the subprocess boundary is the trust boundary, as before."""
    env(None)
    out = _payload(tool, _call(mcp_tools, tool, RULED))
    assert "load_key" in out or "helper" in out or "name" in out, (tool, out)


# ─── the refusal guard, in-process ──────────────────────────────────


def _servers(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    from src.mcp_server import build_server
    from src.mcp_server.http_app import _build_mcp

    return {"stdio": build_server(), "http": _build_mcp()}


def test_every_tool_on_both_servers_is_guarded(env, monkeypatch):
    from src.mcp_server.guard import is_guarded

    for label, mcp in _servers(monkeypatch).items():
        tools = mcp._tool_manager._tools
        assert len(tools) > 5, label
        bare = [n for n, t in tools.items() if not is_guarded(t.fn)]
        assert not bare, f"{label}: unguarded tools {bare}"


def test_a_refused_caller_is_refused_by_every_tool(env, monkeypatch):
    """No HTTP verifier here: the tool bodies are called directly, the way
    stdio, tests and any future transport reach them."""
    from mcp.server.fastmcp.exceptions import ToolError

    env("coder", refused="no longer a member of ws-one")
    servers = _servers(monkeypatch)
    answered: list[str] = []
    for label, mcp in servers.items():
        for name, tool in mcp._tool_manager._tools.items():
            try:
                result = tool.fn()   # the guard runs before any argument is bound
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            except ToolError as exc:
                if "no longer a member" in str(exc):
                    continue
            answered.append(f"{label}:{name}")
    assert not answered, f"tools that did not refuse: {answered}"

    # …and through the SDK's own entry point, with valid arguments.
    for label, tool_name, args in (
        ("stdio", "find_symbol", {"name": "handler", "repo_slug": RULED}),
        ("http", "get_my_access", {"repo_slug": RULED}),
    ):
        tool = servers[label]._tool_manager._tools[tool_name]
        with pytest.raises(ToolError, match="no longer a member"):
            asyncio.run(tool.run(args))


def test_a_caller_that_is_not_refused_passes_the_guard(env, mcp_tools):
    env("full")
    assert mcp_tools["find_symbol"](name="handler", repo_slug=RULED)["count"] == 1
