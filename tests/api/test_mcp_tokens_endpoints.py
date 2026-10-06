"""MCP tokens over HTTP: who may issue, what they get back, and what a holder sees.

The routes under test (`src/api/routers/mcp_tokens.py`) are the superadmin's
tool for handing one person a token for an explicit list of repositories. The
role tests are the point: every route refuses everybody who is not the
superadmin (a global admin included, unless the operator widened it), and a
holder can see and revoke only their own tokens.

The world is `tests/api/rbac_world.py`: real routers, real resolver, SQLite.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event

from tests.api.rbac_world import A_REPO, _sqlite_booleans, world

ALL_ROLES = ["su", "gadmin", "owner_a", "admin_a", "editor_a", "member_a", "viewer_a",
             "loner"]
NOT_SUPERADMIN = [r for r in ALL_ROLES if r != "su"]
HOLDER = "member-a@acme-corp.io"             # a member of workspace A


def _routers():
    from src.api.routers import mcp_access, mcp_tokens

    return (mcp_tokens.router, mcp_tokens.me_router, mcp_access.router)


@pytest.fixture
async def tokens(tmp_path, monkeypatch):
    from src.access import resolver
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    for name in ("CELMIS_MCP_TOKEN_ISSUERS", "CELMIS_MCP_SELF_SERVICE",
                 "CELMIS_MCP_TOKEN_MAX_DAYS"):
        monkeypatch.delenv(name, raising=False)
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        token_store.invalidate()
        w.engine = engine
        try:
            yield w
        finally:
            engine.dispose()
            token_store.invalidate()
            get_settings.cache_clear()


def _issue_body(**over):
    body = {"user_ref": HOLDER, "workspace_id": "ws-a", "repos": [A_REPO],
            "label": "laptop"}
    body.update(over)
    return body


async def _issue(w, who="su", **over):
    return await w.client.post("/api/admin/mcp-tokens", json=_issue_body(**over),
                               headers=w.h(who, "ws-a"))


def _env(monkeypatch, **env):
    from src.config import get_settings

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# ─── role and scope: every issuer route refuses everybody else ───────


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_issues_a_token(tokens, who):
    r = await _issue(tokens, who)
    assert r.status_code == 403, (who, r.text)
    assert "token" not in r.json() and "mcp_json" not in r.json()


async def test_the_superadmin_issues_a_token(tokens):
    r = await _issue(tokens, "su")
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_issues_an_oauth_grant(tokens, who):
    r = await tokens.client.post(
        "/api/admin/mcp-grants",
        json={"user_ref": HOLDER, "workspace_id": "ws-a", "repos": [A_REPO]},
        headers=tokens.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.text)


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_lists_tokens(tokens, who):
    r = await tokens.client.get("/api/admin/mcp-tokens", headers=tokens.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.text)


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_sees_the_repo_picker(tokens, who):
    r = await tokens.client.get("/api/admin/mcp-repos", params={"workspace": "ws-a"},
                                headers=tokens.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.text)


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_reads_the_call_log(tokens, who):
    r = await tokens.client.get("/api/admin/mcp-calls", headers=tokens.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.text)


@pytest.mark.parametrize("who", NOT_SUPERADMIN)
async def test_only_the_superadmin_changes_or_revokes_a_token(tokens, who):
    issued = (await _issue(tokens)).json()
    h = tokens.h(who, "ws-a")
    patch = await tokens.client.patch(f"/api/admin/mcp-tokens/{issued['id']}",
                                      json={"repos": ["*"]}, headers=h)
    revoke = await tokens.client.post(f"/api/admin/mcp-tokens/{issued['id']}/revoke",
                                      headers=h)
    assert patch.status_code == 403 and revoke.status_code == 403, (who, patch.text)
    listed = (await tokens.client.get("/api/admin/mcp-tokens",
                                      headers=tokens.h("su", "ws-a"))).json()
    [row] = [t for t in listed if t["id"] == issued["id"]]
    assert row["status"] == "active" and row["repos"] == [A_REPO]


async def test_the_operator_can_widen_issuing_to_platform_admins(tokens, monkeypatch):
    assert (await _issue(tokens, "gadmin")).status_code == 403
    _env(monkeypatch, CELMIS_MCP_TOKEN_ISSUERS="platform_admin")
    assert (await _issue(tokens, "gadmin")).status_code == 201
    assert (await _issue(tokens, "admin_a")).status_code == 403, (
        "a workspace admin is not a platform admin")


# ─── what issuing returns ────────────────────────────────────────────


async def test_the_token_is_returned_once_and_never_listed(tokens):
    issued = (await _issue(tokens)).json()
    secret = issued["token"]
    assert secret.count(".") == 2, "a signed token"
    listed = await tokens.client.get("/api/admin/mcp-tokens", headers=tokens.h("su", "ws-a"))
    mine = await tokens.client.get("/api/mcp/tokens/me", headers=tokens.h("member_a", "ws-a"))
    for r in (listed, mine):
        assert secret not in r.text
        assert "token" not in {k for t in (r.json()["tokens"] if isinstance(r.json(), dict)
                                           else r.json()) for k in t}


async def test_the_snippet_reads_the_token_from_the_environment(tokens):
    issued = (await _issue(tokens)).json()
    assert issued["token"] not in str(issued["mcp_json"])
    server = issued["mcp_json"]["mcpServers"]["celmis"]
    assert server["headers"]["Authorization"] == "Bearer ${CELMIS_MCP_TOKEN}"
    assert server["url"].endswith("/mcp/dev/"), "the dev endpoint, trailing slash included"
    assert issued["url"] == server["url"]


async def test_a_token_is_read_only_unless_asked(tokens):
    plain = (await _issue(tokens)).json()
    assert plain["allow_write"] is False and plain["profile"] == "dev"
    r = await _issue(tokens, allow_write=True, profile="full")
    assert r.status_code == 201 and r.json()["allow_write"] is True
    assert r.json()["url"].endswith("/mcp/") and not r.json()["url"].endswith("/dev/")


async def test_the_dev_profile_cannot_write(tokens):
    r = await _issue(tokens, allow_write=True, profile="dev")
    assert r.status_code == 422, r.text


async def test_the_expiry_is_capped_by_the_operator(tokens, monkeypatch):
    from datetime import UTC, datetime, timedelta

    _env(monkeypatch, CELMIS_MCP_TOKEN_MAX_DAYS="10")
    issued = (await _issue(tokens, expires_in_days=365)).json()
    expires = datetime.fromisoformat(issued["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert expires <= datetime.now(UTC) + timedelta(days=10, minutes=1)
    assert expires >= datetime.now(UTC) + timedelta(days=9)


@pytest.mark.parametrize("repos", [[], [""], ["a b"], ["../etc"], ["x" * 300]])
async def test_a_bad_repo_list_is_refused(tokens, repos):
    r = await _issue(tokens, repos=repos)
    assert r.status_code in (422,), (repos, r.text)


async def test_a_person_outside_the_workspace_gets_no_token(tokens):
    r = await _issue(tokens, user_ref="loner@acme-corp.io")
    assert r.status_code == 422, r.text
    assert "member" in r.json()["detail"]


async def test_an_unknown_person_or_workspace_is_a_404(tokens):
    assert (await _issue(tokens, user_ref="nobody@acme-corp.io")).status_code == 404
    assert (await _issue(tokens, workspace_id="no-such-ws")).status_code == 404


async def test_the_list_says_which_registered_repos_each_entry_reaches(tokens):
    await _issue(tokens, repos=["github_aco-*", "github_zzz-*"])
    [row] = (await tokens.client.get("/api/admin/mcp-tokens",
                                     headers=tokens.h("su", "ws-a"))).json()
    assert row["matched_repos"] == [A_REPO]
    assert row["user_email"] == HOLDER and row["kind"] == "pat"


async def test_the_picker_lists_the_workspaces_own_repositories_only(tokens):
    r = await tokens.client.get("/api/admin/mcp-repos", params={"workspace": "ws-a"},
                                headers=tokens.h("su", "ws-a"))
    assert r.status_code == 200
    assert [x["slug"] for x in r.json()] == [A_REPO]
    assert (await tokens.client.get("/api/admin/mcp-repos", params={"workspace": "nope"},
                                    headers=tokens.h("su", "ws-a"))).status_code == 404


async def test_every_change_is_audited_without_the_token(tokens):
    issued = (await _issue(tokens)).json()
    await tokens.client.patch(f"/api/admin/mcp-tokens/{issued['id']}",
                              json={"expires_in_days": 5}, headers=tokens.h("su", "ws-a"))
    await tokens.client.post(f"/api/admin/mcp-tokens/{issued['id']}/revoke",
                             headers=tokens.h("su", "ws-a"))
    actions = [a["action"] for a in tokens.audit]
    assert actions == ["mcp.token_issued", "mcp.token_updated", "mcp.token_revoked"]
    assert issued["token"] not in str(tokens.audit)


async def test_a_grant_for_oauth_returns_no_token_value(tokens):
    r = await tokens.client.post(
        "/api/admin/mcp-grants",
        json={"user_ref": HOLDER, "workspace_id": "ws-a", "repos": [A_REPO]},
        headers=tokens.h("su", "ws-a"))
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "oauth_grant" and "token" not in r.json()
    assert (await tokens.client.post(
        "/api/admin/mcp-grants",
        json={"user_ref": "loner@acme-corp.io", "workspace_id": "ws-a", "repos": ["*"]},
        headers=tokens.h("su", "ws-a"))).status_code == 422


# ─── the call log ────────────────────────────────────────────────────


def _log(w, **kw):
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from src.db.models import McpCallLog

    row = dict(ts=datetime.now(UTC), workspace_id="wsid-a", user_id="u-member_a",
               token_id="t1", kind="pat", client_id="", tool="find_symbol", profile="dev",
               repos=[A_REPO], status="ok", result_bytes=10, result_items=1,
               duration_ms=3, args_hash="abc")
    row.update(kw)
    with Session(w.engine) as s:
        s.add(McpCallLog(**row))
        s.commit()


async def test_the_call_log_filters_and_exports(tokens):
    _log(tokens, tool="find_symbol")
    _log(tokens, tool="=HYPERLINK(1)", status="denied")
    h = tokens.h("su", "ws-a")
    everything = (await tokens.client.get("/api/admin/mcp-calls", headers=h)).json()
    assert len(everything) == 2
    denied = (await tokens.client.get("/api/admin/mcp-calls", params={"status": "denied"},
                                      headers=h)).json()
    assert [c["tool"] for c in denied] == ["=HYPERLINK(1)"]
    only = (await tokens.client.get("/api/admin/mcp-calls", params={"tool": "find_symbol"},
                                    headers=h)).json()
    assert len(only) == 1 and "args_hash" not in only[0]
    csv_ = await tokens.client.get("/api/admin/mcp-calls", params={"format": "csv"},
                                   headers=h)
    assert csv_.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK(1)" in csv_.text, "a spreadsheet would run the cell as a formula"


# ─── a holder's own tokens ───────────────────────────────────────────


async def test_a_holder_sees_only_their_own_tokens_as_metadata(tokens):
    mine = (await _issue(tokens, label="mine")).json()
    await _issue(tokens, user_ref="editor-a@acme-corp.io", label="theirs")
    r = await tokens.client.get("/api/mcp/tokens/me", headers=tokens.h("member_a", "ws-a"))
    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body["tokens"]] == [mine["id"]]
    assert body["self_service_enabled"] is False


async def test_a_holder_revokes_their_own_token(tokens):
    mine = (await _issue(tokens)).json()
    r = await tokens.client.post(f"/api/mcp/tokens/{mine['id']}/revoke",
                                 headers=tokens.h("member_a", "ws-a"))
    assert r.status_code == 200 and r.json()["status"] == "revoked"


async def test_somebody_elses_token_answers_like_a_missing_one(tokens):
    theirs = (await _issue(tokens, user_ref="editor-a@acme-corp.io")).json()
    h = tokens.h("member_a", "ws-a")
    other = await tokens.client.post(f"/api/mcp/tokens/{theirs['id']}/revoke", headers=h)
    missing = await tokens.client.post("/api/mcp/tokens/no-such-id/revoke", headers=h)
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())
    assert other.status_code == 404
    still = (await tokens.client.get("/api/admin/mcp-tokens",
                                     headers=tokens.h("su", "ws-a"))).json()
    assert [t["status"] for t in still if t["id"] == theirs["id"]] == ["active"]


# ─── self-service ────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["owner_a", "member_a", "viewer_a", "su"])
async def test_self_service_is_off_by_default(tokens, who):
    r = await tokens.client.post("/api/mcp/token", headers=tokens.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.text)
    assert "administrator" in r.json()["detail"]


async def test_a_self_issued_token_is_a_self_kind_capped_by_its_holder(tokens, monkeypatch):
    _env(monkeypatch, CELMIS_MCP_SELF_SERVICE="on")
    r = await tokens.client.post("/api/mcp/token", headers=tokens.h("member_a", "ws-a"))
    assert r.status_code == 200, r.text
    mine = (await tokens.client.get("/api/mcp/tokens/me",
                                    headers=tokens.h("member_a", "ws-a"))).json()
    [row] = mine["tokens"]
    assert row["kind"] == "self" and row["repos"] == ["*"]
    assert mine["self_service_enabled"] is True
    # and it is the one write path a click can never widen
    r = await tokens.client.post("/api/mcp/token", json={"scopes": ["write:config"]},
                                 headers=tokens.h("member_a", "ws-a"))
    assert r.status_code == 403, "a member may not ask for a configuration scope"
