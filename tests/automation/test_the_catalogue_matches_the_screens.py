"""The UI catalogue cannot drift from the screens it describes.

`src/automation/ui_catalogue.py` is the one list of where things are: the
agent opens pages and spotlights controls from it, and quotes its descriptions
when it answers "what does this option do". It is only worth having if it is
true, and the ways it goes false are all quiet: a control is moved and its
`data-tour` attribute is left behind (the tour highlights nothing), a setting
is added to the form and nobody adds it here (the agent has never heard of
it), a setting moves to another section (the agent opens the wrong tab).

So this file reads the web source — the same way the older `test_tour.py`
does — and pins each of those. A failure names the thing that moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.automation import ui_catalogue as cat
from src.automation.tour import TARGETS

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
MODEL = (WEB / "components/review-settings/model.ts").read_text(encoding="utf-8")
ROUTES = (WEB / "lib/review-settings-routes.ts").read_text(encoding="utf-8")


def _web_source() -> str:
    parts = []
    for sub in ("app", "components", "lib", "ee"):
        for path in (WEB / sub).rglob("*"):
            if path.suffix in (".ts", ".tsx") and ".test." not in path.name:
                parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


SOURCE = _web_source()


def _inheritable_keys() -> list[str]:
    block = MODEL.split("export const INHERITABLE_KEYS = [", 1)[1].split("] as const", 1)[0]
    return re.findall(r'"([a-z_]+)"', block)


def _field_sections() -> dict[str, str]:
    block = MODEL.split("export const FIELD_SECTION", 1)[1].split("};", 1)[0]
    return dict(re.findall(r'^\s*([a-z_]+): "([a-z]+)",', block, re.M))


def test_there_is_a_catalogue_of_a_useful_size():
    assert len(cat.DESTINATIONS) >= 15
    assert len(cat.FIELDS) >= 60


@pytest.mark.parametrize("key", _inheritable_keys())
def test_every_review_setting_has_an_entry(key):
    assert key in cat.FIELDS_BY_KEY, (
        f"the review setting {key!r} is edited on /review-settings but the "
        "agent cannot show it: add it to FIELDS in ui_catalogue.py")


@pytest.mark.parametrize("key,section", sorted(_field_sections().items()))
def test_every_setting_is_catalogued_in_the_section_the_page_edits_it_in(key, section):
    if key == "agents":
        return  # the per-agent model block: entries agent_model & co.
    entry = cat.FIELDS_BY_KEY[key]
    assert entry.destination == f"review_settings.{section}", (
        f"{key} is edited in the {section!r} section, the catalogue says "
        f"{entry.destination!r}")


def test_every_settings_section_is_a_destination():
    ids = re.findall(r'"([a-z]+)",', ROUTES.split("SECTION_IDS = [", 1)[1].split("] as const", 1)[0])
    assert set(ids) == {d.section for d in cat.DESTINATIONS.values() if d.section}


@pytest.mark.parametrize("section", sorted(d.section for d in cat.DESTINATIONS.values() if d.section))
def test_every_section_frame_carries_the_section_anchor(section):
    assert f'id="{section}"' in (
        "".join(p.read_text(encoding="utf-8")
                for p in (WEB / "components/review-settings").glob("section-*.tsx")))
    # the generic anchor is derived from the frame's id in field.tsx
    field = (WEB / "components/review-settings/field.tsx").read_text(encoding="utf-8")
    assert "data-tour={`settings-section:${id}`}" in field


def test_every_setting_row_marks_itself_with_its_field_name():
    field = (WEB / "components/review-settings/field.tsx").read_text(encoding="utf-8")
    assert "`setting:${field}`" in field


@pytest.mark.parametrize("key", [
    f.key for f in cat.FIELDS if f.anchor == f"setting:{f.key}"
    and f.key in _inheritable_keys()])
def test_every_generic_setting_is_rendered_by_a_row_with_that_field(key):
    sections = "".join(p.read_text(encoding="utf-8")
                       for p in (WEB / "components/review-settings").glob("section-*.tsx"))
    # Either a row written with the literal, or a helper row given the key as
    # a quoted string in the section's own field list.
    assert (f'field="{key}"' in sections or f'"{key}"' in sections), (
        f"no row renders {key}, so its anchor would never exist")


@pytest.mark.parametrize("base", sorted(cat.known_anchors()))
def test_every_catalogued_anchor_exists_in_the_web_source(base):
    pattern = rf"""["'`]{re.escape(base)}(?:["'`:]|\$\{{)"""
    assert re.search(pattern, SOURCE), (
        f"data-tour {base!r} is catalogued but nothing in web/ carries it")


def test_every_older_tour_target_is_still_in_the_catalogue():
    bases = {f.anchor.split(":")[0] for f in cat.FIELDS}
    missing = sorted(t for t in TARGETS if t not in bases)
    assert not missing, f"tour targets the catalogue does not know: {missing}"


def test_every_anchor_can_pass_the_browsers_id_filter():
    """lib/guide-tour.ts drops ids that do not match this; a catalogue anchor
    that fails it would be silently thrown away on arrival."""
    browser = re.compile(r"^[a-z][a-z0-9-]*(:[a-z_]+)?$")
    for f in cat.FIELDS:
        for agent in (cat.SWITCH_AGENTS if f.per_agent else ("",)):
            anchor = f.anchor.format(agent=agent or "x")
            assert browser.match(anchor), anchor
            if f.opener:
                assert browser.match(f.opener.format(agent=agent or "x"))
    for d in cat.DESTINATIONS.values():
        assert browser.match(d.anchor), d.anchor


def test_every_field_belongs_to_a_known_destination():
    for f in cat.FIELDS:
        assert f.destination in cat.DESTINATIONS, f.key


def test_field_keys_are_unique():
    keys = [f.key for f in cat.FIELDS]
    assert len(keys) == len(set(keys))


def test_every_description_is_one_readable_sentence_of_bounded_length():
    for f in cat.FIELDS:
        assert 20 <= len(f.what) <= 460, f.key
    for d in cat.DESTINATIONS.values():
        assert d.what and d.title, d.id


def test_the_routes_use_the_query_the_settings_page_reads():
    assert 'qs.set("repo"' in ROUTES and 'qs.set("section"' in ROUTES and 'qs.set("agent"' in ROUTES


def test_the_knowledge_is_generated_from_the_catalogue():
    from src.automation.knowledge import BY_ID

    prompts = BY_ID["ui-prompts"].body
    assert "prompt_replace:<agent>" in prompts
    assert "advanced option" in prompts
    assert "built-in: info (everything)" in BY_ID["ui-filters"].body
