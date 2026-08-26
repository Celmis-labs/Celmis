"""The self-hosted answer says what the reply told it, not what the page
remembers being true.

THE SPLIT LIVED TWICE, AND ONLY THE UNREAD COPY WAS DERIVED. `execute()` works
out which surfaces a workspace admin can point at their own server from the
rule that actually refuses a `base_url` at save time, and puts the two lists on
the wire. Nothing read them. The page recited the same division as prose, in
sixteen dictionaries, and the two agreed only because nobody had moved the rule
yet. The morning embeddings become a dropdown, the derived copy follows and the
recited one starts lying — in every language at once, to the reader least able
to check.

So the paragraphs no longer name a surface, and the names come from the reply.
That is the whole point of the marker: it is the one thing the setup guide
cannot say, because it is not about commands, it is about who is allowed to
type them.

A RUN IS A ROW. Replies are written to the history table and re-rendered on
every visit, which cuts both ways here. Old rows carry neither field, so the
page must be able to say nothing — and no row may carry the commands, because
a copy stored there would still be showing last month's flags next year.

AND THE PLACE IT SENDS YOU TO MUST EXIST. The instruction is a breadcrumb and
a quoted dropdown entry; both are only worth reading if the words turn up on
the screen at the end of them. In Czech they did not: the crumb said
"Konfigurace LLM" and the page says "Nastavení LLM".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
AUTOMATION = (WEB / "app" / "(app)" / "automation" / "page.tsx").read_text(encoding="utf-8")
LLM_SETTINGS = (WEB / "app" / "(app)" / "settings" / "llm" / "page.tsx").read_text(encoding="utf-8")
MESSAGES = WEB / "lib" / "i18n" / "messages"

LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))

#: The four card titles, as the settings page spells them — the words the
#: agent's list has to reuse, because that list is an instruction to go and
#: find them.
CARD_TITLE_KEYS = (
    "settings.llm.chatTitle", "settings.llm.reviewTitle",
    "settings.llm.agentTitle", "settings.llm.embeddingsTitle",
)


def _strip_comments(source: str) -> str:
    """Comments here NAME what they removed in order to explain the absence —
    "the setup guide is NOT read out of the reply" contains every spelling the
    tests below check is gone. Grepping them is how a guard passes while the
    thing it guards has been put back."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def _body(source: str, signature: str) -> str:
    """One top-level declaration, from its signature to the closing brace that
    is alone on its own line — not the `}: {` that ends a destructured
    parameter list, which is the first `}` in column one in every one of
    these."""
    start = source.index(signature)
    end = source.index("\n}\n", start)
    return source[start:end]


def _dict(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))


EN = _dict("en")
AUTOMATION_CODE = _strip_comments(AUTOMATION)
SETTINGS_CODE = _strip_comments(LLM_SETTINGS)


def test_there_are_locales_to_check():
    """Guards the guard: a bad glob makes every per-locale case vacuous."""
    assert len(LOCALES) >= 14, LOCALES


# ─── the split comes off the wire ────────────────────────────────────


def test_the_page_reads_the_surfaces_the_reply_named():
    """Both fields, or the page is back to knowing the answer by itself."""
    assert "result.ui_surfaces" in AUTOMATION_CODE
    assert "result.env_surfaces" in AUTOMATION_CODE


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("paragraph", ["what", "where"])
def test_the_prose_does_not_recite_which_surface_is_whose(locale: str, paragraph: str):
    """Neither the pitch nor the instruction may list the cards. Named there
    it is a second copy — sixteen of them — that agrees today and cannot be
    corrected by the rule that made it wrong. `what` said "chat, PR review and
    this agent can run on a model you host yourself", which is the same claim
    the marker makes, worded so it reads as background."""
    messages = _dict(locale)
    prose = messages[f"automation.selfHosted.{paragraph}"].lower()
    for key in CARD_TITLE_KEYS:
        assert messages[key].lower() not in prose, (
            f"{locale}: automation.selfHosted.{paragraph} names the "
            f"{messages[key]} card in prose"
        )


@pytest.mark.parametrize("locale", LOCALES)
def test_the_exception_paragraph_does_not_name_the_dropdown_surfaces(locale: str):
    """The paragraph explains WHY one surface is not a workspace choice. Which
    ones can be chosen is the marker's to say."""
    messages = _dict(locale)
    body = messages["automation.selfHosted.embeddings"].lower()
    for key in ("settings.llm.chatTitle", "settings.llm.reviewTitle",
                "settings.llm.agentTitle"):
        assert messages[key].lower() not in body, (
            f"{locale}: automation.selfHosted.embeddings names the "
            f"{messages[key]} card in prose"
        )


def test_the_exception_paragraph_does_not_name_the_pinned_surface():
    """Checked on the master copy the other fifteen are translated from.
    Elsewhere "embedding server" — the machine the code is sent to, which is
    true whoever configured it — is the same word as the card's name in some
    languages, and this cannot tell them apart."""
    body = EN["automation.selfHosted.embeddings"].lower()
    assert EN["settings.llm.embeddingsTitle"].lower() not in body


def test_the_names_printed_are_the_names_on_the_cards():
    """"Open the card for the part you want to move" is only followable if the
    list under it uses the words those cards actually carry."""
    block = AUTOMATION_CODE[AUTOMATION_CODE.index("const SURFACE_TITLES"):]
    block = block[:block.index("};")]
    spelled = dict(re.findall(r'(\w+): "([\w.]+)"', block))

    on_the_page = dict(re.findall(
        r'surface="(\w+)".*?title=\{t\("([\w.]+)"\)\}', SETTINGS_CODE, flags=re.S))
    assert on_the_page, "the settings page no longer titles its cards with keys"
    assert spelled == on_the_page, (
        "the agent spells a surface differently from its own settings card"
    )


def test_every_surface_the_backend_can_send_has_a_name():
    """A surface in neither list is one the answer silently omits; a surface in
    a list with no card title of its own is printed as its slug, which is at
    least honest. Neither should be needed for the four that ship."""
    from src.llm.profiles import PROFILE_NAMES

    block = AUTOMATION_CODE[AUTOMATION_CODE.index("const SURFACE_TITLES"):]
    block = block[:block.index("};")]
    spelled = dict(re.findall(r'(\w+): "([\w.]+)"', block))
    assert set(spelled) == set(PROFILE_NAMES)
    for key in spelled.values():
        assert key in EN, key


def test_a_reply_written_before_the_marker_existed_still_reads():
    """Those rows are in the history table and re-render on every visit. They
    carry neither field, and a page that filled the gap with today's division
    would be putting an answer under a question that was never given it."""
    names = _body(AUTOMATION_CODE, "function surfaceNames")
    assert "Array.isArray(raw)" in names, "a missing field would be iterated"
    assert "return []" in names

    line = _body(AUTOMATION_CODE, "function SurfaceList")
    assert "names.length === 0" in line and "return null" in line, (
        "an empty list would render its label with nothing after it"
    )


# ─── the commands stay at the endpoint that owns them ────────────────


@pytest.mark.parametrize("spelling", [
    "result.guide", "setup_guide", "local_setup_guide", "guideFrom",
])
def test_the_commands_are_not_read_out_of_the_reply(spelling: str):
    """Marker-only is the contract, deliberately: a run's result is written to
    its row, so a guide carried there would be frozen at the day it was asked.
    The page had a reader for three field names the backend never sends —
    thirty-five lines that could only ever return null, and that would quietly
    start preferring a stale copy the moment somebody "helpfully" added one."""
    assert spelling not in AUTOMATION_CODE


def test_the_guide_is_fetched_on_the_key_the_settings_page_uses():
    """One document, one cache entry: somebody who opened it under Settings
    should not wait for it a second time under a reply."""
    assert '"llm-local-setup-guide"' in AUTOMATION_CODE
    assert '"llm-local-setup-guide"' in _strip_comments(
        (WEB / "components" / "local-setup-guide.tsx").read_text(encoding="utf-8"))


@pytest.mark.parametrize("state", ["isLoading", "error", "data"])
def test_the_answer_accounts_for_every_state_of_that_fetch(state: str):
    """The failure state was the one missing, and it was the one the prose
    pointed at: the paragraph promised "the variables below" and a failed
    fetch rendered nothing at all under it. The reader saw a sentence, then
    the end of the answer, with no way to tell something was missing.

    Matched as the CONDITION that puts it on screen — mentioning `guide.error`
    while rendering it behind something else is how this comes back."""
    panel = _body(AUTOMATION_CODE, "function SelfHosted")
    assert re.search(rf"guide\.{state}\s*(?:&&|\?)", panel), (
        f"the {state} state of the setup-guide fetch reaches nobody"
    )


def test_the_prose_no_longer_promises_a_block_that_may_not_arrive():
    """Even with the error shown, a sentence pointing DOWN at variables is
    wrong whenever they are not there. It says what pins them instead."""
    assert "below" not in EN["automation.selfHosted.embeddings"].lower()


# ─── the dropdown entry, and the sentence that quotes it ─────────────


def test_the_provider_entry_is_translated():
    """Fourteen of sixteen dictionaries already translated that parenthetical
    inside the instruction naming it, so the option itself was the one English
    string in the sentence's own subject."""
    assert '"Self-hosted (OpenAI-compatible)"' not in SETTINGS_CODE
    assert "settings.llm.selfHostedOption" in SETTINGS_CODE


@pytest.mark.parametrize("locale", LOCALES)
def test_the_sentence_quotes_the_entry_rather_than_spelling_it(locale: str):
    """One string in the dropdown and in the instruction that sends you to it.
    Spelled twice they drift, and the drift is invisible to whoever ships it:
    it reads correctly in the language they speak."""
    messages = _dict(locale)
    assert "{option}" in messages["automation.selfHosted.where"]
    assert "{option}" in messages["settings.llm.selfHostedHint"]


@pytest.mark.parametrize("locale", LOCALES)
def test_openai_compatible_stays_recognisable(locale: str):
    """It is what the protocol is called, and the reason a reader knows their
    server qualifies. Translating it away leaves a menu entry that matches
    nothing in their server's documentation."""
    label = _dict(locale)["settings.llm.selfHostedOption"]
    assert "Self-hosted" in label, label
    assert "OpenAI" in label, label


# ─── the breadcrumb leads somewhere that says those words ────────────


@pytest.mark.parametrize("locale", LOCALES)
def test_the_breadcrumb_names_the_page_it_leads_to(locale: str):
    """A person following "Nastavení → Konfigurace LLM" arrives at a page
    titled "Nastavení LLM" and has to guess whether they are in the right
    place. The crumb is only navigation if its words are on the screen at the
    end of it."""
    messages = _dict(locale)
    where = messages["automation.selfHosted.where"]
    assert messages["nav.settings"] in where, (
        f"{locale}: the crumb does not name the sidebar entry"
    )
    assert messages["settings.llm.pageTitle"] in where, (
        f"{locale}: the crumb names a page title that is not on that page"
    )
