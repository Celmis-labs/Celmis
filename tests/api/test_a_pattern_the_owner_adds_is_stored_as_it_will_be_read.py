"""The owner reads back the pattern that hides, not the one that was typed.

A path pattern typed with a leading ``/`` or ``./`` is stored from the root of the
repository (``src.access.shares.anchor_glob``), so the detail, the audit row and the
matcher all show one thing, and a pattern that could hide nothing is never kept.

World: `tests/api/sharing_world.py` (B gives A one repository).
"""

from __future__ import annotations

from tests.api.sharing_world import add_share, share_row


async def _narrow(w, share_id: str, body: dict):
    return await w.client.patch(f"/api/sharing/shares/{share_id}", json=body,
                                headers=w.h("admin_b", "ws-b"))


async def test_a_pattern_typed_from_the_root_is_stored_without_the_slash(sw):
    share_id = add_share(sw)
    r = await _narrow(sw, share_id, {"add_deny": ["/secrets/**", "./docs/internal/", "src/**/"]})
    assert r.status_code == 200, r.text
    assert r.json()["deny_globs"] == ["secrets/**", "docs/internal/", "src/**"]
    assert share_row(sw, share_id).deny_globs == ["secrets/**", "docs/internal/", "src/**"]


async def test_two_spellings_of_one_pattern_are_one_pattern(sw):
    share_id = add_share(sw, deny=["secrets/**"])
    r = await _narrow(sw, share_id, {"add_deny": ["/secrets/**"]})
    assert r.json()["deny_globs"] == ["secrets/**"] and r.json()["deny_count"] == 1


async def test_the_audit_row_records_the_pattern_that_took_effect(sw):
    from tests.api.sharing_world import audit_rows

    share_id = add_share(sw)
    await _narrow(sw, share_id, {"add_deny": ["/secrets/**"]})
    (row,) = audit_rows(sw, "sharing.narrowed")
    assert row["detail"]["changes"]["deny_globs"]["after"] == ["secrets/**"]


async def test_a_pattern_that_is_only_the_root_hides_everything(sw):
    share_id = add_share(sw)
    r = await _narrow(sw, share_id, {"add_deny": ["/"]})
    assert r.json()["deny_globs"] == ["**"]
