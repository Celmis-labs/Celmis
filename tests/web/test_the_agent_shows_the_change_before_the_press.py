"""A review change is confirmed by looking at it, not at its name.

"Add review rules" on a card with a Confirm button says nothing about WHICH
rules, where they land, or whether they are live the moment the button is
pressed. The card shows the change itself — the rules as stored, the setting
and its new value — and after the press, where to see it. Read with comments
stripped, like the conversation tests next door.
"""

from __future__ import annotations

import json

import pytest

from tests.web.test_the_agent_is_a_conversation import (
    CODE,
    MESSAGES,
    REPLY_BODY,
    _array,
    _body,
    _squash,
)

PREVIEW_BODY = _squash(_body(CODE, "ChangePreview"))
OUTCOME_BODY = _squash(_body(CODE, "ConfigOutcome"))
CONFIG_KEYS = sorted(
    k for k in json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    if k.startswith("automation.config."))


def test_the_page_knows_the_same_config_verbs_as_the_server():
    from src.automation.chat import CONFIG_VERBS

    assert _array(CODE, "CONFIG_VERBS") == list(CONFIG_VERBS)


def test_the_plan_card_renders_the_change_for_a_config_step():
    assert "<ChangePreview preview={s.preview} said={said} />" in REPLY_BODY
    # Still inside the per-step block, before the Confirm button.
    assert REPLY_BODY.index("<ChangePreview") < REPLY_BODY.index('t("automation.confirm")')


def test_every_rule_is_shown_whole_before_the_press():
    for field in ("r.title", "r.severity", "r.path_glob", "r.agents",
                  "r.instructions"):
        assert field in PREVIEW_BODY, f"the card hides a rule's {field}"


def test_the_card_says_whether_the_rules_wait_or_go_live():
    assert 'said("automation.config.willPending")' in PREVIEW_BODY
    assert 'said("automation.config.willActive")' in PREVIEW_BODY
    assert "preview.status === \"pending\"" in PREVIEW_BODY


def test_a_setting_is_shown_with_its_new_value():
    assert "preview.key" in PREVIEW_BODY
    assert "settingValue(preview.value, said)" in PREVIEW_BODY


def test_the_outcome_links_only_into_the_app():
    assert "isInAppHref(l.href)" in OUTCOME_BODY
    assert "CONFIG_LINK_LABELS[l.label]" in OUTCOME_BODY


def test_a_config_only_run_is_not_reported_as_started_on_n_repositories():
    assert "isConfigOnly(run.steps) && (" in REPLY_BODY
    assert "<ConfigOutcome result={run.result} said={said} />" in REPLY_BODY


@pytest.mark.parametrize("locale", sorted(p.stem for p in MESSAGES.glob("*.json")))
def test_every_locale_has_the_config_strings(locale):
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    assert CONFIG_KEYS
    missing = [k for k in CONFIG_KEYS if not data.get(k, "").strip()]
    assert not missing, f"{locale} is missing {missing}"


def test_ukrainian_is_written_not_copied():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    echoed = [k for k in CONFIG_KEYS if uk[k] == en[k]]
    assert not echoed, echoed
