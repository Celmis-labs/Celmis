"""The notice about a stage that did not run is in the review language, and counts
only what the pull request is told.

THE DEFECT. A repository reviewing in Ukrainian with the comment threshold at
"error" got, when one non-critical stage failed:

    ⚠ PARTIAL REVIEW — quality did not run. 1 other stage completed; its 3
    comments are below.

in English, and "3 comments" were findings under the threshold that the
comment never lists: a number with nothing below it. The failure reasons, the
"thinner than a full review" note and the "ADJUSTED" lines were English too.

THE RULE. Every sentence of the notice comes from the message catalogue, and
the count is of the findings at or above the threshold. English stays exactly
as it was.
"""

from __future__ import annotations

import re

import pytest

from src.llm.capabilities import (
    ADJUST_CLAMPED,
    ADJUST_DROPPED,
    ADJUST_SWAPPED,
    PARAM_MAX_OUTPUT_TOKENS,
    PARAM_MODEL,
    PARAM_REASONING,
    PARAM_TEMPERATURE,
    ParameterAdjustment,
)
from src.llm.errors import curated_reason, reason_sentences
from src.review import messages
from src.review.models import FindingSeverity, ReviewBatch
from src.review.providers.base import _failure_headline
from tests.review.test_the_pr_says_what_it_does import _finding, _pr

ERROR, WARNING = FindingSeverity.ERROR, FindingSeverity.WARNING
CYRILLIC = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")
#: Words of the English templates; none may survive in a Ukrainian notice.
ENGLISH = ("did not run", "PARTIAL", "FAILED", "SKIPPED", "ADJUSTED", "other stage",
           "reviewed", "approval", "thinner", "completed", "below")


def _batch(language, *, findings=(), threshold=None, run=("security",), failed=(),
           errors=None, **kw) -> ReviewBatch:
    batch = ReviewBatch(
        pull_request=_pr("github", "o/r", 1),
        findings=[_finding(s, line=11 + i, title=f"T{i}") for i, s in enumerate(findings)],
        agents_run=list(run), agents_failed=list(failed), agent_errors=dict(errors or {}), **kw)
    batch.review_language = language
    batch.comment_min_severity = threshold
    return batch


# ─── the count ──────────────────────────────────────────────────────


def test_the_notice_counts_the_comments_the_pull_request_is_told_not_all_that_were_found() -> None:
    quiet = _batch("en", findings=[WARNING, WARNING, WARNING], threshold="error",
                   run=["security"], failed=["quality"])
    assert quiet.partial_banner == (
        "⚠ PARTIAL REVIEW — quality did not run. "
        "The 1 other stage that ran found nothing to comment on.\n\n")
    assert "comments are below" not in quiet.partial_banner

    told = _batch("en", findings=[ERROR, WARNING, WARNING], threshold="error",
                  run=["security", "defect"], failed=["quality"])
    assert told.partial_banner == (
        "⚠ PARTIAL REVIEW — quality did not run. "
        "2 other stages completed; their 1 comment is below.\n\n")


# ─── English is what it was ─────────────────────────────────────────


@pytest.mark.parametrize(("kwargs", "expected"), [
    (dict(findings=[ERROR, ERROR], run=["security"], failed=["quality"]),
     "⚠ PARTIAL REVIEW — quality did not run. "
     "1 other stage completed; its 2 comments are below."),
    (dict(findings=[ERROR], run=["security", "defect", "docs"], failed=["quality"]),
     "⚠ PARTIAL REVIEW — quality did not run. "
     "3 other stages completed; their 1 comment is below."),
    (dict(run=["security", "defect"], failed=["quality"]),
     "⚠ PARTIAL REVIEW — quality did not run. "
     "The 2 other stages that ran found nothing to comment on."),
    (dict(run=["security"], failed=["architect"]),
     "⚠ PARTIAL REVIEW — architect did not run. architect is a critical stage, "
     "so this review cannot be an approval."),
    (dict(run=["architect", "security"], failed=["architect", "security"]),
     "⚠ PARTIAL REVIEW — architect, security did not run. architect, security "
     "are critical stages, so this review cannot be an approval."),
    (dict(run=[], failed=["architect"]),
     "⚠ REVIEW FAILED — architect did not run, and no other stage produced an "
     "answer either. This pull request has NOT been reviewed."),
])
def test_the_english_notice_is_word_for_word_what_it_was(kwargs, expected) -> None:
    assert _batch(None, **kwargs).partial_banner == expected + "\n\n"


def test_the_english_skip_notice_is_unchanged() -> None:
    assert _batch(None, run=[], failed=[]).partial_banner == (
        "⚠ REVIEW SKIPPED — no agent ran: every agent is disabled for this "
        "repository (see /review-settings). This pull request has NOT been "
        "reviewed. Re-enable at least one agent to resume reviews.\n\n")


# ─── Ukrainian is Ukrainian ─────────────────────────────────────────


@pytest.mark.parametrize("kwargs", [
    dict(run=[], failed=[]),
    dict(run=[], failed=["architect"]),
    dict(run=["security"], failed=["architect"]),
    dict(run=["architect", "security"], failed=["architect", "security"]),
    dict(findings=[ERROR, ERROR], run=["security"], failed=["quality"]),
    dict(findings=[ERROR], run=["security", "defect", "docs"], failed=["quality"]),
    dict(run=["security", "defect"], failed=["quality"]),
    dict(findings=[WARNING], threshold="error", run=["security"], failed=["quality"]),
])
def test_every_arm_of_the_notice_is_ukrainian(kwargs) -> None:
    notice = _batch("uk", **kwargs).partial_banner
    assert CYRILLIC.search(notice), notice
    for word in ENGLISH:
        assert word not in notice, (word, notice)


def test_the_ukrainian_notice_keeps_its_numbers_and_names() -> None:
    notice = _batch("uk", findings=[ERROR, ERROR], run=["security", "defect"],
                    failed=["quality"]).partial_banner
    assert "quality" in notice and "(2)" in notice and ": 2." in notice


# ─── the reasons ────────────────────────────────────────────────────


def test_every_curated_reason_has_a_ukrainian_wording() -> None:
    assert set(reason_sentences()) == set(messages.REASONS_UK)
    for code, text in messages.REASONS_UK.items():
        assert CYRILLIC.search(text), code


def test_a_curated_reason_is_said_in_the_review_language_and_the_record_keeps_english() -> None:
    english = curated_reason("quota_exhausted")
    assert english == "provider quota exhausted"
    assert messages.localise_reason(english, "uk") == "квоту провайдера вичерпано"
    assert messages.localise_reason(english, "en") == english
    assert messages.localise_reason(english, None) == english

    batch = _batch("uk", run=["security"], failed=["defect"], errors={"defect": english})
    assert batch.agent_errors["defect"] == english
    assert "квоту провайдера вичерпано" in batch.partial_banner
    assert english not in batch.partial_banner


def test_a_sentence_after_a_known_reason_and_an_unknown_reason_are_left_as_written() -> None:
    refused = curated_reason("model_refused") + ": it declined"
    assert messages.localise_reason(refused, "uk") == (
        "модель відмовилась перевіряти цю зміну: it declined")
    assert messages.localise_reason("a stage's own words", "uk") == "a stage's own words"
    assert messages.localise_reason("", "uk") == ""


def test_the_failed_headline_has_no_english_left_in_a_ukrainian_review() -> None:
    reasoned = _batch("uk", run=[], failed=["defect"],
                      errors={"defect": curated_reason("rate_limited")})
    assert _failure_headline(reasoned, "uk") == (
        "### ❌ Рев'ю не вдалося: провайдер обмежує частоту запитів цього workspace")

    bare = _batch("uk", run=[], failed=["defect", "security"])
    assert _failure_headline(bare, "uk") == (
        "### ❌ Рев'ю не вдалося: жоден етап рев'ю не завершився (defect, security)")
    assert _failure_headline(bare, None) == (
        "### ❌ Review failed: no review stage completed (defect, security)")

    nothing = _batch("uk", run=[], failed=[])
    assert _failure_headline(nothing, "uk").endswith("жоден етап рев'ю не завершився")
    assert _failure_headline(nothing, None).endswith("no review stage completed")


# ─── the other two notes ────────────────────────────────────────────

CLAMP = ParameterAdjustment("security", PARAM_MAX_OUTPUT_TOKENS, 100000, 65535,
                            ADJUST_CLAMPED, "model ceiling is 65535", "m1")
REASONING = ParameterAdjustment("defect", PARAM_REASONING, "minimal", None,
                                ADJUST_DROPPED, "", "m1")
TEMPERATURE = ParameterAdjustment("defect", PARAM_TEMPERATURE, 0.2, None,
                                  ADJUST_DROPPED, "", None)
SWAP = ParameterAdjustment("architect", PARAM_MODEL, "big", "small", ADJUST_SWAPPED, "", None)
GRAPH = ParameterAdjustment(None, "graph_context", None, "3/5 files", "partial", "", None)
GRAPH_DOWN = ParameterAdjustment(None, "graph_context", None, None, "unavailable", "", None)
REPLY = ParameterAdjustment("defect", "agent_reply", None, None, "near_empty", "", None)
UNKNOWN = ParameterAdjustment("defect", "top_p", 1, 0.5, "clamped_somehow", "", None)


@pytest.mark.parametrize("adjustment", [CLAMP, REASONING, TEMPERATURE, SWAP, GRAPH, GRAPH_DOWN,
                                        REPLY, UNKNOWN],
                         ids=lambda a: f"{a.parameter}-{a.action}")
def test_an_adjustment_is_said_in_the_review_language(adjustment) -> None:
    uk = _batch("uk", parameter_adjustments=[adjustment]).adjustments_notice
    assert uk.startswith("⚙ ЗМІНЕНО — ") and "ADJUSTED" not in uk
    assert CYRILLIC.search(uk[len("⚙ ЗМІНЕНО — "):]), uk
    en = _batch(None, parameter_adjustments=[adjustment]).adjustments_notice
    assert en.startswith("⚙ ADJUSTED — ") and en.endswith(".\n\n")


def test_the_english_adjustments_are_word_for_word_what_they_were() -> None:
    notice = _batch(None, parameter_adjustments=[CLAMP, REASONING, SWAP, REPLY, GRAPH]
                    ).adjustments_notice
    assert notice.splitlines()[:5] == [
        "⚙ ADJUSTED — max_output_tokens 100000 was above what m1 accepts and was "
        "cut to 65535 for security: model ceiling is 65535.",
        "⚙ ADJUSTED — reasoning 'minimal' was refused by m1 and the review ran "
        "without it for defect.",
        "⚙ ADJUSTED — architect ran on the fallback model small instead of big.",
        "⚙ ADJUSTED — defect gave a near-empty reply.",
        "⚙ ADJUSTED — graph context partial (3/5 files).",
    ]


def test_a_thinner_review_is_said_in_the_review_language() -> None:
    kw = dict(run=["security"], agents_skipped=["second_pass"],
              errors={"second_pass": curated_reason("local_timeout")})
    uk = _batch("uk", **kw).partial_banner
    assert uk.startswith("ℹ Це рев'ю неповне: second_pass (запит перевищив")
    en = _batch(None, **kw).partial_banner
    assert en.startswith("ℹ This review is thinner than a full one: second_pass (the request passed")
