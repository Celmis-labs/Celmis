"""REST and MCP side doors onto per-repo data keyed by slug alone.

The companion of test_mcp_graph_belongs_to_one_tenant.py. Each of these read
something stored per slug — a clone, a snapshot, a review run, a deprecation
row — and checked something other than whose repository it was:

  * ``require_repo_permission`` consulted team grants (looked up across every
    workspace) and let a global admin through outright, so
    ``/api/intel/{ownership,architecture,reverse-index}/{repo_slug}`` answered
    for any tenant's repository;
  * ``GET /api/review-policies/{repo_slug:path}/branches`` ran ``git`` in
    ``repos_dir / repo_slug`` for any string — any tenant's clone, or any
    directory ``../`` could reach;
  * the deprecation consumer scan walked every tenant's graph and wrote the
    results onto one tenant's row;
  * MCP ``caller_access`` decided research rules without asking whether the
    repository was the caller's tenant's at all, and ``list_deprecations`` /
    ``get_review`` read across every tenant.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from src.users import User

WS_A, WS_B = "ws-alpha", "ws-beta"


# Test-side shim: render JSONB as sqlite JSON (real DDL comes from Alembic).
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"
SLUG_A, SLUG_B = "github_alpha-api", "github_beta-secret"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from src.api.auto_review import AutoReviewStore, RepoConfig
    from src.config import get_settings

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    store = AutoReviewStore(tmp_path / "secrets" / "auto_review.db")
    for ws, slug, full in ((WS_A, SLUG_A, "alpha/api"),
                           (WS_B, SLUG_B, "beta/secret")):
        store.upsert(RepoConfig(
            user_id=f"user-{ws}", repo_slug=slug, provider="github",
            full_name=full, url=f"https://github.com/{full}", workspace_id=ws,
        ))
    monkeypatch.setattr("src.api.auto_review._default_store", store)
    yield store
    get_settings.cache_clear()


@pytest.fixture
def multi_tenant(monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()
    yield
    monkeypatch.delenv("CELMIS_DEPLOYMENT_MODE")
    reset_mode_cache()


@pytest.fixture
def single_tenant(monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
    reset_mode_cache()
    yield
    reset_mode_cache()


# ─── require_repo_permission: the tenant binding ─────────────────────


def _guarded_app(user: User, ws: str) -> TestClient:
    from src.api.deps import current_workspace_id, get_current_user, require_repo_permission

    app = FastAPI()

    @app.get("/r/{repo_slug:path}")
    async def _route(repo_slug: str,
                     _p: User = Depends(require_repo_permission("read"))):
        return {"ok": repo_slug}

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: ws
    return TestClient(app)


def test_a_global_admin_cannot_reach_another_tenants_repo(registry, multi_tenant):
    client = _guarded_app(User(id="root", email="r@x", is_admin=True), WS_A)

    assert client.get(f"/r/{SLUG_B}").status_code == 404
    assert client.get(f"/r/{SLUG_A}").status_code == 200
    # either spelling of the caller's own repository
    assert client.get("/r/alpha/api").status_code == 200


def test_foreign_and_unknown_answer_alike(registry, multi_tenant):
    client = _guarded_app(User(id="root", email="r@x", is_admin=True), WS_A)
    foreign = client.get(f"/r/{SLUG_B}")
    missing = client.get("/r/github_nobody-nothing")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_single_tenant_keeps_the_old_permission_rules(registry, single_tenant):
    client = _guarded_app(User(id="root", email="r@x", is_admin=True), WS_A)

    assert client.get(f"/r/{SLUG_B}").status_code == 200


# ─── branches route ─────────────────────────────────────────────────


def _git_clone_at(path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)


async def test_branches_only_for_the_callers_own_repo(registry, tmp_path):
    from src.api.routers.review_policies import list_branches
    from src.config import get_settings

    _git_clone_at(get_settings().repo_path(SLUG_A), "alpha-main")
    _git_clone_at(get_settings().repo_path(SLUG_B), "beta-secret-branch")
    user = User(id="user-a", email="a@x")

    own = await list_branches(SLUG_A, user=user, ws_id=WS_A)
    foreign = await list_branches(SLUG_B, user=user, ws_id=WS_A)

    assert "alpha-main" in own.branches
    assert foreign.branches == [] and foreign.default_branch is None


@pytest.mark.parametrize("slug", [f"../repos/{SLUG_B}", f"x/../../repos/{SLUG_B}",
                                  "..", "/etc"])
async def test_branches_refuses_traversal(registry, slug):
    from src.api.routers.review_policies import list_branches

    out = await list_branches(slug, user=User(id="user-a", email="a@x"), ws_id=WS_A)
    assert out.branches == []


# ─── an invalid slug anywhere in the API is a 404 ───────────────────


def test_an_invalid_slug_deep_in_a_handler_answers_404():
    from src.api.main import _install_validation_redaction
    from src.config import get_settings

    app = FastAPI()
    _install_validation_redaction(app)

    @app.get("/v/{slug:path}")
    def _route(slug: str):
        return {"p": str(get_settings().repo_vault_path(slug))}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v/../../etc").status_code in (404,)
    assert client.get("/v/a%2F..%2F..%2Fetc").status_code == 404
    assert client.get("/v/github_ok-repo").status_code == 200


# ─── deprecation scan ───────────────────────────────────────────────


def test_deprecation_scan_reads_only_the_rows_workspace(registry, multi_tenant,
                                                        monkeypatch):
    from src.api.routers import intel
    from src.mcp_server import tools

    monkeypatch.setattr(tools, "list_repos", lambda *a, **k: [
        tools.RepoSummary(slug=s, full_path=s, indexed=True, graph_path=None,
                          symbol_count=1) for s in (SLUG_A, SLUG_B)])
    scanned: list[str] = []

    def fake_callers(symbol_id, repo_slug, **_k):
        scanned.append(repo_slug)
        return {"callers": [{"name": "c", "file": "f.py", "start_line": 1}]}

    monkeypatch.setattr(tools, "find_callers", fake_callers)
    out = intel._scan_consumers("sym", workspace_id=WS_A)

    assert scanned == [SLUG_A]
    assert {c["repo_slug"] for c in out} == {SLUG_A}


# ─── MCP caller_access ──────────────────────────────────────────────


@pytest.fixture
def mcp_caller(monkeypatch):
    from src.access import RepoAccessDecision
    from src.mcp_server.identity import McpCaller

    state = {"caller": McpCaller("user-a", False, WS_A, (), authenticated=True)}
    monkeypatch.setattr("src.mcp_server.identity.resolve_caller",
                        lambda: state["caller"])
    monkeypatch.setattr(
        "src.access.resolve_access",
        lambda *, user_id, is_admin, workspace_id, repos:  # noqa: ARG005
            {r: RepoAccessDecision.full(r) for r in repos},
    )
    return state


def test_caller_access_denies_another_tenants_repo(registry, multi_tenant, mcp_caller):
    from src.mcp_server.identity import McpCaller, caller_access

    for caller in (McpCaller("user-a", False, WS_A, (), authenticated=True),
                   McpCaller("root", True, WS_A, (), authenticated=True)):
        mcp_caller["caller"] = caller
        _c, access = caller_access([SLUG_A, SLUG_B, "../" + SLUG_B])
        assert access[SLUG_A].researchable
        assert not access[SLUG_B].researchable
        assert not access["../" + SLUG_B].researchable


def test_caller_access_single_tenant_is_unchanged(registry, single_tenant, mcp_caller):
    from src.mcp_server.identity import caller_access

    _c, access = caller_access([SLUG_A, SLUG_B])
    assert access[SLUG_A].researchable and access[SLUG_B].researchable


def test_mcp_list_deprecations_is_one_tenants(registry, multi_tenant, mcp_caller,
                                              tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.models import Base, DeprecatedSymbol
    from src.mcp_server import http_app, tools

    engine = create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(engine, tables=[DeprecatedSymbol.__table__])
    with Session(engine) as s:
        for i, (ws, slug) in enumerate(((WS_A, SLUG_A), (WS_B, SLUG_B))):
            s.add(DeprecatedSymbol(id=str(i), workspace_id=ws, repo_slug=slug,
                                   symbol=f"sym{i}", reason="", consumers=[]))
        s.commit()
    monkeypatch.setattr(http_app, "_sync_engine", lambda: engine)

    m = FastMCP("t")
    http_app._register_tools(m, tools)
    fn = m._tool_manager._tools["list_deprecations"].fn

    assert {d["repo_slug"] for d in fn()["deprecations"]} == {SLUG_A}
    assert fn(repo_slug=SLUG_B)["deprecations"] == []


def test_mcp_get_review_searches_only_the_callers_runs(registry, multi_tenant,
                                                       mcp_caller, tmp_path,
                                                       monkeypatch):
    import sqlite3

    from src.mcp_server import http_app

    db = tmp_path / "runs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE review_runs (id TEXT, pr_ref TEXT, pr_repo TEXT, "
            "status TEXT, verdict TEXT, tokens_input INT, tokens_output INT, "
            "cost_usd REAL, started_at TEXT, findings_json TEXT, "
            "workspace_id TEXT)")
        conn.execute(
            "INSERT INTO review_runs VALUES ('b1', 'github:beta/secret#7', "
            f"'{SLUG_B}', 'done', 'fail', 1, 1, 0, '2026-10-02', "
            "'[{\"file\": \"keys.py\", \"title\": \"leak\"}]', ?)", (WS_B,))

    class _Store:
        db_path = db

    monkeypatch.setattr("src.api.review_runs.get_review_run_store", lambda: _Store())
    out = http_app._get_review_impl("beta/secret#7")

    assert "findings" not in out
    assert out == {"error": "no review found for 'beta/secret#7'"}


def test_http_errors_are_not_raised_for_an_unbound_caller(registry, multi_tenant,
                                                          mcp_caller):
    """A token with no resolvable workspace owns nothing — denied, not 500."""
    from src.mcp_server.identity import McpCaller, caller_access

    mcp_caller["caller"] = McpCaller("client:x", False, "default", (),
                                     authenticated=True, workspace_resolved=False)
    try:
        _c, access = caller_access([SLUG_A])
    except HTTPException:  # pragma: no cover
        pytest.fail("caller_access raised")
    assert not access[SLUG_A].researchable


# ─── branches: the user-keyed fallback is not a membership ──────────


async def test_branches_ignore_a_row_left_in_a_workspace_the_user_left(
        registry, multi_tenant):
    """user-b registered SLUG_B in WS_B, then was moved to WS_A (removing a
    member deletes the membership, not their registration rows)."""
    from src.api.routers.review_policies import list_branches
    from src.config import get_settings

    _git_clone_at(get_settings().repo_path(SLUG_B), "beta-secret-branch")
    former = User(id=f"user-{WS_B}", email="b@x")

    out = await list_branches(SLUG_B, user=former, ws_id=WS_A)
    assert out.branches == [] and out.default_branch is None


async def test_branches_single_tenant_keeps_the_user_fallback(registry,
                                                              single_tenant):
    from src.api.routers.review_policies import list_branches
    from src.config import get_settings

    _git_clone_at(get_settings().repo_path(SLUG_B), "beta-main")
    out = await list_branches(SLUG_B, user=User(id=f"user-{WS_B}", email="b@x"),
                              ws_id=WS_A)
    assert "beta-main" in out.branches


# ─── a slug that is not one path segment is never stored ────────────


BAD_REPO_URLS = ["github:acme/foo..bar", "https://github.com/acme/foo%20bar",
                 "gitlab:acme/a+b"]


@pytest.mark.parametrize("url", BAD_REPO_URLS)
def test_registering_an_unaddressable_slug_is_refused_before_storing(
        registry, url):
    from src.api.routers.repos import add_repo
    from src.api.schemas import RepoAddRequest

    before = {c.repo_slug for c in registry.list_for_workspace(WS_A)}
    with pytest.raises(HTTPException) as exc:
        add_repo(None, RepoAddRequest(url=url, index=False),
                 user=User(id="user-a", email="a@x"), workspace_id=WS_A)
    assert exc.value.status_code == 422
    assert {c.repo_slug for c in registry.list_for_workspace(WS_A)} == before


@pytest.mark.parametrize("url", BAD_REPO_URLS)
def test_automation_refuses_an_unaddressable_slug_before_storing(registry, url):
    from src.automation.actions import ActionError, Actor, register_repo

    before = {c.repo_slug for c in registry.list_for_workspace(WS_A)}
    with pytest.raises(ActionError):
        register_repo(Actor("user-a", "a@x", WS_A), url, index=False)
    assert {c.repo_slug for c in registry.list_for_workspace(WS_A)} == before


@pytest.fixture
def legacy_bad_row(registry):
    """A row stored before slugs were validated."""
    from src.api.auto_review import RepoConfig

    registry.upsert(RepoConfig(
        user_id="user-a", repo_slug="github_alpha-foo..bar", provider="github",
        full_name="alpha/foo..bar", url="https://github.com/alpha/foo..bar",
        workspace_id=WS_A,
    ))
    return "github_alpha-foo..bar"


def test_one_bad_stored_slug_does_not_take_down_the_repo_list(legacy_bad_row):
    from src.api.routers.repos import list_repos

    out = list_repos(user=User(id="user-a", email="a@x"), workspace_id=WS_A)
    by_slug = {r.slug: r for r in out}
    assert SLUG_A in by_slug
    assert by_slug[legacy_bad_row].indexed is False


def test_one_bad_stored_slug_does_not_break_automation_listing(legacy_bad_row):
    from src.automation.actions import Actor, list_repos

    out = list_repos(Actor("user-a", "a@x", WS_A))
    assert {r["repo"] for r in out["repos"]} >= {SLUG_A, legacy_bad_row}


# ─── MCP project tools: a foreign project reads like a missing one ──


@pytest.fixture
def projects(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.models import Base, Project, ProjectRepo
    from src.mcp_server import http_app

    engine = create_engine(f"sqlite:///{tmp_path/'p.db'}")
    Base.metadata.create_all(engine, tables=[Project.__table__,
                                             ProjectRepo.__table__])
    ids = {}
    with Session(engine) as s:
        for ws, slug in ((WS_A, SLUG_A), (WS_B, SLUG_B)):
            p = Project(workspace_id=ws, name=ws, description="")
            s.add(p)
            s.flush()
            s.add(ProjectRepo(project_id=p.id, repo_slug=slug))
            ids[ws] = str(p.id)
        s.commit()
    monkeypatch.setattr(http_app, "_sync_engine", lambda: engine)
    return ids


MISSING_PROJECT = "00000000-0000-0000-0000-000000000000"


def test_project_tools_answer_a_foreign_project_like_a_missing_one(
        registry, multi_tenant, mcp_caller, projects):
    from src.mcp_server import http_app

    assert http_app._project_repo_slugs(projects[WS_A]) == [SLUG_A]
    assert http_app._project_repo_slugs(projects[WS_B]) == []

    for impl, kw in ((http_app._search_symbols_impl,
                      dict(query="x", kind=None, limit=5)),
                     (http_app._find_consumers_impl, dict(symbol="x"))):
        foreign = impl(projects[WS_B], **kw)
        missing = impl(MISSING_PROJECT, **kw)
        assert SLUG_B not in str(foreign), "another tenant's repo was named"
        assert foreign.keys() == missing.keys()
        assert "blocked_repos" not in foreign


def test_project_tools_single_tenant_unchanged(registry, single_tenant,
                                               mcp_caller, projects):
    from src.mcp_server import http_app

    assert http_app._project_repo_slugs(projects[WS_B]) == [SLUG_B]


# ─── MCP bootstrap_client: ownership only for a researchable target ──


@pytest.fixture
def snapshots(monkeypatch):
    seen: list[str] = []

    def fake_load(slug):
        seen.append(slug)
        return {"stats": {"top_owners": [{"identity": f"dev@{slug}",
                                          "commits": 9}]}}

    monkeypatch.setattr("src.ownership.builder.load_snapshot", fake_load)
    return seen


def _bootstrap(project_id: str, target: str) -> dict:
    from src.mcp_server import http_app

    return http_app._bootstrap_client_impl(
        project_id=project_id, target_repo_slug=target,
        target_endpoint=None, language="python")


def test_bootstrap_client_does_not_hand_out_another_tenants_owners(
        registry, multi_tenant, mcp_caller, projects, snapshots):
    foreign = _bootstrap(MISSING_PROJECT, SLUG_B)
    missing = _bootstrap(MISSING_PROJECT, "github_nobody-nothing")

    assert foreign["top_owners"] == [] == missing["top_owners"]
    assert SLUG_B not in snapshots, "the foreign snapshot was read at all"
    assert foreign.keys() == missing.keys()
    # the caller's own target still gets its owners
    assert _bootstrap(projects[WS_A], SLUG_A)["top_owners"] == [
        {"identity": f"dev@{SLUG_A}", "commits": 9}]


def test_bootstrap_client_respects_a_research_denial(registry, multi_tenant,
                                                     mcp_caller, projects,
                                                     snapshots, monkeypatch):
    from src.access import RepoAccessDecision

    monkeypatch.setattr(
        "src.access.resolve_access",
        lambda *, user_id, is_admin, workspace_id, repos:  # noqa: ARG005
            {r: RepoAccessDecision.denied(r) for r in repos},
    )
    assert _bootstrap(projects[WS_A], SLUG_A)["top_owners"] == []
    assert snapshots == []


def test_bootstrap_client_single_tenant_unchanged(registry, single_tenant,
                                                  mcp_caller, projects,
                                                  snapshots):
    assert _bootstrap(projects[WS_B], SLUG_B)["top_owners"] != []
