"""The MCP call log says which knowledge packs a call read from, by id only.

The contract (src/mcp_server/callctx.py, audit.py; docs/CODEBASE.md "Usage"):

  * a tool notes the packs it read with `callctx.note_packs`; only uuids are
    kept, so a title, a path or a query passed by mistake never reaches the log;
  * the row written for the call carries those ids in `mcp_call_log.packs` (empty
    becomes NULL) and does NOT mirror them into the `mcp.call` action-audit row:
    that page is read by workspace owners, who must not learn the ids of packs
    they cannot see, so the action row is the same whether or not packs were read;
  * a refused call has no packs; the count shown to a pack's maintainers
    (`usage_30d`) is made of these rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from src.db.models import McpCallLog
from src.mcp_server import audit, callctx
from tests.knowledge.pack_rig import A, rig

P1, P2 = str(uuid.uuid4()), str(uuid.uuid4())


def _row(rec, **extra):
    base = {"ts": datetime.now(UTC), "tool": rec.tool, "profile": rec.profile,
            "repos": sorted(rec.repos), "packs": sorted(rec.packs), "status": rec.status,
            "result_bytes": 1, "result_items": 1, "duration_ms": 1, "args_hash": "h",
            "workspace_id": A, "user_id": "u", "token_id": None, "kind": "user", "client_id": ""}
    return {**base, **extra}


def test_only_uuids_are_kept_and_repeats_collapse():
    rec = callctx.begin("skills")
    callctx.note_packs(P1, [P2, P1], (P1,))
    callctx.note_packs("Billing runbook", "notes/n.md", "select * from packs", "", None, 7)
    callctx.note_packs(P1.upper(), P1.replace("-", ""))
    assert rec.packs == {P1, P2}
    callctx.reset()
    callctx.note_packs(P1)                                  # no call in flight: nothing, no error


def test_a_call_that_noted_nothing_has_no_packs():
    rec = callctx.begin("search")
    assert rec.packs == set()
    callctx.reset()


async def test_the_row_carries_the_ids_and_the_action_audit_row_never_does(tmp_path, monkeypatch):
    mirrored: list[dict] = []
    import src.security.audit as security_audit

    monkeypatch.setattr(security_audit, "record_action", lambda **kw: mirrored.append(kw))
    async with rig(tmp_path, monkeypatch) as r:
        with_packs = callctx.begin("skills")
        callctx.note_packs(P2, P1)
        audit.write_row(_row(with_packs))
        without = callctx.begin("search_code")
        audit.write_row(_row(without))
        callctx.reset()
        audit.write_row(_row(with_packs, tool="(refused:expired)", packs=[], status="denied"))

        async with r.session() as s:
            rows = {x.tool: x for x in (await s.scalars(select(McpCallLog))).all()}
        assert rows["skills"].packs == sorted([P1, P2])
        assert rows["search_code"].packs is None and rows["(refused:expired)"].packs is None

    by_tool = {m["target"]: m["detail"] for m in mirrored}
    assert set(by_tool) == {"skills", "search_code", "(refused:expired)"}
    assert all("packs" not in detail for detail in by_tool.values())
    assert P1 not in repr(mirrored) and P2 not in repr(mirrored)


def test_record_call_queues_the_ids_and_never_a_value(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)
    rec = callctx.begin("skills")
    callctx.note_packs(P1, "a title that must not be logged")
    rec.args_hash = "digest"
    audit.record_call(rec)
    callctx.reset()
    assert queued[0]["packs"] == [P1]
    assert "must not be logged" not in repr(queued)


def test_a_record_without_the_field_still_writes(monkeypatch):
    """An older queued row, or a stand-in record, has no `packs`."""
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)

    class Old:
        tool, profile, repos, status = "skills", "full", {"r"}, "ok"
        result_bytes = result_items = duration_ms = 0
        args_hash = ""

    audit.record_call(Old())
    assert queued and queued[0]["packs"] == []
