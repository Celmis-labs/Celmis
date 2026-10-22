"""The receiving workspace's audit trail does not tell its administrators what another
workspace shared with it.

Every step of a share leaves a row somewhere: the token it was put on, the calls
made through it, the owner narrowing or ending it. The trails are read by different
people, and the receiver's administrators are exactly the ones who must not learn
that the repository exists:

  * the receiver's own rows (`mcp.token_issued`, `mcp.call`) carry a NUMBER of shared
    repositories and never an id, a slug or a name; the mirror of a call holds no
    `shares` at all;
  * the `sharing.*` rows are written into the OWNER's workspace, whatever workspace
    the person who caused them was working in;
  * the call log (`/api/admin/mcp-calls`) is the one page that names a share, and it
    is read by whoever issues tokens, which is the platform side.

World: `tests/api/sharing_world.py` with the audit router mounted and the real
action-audit writer put back (the world replaces it with a spy).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.api.audit_support import operations, page
from tests.api.rbac_world import A_REPO, B_REPO, B_REPO_FULL, B_SECRET
from tests.api.sharing_world import (
    HOLDER,
    HOLDER_EMAIL,
    WS_A,
    WS_B,
    add_share,
    real_audit_trail,
    shared_world,
)

PATHS = ("/api/audit?mode=action&limit=500", "/api/audit/export", "/api/audit/facets",
         "/api/audit/stats")
RECEIVERS_ADMINS = ("owner_a", "admin_a", "admin2_a")


def _needles(share_id: str) -> list[str]:
    """Everything that would give the shared repository away, as it is written."""
    return [B_REPO, B_REPO_FULL, B_REPO_FULL.lower(), B_SECRET, B_SECRET.lower(), share_id,
            "sharing.", "Bravo", WS_B]


async def _routers():
    from src.api.routers import audit as audit_router

    return (audit_router.router,)


def _log_call(w, share_id: str) -> None:
    """One call through the share, written the way the MCP server writes it: the call
    log row and the mirror to the action audit."""
    from src.mcp_server import audit as mcp_audit

    mcp_audit.write_row({
        "ts": datetime.now(UTC), "workspace_id": WS_A, "user_id": w.uid(HOLDER),
        "token_id": "tok-1", "kind": "pat", "client_id": "", "tool": "read_symbol",
        "profile": "dev", "repos": [A_REPO], "packs": [], "shares": [share_id],
        "status": "ok", "result_bytes": 1234, "result_items": 7, "duration_ms": 9,
        "args_hash": "abc123"})


async def _story(w) -> str:
    """The share is put on a token, used, narrowed and ended, and the token is
    revoked. Returns the share's id."""
    share_id = add_share(w)
    r = await w.client.post("/api/admin/mcp-tokens", headers=w.h("su", "ws-a"), json={
        "user_ref": HOLDER_EMAIL, "workspace_id": "ws-a", "label": "laptop",
        "repos": [A_REPO], "shares": [share_id]})
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]
    _log_call(w, share_id)
    r = await w.client.patch(f"/api/sharing/shares/{share_id}", headers=w.h("admin_b", "ws-b"),
                             json={"add_deny": ["secrets/**"]})
    assert r.status_code == 200, r.text
    r = await w.client.post(f"/api/sharing/shares/{share_id}/revoke",
                            headers=w.h("admin_b", "ws-b"))
    assert r.status_code == 200, r.text
    r = await w.client.post(f"/api/admin/mcp-tokens/{token_id}/revoke", headers=w.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    return share_id


@pytest.fixture
async def aw(tmp_path, monkeypatch):
    async with shared_world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit_trail(monkeypatch)
        yield w


def _rows(text: str) -> list[dict]:
    return [r for r in json.loads(text)["records"]]


# ─── the receiver's administrators ───────────────────────────────────


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", RECEIVERS_ADMINS)
async def test_the_audit_page_of_the_receiving_workspace_names_nothing_of_a_shared_repository(
        aw, who, path):
    share_id = await _story(aw)
    r = await page(aw, who, path)
    assert r.status_code == 200, r.text
    text = r.text.lower()
    for needle in _needles(share_id):
        assert needle.lower() not in text, (who, path, needle)


async def test_the_receiving_workspace_still_reads_its_own_rows_of_the_story(aw):
    """The check above would pass on an empty page: these are the rows it must keep."""
    await _story(aw)
    ops = await operations(aw, "admin_a")
    assert {"mcp.token_issued", "mcp.call", "mcp.token_revoked"} <= set(ops)
    assert not [o for o in ops if o.startswith("sharing.")]


async def test_the_token_row_of_the_receiver_holds_a_number_and_the_mirror_of_a_call_holds_no_share(
        aw):
    await _story(aw)
    rows = _rows((await page(aw, "admin_a")).text)
    (issued,) = [r for r in rows if r["operation"] == "mcp.token_issued"]
    assert issued["extra"]["shared_repos"] == 1
    assert issued["extra"]["repos"] == [A_REPO]
    (call,) = [r for r in rows if r["operation"] == "mcp.call"]
    assert "shares" not in call["extra"] and "packs" not in call["extra"]
    assert call["extra"]["repos"] == [A_REPO], "the slug of the shared repository is not there"


async def test_the_audit_filter_by_repository_finds_no_row_of_the_shared_one(aw):
    """Asking the page for the shared slug is asking it to confirm the repository."""
    share_id = await _story(aw)
    for who in RECEIVERS_ADMINS:
        r = await page(aw, who, f"/api/audit?repo={B_REPO}&limit=500")
        assert r.status_code == 200 and _rows(r.text) == [], who
        facets = (await page(aw, who, "/api/audit/facets")).json()
        assert B_REPO not in facets["repos"] and share_id not in str(facets)


# ─── the owner ───────────────────────────────────────────────────────


async def test_the_owner_reads_what_happened_to_its_share_in_its_own_trail(aw):
    share_id = await _story(aw)
    r = await page(aw, "admin_b", ws="ws-b")
    assert r.status_code == 200, r.text
    rows = [x for x in _rows(r.text) if str(x["operation"]).startswith("sharing.")]
    assert {x["operation"] for x in rows} == {
        "sharing.token_attached", "sharing.narrowed", "sharing.revoked",
        "sharing.token_detached"}
    assert {x["target"] for x in rows} == {share_id}
    assert {x["workspace_id"] for x in rows} == {WS_B}


async def test_the_owner_reads_nothing_of_the_receivers_own_rows(aw):
    """The owner learns who holds its share from the share, not from the receiver's trail."""
    await _story(aw)
    ops = await operations(aw, "admin_b", ws="ws-b")
    assert "mcp.token_issued" not in ops and "mcp.call" not in ops


# ─── the platform side ───────────────────────────────────────────────


async def test_the_superadmin_reads_both_trails(aw):
    share_id = await _story(aw)
    rows = _rows((await page(aw, "su", ws="ws-a")).text)
    by_op = {}
    for x in rows:
        by_op.setdefault(x["operation"], []).append(x)
    assert {"mcp.token_issued", "mcp.call", "sharing.token_attached", "sharing.narrowed",
            "sharing.revoked", "sharing.token_detached"} <= set(by_op)
    assert {x["workspace_id"] for x in by_op["sharing.revoked"]} == {WS_B}
    assert by_op["sharing.revoked"][0]["target"] == share_id


async def test_the_call_log_is_where_a_call_through_a_share_is_named_and_only_for_the_issuer(aw):
    share_id = await _story(aw)
    r = await aw.client.get("/api/admin/mcp-calls", headers=aw.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    (call,) = r.json()
    assert call["shares"] == [f"{B_REPO}@Bravo {B_SECRET}"]
    assert call["repos"] == [A_REPO] and share_id not in r.text
    for who in RECEIVERS_ADMINS:
        denied = await aw.client.get("/api/admin/mcp-calls", headers=aw.h(who, "ws-a"))
        assert denied.status_code == 403, who
        assert B_REPO not in denied.text and share_id not in denied.text
