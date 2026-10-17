"""The audit page does not tell a workspace admin what the pack page hides.

The contract (src/api/routers/audit.py "Knowledge packs", docs/CODEBASE.md):

  * a `codebase.*` row is about the pack named by its `target`, and is shown only
    to a reader who may see that pack in the row's workspace right now — a
    restricted pack's id, uploads, tokens and members are not visible to an owner
    or admin who is not named on it, nor to a global admin who is not the
    superadmin;
  * the superadmin reads every `codebase.*` row, including the rows of a pack
    that has since been deleted, which nobody else can read;
  * the gate runs before counting: the list, the facets, the stats and the CSV
    export all agree, and a refused row is not counted as "hidden";
  * the `mcp.call` row carries no pack id; one written before that, with
    `extra.packs`, is shown to a reader with only the entries of packs they may see.
"""

from __future__ import annotations

import json

from tests.api.audit_support import operations, page, real_audit
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world


async def _routers():
    from src.api.routers import audit as audit_router

    return (audit_router.router,)


async def _two_packs(w):
    """A restricted pack nobody but its creator is named on, and one granted to
    team-a (whose members include owner_a and admin_a)."""
    out = {}
    for slug, teams in (("alone", []), ("shared", [{"team_id": w.ids["team_a"],
                                                       "permission": "read"}])):
        r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
            "slug": slug, "title": f"Pack {slug}", "description": "d",
            "audience": "restricted", "teams": teams})
        assert r.status_code == 201, r.text
        out[slug] = r.json()["id"]
        r = await upload_zip(w, "editor_a", out[slug], note_zip(slug))
        assert r.status_code == 201, r.text
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{out[slug]}/tokens",
                       json={"label": f"{slug}-ci", "expires_in_days": 30})
        assert r.status_code == 201, r.text
    return out


async def test_a_workspace_admin_reads_no_row_of_a_restricted_pack_they_are_not_named_on(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)

        # the pack page already hides the pack from this admin
        assert (await call(w, "admin2_a", "GET", f"/api/codebase/packs/{ids['alone']}")
                ).status_code == 404

        for path in ("/api/audit?mode=action&limit=500", "/api/audit/export",
                     "/api/audit/facets", "/api/audit/stats"):
            r = await page(w, "admin2_a", path)
            assert r.status_code == 200, r.text
            for pid in ids.values():
                assert pid not in r.text, path
        assert not [o for o in await operations(w, "admin2_a") if o.startswith("codebase.")]

        facets = (await page(w, "admin2_a", "/api/audit/facets")).json()
        assert not [o for o in facets["operations"] if o.startswith("codebase.")]
        stats = (await page(w, "admin2_a", "/api/audit/stats")).json()
        assert stats["total_calls"] == 0 and stats["hidden_unattributed"] == 0


async def test_a_workspace_admin_named_through_a_team_reads_the_rows_of_that_pack_alone(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)

        r = await page(w, "admin_a")
        assert r.status_code == 200, r.text
        rows = [x for x in r.json()["records"] if str(x["operation"]).startswith("codebase.")]
        assert {x["target"] for x in rows} == {ids["shared"]}
        assert {"codebase.pack_created", "codebase.version_uploaded",
                "codebase.token_created"} <= {x["operation"] for x in rows}
        assert ids["alone"] not in r.text

        stats = (await page(w, "admin_a", "/api/audit/stats")).json()
        assert stats["total_calls"] == len(rows)
        csv_text = (await page(w, "admin_a", "/api/audit/export")).text
        assert ids["shared"] in csv_text and ids["alone"] not in csv_text


async def test_a_global_admin_who_is_not_the_superadmin_does_not_read_a_restricted_pack_row(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)

        r = await page(w, "gadmin")
        assert r.status_code == 200, r.text
        assert ids["alone"] not in r.text and ids["shared"] not in r.text
        stats = (await page(w, "gadmin", "/api/audit/stats")).json()
        assert stats["total_calls"] == 0 and stats["hidden_unattributed"] == 0


async def test_the_superadmin_reads_every_pack_row(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)

        r = await page(w, "su")
        assert r.status_code == 200, r.text
        assert {x["target"] for x in r.json()["records"]
                if str(x["operation"]).startswith("codebase.")} == set(ids.values())
        assert ids["alone"] in (await page(w, "su", "/api/audit/export")).text


async def test_the_rows_of_a_deleted_pack_are_the_superadmins_alone(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)
        assert ids["shared"] in (await page(w, "admin_a")).text

        r = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{ids['shared']}")
        assert r.status_code in (200, 204), r.text

        for who in ("admin_a", "owner_a", "gadmin"):
            assert ids["shared"] not in (await page(w, who)).text, who
        ops = [x for x in (await page(w, "su")).json()["records"] if x["target"] == ids["shared"]]
        assert "codebase.pack_deleted" in {x["operation"] for x in ops}


def _write_old_mcp_row(workspace_id: str, user_id: str, packs: list[str]) -> None:
    """A `mcp.call` row as it was written before the mirror was removed."""
    import src.security.audit as audit

    audit.record_action(
        action="mcp.call", actor_id=user_id, workspace_id=workspace_id, target="skills",
        detail={"status": "ok", "repos": [], "packs": packs})


async def test_an_old_mcp_call_row_shows_only_the_packs_the_reader_may_see(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)
        _write_old_mcp_row(w.ws["ws-a"], w.uid("editor_a"),
                           [ids["alone"], f"{ids['alone']}/secret-runbook", ids["shared"],
                            f"{ids['shared']}/shared-runbook"])

        def packs_in(body: dict) -> list[str]:
            rows = [x for x in body["records"] if x["operation"] == "mcp.call"]
            assert len(rows) == 1
            return rows[0]["extra"].get("packs", [])

        seen = (await page(w, "admin_a")).json()
        assert packs_in(seen) == [ids["shared"], f"{ids['shared']}/shared-runbook"]
        none = (await page(w, "admin2_a")).json()
        assert packs_in(none) == []
        assert "secret-runbook" not in json.dumps(none) and ids["alone"] not in json.dumps(none)
        assert len(packs_in((await page(w, "su")).json())) == 4


async def test_a_new_mcp_call_row_carries_no_pack_at_all(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from src.mcp_server import audit as mcp_audit

    async with world(tmp_path, monkeypatch, extra_routers=await _routers()) as w:
        real_audit(monkeypatch, tmp_path)
        ids = await _two_packs(w)
        mcp_audit.write_row({
            "ts": datetime.now(UTC), "workspace_id": w.ws["ws-a"], "user_id": w.uid("editor_a"),
            "token_id": None, "kind": "pat", "client_id": "", "tool": "skills", "profile": "dev",
            "repos": [], "packs": [ids["alone"], f"{ids['alone']}/secret-runbook"],
            "status": "ok", "result_bytes": 10, "result_items": 1, "duration_ms": 3,
            "args_hash": "abc"})

        for who in ("admin_a", "admin2_a", "su"):
            r = await page(w, who)
            rows = [x for x in r.json()["records"] if x["operation"] == "mcp.call"]
            assert len(rows) == 1 and "packs" not in rows[0]["extra"], who
            assert ids["alone"] not in json.dumps(rows[0]) and "secret-runbook" not in r.text, who


def test_a_scope_built_without_a_pack_gate_refuses_every_pack_row():
    from src.api.routers.audit import _Scope

    row = {"operation": "codebase.pack_created", "workspace_id": "w", "target": "p",
           "actor_id": "u"}
    assert not _Scope(global_=True, workspace_id=None).allows(row)
    assert not _Scope(global_=False, workspace_id="w", actor_id="u").allows(row)
    other = {"operation": "workspace.member_role_changed", "workspace_id": "w", "target": "x"}
    assert _Scope(global_=False, workspace_id="w").allows(other)
