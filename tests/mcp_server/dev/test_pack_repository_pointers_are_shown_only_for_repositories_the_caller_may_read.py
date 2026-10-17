"""A pack may point at code; the pointer is shown only to someone who can read that code.

The contract (docs/CODEBASE.md "Brief", src/mcp_server/dev_profile/pack_access.py):

  * a pointer (`repo`, `repo:path`, `repo:path:line`) in a note, a checklist or a
    skill is shown only when the caller may read the repository as code
    (the `DevScope` of the call: the person's rules and the token's repository
    list), and the path is not one a deny rule of theirs hides;
  * every other pointer is dropped without a trace: no placeholder, no count, no
    word about it, and the repository's name appears nowhere in the answer;
  * `brief repos=...` narrows the pointers further to the repositories named;
  * a scope that cannot be worked out shows no pointer at all, and the pack's
    own text is still answered;
  * the `idx:` line of a brief names the repositories it cites and only those,
    sorted by slug whatever order the pointers were written in;
    every other answer of these tools says `idx: none`.
"""

from __future__ import annotations

from src.mcp_server.dev_profile import access
from src.mcp_server.dev_profile import tools_skills as ts
from src.mcp_server.dev_profile.freshness import idx_line, read_freshness
from tests.mcp_server.dev.pack_world import indexed

TASK = "invoicing routine"
HIDDEN = ("payments-core", "ghost-repo", "keys.py")


def _repos_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith("repos:")]


async def test_a_brief_names_the_pointers_of_a_readable_repository_and_drops_the_rest(pack_world):
    pack_world.readable(("shop-api",), deny={"shop-api": ("app/private/**",)})
    pack_world.as_caller("editor2")
    text, _rec = pack_world.call(ts.run_brief, TASK, tool="brief")
    assert _repos_lines(text) and set(_repos_lines(text)) == {"Repos: shop-api:app/db.py:12"}
    for word in HIDDEN:
        assert word not in text, word
    assert "withheld" not in text and "hidden" not in text and "dropped" not in text


async def test_a_repository_the_caller_reads_only_as_metadata_is_not_pointed_at(pack_world):
    pack_world.readable(("shop-api",), metadata=("payments-core",))
    pack_world.as_caller("member")
    text, _rec = pack_world.call(ts.run_brief, TASK, tool="brief")
    assert "payments-core" not in text and "ghost-repo" not in text
    assert "shop-api:app/db.py:12" in text


async def test_no_pointer_is_shown_when_no_repository_is_readable(pack_world):
    pack_world.as_caller("owner")
    brief_text, _ = pack_world.call(ts.run_brief, TASK, tool="brief")
    skill_text, _ = pack_world.call(ts.run, "", "open/rollout")
    note_text, _ = pack_world.call(ts.run, "", "open", "notes/open-note.md")
    for text in (brief_text, skill_text, note_text):
        assert _repos_lines(text) == []
        for word in ("shop-api", "payments-core", "ghost-repo"):
            assert word not in text, word
    assert "Invoicing marigold steps" in skill_text          # the pack's own text is unharmed
    assert "Invoicing marigold lives next to the billing code" in note_text


async def test_a_skill_and_a_file_show_the_same_filtered_repos_line(pack_world):
    pack_world.readable(("shop-api",), deny={"shop-api": ("app/private/**",)})
    pack_world.as_caller("owner")
    skill_text, _ = pack_world.call(ts.run, "", "open/rollout")
    note_text, _ = pack_world.call(ts.run, "", "open", "notes/open-note.md")
    assert "repos: shop-api:app/db.py:12" in skill_text.splitlines()
    assert "repos: shop-api:app/db.py:12" in note_text.splitlines()
    for text in (skill_text, note_text):
        for word in HIDDEN:
            assert word not in text, word


async def test_the_front_matter_that_holds_the_pointers_is_never_printed_raw(pack_world):
    pack_world.readable(("shop-api", "payments-core", "ghost-repo"))
    pack_world.as_caller("owner")
    for args in (("", "open/rollout"), ("", "open", "notes/open-note.md")):
        text, _ = pack_world.call(ts.run, *args)
        assert "---" not in text and "repos: [" not in text


async def test_a_search_and_the_list_never_carry_a_pointer(pack_world):
    pack_world.readable(("shop-api", "payments-core"))
    pack_world.as_caller("owner")
    for text in (pack_world.call(ts.run)[0], pack_world.call(ts.run, "invoicing", "", "", True)[0]):
        assert "shop-api" not in text and "payments-core" not in text


async def test_repos_narrows_a_brief_to_the_repositories_the_caller_named(pack_world):
    pack_world.readable(("shop-api", "payments-core"))
    pack_world.as_caller("owner")
    everything, _ = pack_world.call(ts.run_brief, TASK, "", tool="brief")
    assert "shop-api:app/db.py:12" in everything and "payments-core:src/x.py" in everything
    only, _ = pack_world.call(ts.run_brief, TASK, "payments-core", tool="brief")
    assert "payments-core:src/x.py" in only and "shop-api" not in only
    other, _ = pack_world.call(ts.run_brief, TASK, "payments-core, other", tool="brief")
    assert "payments-core:src/x.py" in other and "shop-api" not in other
    none, _ = pack_world.call(ts.run_brief, TASK, "other", tool="brief")
    assert _repos_lines(none) == [] and "payments-core" not in none and "shop-api" not in none


async def test_a_scope_that_fails_shows_no_pointer_and_still_answers(pack_world, monkeypatch):
    def broken(candidates=None):
        raise RuntimeError("the rules store is down")

    monkeypatch.setattr(access, "mcp_scope", broken)
    pack_world.as_caller("owner")
    brief_text, _ = pack_world.call(ts.run_brief, TASK, tool="brief")
    skill_text, _ = pack_world.call(ts.run, "", "open/rollout")
    assert "[open@v1]" in brief_text and "Invoicing marigold steps" in skill_text
    for text in (brief_text, skill_text):
        assert _repos_lines(text) == [] and "shop-api" not in text and "down" not in text


async def test_the_idx_line_of_a_brief_names_the_repositories_it_cites_and_no_others(
        pack_world, monkeypatch):
    indexed(monkeypatch, "shop-api", "payments-core", "ghost-repo")
    pack_world.readable(("shop-api",), metadata=("payments-core",))
    pack_world.as_caller("owner")
    text, _ = pack_world.call(ts.run_brief, TASK, tool="brief")
    first = text.splitlines()[0]
    assert first == idx_line([read_freshness(["shop-api"])["shop-api"]])
    assert first.startswith("idx: shop-api develop@3f2a91c") and first.endswith("fresh")
    assert "payments-core" not in first and "ghost-repo" not in first


async def test_the_idx_line_lists_the_cited_repositories_sorted_by_slug(pack_world, monkeypatch):
    indexed(monkeypatch, "shop-api", "payments-core")
    pack_world.readable(("shop-api", "payments-core"))      # the pointers name shop-api first
    pack_world.as_caller("owner")
    first = pack_world.call(ts.run_brief, TASK, tool="brief")[0].splitlines()[0]
    fresh = read_freshness(["payments-core", "shop-api"])
    assert first == idx_line([fresh["payments-core"], fresh["shop-api"]])
    assert first.index("payments-core") < first.index("shop-api")


async def test_the_idx_line_is_none_when_a_brief_cites_no_repository(pack_world, monkeypatch):
    indexed(monkeypatch, "shop-api")
    pack_world.as_caller("owner")                           # nothing readable: nothing cited
    assert pack_world.call(ts.run_brief, TASK, tool="brief")[0].startswith("idx: none\n")
    pack_world.readable(("shop-api",))
    for text in (pack_world.call(ts.run)[0], pack_world.call(ts.run, "invoicing")[0],
                 pack_world.call(ts.run, "", "open/rollout")[0],
                 pack_world.call(ts.run, "", "nothing/here")[0]):
        assert text.startswith("idx: none\n")
