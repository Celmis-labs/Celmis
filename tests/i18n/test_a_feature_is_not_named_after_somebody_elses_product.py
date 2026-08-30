"""The button was called "Fix with Claude". That is a feature name.

Anthropic's terms (code.claude.com/docs/en/legal-and-compliance) draw the
line in one sentence: "You can accurately say, in plain text, that your
product has Claude Code preinstalled or that it runs Claude Code. But you
can't use the Claude Code or Anthropic names or logos as part of your own
product, feature, or company name."

So every DESCRIPTIVE string stays — "Claude Code (subscription)" naming which
engine runs, "Claude Code agent — researches the code", the page that exists
to connect a Claude account. What had to go is the name of a thing this
product does: an action button, in sixteen languages, plus every sentence
that quoted it.

WHAT THIS TEST CANNOT DECIDE. Whether a phrase reads as a product name is a
judgement, and no assertion makes it. This is keyed on the one name that was
removed, in every spelling the sixteen locales gave it — a regression guard,
not a rule. Two of them had left the English label inside otherwise
translated sentences, which is why the English spelling is checked in every
file rather than only in `en`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MESSAGES = Path(__file__).resolve().parents[2] / "web" / "lib" / "i18n" / "messages"

#: How each locale spelled the old label. The English one is checked
#: everywhere: `alertGuide.in.grafana.s4` carried it verbatim in thirteen
#: translated files.
RETIRED = {
    "cs": ("Opravit s Claudem",),
    "de": ("Mit Claude beheben",),
    "en": (),
    "es": ("Corregir con Claude",),
    "fr": ("Corriger avec Claude",),
    "it": ("Correggi con Claude",),
    "ja": ("Claude で修正",),
    "ko": ("Claude로 수정",),
    "nl": ("Oplossen met Claude",),
    "pl": ("Napraw z Claude",),
    "pt": ("Corrigir com o Claude",),
    "ro": ("Repară cu Claude",),
    "sk": ("Opraviť s Claude",),
    "tr": ("Claude ile düzelt",),
    "uk": ("Виправити з Claude",),
    "zh": ("用 Claude 修复",),
}

EVERYWHERE = ("Fix with Claude",)

LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))


def test_there_are_locales_to_check() -> None:
    assert len(LOCALES) >= 16, f"only found {LOCALES}"


@pytest.mark.parametrize("locale", LOCALES)
def test_the_retired_label_is_gone(locale: str) -> None:
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    banned = EVERYWHERE + RETIRED.get(locale, ())
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        for phrase in banned:
            assert phrase not in value, (
                f"{locale}.json :: {key} still names the feature "
                f"{phrase!r}. Anthropic's terms forbid the mark in a feature "
                f"name; the label is {'Fix from here'!r} now."
            )


@pytest.mark.parametrize("locale", LOCALES)
def test_the_replacement_is_present_and_translated(locale: str) -> None:
    """Present in every locale, and not left in English in fifteen of them."""
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    for key in ("alerts.fixFromHere", "deps.fixFromHere"):
        assert key in data, f"{locale}.json has no {key}"
        assert data[key].strip(), f"{locale}.json :: {key} is empty"
    if locale not in ("en",):
        assert data["alerts.fixFromHere"] != "Fix from here", (
            f"{locale}.json left the label in English — the file it replaced "
            f"had that problem in two of its three keys"
        )


def test_describing_what_the_product_runs_is_untouched() -> None:
    """The permitted half. If this fails the rename went too far.

    Saying a product runs Claude Code is explicitly allowed, and these
    strings are how a person finds out what they are connecting to.
    """
    data = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    assert data["claude.title"] == "Claude Code"
    assert "Claude Code" in data["settings.llm.engineClaude"]
    assert "Claude" in data["claude.connectButton"]


# ─── the same rule, one level up: our own sections ───────────────────
#
# The button was renamed and the navigation entry with it, and the sentences
# UNDERNEATH them kept the mark: "Dispatch a bug straight to the Claude
# agent", "Claude agent: code-editing sessions on your repos". The guard above
# did not see them because it is keyed on the button.
#
# Word order made it worse than it looked. A scan for "Claude agent" found
# seven locales of sixteen; the other nine say "agentovi Claude",
# "à l'agent Claude", "al agente de Claude" — the same construction, inflected.
#
# NOT every mention: "Claude Code agent — researches the code" names
# Anthropic's product and says what it does, which the terms permit in as many
# words. These are the keys that label a section of OURS.

OUR_SECTIONS = (
    "nav.agent",
    "tour.step.agent.title",
    "tour.step.agent.desc",
    "alerts.subtitle",
)

MARKS = ("Claude", "Клод", "クロード", "클로드", "克劳德", "克勞德", "Anthropic")


@pytest.mark.parametrize("locale", LOCALES)
def test_our_own_sections_are_not_named_after_the_mark(locale: str) -> None:
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    for key in OUR_SECTIONS:
        assert key in data, f"{locale}.json has no {key}"
        value = data[key]
        for mark in MARKS:
            assert mark not in value, (
                f"{locale}.json :: {key} names a section of ours with "
                f"{mark!r}: {value!r}. Saying the product runs Claude Code is "
                f"permitted; naming your own section with the mark is not."
            )


def test_naming_anthropics_product_is_still_allowed() -> None:
    """The other half, so a future sweep that goes too far fails here.

    "Claude Code agent — researches the code" is an engine the operator picks
    between; erasing the mark there would leave them guessing what they chose.
    """
    data = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    assert "Claude Code" in data["settings.llm.docsEngineAgent"]
    assert "Claude Code" in data["repositories.vaultEngineAgent"]

