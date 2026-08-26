"""The three artefacts a buyer asks for, where a buyer can find them.

They existed and took five steps to reach: Repositories → the Dependencies
sub-tab → run an audit → scroll a long page to the bottom → past the
Word/Markdown/Print row, below a divider. Neither the navigation, nor the
capability reference, nor any label outside those two buttons used the word
SBOM at all — so the most commercial thing the product does was reachable only
by accident, and only by somebody who already knew it was there.

The audit that prompted this put it plainly: everything addressed to a
developer was reachable, everything addressed to a buyer was not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
CARD = (WEB / "components" / "compliance-artifacts.tsx").read_text()
DEPS = (WEB / "app" / "(app)" / "dependencies" / "page.tsx").read_text()
SCENARIOS = (WEB / "components" / "scenario-cards.tsx").read_text()
MESSAGES = WEB / "lib" / "i18n" / "messages"
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))

LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))
KEYS = [k for k in EN if k.startswith("deps.artifacts")]


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


# ─── where it sits ───────────────────────────────────────────────────


def test_the_card_is_above_the_audit_not_below_it():
    """At the bottom of a long page, under a divider, after three other export
    buttons, is indistinguishable from absent."""
    body = _strip_comments(DEPS)
    card = body.index("<ComplianceArtifacts")
    scope = body.index('t("deps.scopeTitle")')
    findings = body.index('t("deps.hygieneTitle")')
    assert card < scope, "the artefacts card sank below the scope card"
    assert card < findings


def test_the_vault_is_presented_as_the_third_artefact():
    """It was produced, exported and never called what it is.

    An SBOM says what is in the product; the evidence pack says what we knew
    and when; the vault says how the thing works — the technical documentation
    the CRA asks for alongside the inventory. It is also the only one of the
    three that outlives the subscription, which is the strongest thing anyone
    can say about it and was said nowhere.
    """
    body = _strip_comments(CARD)
    assert "deps.artifactsVault" in body, "the vault is not on the card"
    assert 'href="/docs"' in body, "the card names it but does not open it"
    text = EN["deps.artifactsVaultWhat"]
    assert "CRA" in text or "Cyber Resilience" in text
    assert "subscription" in text.lower(), (
        "the durability argument — it keeps working after the subscription "
        "ends — is the reason this belongs beside the other two"
    )


def test_the_card_says_what_each_file_is():
    """"SBOM" and "evidence pack" are names for people who already know they
    need them. Somebody who does not should be able to learn it here rather
    than by downloading a zip to see what falls out."""
    body = _strip_comments(CARD)
    for key in ("deps.artifactsSbomWhat", "deps.artifactsEvidenceWhat"):
        assert key in body, f"{key} is not rendered"
    assert "deps.artifactsWhyAsk" in body, "nothing explains who asks for these"


def test_the_explanation_names_the_deadline_and_the_standard():
    """Vague compliance language is worth nothing to the person who has to
    decide whether this matters to them."""
    assert "CycloneDX" in EN["deps.artifactsSbomWhat"]
    body = EN["deps.artifactsWhyAskBody"]
    assert "Cyber Resilience Act" in body
    assert "2026" in body


def test_the_evidence_description_says_why_the_hashes_are_there():
    """A zip of files is not evidence. The hashes are what let a third party
    check it without trusting the tool that produced it — that is the whole
    claim, and stating it is the difference between a download and an
    argument."""
    text = EN["deps.artifactsEvidenceWhat"].lower()
    assert "sha256" in text
    assert "trust" in text


def test_downloads_go_through_the_authenticated_helper():
    """`<a href download>` is a browser navigation: no Authorization header,
    and every export endpoint reads that header and nothing else. Both of these
    buttons shipped that way once and downloaded a 401 body."""
    body = _strip_comments(CARD)
    assert "downloadWithAuth" in body
    assert "<a href=" not in body, "a bare anchor is back; it downloads a 401"


def test_the_buttons_explain_themselves_when_disabled():
    """Two greyed-out buttons with no sentence beside them is a dead end."""
    body = _strip_comments(CARD)
    assert "deps.artifactsNeedRun" in body
    assert "!runId" in body


# ─── the recipe that never mentioned it ──────────────────────────────


def test_the_capability_recipe_covers_the_artefacts():
    """It had four steps — table, CVEs, filter, fix-with-Claude — and stopped
    exactly before the part a non-developer comes to this page for."""
    match = re.search(r'\{ key: "deps",[^}]*nSteps: (\d+)', SCENARIOS)
    assert match, "the deps recipe card is gone"
    assert int(match.group(1)) >= 6, "the recipe still stops before the SBOM"

    for step in ("onboarding.g.deps.s5", "onboarding.g.deps.s6"):
        assert step in EN, f"{step} has no text"
    assert "SBOM" in EN["onboarding.g.deps.s5"]


def test_every_recipe_step_the_card_promises_has_text():
    """`nSteps` drives a loop over s1..sN. A number raised without adding the
    strings renders the raw key as a bullet."""
    match = re.search(r'\{ key: "deps",[^}]*nSteps: (\d+)', SCENARIOS)
    n = int(match.group(1))
    missing = [f"onboarding.g.deps.s{i}" for i in range(1, n + 1)
               if f"onboarding.g.deps.s{i}" not in EN]
    assert not missing, f"the card renders {missing} as raw keys"


# ─── the strings ─────────────────────────────────────────────────────


def test_there_are_strings_at_all():
    assert len(KEYS) >= 9, f"only {len(KEYS)} artefact keys"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_carries_them(locale):
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [k for k in KEYS if k not in data]
    missing += [k for k in ("onboarding.g.deps.s5", "onboarding.g.deps.s6")
                if k not in data]
    assert not missing, f"{locale} is missing {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_no_locale_shows_english(locale):
    if locale == "en":
        pytest.skip("it is the English")
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    untranslated = [k for k in KEYS if data.get(k) == EN[k]]
    assert not untranslated, f"{locale} still shows English for {untranslated}"


def test_the_placeholder_is_supplied():
    """A {repos} that nothing fills renders the braces to the reader."""
    assert "{repos}" in EN["deps.artifactsWhyAskBody"]
    assert "repos:" in _strip_comments(CARD)


def test_no_russian_reached_the_catalogue():
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for key in KEYS:
        assert not set("ыэъё") & set(uk[key].lower()), f"{key}: {uk[key]}"
