"""The fixed text Celmis writes on a pull request comes from one catalog.

`en` and `uk`, the same keys with the same placeholders in both; a language
with no catalog, and a key a catalog lacks, fall back to English instead of
breaking a comment.
"""

from __future__ import annotations

import re

import pytest

from src.review import messages
from src.review.messages import EN, UK, plural_form, t, tn
from src.review.orchestrator import _lost_diff_note
from src.review.providers.base import (
    _format_feedback_comment,
    _format_started_comment,
    _format_status_comment,
)
from src.review.settings import ReviewSettings
from tests.review.test_the_pr_says_what_it_does import _pr

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _base(key: str) -> str:
    return key.rsplit(".", 1)[0] if key.rsplit(".", 1)[-1] in ("one", "few", "many", "other") else key


def test_the_two_catalogs_have_the_same_keys_and_placeholders() -> None:
    plain_en = {k for k in EN if _base(k) == k}
    plain_uk = {k for k in UK if _base(k) == k}
    assert plain_en == plain_uk
    for key in plain_en:
        assert sorted(PLACEHOLDER.findall(EN[key])) == sorted(PLACEHOLDER.findall(UK[key])), key
    # Plural groups: the same group exists in both, and each form of a group
    # carries the same placeholders.
    groups_en = {_base(k) for k in EN if _base(k) != k}
    assert groups_en == {_base(k) for k in UK if _base(k) != k}
    for group in groups_en:
        wanted = {frozenset(PLACEHOLDER.findall(v)) for k, v in {**EN, **UK}.items()
                  if _base(k) == group}
        assert len(wanted) == 1, group


def test_every_ukrainian_text_is_ukrainian() -> None:
    for key, text in UK.items():
        assert re.search(r"[А-Яа-яІіЇїЄєҐґ]", text), f"{key} is not translated"


def test_an_unknown_language_and_a_missing_key_fall_back_to_english() -> None:
    assert t("started.title", "xx") == EN["started.title"]
    assert t("started.title", None) == EN["started.title"]
    assert t("started.title", "uk-UA") == UK["started.title"]
    assert t("no.such.key", "uk") == "no.such.key"


def test_a_placeholder_nobody_filled_is_left_visible_not_crashed() -> None:
    assert t("status.skipped", "en") == EN["status.skipped"]
    assert t("status.skipped", "en", reason="draft") == "### ⏭️ Skipped: draft"


@pytest.mark.parametrize("n, form", [(1, "one"), (2, "few"), (4, "few"), (5, "many"),
                                     (11, "many"), (21, "one"), (22, "few"), (112, "many")])
def test_ukrainian_plurals_follow_the_cldr_rules(n, form) -> None:
    assert plural_form(n, "uk") == form


def test_english_plurals_are_one_and_other() -> None:
    assert [plural_form(n, "en") for n in (0, 1, 2)] == ["other", "one", "other"]


def test_a_lost_diff_is_said_with_the_right_plural_in_both_languages() -> None:
    assert "1 changed file but" in _lost_diff_note(1)
    assert "8 changed files but" in _lost_diff_note(8, "en")
    assert "1 змінений файл," in _lost_diff_note(1, "uk")
    assert "3 змінені файли," in _lost_diff_note(3, "uk")
    assert "8 змінених файлів," in _lost_diff_note(8, "uk")
    assert tn("lost_diff", 5, "de", files=5) == _lost_diff_note(5)


def test_the_lifecycle_comments_follow_the_review_language() -> None:
    pr = _pr("github", "o/r", 1)
    started = _format_started_comment(pr, agents=["defect"], started_at="now", language="uk")
    assert "перевіряє цей PR" in started and "Агенти" in started
    assert "Celmis is reviewing" in _format_started_comment(
        pr, agents=["defect"], started_at="now")
    failed = _format_status_comment(pr, outcome="failed", reason="boom", language="uk")
    assert "Рев'ю не вдалося: boom" in failed
    skipped = _format_status_comment(pr, outcome="skipped", reason="draft")
    assert "### ⏭️ Skipped: draft" in skipped
    note = _format_feedback_comment(pr, reason="draft", language="uk")
    assert "не перевіряв" in note and "`abcdef1`" in note


def test_the_language_of_a_repository_beats_the_workspaces() -> None:
    assert messages.resolve_language("uk", "ws") == "uk"
    assert messages.resolve_language("", None) == "en"
    assert messages.resolve_language("pl") == "en"  # no Polish catalog: English


def test_the_install_has_a_marker_style_and_a_bot_handle() -> None:
    settings = ReviewSettings()
    assert settings.marker_style == "auto"
    assert settings.bot_handle == "@celmis"
    with pytest.raises(ValueError):
        ReviewSettings(bot_handle="celmis")
    with pytest.raises(ValueError):
        ReviewSettings(marker_style="loud")
