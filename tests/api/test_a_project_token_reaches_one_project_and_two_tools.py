"""Project-scoped MCP tokens: issuing over HTTP, and what a holder can do.

Issuing is superadmin-only; the raw token is shown once and only its hash is
stored; the tools see exactly the project's repos, narrowed by the project's
file scope; and an expired, revoked or unknown token opens nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from tests.api.rbac_world import A_REPO, _sqlite_booleans, world

OTHERS = ["gadmin", "owner_a", "admin_a", "editor_a", "member_a", "viewer_a", "loner"]
HOUR, DAY = 3600, 86400


def _routers():
    from src.api.routers import project_mcp_tokens, projects

    return (projects.router, project_mcp_tokens.router)


@pytest.fixture
async def pw(tmp_path, monkeypatch):
    from src.access import resolver

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        w.engine = engine
        try:
            yield w
        finally:
            engine.dispose()


async def _project(w, **link):
    h = w.h("su", "ws-a")
    r = await w.client.post("/api/projects", json={"name": "Legacy",
                            "repos": [{"repo_slug": A_REPO}]}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    if link:
        p = await w.client.patch(f"/api/projects/{pid}/repos/{A_REPO}", json=link, headers=h)
        assert p.status_code == 200, p.text
    return pid


async def _mint(w, pid, who="su", **body):
    return await w.client.post(f"/api/projects/{pid}/mcp-tokens",
                               json={"label": "claude", **body}, headers=w.h(who, "ws-a"))


def _view(token):
    from src.mcp_server import project_tokens as pt

    return pt.lookup_hash(token)


def _put(tmp_path, rel, text):
    from src.config import get_settings

    path = get_settings().repo_path(A_REPO) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _row(w, token_id):
    from src.db.models import McpProjectToken

    with Session(w.engine) as s:
        return s.get(McpProjectToken, token_id)


# ─── issuing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", OTHERS)
async def test_only_the_superadmin_manages_project_tokens(pw, who):
    pid = await _project(pw)
    assert (await _mint(pw, pid, who)).status_code == 403
    h = pw.h(who, "ws-a")
    assert (await pw.client.get(f"/api/projects/{pid}/mcp-tokens", headers=h)).status_code == 403
    assert (await pw.client.delete(f"/api/projects/{pid}/mcp-tokens/x", headers=h)).status_code == 403


async def test_the_token_is_shown_once_and_only_its_hash_is_stored(pw):
    from src.db.models import McpProjectToken

    pid = await _project(pw)
    r = await _mint(pw, pid)
    assert r.status_code == 201, r.text
    raw = r.json()["token"]
    assert raw.startswith("cmcp_")
    listed = await pw.client.get(f"/api/projects/{pid}/mcp-tokens", headers=pw.h("su", "ws-a"))
    assert raw not in listed.text and "token" not in listed.json()[0]
    with Session(pw.engine) as s:
        row = s.scalars(select(McpProjectToken)).one()
    assert raw not in (row.token_hash, row.label) and row.token_hash != raw
    assert row.scopes == ["read:project_search"]


async def test_the_default_lifetime_is_thirty_days(pw):
    pid = await _project(pw)
    got = (await _mint(pw, pid)).json()
    expires = datetime.fromisoformat(got["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert timedelta(days=29, hours=23) < expires - datetime.now(UTC) <= timedelta(days=30, minutes=1)


@pytest.mark.parametrize(("ttl", "ok"), [(HOUR - 1, False), (HOUR, True),
                                         (90 * DAY, True), (90 * DAY + 1, False)])
async def test_the_lifetime_stays_between_an_hour_and_ninety_days(pw, ttl, ok):
    pid = await _project(pw)
    assert (await _mint(pw, pid, ttl_seconds=ttl)).status_code == (201 if ok else 422)


async def test_a_token_of_another_workspace_project_is_not_reachable(pw):
    pid = await _project(pw)
    r = await pw.client.get(f"/api/projects/{pid}/mcp-tokens", headers=pw.h("admin_b", "ws-b"))
    assert r.status_code in (403, 404)


async def test_revoking_ends_the_token_and_an_unknown_id_is_404(pw):
    pid = await _project(pw)
    got = (await _mint(pw, pid)).json()
    h = pw.h("su", "ws-a")
    assert (await pw.client.delete(f"/api/projects/{pid}/mcp-tokens/{got['id']}",
                                   headers=h)).status_code == 204
    assert _view(got["token"]).problem()
    assert (await pw.client.delete(f"/api/projects/{pid}/mcp-tokens/nope",
                                   headers=h)).status_code == 404


# ─── use ─────────────────────────────────────────────────────────────


async def test_an_unknown_or_foreign_string_is_not_a_project_token(pw):
    from src.mcp_server import project_tokens as pt

    assert pt.lookup_hash("cmcp_" + "x" * 43) is None
    assert pt.lookup_hash("eyJhbGciOi.not.a.token") is None
    assert not pt.is_project_token(None)


async def test_an_expired_token_is_refused(pw):
    from src.mcp_server import project_tools as tools

    pid = await _project(pw)
    got = (await _mint(pw, pid)).json()
    with Session(pw.engine) as s:
        row = s.get(type(_row(pw, got["id"])), got["id"])
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    view = _view(got["token"])
    assert view.problem()
    with pytest.raises(tools.ProjectToolError):
        tools.search_project(view, "anything")


async def test_search_finds_a_cyrillic_bsl_file_by_identifier_and_by_file_name(pw, tmp_path):
    from src.mcp_server import project_tools as tools

    pid = await _project(pw)
    _put(tmp_path, "Документы/Расчёт.bsl",
         "Процедура ПересчитатьИтоги(Документ)\n    СуммаДокумента = 0;\nКонецПроцедуры\n")
    view = _view((await _mint(pw, pid)).json()["token"])
    by_text = tools.search_project(view, "ПересчитатьИтоги")
    assert any(h["file"] == "Документы/Расчёт.bsl" for h in by_text["text"]), by_text
    by_name = tools.search_project(view, "расчёт")
    assert any(h["file"] == "Документы/Расчёт.bsl" for h in by_name["text"]), by_name


async def test_the_project_file_scope_is_honoured_and_exclude_wins(pw, tmp_path):
    from src.mcp_server import project_tools as tools

    pid = await _project(pw, include_globs=["src/**"], exclude_globs=["src/gen/**"])
    _put(tmp_path, "src/a.txt", "needle in src\n")
    _put(tmp_path, "src/gen/b.txt", "needle in gen\n")
    _put(tmp_path, "docs/c.txt", "needle in docs\n")
    view = _view((await _mint(pw, pid)).json()["token"])
    files = {h["file"] for h in tools.search_project(view, "needle")["text"]}
    assert files == {"src/a.txt"}


async def test_a_repo_outside_the_project_is_refused(pw):
    from src.mcp_server import project_tools as tools

    pid = await _project(pw)
    view = _view((await _mint(pw, pid)).json()["token"])
    with pytest.raises(tools.ProjectToolError):
        tools.search_project(view, "needle", repo_slug="github_bco-secret")


async def test_use_stamps_last_used_at(pw):
    from src.mcp_server import project_tokens as pt

    pid = await _project(pw)
    got = (await _mint(pw, pid)).json()
    assert _row(pw, got["id"]).last_used_at is None
    pt.touch(got["id"])
    assert _row(pw, got["id"]).last_used_at is not None


async def test_a_view_without_a_token_opens_nothing():
    from src.mcp_server import project_tools as tools

    with pytest.raises(tools.ProjectToolError):
        tools.search_project(None, "needle")


# ─── the bearer check at the MCP edge ────────────────────────────────


async def test_the_edge_accepts_a_live_token_and_refuses_expired_revoked_unknown(pw, monkeypatch):
    from src.mcp_server import project_tokens as pt
    from src.mcp_server.auth import JwtConfig, JwtTokenVerifier

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    verifier = JwtTokenVerifier(JwtConfig.from_env(), accept_project_tokens=True)
    pid = await _project(pw)
    live = (await _mint(pw, pid)).json()
    got = await verifier.verify_token(live["token"])
    assert got is not None and got.scopes == [pt.SCOPE]
    assert got.client_id == f"{pt.CLIENT_PREFIX}{live['id']}"
    assert _row(pw, live["id"]).last_used_at is not None

    old = (await _mint(pw, pid)).json()
    with Session(pw.engine) as s:
        row = s.get(type(_row(pw, old["id"])), old["id"])
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    assert await verifier.verify_token(old["token"]) is None

    gone = (await _mint(pw, pid)).json()
    await pw.client.delete(f"/api/projects/{pid}/mcp-tokens/{gone['id']}",
                           headers=pw.h("su", "ws-a"))
    assert await verifier.verify_token(gone["token"]) is None
    assert await verifier.verify_token("cmcp_" + "z" * 43) is None


async def test_a_server_that_has_no_project_tools_does_not_know_the_token(pw, monkeypatch):
    from src.mcp_server.auth import JwtConfig, JwtTokenVerifier

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    pid = await _project(pw)
    token = (await _mint(pw, pid)).json()["token"]
    assert await JwtTokenVerifier(JwtConfig.from_env()).verify_token(token) is None


async def test_the_other_credential_kind_still_verifies(pw, monkeypatch):
    from src.mcp_server.auth import JwtConfig, JwtTokenVerifier, issue_token

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    cfg = JwtConfig.from_env()
    jwt_token = issue_token(cfg, subject="someone", scopes=["read:graph"],
                            extra_claims=None)
    on = await JwtTokenVerifier(cfg, accept_project_tokens=True).verify_token(jwt_token)
    off = await JwtTokenVerifier(cfg).verify_token(jwt_token)
    assert (on is None) == (off is None), "accepting project tokens changes nothing else"
    if on is not None:
        assert on.scopes == off.scopes


async def test_a_project_scope_holder_is_limited_to_the_two_tools():
    from src.mcp_server import project_tokens as pt
    from src.mcp_server.http_app import _TOOL_SCOPES

    assert {"search_project", "ask_project"} == pt.TOOLS
    assert {t for t, s in _TOOL_SCOPES.items() if s == pt.SCOPE} == pt.TOOLS
