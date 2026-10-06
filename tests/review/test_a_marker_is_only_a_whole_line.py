"""A marker is a whole line of its own, outside code, whatever line ends the text has.

Three ways the invisible markers could go wrong on text we do not control: a
model's finding that QUOTES a marker (Celmis reviewing its own repository), a
description a person saved from a web form (CRLF), and a description so long
that the hidden form no longer fits. None may turn a mention into a marker,
lose a real marker, or tear the description block from its end.
"""

from __future__ import annotations

import pytest

from src.review import markers
from src.review.markers import bitbucket_flavour, fit, has_marker, hide, reveal
from src.review.models import ReviewBatch
from src.review.pr_actions import (
    SUMMARY_HEADING,
    compose_description,
    description_insights,
    split_description,
)
from tests.review.test_a_rerun_does_not_double_the_comments import settings  # noqa: F401 — fixture
from tests.review.test_the_pr_says_what_it_does import _pr

FINDING = "<!-- celmis:finding fp=0123456789abcdef sha=0123456789ab -->"


@pytest.fixture(autouse=True)
def _fresh_marker_style():
    markers.reset_style_fallback()
    yield
    markers.reset_style_fallback()


@pytest.mark.parametrize("style", ["refdef", "zwsp"])
def test_a_marker_quoted_in_a_sentence_or_a_code_span_survives_byte_for_byte(style, settings) -> None:  # noqa: F811
    text = f"see `<!-- celmis:review -->` in code and {FINDING} inline"
    assert hide(text, style) == text
    assert not has_marker(hide(text, style), "review") and not has_marker(text, "finding")
    assert not markers.is_bot_text(text)


def test_a_marker_in_a_fenced_code_block_is_only_text(settings) -> None:  # noqa: F811
    text = f"A finding about the bot:\n\n```html\n{FINDING}\n<!-- celmis:review -->\n```\n"
    assert hide(text, "refdef") == text
    assert not markers.is_bot_text(text)
    assert reveal("```\n[//]: # (celmis:review)\n```") == "```\n[//]: # (celmis:review)\n```"


def test_a_stray_fence_does_not_hide_the_marker_after_it(settings) -> None:  # noqa: F811
    text = "an unclosed ``` in the model's words\n\n<!-- celmis:review -->"
    assert has_marker(text, "review")
    assert "[//]: # (celmis:review)" in hide(text, "refdef")


@pytest.mark.parametrize("style", ["refdef", "zwsp"])
def test_a_marker_in_a_text_with_windows_line_ends_is_still_found(style, settings) -> None:  # noqa: F811
    hidden = hide("a\n\n<!-- celmis:review -->\n\nb", style)
    crlf = hidden.replace("\n", "\r\n")
    assert has_marker(crlf, "review")
    assert "<!-- celmis:review -->" in reveal(crlf)
    assert has_marker("a\r\n<!-- celmis:review -->\r\nb", "review")


def test_a_description_with_windows_line_ends_is_not_stacked(settings) -> None:  # noqa: F811
    first = compose_description(
        "Author's words.", insights=f"{SUMMARY_HEADING}\n\nfirst", commit="aaaaaaa1",
        existing_mode="append", new_commits_mode="replace")
    saved = hide(first, "refdef").replace("\n", "\r\n")
    before, inner, after = split_description(reveal(saved))
    assert inner is not None and "first" in inner
    second = compose_description(
        reveal(saved), insights=f"{SUMMARY_HEADING}\n\nsecond", commit="bbbbbbb2",
        existing_mode="append", new_commits_mode="replace")
    assert second.count(SUMMARY_HEADING) == 1 and "first" not in second


def test_a_description_that_lost_its_markers_in_a_web_form_is_found_by_its_heading() -> None:
    text = f"Author.\r\n\r\n{SUMMARY_HEADING}\r\n\r\nstale\r\n"
    before, inner, after = split_description(text)
    assert inner is not None and "stale" in inner and before.startswith("Author.")


def test_our_block_typed_right_after_the_authors_text_keeps_its_own_lines(settings) -> None:  # noqa: F811
    glued = "Author's words.<!-- celmis:summary:start -->\nold\n<!-- celmis:summary:end -->tail"
    out = compose_description(
        glued, insights=f"{SUMMARY_HEADING}\n\nnew", commit="ccccccc3",
        existing_mode="append", new_commits_mode="replace")
    assert has_marker(hide(out, "refdef"), "summary")
    assert "[//]: # (celmis:summary:start)" in hide(out, "refdef")
    assert "words.<!--" not in out and "-->tail" not in out


@pytest.mark.parametrize("style", ["refdef", "zwsp"])
def test_a_description_near_the_limit_keeps_the_block_between_its_markers(style, settings) -> None:  # noqa: F811
    limit = 30_000
    author = "A" * 29_000
    out = compose_description(
        author, insights=f"{SUMMARY_HEADING}\n\n" + ("overview " * 600), commit="abc1234",
        existing_mode="append", new_commits_mode="replace", limit=limit)
    cut = fit(out, limit, style)
    assert len(cut) <= limit
    revealed = reveal(cut)
    start = revealed.index("<!-- celmis:summary:start -->")
    end = revealed.index("<!-- celmis:summary:end -->")
    assert "overview" in revealed[start:end]
    before, inner, after = split_description(revealed)
    assert inner is not None and SUMMARY_HEADING in inner


def test_a_cut_keeps_the_markers_where_they_were(settings) -> None:  # noqa: F811
    text = "<!-- celmis:summary:start -->\n" + "x" * 3000 + "\n<!-- celmis:summary:end -->\n" + "tail"
    out = reveal(fit(text, 800, "refdef"))
    assert out.index("celmis:summary:start") < out.index("xxx") < out.index("celmis:summary:end")
    assert out.rstrip().endswith("tail")


def test_the_summary_the_builder_writes_starts_with_the_heading_the_repair_looks_for() -> None:
    batch = ReviewBatch(pull_request=_pr("bitbucket", "w/r", 1))
    batch.pr_overview = "What this PR does."
    assert description_insights(batch).startswith(SUMMARY_HEADING)


def test_the_closing_details_tag_keeps_the_space_after_it() -> None:
    out = bitbucket_flavour("with <details><summary>x</summary>y</details> and more")
    assert "y and more" in out
