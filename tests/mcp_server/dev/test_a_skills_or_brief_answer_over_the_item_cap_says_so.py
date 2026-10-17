"""`skills` and `brief` say when they stopped reading the packs.

The contract (docs/MCP_DEV.md "skills / brief", src/mcp_server/dev_profile/tools_skills.py):

  * when the items they rank or list were cut at the safety cap
    (`store.MAX_RANKED_ITEMS`), the answer carries one notice line,
    `index limit reached: part of your packs was not read; narrow the query`,
    for the list, for a search and for a brief — never silently a shorter answer;
  * under the cap there is no such line, and the answer is what it was;
  * the notice is inside the brief's budget: the answer still fits.
"""

from __future__ import annotations

from src.knowledge import store
from src.mcp_server.dev_profile import emit
from src.mcp_server.dev_profile import tools_skills as ts

TASK = "invoicing routine"


async def test_nothing_is_said_when_the_packs_were_read_whole(pack_world):
    pack_world.as_caller("owner")
    for text in (pack_world.call(ts.run)[0], pack_world.call(ts.run, "invoicing")[0],
                 pack_world.call(ts.run_brief, TASK, tool="brief")[0]):
        assert ts.TRUNCATED not in text


async def test_a_list_a_search_and_a_brief_over_the_cap_carry_the_notice(pack_world, monkeypatch):
    pack_world.as_caller("owner")
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", 4)
    listed = pack_world.call(ts.run)[0]
    searched = pack_world.call(ts.run, "invoicing")[0]
    briefed = pack_world.call(ts.run_brief, TASK, tool="brief")[0]
    for text in (listed, searched, briefed):
        assert ts.TRUNCATED in text.splitlines(), text[:300]
        assert text.count(ts.TRUNCATED) == 1
    assert "[open@v1]" in briefed or "[named@v1]" in briefed or "[staff@v1]" in briefed


async def test_a_brief_with_the_notice_still_fits_its_budget(pack_world, monkeypatch):
    pack_world.as_caller("owner")
    monkeypatch.setattr(store, "MAX_RANKED_ITEMS", 6)
    text = pack_world.call(ts.run_brief, TASK, tool="brief")[0]
    assert ts.TRUNCATED in text
    assert len(text) <= emit.budget_chars("brief")
