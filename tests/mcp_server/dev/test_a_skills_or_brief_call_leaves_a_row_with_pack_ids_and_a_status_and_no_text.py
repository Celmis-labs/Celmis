"""The audit row of a `skills` or `brief` call, written by the real call envelope.

The contract (src/mcp_server/call_envelope.py, audit.py, docs/CODEBASE.md "Usage"):

  * the row of a call names the packs it drew on by id, and `<pack id>/<skill>` for
    a skill whose body it returned; a skill opened is a use of that skill, a
    list or a search is a use of the packs on the page, nothing more;
  * the status is `ok` for an answer, `denied` for a skill that is hidden,
    missing or another workspace's (the same status whichever of the three it
    was), for a refused token, and never `ok` for an answer made of a refusal;
  * the row holds no argument value: not the query, not the task text, not the
    name asked for; only the one-way digest of the arguments;
  * a call by someone who may read no pack leaves a row with no packs.
"""

from __future__ import annotations

import json
import re

import mcp.types as types
import pytest

from src.mcp_server import audit
from tests.mcp_server.dev.pack_world import PACKS

NEEDLE = "zebracanary"


@pytest.fixture
async def served(pack_world, monkeypatch):
    """``call(tool, **args) -> (text, row)`` through the dev server's own handler."""
    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    from src.mcp_server.dev_profile import build_dev_mcp

    rows: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", rows.append)
    handler = build_dev_mcp()._mcp_server.request_handlers[types.CallToolRequest]

    async def call(tool: str, **args):
        before = len(rows)
        req = types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=tool, arguments=args))
        result = (await handler(req)).root
        assert len(rows) == before + 1, "one call, one row"
        return "\n".join(c.text for c in result.content), rows[-1]

    return call


async def test_opening_a_skill_leaves_the_pack_and_the_skill_entry_and_an_ok_status(
        pack_world, served):
    pack_world.as_caller("editor2")
    text, row = await served("skills", name="staff/oncall")
    pid = pack_world.ids["staff"]
    assert "skill staff/oncall [staff@v1]" in text
    assert (row["tool"], row["profile"], row["status"]) == ("skills", "dev", "ok")
    assert row["packs"] == sorted([pid, f"{pid}/oncall"])
    assert row["repos"] == []


async def test_a_hidden_a_missing_and_a_foreign_skill_leave_the_same_denied_row(pack_world, served):
    pack_world.as_caller("viewer")
    seen = []
    for name in ("private/diary", "private/missing", "zzz/none", "bridge", "abroad/bridge"):
        text, row = await served("skills", name=name)
        seen.append((text, row["status"], tuple(row["packs"]), row["tool"]))
    assert len(set(seen)) == 1
    assert seen[0][1:] == ("denied", (), "skills")
    assert "skill not found or not accessible" in seen[0][0]


async def test_a_list_and_a_search_name_the_packs_on_the_page_and_no_skill(pack_world, served):
    pack_world.as_caller("editor2")
    expected = sorted(pack_world.ids[p] for p in ("open", "staff", "vault"))
    for args in ({}, {"query": "invoicing"}, {"query": "invoicing", "detailed": True}):
        _text, row = await served("skills", **args)
        assert row["status"] == "ok" and row["packs"] == expected, args
        assert not any("/" in p for p in row["packs"])


async def test_a_brief_names_the_packs_it_quoted_and_each_is_a_pack_the_caller_may_read(
        pack_world, served):
    pack_world.as_caller("member")
    text, row = await served("brief", task="fix the invoicing routine")
    quoted = set(re.findall(r"\[([a-z]+)@v1\]", text))
    assert quoted and quoted <= {"open", "staff", "vault"}
    assert row["status"] == "ok" and row["tool"] == "brief"
    assert row["packs"] == sorted(pack_world.ids[p] for p in quoted)


async def test_the_row_holds_no_text_the_caller_sent(pack_world, served):
    pack_world.as_caller("owner")
    _t, row = await served("skills", query=f"invoicing {NEEDLE}")
    _t2, row2 = await served("brief", task=f"invoicing routine {NEEDLE} please")
    _t3, row3 = await served("skills", name="open/rollout", path=f"{NEEDLE}.md")
    for r in (row, row2, row3):
        assert NEEDLE not in json.dumps(r, default=str)
        assert re.fullmatch(r"[0-9a-f]{16}", r["args_hash"]), r["args_hash"]
    assert len({row["args_hash"], row2["args_hash"], row3["args_hash"]}) == 3
    for r in (row, row2, row3):
        for entry in r["packs"]:
            assert re.fullmatch(r"[0-9a-f-]{36}(/[a-z0-9._-]{1,64})?", entry), entry


async def test_a_refused_token_leaves_a_denied_row_with_no_packs(pack_world, served):
    pack_world.as_caller("owner", refused="This MCP token was revoked.")
    for tool, args in (("skills", {}), ("skills", {"name": "open/rollout"}), ("brief", {"task": "invoicing"})):
        text, row = await served(tool, **args)
        assert row["status"] == "denied" and row["packs"] == [], (tool, args)
        assert "marigold" not in text


@pytest.mark.parametrize("kind", ["project", "internal"])
async def test_a_project_or_internal_token_leaves_a_row_with_no_packs(pack_world, served, kind):
    pack_world.as_caller("owner", kind=kind)
    for tool, args in (("skills", {}), ("skills", {"query": "invoicing"}),
                       ("skills", {"name": "open/rollout"}), ("brief", {"task": "invoicing routine"})):
        text, row = await served(tool, **args)
        assert row["packs"] == [], (tool, args)
        assert not set(PACKS) & set(re.findall(r"\[([a-z]+)@v\d+\]", text))
    _t, row = await served("skills", name="open/rollout")
    assert row["status"] == "denied"


async def test_an_empty_task_gets_one_line_saying_so_and_leaves_no_packs(pack_world, served):
    pack_world.as_caller("owner")
    text, row = await served("brief", task="   ")
    assert "task is empty" in text and row["packs"] == []
