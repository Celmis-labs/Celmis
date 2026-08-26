"""What a workspace with nothing in it yet is told to do.

The dashboard's first screen was three cards reading 0, 0, 0, each with a
ghost "Manage →". A zero is not a measurement, it is a question, and three of
them at once is a wall with no door: nothing on that screen says which of the
three to open first, or that they are even ordered.

Under them sat a banner whose second paragraph recited the sidebar —
"Dashboard · Repositories · Code review · Ask the code · Claude agent · …" —
naming what was already on screen, in one run-on sentence, to a person who had
not yet done anything at all.

These are pinned here because they cost nothing to undo: a later edit adding a
fourth metric card copies the pattern of whichever card it sits next to, and
help text is the first thing a hurried change reaches for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
DASHBOARD = (WEB / "app" / "(app)" / "dashboard" / "page.tsx").read_text()
DEPENDENCIES = (WEB / "app" / "(app)" / "dependencies" / "page.tsx").read_text()
MESSAGES = WEB / "lib" / "i18n" / "messages"
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))

LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))


def _strip_comments(source: str) -> str:
    """Comments here NAME what they removed in order to explain the absence.

    Grepping them is how a guard passes while the thing it guards is gone —
    which has happened on this repo more than once.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


# ─── the three zeros ─────────────────────────────────────────────────


def test_a_zero_offers_the_verb_rather_than_the_count_alone():
    body = _strip_comments(DASHBOARD)
    assert "count === 0" in body, "no zero state — every card reads the same at 0"
    for key in ("dash.zero.connectionsCta", "dash.zero.reposCta", "dash.zero.reviewsCta"):
        assert key in body, f"{key} is not rendered"


def test_the_empty_card_reads_as_empty_before_it_is_read():
    """A dashed border says "nothing here yet" from across the room, which is
    how somebody skims three cards without reading any of them."""
    assert "border-dashed" in _strip_comments(DASHBOARD)


def test_every_metric_card_goes_through_the_same_component():
    """Three hand-rolled cards is how they drifted apart in the first place —
    one grew a footnote, one grew a different button, none grew a zero state."""
    body = _strip_comments(DASHBOARD)
    assert body.count("<MetricCard") == 3
    # Only the metrics row — the reviews list below it is a different kind of
    # card and builds its own header quite correctly.
    grid = body[body.find('className="grid gap-4 md:grid-cols-3"'):]
    grid = grid[:grid.find("</div>")]
    assert "<CardHeader" not in grid, "a metric card is still hand-rolled"


def test_the_zero_state_cta_leads_somewhere_specific():
    """"Get started" pointing at a hub is a second decision, not a first step."""
    for href in ('href="/connections"', 'href="/repositories"', 'href="/reviews"'):
        assert href in DASHBOARD


# ─── the banner ──────────────────────────────────────────────────────


def test_the_banner_no_longer_recites_the_sidebar():
    """Every locale carried a paragraph listing the navigation. It described
    what was already visible and answered no question anybody had."""
    assert "dash.tips.legend" not in _strip_comments(DASHBOARD)
    for locale in LOCALES:
        data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
        assert "dash.tips.legend" not in data, f"{locale} still carries the legend"


def test_the_steps_are_ticked_from_live_data():
    """A checklist that does not know what you have done is a poster. These
    read the same queries the cards above them do."""
    body = _strip_comments(DASHBOARD)
    assert "connectedCount > 0" in body
    assert "repoCount > 0" in body
    assert "history.data?.length ?? 0) > 0" in body


def test_a_finished_step_stays_visible():
    """Removing it silently shortens the list and loses the one thing that
    makes a checklist feel like progress."""
    assert "line-through" in _strip_comments(DASHBOARD)


def test_each_step_is_the_link_to_the_step():
    body = _strip_comments(DASHBOARD)
    start = body.find("const steps = [")
    assert start > 0, "the step list is gone"
    steps = body[start:body.find("];", start)]
    for href in ('"/connections"', '"/repositories"', '"/reviews"'):
        assert href in steps, f"step {href} is not clickable"


# ─── help where the question is asked ────────────────────────────────


def test_the_branch_answer_sits_on_the_scope_card():
    """"Why is a vulnerability I already fixed still listed" is asked while
    looking at the scope card, and was answered only inside a dialog behind a
    "?" in the page header."""
    body = _strip_comments(DEPENDENCIES)
    scope = body[body.find('t("deps.scopeTitle")'):]
    assert "<InlineHelp" in scope[:800], "no in-flow help on the scope card"
    assert "deps.helpBranchTitle" in scope[:800]


def test_the_engine_answer_sits_beside_the_engine_picker():
    body = _strip_comments(DEPENDENCIES)
    report = body[body.find('t("deps.reportTitle")'):]
    assert "deps.helpEngineTitle" in report[:1200], (
        "the engine caveat is not next to the picker it is about"
    )


def test_the_header_dialog_survives():
    """The in-flow notes are footnotes, read at a moment of doubt. The
    walkthrough is a different thing and both are wanted."""
    body = _strip_comments(DEPENDENCIES)
    assert "setHelpOpen(true)" in body
    assert 'DialogTitle>{t("deps.helpTitle")' in body


def test_inline_help_is_a_native_disclosure():
    """It prints, Ctrl+F finds the closed text, and it holds no state. A
    popover would lose all three."""
    source = (WEB / "components" / "ui" / "inline-help.tsx").read_text()
    assert "<details" in source
    assert "useState" not in source
    # Safari draws its own marker through a pseudo-element that list-none
    # alone does not remove.
    assert "webkit-details-marker" in source


# ─── the strings themselves ──────────────────────────────────────────


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_carries_the_new_keys(locale: str):
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [
        key for key in EN
        if key.startswith(("dash.zero.", "dash.next.")) and key not in data
    ]
    assert not missing, f"{locale} is missing {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_no_locale_falls_back_to_english_for_them(locale: str):
    """A key present in every file but holding the English string is the same
    outage as a missing key, and it passes a completeness test."""
    if locale == "en":
        pytest.skip("it is the English")
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    untranslated = [
        key for key in EN
        if key.startswith(("dash.zero.", "dash.next.")) and data.get(key) == EN[key]
    ]
    assert not untranslated, f"{locale} still shows English for {untranslated}"


def test_no_russian_reached_the_catalogue():
    """A hard product rule, and the locales nearest to it are the ones a
    machine translation slips into."""
    forbidden = ("ru.json",)
    assert not [p for p in MESSAGES.glob("*.json") if p.name in forbidden]
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for key in EN:
        if key.startswith(("dash.zero.", "dash.next.")):
            # ы, э, ъ do not occur in Ukrainian.
            assert not set("ыэъё") & set(uk[key].lower()), f"{key}: {uk[key]}"


# ─── the workspace switcher, and a trap that is invisible in markup ───


def test_the_switcher_does_not_use_a_fixed_click_away_layer():
    """It did, and on a phone the menu would not open.

    The top bar has `backdrop-blur`, and `backdrop-filter` makes an element the
    CONTAINING BLOCK for fixed-position descendants — so `fixed inset-0`
    covered the 44px bar rather than the viewport, landing exactly on the
    button that had just been tapped. One tap opened the menu and the same
    gesture's follow-up event hit the layer and closed it, which from outside
    is indistinguishable from "it does not open".

    Nothing about `<div className="fixed inset-0" />` looks wrong, which is why
    this is pinned rather than left to review.
    """
    shell = (WEB / "components" / "app-shell.tsx").read_text()
    i = shell.index("function WorkspaceSwitcher(")
    body = shell[i:shell.index("\nfunction ", i + 10)]
    assert 'className="fixed inset-0' not in body, (
        "the fixed click-away layer is back; it is trapped by backdrop-filter"
    )
    assert 'document.addEventListener("pointerdown"' in body
    # pointerdown, not click: it fires before the synthetic click that caused
    # the original problem.
    assert '"click"' not in body.split("addEventListener")[1][:120]


def test_the_menu_can_paint_above_the_drawer():
    """The sidebar drawer is z-40 — a lower value here slides the menu
    underneath it on a phone."""
    shell = (WEB / "components" / "app-shell.tsx").read_text()
    i = shell.index("function WorkspaceSwitcher(")
    body = shell[i:shell.index("\nfunction ", i + 10)]
    assert "z-50" in body


def test_the_menu_escapes_the_bar_entirely():
    """It broke twice from inside that box, each time invisibly from its own
    markup.

    First `backdrop-filter` on the bar, which makes the bar the containing
    block for fixed-position descendants — so a `fixed inset-0` click-away
    layer covered the 44px bar rather than the viewport and landed on the
    button that had just been tapped.

    Then the bar's `overflow-x-clip`. An absolutely-positioned panel hanging
    below a clipped ancestor is fine in Chrome, which implements
    `overflow-x: clip` with `overflow-y: visible` as the spec says. Where that
    pair is handled differently the y axis clips too, and the panel — which
    sits entirely below a 44px bar — is erased. Reported from a phone as "does
    not open"; not reproducible in Chrome, at any viewport.

    A portal has no ancestor to clip it, no stacking context to trap it, and no
    containing block to mismeasure it. Rendering it back beside the button
    reopens both bugs at once, so it is pinned.
    """
    shell = (WEB / "components" / "app-shell.tsx").read_text()
    i = shell.index("function WorkspaceSwitcher(")
    body = shell[i:shell.index("\nfunction ", i + 10)]
    assert "createPortal(" in body, "the panel is back inside the top bar"
    assert "document.body" in body

    stripped = _strip_comments(body)
    assert "absolute right-0 top-full" not in stripped, (
        "the panel is positioned against the bar again"
    )


def test_a_click_inside_the_portal_does_not_close_it_first():
    """With the panel in <body> it is no longer a descendant of the button's
    wrapper, so an outside-click test that only knows about the wrapper treats
    every workspace in the list as 'outside' and closes the menu on the same
    gesture that picks one."""
    shell = (WEB / "components" / "app-shell.tsx").read_text()
    i = shell.index("function WorkspaceSwitcher(")
    body = shell[i:shell.index("\nfunction ", i + 10)]
    assert "menuRef" in body, "the portal node is not tracked"
    onda = body[body.index("const onDown"):]
    assert "menuRef.current?.contains" in onda[:400], (
        "the outside-click test does not know about the portal, so choosing a "
        "workspace closes the menu instead"
    )
