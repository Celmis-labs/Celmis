"""Helpers for the tests that read the audit page over knowledge packs — not a
test module.

`real_audit(w, monkeypatch, tmp_path)` puts the real action-audit writer back
(the `world` fixture swaps it for a spy) into a fresh file under the test's own
directory, and points the sync engine the MCP-side code reads at the world's
database, so a row written by `record_action` is a row the audit router reads.
"""

from __future__ import annotations

from pathlib import Path

import src.security.audit as audit_mod

REAL_RECORD_ACTION = audit_mod.record_action


def real_audit(monkeypatch, tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from src.access import resolver

    monkeypatch.setattr(audit_mod, "record_action", REAL_RECORD_ACTION)
    monkeypatch.setattr(audit_mod, "_default_logger", None)
    monkeypatch.setattr(resolver, "_ENGINE", create_engine(f"sqlite:///{tmp_path / 'celmis.db'}"))


async def page(w, who: str, path: str = "/api/audit?mode=action&limit=500", ws: str = "ws-a"):
    """The audit page as ``who`` reads it: the response."""
    return await w.client.request("GET", path, headers=w.h(who, ws))


async def operations(w, who: str, ws: str = "ws-a") -> list[str]:
    r = await page(w, who, ws=ws)
    assert r.status_code == 200, r.text
    return [str(rec.get("operation")) for rec in r.json()["records"]]
