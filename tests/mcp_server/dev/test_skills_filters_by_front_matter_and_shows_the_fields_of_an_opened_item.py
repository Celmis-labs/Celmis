"""`skills where=` narrows the catalogue and the search by front-matter fields.

The contract (docs/MCP_DEV.md "Knowledge packs"): `where='status=verified lang=uk'` keeps
items whose front matter matches, AND across keys; it works with and without `query=`;
a malformed `where` is one error line; an opened file shows its fields on a `fields:`
line, and never the `repos` pointers that way.
"""

from __future__ import annotations

from src.mcp_server.dev_profile import tools_skills as ts
from tests.knowledge.pack_rig import A
from tests.mcp_server.dev.pack_world import _make_pack

CARD = ("---\ntype: process\nid: PR-7\nstatus: verified\nlang: uk\ntags: [billing, month-end]\n"
        "repos: [secret-repo]\n---\n# Close the month\n\nPost the zebracorn accruals.\n")
DRAFT = ("---\ntype: process\nid: PR-8\nstatus: draft\nlang: en\ntags: [billing]\n---\n"
         "# Close the quarter\n\nPost the zebracorn accruals.\n")
SKILL = ("---\nname: closing\ndescription: Close the books\nstatus: verified\nlang: en\n---\n"
         "# Closing\n\nSteps for the zebracorn close.\n")


async def _cards(world):
    await _make_pack(world.rig, "editor", A, "cards", "workspace", (), [
        ("closing/SKILL.md", SKILL), ("notes/close-month.md", CARD),
        ("notes/close-quarter.md", DRAFT)])
    world.as_caller("member")


def _search(world, **kw):
    text, _rec = world.call(ts.run, kw.get("query", ""), kw.get("name", ""), kw.get("path", ""),
                            False, "", kw.get("where", ""))
    return text


async def test_a_search_with_where_keeps_only_the_items_whose_front_matter_matches(pack_world):
    await _cards(pack_world)
    everything = _search(pack_world, query="zebracorn")
    assert "cards/notes/close-month.md" in everything and "cards/notes/close-quarter.md" in everything
    only = _search(pack_world, query="zebracorn", where="status=verified")
    assert "cards/notes/close-month.md" in only and "cards/closing/SKILL.md" in only
    assert "close-quarter" not in only
    both = _search(pack_world, query="zebracorn", where="status=verified lang=uk")
    assert "close-month" in both and "SKILL.md" not in both and "close-quarter" not in both


async def test_a_list_field_matches_by_membership_and_values_are_case_insensitive(pack_world):
    await _cards(pack_world)
    text = _search(pack_world, query="zebracorn", where="TAGS=Month-End")
    assert "close-month" in text and "close-quarter" not in text
    either = _search(pack_world, query="zebracorn", where="lang=uk|en type=process")
    assert "close-month" in either and "close-quarter" in either and "SKILL.md" not in either


async def test_the_catalogue_is_filtered_too(pack_world):
    await _cards(pack_world)
    assert "cards/closing" in _search(pack_world, where="status=verified")
    assert "cards/closing" not in _search(pack_world, where="status=draft")


async def test_a_bad_where_is_one_error_line_and_repos_cannot_be_probed(pack_world):
    await _cards(pack_world)
    for bad in ("status", "repos=secret-repo"):
        text = _search(pack_world, query="zebracorn", where=bad)
        assert "key=value" in text and "close-month" not in text, text
        assert "close-month" not in text


async def test_an_opened_file_shows_its_fields_but_not_its_repository_pointers(pack_world):
    await _cards(pack_world)
    text = _search(pack_world, name="cards", path="notes/close-month.md")
    fields = next(line for line in text.splitlines() if line.startswith("fields:"))
    assert "status=verified" in fields and "lang=uk" in fields and "tags=billing,month-end" in fields
    assert "secret-repo" not in fields and "repos" not in fields
    skill = _search(pack_world, name="cards/closing")
    assert "fields: status=verified lang=en" in skill


def test_the_skills_description_names_where_and_stays_short():
    assert "where=" in ts.SKILLS_DESCRIPTION and len(ts.SKILLS_DESCRIPTION) <= 200
