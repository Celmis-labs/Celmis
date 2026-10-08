"""While an outside client holds a live project MCP token, the project's
repository set and file scope are the superadmin's alone; the file scope is
always the superadmin's. A revoked or expired token freezes nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.api.rbac_world import A_REPO, world

OTHERS = ["gadmin", "owner_a", "admin_a", "editor_a", "member_a"]


def _routers():
    from src.api.routers import project_mcp_tokens, projects

    return (projects.router, project_mcp_tokens.router)


@pytest.fixture
async def pw(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        yield w


async def _project(w):
    r = await w.client.post("/api/projects", json={"name": "P", "repos": [{"repo_slug": A_REPO}]},
                            headers=w.h("su", "ws-a"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _token(w, pid):
    r = await w.client.post(f"/api/projects/{pid}/mcp-tokens", json={}, headers=w.h("su", "ws-a"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add(w, pid, who, **extra):
    return await w.client.post(f"/api/projects/{pid}/repos", json={"repo_slug": A_REPO, **extra},
                               headers=w.h(who, "ws-a"))


async def _remove(w, pid, who):
    return await w.client.delete(f"/api/projects/{pid}/repos/{A_REPO}", headers=w.h(who, "ws-a"))


@pytest.mark.parametrize("who", OTHERS)
async def test_only_the_superadmin_patches_the_file_scope(pw, who):
    pid = await _project(pw)
    url = f"/api/projects/{pid}/repos/{A_REPO}"
    r = await pw.client.patch(url, json={"exclude_globs": ["x/**"]}, headers=pw.h(who, "ws-a"))
    assert r.status_code == 403
    r = await pw.client.patch(url, json={"exclude_globs": ["x/**"]}, headers=pw.h("su", "ws-a"))
    assert r.status_code == 200


@pytest.mark.parametrize("who", OTHERS)
async def test_globs_on_add_are_refused_for_anyone_but_the_superadmin(pw, who):
    pid = await _project(pw)
    assert (await _add(pw, pid, who, exclude_globs=["x/**"])).status_code == 403


@pytest.mark.parametrize("who", OTHERS)
async def test_add_and_remove_are_refused_while_a_token_is_live(pw, who):
    pid = await _project(pw)
    await _token(pw, pid)
    for r in (await _add(pw, pid, who), await _remove(pw, pid, who)):
        assert r.status_code == 403 and "active MCP token" in r.json()["detail"]


@pytest.mark.parametrize("who", OTHERS)
async def test_a_revoked_token_freezes_nothing(pw, who):
    pid = await _project(pw)
    tid = await _token(pw, pid)
    d = await pw.client.delete(f"/api/projects/{pid}/mcp-tokens/{tid}", headers=pw.h("su", "ws-a"))
    assert d.status_code == 204
    assert (await _add(pw, pid, who)).status_code != 403
    assert (await _remove(pw, pid, who)).status_code != 403


@pytest.mark.parametrize("who", OTHERS)
async def test_an_expired_token_freezes_nothing(pw, who, tmp_path):
    from src.db.models import McpProjectToken

    pid = await _project(pw)
    tid = await _token(pw, pid)
    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    with Session(engine) as s:
        s.get(McpProjectToken, tid).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    engine.dispose()
    assert (await _add(pw, pid, who)).status_code != 403
    assert (await _remove(pw, pid, who)).status_code != 403


async def test_the_superadmin_may_change_the_repos_of_a_project_with_a_live_token(pw):
    pid = await _project(pw)
    await _token(pw, pid)
    assert (await _remove(pw, pid, "su")).status_code == 204
    assert (await _add(pw, pid, "su", exclude_globs=["x/**"])).status_code == 201
