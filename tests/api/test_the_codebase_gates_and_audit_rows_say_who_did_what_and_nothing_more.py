"""The two role gates in front of every Codebase route, and the audit trail behind
every change.

The contract (docs/CODEBASE.md, "Access"; docs/ROLES_AND_ACCESS.md):

  * the reader gate lets in a global admin and anyone with a role in the active
    workspace, and nobody else; the author gate (creating a pack) lets in a global
    admin and owner, admin or editor — each answers 403 in its own words;
  * only the two invite routes are open to any signed-in person, because an
    invite link names its own workspace; the three publish routes answer to a
    pack publish token and to nothing else (`tests/api/test_a_pack_token_...`);
    every other route sits behind the reader gate, and nobody unauthenticated
    gets in anywhere;
  * every change leaves one audit row naming the actor, the workspace and the
    pack's id, with counts and ids as detail — never a title, a description, a
    path, a note, a file's text, an email or a token.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import deps
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world

MEMBER = SimpleNamespace(id="u-1", email="x@acme-corp.io", is_admin=False)
ADMIN = SimpleNamespace(id="u-2", email="y@acme-corp.io", is_admin=True)


def _role(monkeypatch, role):
    monkeypatch.setattr(deps, "workspace_role", lambda user_id, workspace_id: role)


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "member", "viewer"])
async def test_any_role_in_the_workspace_passes_the_reader_gate(monkeypatch, role):
    _role(monkeypatch, role)
    assert await deps.require_codebase_reader(user=MEMBER, workspace_id="w") is MEMBER


@pytest.mark.parametrize("role", [None, "", "guest", "superuser", "Owner", "owner "])
async def test_no_role_or_an_unknown_one_fails_the_reader_gate_closed(monkeypatch, role):
    _role(monkeypatch, role)
    with pytest.raises(HTTPException) as caught:
        await deps.require_codebase_reader(user=MEMBER, workspace_id="w")
    assert (caught.value.status_code, caught.value.detail) == (403, "Requires workspace membership")


async def test_a_global_admin_needs_no_role_for_either_gate(monkeypatch):
    _role(monkeypatch, None)
    assert await deps.require_codebase_reader(user=ADMIN, workspace_id="w") is ADMIN
    assert await deps.require_pack_author(user=ADMIN, workspace_id="w") is ADMIN


@pytest.mark.parametrize("role,passes", [("owner", True), ("admin", True), ("editor", True),
                                         ("member", False), ("viewer", False), (None, False),
                                         ("guest", False)])
async def test_only_owner_admin_and_editor_pass_the_author_gate(monkeypatch, role, passes):
    _role(monkeypatch, role)
    if passes:
        assert await deps.require_pack_author(user=MEMBER, workspace_id="w") is MEMBER
        return
    with pytest.raises(HTTPException) as caught:
        await deps.require_pack_author(user=MEMBER, workspace_id="w")
    assert (caught.value.status_code, caught.value.detail) == (
        403, "Requires the editor role or higher to create packs")


def test_only_the_invite_and_publish_routes_skip_the_reader_gate_and_creating_asks_for_an_author():
    from src.api.routers.codebase import router

    seen = {}
    for route in router.routes:
        gates = {d.call.__name__ for d in route.dependant.dependencies if d.call}
        gate = next(g for g in gates if g.startswith("require_"))
        seen[(route.path, sorted(route.methods)[0])] = gate
    assert seen, "the router has no routes"
    for (path, method), gate in seen.items():
        if path.startswith("/api/codebase/invites/"):
            assert gate == "require_signed_in", (path, method)
        elif path.startswith("/api/codebase/publish"):
            assert gate == "require_pack_token", (path, method)
        elif (path, method) == ("/api/codebase/packs", "POST"):
            assert gate == "require_pack_author", (path, method)
        else:
            assert gate == "require_codebase_reader", (path, method)


async def test_nobody_unknown_and_nobody_without_a_role_gets_anything(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        from tests.api.codebase_support import seeded
        ids = await seeded(w)
        routes = [("GET", "/api/codebase/packs"), ("GET", "/api/codebase/search?q=a"),
                  ("POST", "/api/codebase/brief"), ("GET", f"/api/codebase/packs/{ids['pw']}"),
                  ("POST", "/api/codebase/invites/preview"), ("POST", "/api/codebase/invites/redeem")]
        for method, path in routes:
            r = await w.client.request(method, path, json={"task": "a", "token": "t"},
                                       headers={"X-Test-User": "nobody"})     # not a known person
            assert r.status_code == 401, (method, path, r.status_code)
        # signed in but with no role in A: A's packs are not theirs, whatever header they send
        listed = await call(w, "loner", "GET", "/api/codebase/packs")
        assert [p["slug"] for p in listed.json().get("packs", [])] == []
        got = await call(w, "loner", "GET", f"/api/codebase/packs/{ids['pw']}")
        assert got.status_code in (403, 404)
        found = await call(w, "loner", "GET", "/api/codebase/search?q=quokka")
        assert found.status_code in (200, 403) and "quokka" not in found.text


async def test_each_change_leaves_one_row_of_ids_and_shapes_and_no_text(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        title, desc, note = "Ledgerwork secret title", "Ledgerwork secret description", "Ledgerwork secret note"
        r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
            "slug": "ledger", "title": title, "description": desc, "audience": "restricted",
            "teams": [{"team_id": w.ids["team_a"], "permission": "read"}]})
        pid = r.json()["id"]
        base = f"/api/codebase/packs/{pid}"
        await upload_zip(w, "editor_a", pid, note_zip("ledger"), note=note)
        await upload_zip(w, "editor_a", pid, note_zip("ledger"), note=note)
        await call(w, "editor_a", "POST", f"{base}/versions/1/activate")
        await call(w, "editor_a", "PATCH", base, json={"title": title + " 2", "description": desc + " 2"})
        await call(w, "editor_a", "PUT", f"{base}/teams", json={"teams": []})
        await call(w, "editor_a", "POST", f"{base}/members",
                   json={"email": "member-a@acme-corp.io", "permission": "read"})
        await call(w, "editor_a", "DELETE", f"{base}/members/u-member_a")
        inv = await call(w, "editor_a", "POST", f"{base}/invites", json={})
        token = inv.json()["token"]
        await call(w, "member_a", "POST", "/api/codebase/invites/redeem", json={"token": token})
        await call(w, "editor_a", "DELETE", f"{base}/invites/{inv.json()['invite']['id']}")
        await call(w, "editor_a", "DELETE", base)

        rows = [a for a in w.audit if a["action"].startswith("codebase.")]
        assert [a["action"] for a in rows] == [
            "codebase.pack_created", "codebase.version_uploaded", "codebase.version_uploaded",
            "codebase.version_activated", "codebase.pack_updated", "codebase.teams_changed",
            "codebase.member_added", "codebase.member_removed", "codebase.invite_created",
            "codebase.invite_redeemed", "codebase.invite_revoked", "codebase.pack_deleted"]
        for a in rows:
            assert a["target"] == pid and a["workspace_id"] == w.ws["ws-a"]
            assert a["actor_id"] in ("u-editor_a", "u-member_a")
            blob = repr({k: v for k, v in a.items() if k != "actor"})    # the actor is named, by design
            for secret in (title, desc, note, "Ledgerwork", token, "ledger", "notes/n.md",
                           "recipe", "member-a@acme-corp.io"):
                assert secret not in blob, (a["action"], secret)
        assert all(isinstance(v, int | str | bool | list) for a in rows for v in a["detail"].values())


async def test_a_refused_change_leaves_no_row(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                       json={"slug": "plain", "title": "Plain", "audience": "workspace"})
        pid = r.json()["id"]
        before = len(w.audit)
        await call(w, "member_a", "PATCH", f"/api/codebase/packs/{pid}", json={"title": "x"})
        await call(w, "member_a", "DELETE", f"/api/codebase/packs/{pid}")
        await call(w, "member_a", "POST", "/api/codebase/packs", json={"slug": "no", "title": "No"})
        await call(w, "editor_a", "PATCH", f"/api/codebase/packs/{pid}", json={"title": "Plain"})  # no change
        await call(w, "editor_a", "POST", "/api/codebase/invites/redeem", json={"token": "z" * 43})
        assert len(w.audit) == before
