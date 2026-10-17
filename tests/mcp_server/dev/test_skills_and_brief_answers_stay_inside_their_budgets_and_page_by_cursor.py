"""`skills` and `brief` answer inside a token budget, and `skills` hands back the rest by cursor.

The contract (docs/MCP_DEV.md "What it costs", src/mcp_server/dev_profile/emit.py):

  * every answer is cut at a line boundary to the tool's budget (`emit.BUDGETS`;
    `detailed=true` doubles the default) and, when something was left out, says
    how many lines remain and the cursor that continues, in the shape every dev
    tool uses (`... +N more | cursor=X`);
  * following the cursors reads the whole body or list: nothing is lost,
    nothing is shown twice;
  * a cursor from another question, or from a skill that has a newer version,
    starts again from the first page and says so; it is never followed;
  * `brief` has no cursor: it stays inside its budget by leaving items out
    (`... +N more`), the room its own `idx:` line takes included;
  * opening a skill counts as a use of the skill once, on its first page.
"""

from __future__ import annotations

import re

from src.knowledge import store
from src.mcp_server.dev_contract import MORE_RE
from src.mcp_server.dev_profile import cursor as cursor_mod
from src.mcp_server.dev_profile import emit
from src.mcp_server.dev_profile import tools_skills as ts
from tests.knowledge.pack_rig import A, upload
from tests.mcp_server.dev.pack_world import indexed

MORE = re.compile(MORE_RE, re.M)
STEP = re.compile(r"^Step \d{3}:", re.M)
SKILL_LINE = re.compile(r"^([a-z]+)/([a-z0-9-]+) — ", re.M)
HIT = re.compile(r"^([a-z]+)/(\S+) \[", re.M)
#: A tail line is added after the budget is used up: its cursor is a few dozen characters.
TAIL = 220

LONG = ("---\nname: longform\ndescription: Invoicing walk-through.\n---\n# Longform\n\n"
        + "\n".join(f"Step {i:03d}: invoicing detail number {i} with some filler words to fill room."
                    for i in range(1, 401)) + "\n")


async def _pack(world, slug, files, who="owner"):
    async with world.rig.session() as s:
        made = await store.create_pack(s, world.rig.principal(who), A, slug=slug,
                                       title=slug.title(), audience="workspace")
        view = await store.get_pack(s, world.rig.principal(who), A, made.pack.id)
        await store.add_version(s, view, upload(*files))
    return made.pack.id


async def _new_version(world, pack_id, files, who="owner"):
    async with world.rig.session() as s:
        view = await store.get_pack(s, world.rig.principal(who), A, pack_id)
        await store.add_version(s, view, upload(*files))


def _pages(world, query="", name="", path=""):
    """Every page of a question, following the cursor each one hands back."""
    pages, cursor = [], ""
    for _ in range(60):
        text, rec = world.call(ts.run, query, name, path, False, cursor)
        pages.append((text, rec))
        more = MORE.search(text)
        if not more:
            return pages
        cursor = more.group("cursor")
    raise AssertionError("the cursors never ended")


def test_the_two_tools_have_their_budgets_registered():
    assert emit.BUDGETS["skills"] == (800, 4000)
    assert emit.BUDGETS["brief"] == (2000, 4000)
    assert emit.budget_chars("skills") < emit.budget_chars("skills", detailed=True)
    assert emit.budget_chars("brief") == int(2000 * emit.CHARS_PER_TOKEN)


async def test_a_long_skill_is_cut_at_its_budget_and_the_rest_comes_by_cursor(pack_world):
    await _pack(pack_world, "big", [("longform/SKILL.md", LONG)])
    pack_world.as_caller("owner")
    pages = _pages(pack_world, name="big/longform")
    cap = emit.budget_chars("skills")
    assert len(pages) > 3
    for text, _rec in pages:
        assert len(text) <= cap + TAIL, len(text)
        assert text.startswith("idx: none\n")
    steps = [ln for text, _rec in pages for ln in text.splitlines() if STEP.match(ln)]
    assert steps == [f"Step {i:03d}: invoicing detail number {i} with some filler words to fill room."
                     for i in range(1, 401)]
    first = pages[0][0]
    assert "skill big/longform [big@v1]" in first and "# Longform" in first
    assert all("big/longform [big@v1] — continued" in t for t, _r in pages[1:])
    assert MORE.search(first) and not MORE.search(pages[-1][0])


async def test_the_detailed_flag_doubles_the_room_of_a_page(pack_world):
    await _pack(pack_world, "big", [("longform/SKILL.md", LONG)])
    pack_world.as_caller("owner")
    plain = pack_world.call(ts.run, "", "big/longform")[0]
    wide = pack_world.call(ts.run, "", "big/longform", "", True)[0]
    cap = emit.budget_chars("skills")
    assert len(plain) <= cap + TAIL < len(wide) <= 2 * cap + TAIL
    assert len(STEP.findall(wide)) > 1.5 * len(STEP.findall(plain))


async def test_opening_a_skill_counts_one_use_on_its_first_page_only(pack_world):
    pid = await _pack(pack_world, "big", [("longform/SKILL.md", LONG)])
    pack_world.as_caller("owner")
    (first, rec1), (second, rec2) = _pages(pack_world, name="big/longform")[:2]
    assert rec1.packs == {pid, f"{pid}/longform"}
    assert rec2.packs == {pid} and "continued" in second


async def test_a_cursor_from_another_question_starts_again_and_says_so(pack_world):
    await _pack(pack_world, "big", [("longform/SKILL.md", LONG)])
    pack_world.as_caller("owner")
    cursor = MORE.search(pack_world.call(ts.run, "", "big/longform")[0]).group("cursor")
    other = pack_world.call(ts.run, "invoicing", "", "", False, cursor)[0]
    assert cursor_mod.MISMATCH_NOTE in other
    assert not STEP.search(other)                              # not the body of the other question
    again = pack_world.call(ts.run, "", "big/longform", "", False, "not-a-cursor")[0]
    assert cursor_mod.MISMATCH_NOTE in again and "Step 001:" in again


async def test_a_cursor_does_not_survive_a_new_version_of_the_skill(pack_world):
    pid = await _pack(pack_world, "big", [("longform/SKILL.md", LONG)])
    pack_world.as_caller("owner")
    cursor = MORE.search(pack_world.call(ts.run, "", "big/longform")[0]).group("cursor")
    await _new_version(pack_world, pid, [("longform/SKILL.md", LONG.replace("detail", "item"))])
    text, _rec = pack_world.call(ts.run, "", "big/longform", "", False, cursor)
    assert cursor_mod.STALE_NOTE in text
    assert "[big@v2]" in text and "Step 001: invoicing item number 1" in text


async def test_the_list_of_skills_pages_by_cursor_and_loses_and_repeats_nothing(pack_world):
    files = [(f"s{i:02d}/SKILL.md",
              f"---\nname: s{i:02d}\ndescription: Invoicing helper number {i:02d} of the many pack.\n---\n# s{i}\n")
             for i in range(50)]
    await _pack(pack_world, "many", files)
    pack_world.as_caller("owner")
    pages = _pages(pack_world)
    assert len(pages) >= 2
    cap = emit.budget_chars("skills")
    seen = [f"{p}/{s}" for text, _r in pages for p, s in SKILL_LINE.findall(text)]
    assert len(seen) == len(set(seen))
    assert {s for s in seen if s.startswith("many/")} == {f"many/s{i:02d}" for i in range(50)}
    assert {"open/rollout", "staff/oncall", "named/roster"} <= set(seen)
    assert all(len(t) <= cap + TAIL for t, _r in pages)


async def test_a_search_pages_by_cursor_and_shows_each_hit_once(pack_world):
    files = [(f"s{i:02d}/SKILL.md",
              f"---\nname: s{i:02d}\ndescription: Invoicing helper number {i:02d} of the many pack.\n---\n# s{i}\n")
             for i in range(50)]
    await _pack(pack_world, "many", files)
    pack_world.as_caller("owner")
    pages = _pages(pack_world, "invoicing")
    assert len(pages) >= 2
    hits = [f"{p}/{path}" for text, _r in pages for p, path in HIT.findall(text)]
    assert len(hits) == len(set(hits))
    assert {h for h in hits if h.startswith("many/")} == {f"many/s{i:02d}/SKILL.md" for i in range(50)}
    assert all(MORE.search(t) for t, _r in pages[:-1])


async def test_a_brief_stays_inside_its_budget_and_counts_what_it_left_out(pack_world, monkeypatch):
    notes = [(f"notes/n{i:02d}.md",
              f"---\nrepos: [shop-api:app/db.py:{i + 1}]\n---\n# Note {i:02d}\n\n"
              + "Invoicing retries are handled by the billing client. " * 12)
             for i in range(30)]
    await _pack(pack_world, "crowd", notes)
    indexed(monkeypatch, "shop-api")
    pack_world.readable(("shop-api",))
    pack_world.as_caller("owner")
    default, _ = pack_world.call(ts.run_brief, "invoicing retries billing", tool="brief")
    assert len(default) <= emit.budget_chars("brief")
    assert default.startswith("idx: shop-api ")
    assert "Repos: shop-api:app/db.py:" in default
    both = 0
    for tokens in range(700, 120, -3):               # every cut falls somewhere
        monkeypatch.setitem(emit.BUDGETS, "brief", (tokens, tokens))
        text, _ = pack_world.call(ts.run_brief, "invoicing retries billing", tool="brief")
        assert len(text) <= emit.budget_chars("brief"), tokens
        # the idx line names the repositories the text cites, whatever the room
        assert ("Repos: " in text) == text.startswith("idx: shop-api "), tokens
        assert text.startswith("idx: ") and "## " in text, tokens
        both += bool("Repos: " in text and re.search(r"^… \+\d+ more$", text, re.M))
    assert both, "no budget showed a cited repository and also left something out"
