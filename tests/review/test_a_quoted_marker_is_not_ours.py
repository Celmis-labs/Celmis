"""A marker only counts when it is a line of its own.

"Quote reply" copies a comment's raw markdown, marker included, behind `> `.
That human's rebuttal carries our marker text and is not ours — on any
provider, in either form (HTML comment, hidden line), and whoever wrote it.
"""

from __future__ import annotations

import pytest

from src.review import markers
from src.review.markers import has_marker, is_bot_text
from src.review.providers.base import STATUS_IN_PROGRESS_MARK, _format_finding_body
from src.review.providers.bitbucket import BitbucketPRProvider
from tests.review.test_a_rerun_does_not_double_the_comments import (
    BOT,
    HUMAN,
    MARKER,
    _FakeBitbucket,
    _finding,
    _quote_reply,
    settings,  # noqa: F401 — fixture
)

FINDING = "<!-- celmis:finding fp=0123456789abcdef sha=0123456789ab -->"


@pytest.mark.parametrize("marker", [MARKER, STATUS_IN_PROGRESS_MARK, FINDING])
def test_a_quoted_html_marker_is_not_a_marker(marker, settings) -> None:  # noqa: F811
    quoted = f"> {marker}\n> some words\n\nI disagree."
    assert not is_bot_text(quoted)
    assert not has_marker(quoted, MARKER) and not has_marker(quoted, "status")
    assert markers.parse_finding_marker(quoted) is None


def test_a_quoted_hidden_marker_is_not_a_marker(settings) -> None:  # noqa: F811
    quoted = "> [//]: # (celmis:review:under-test)\n> [//]: # (celmis:finding fp=0123456789abcdef sha=0123456789ab)\n\nno"
    assert not is_bot_text(quoted)
    assert not has_marker(quoted, MARKER)
    assert markers.parse_finding_marker(quoted) is None


def test_a_marker_in_the_middle_of_a_sentence_is_not_a_marker(settings) -> None:  # noqa: F811
    assert not has_marker(f"the string {MARKER} marks it", MARKER)
    assert not is_bot_text("we write <!-- celmis:finding --> into comments")


def test_a_real_quote_reply_of_one_of_our_findings_is_not_ours_even_by_our_account(settings) -> None:  # noqa: F811
    ours = _format_finding_body(_finding(0), MARKER)
    quoted = _quote_reply(ours, "Disagree — the caller already holds the lock.")
    # The raw text DOES hold the marker, as a substring ...
    assert MARKER in quoted
    # ... and still it is not ours, whoever the author is.
    assert not has_marker(quoted, MARKER)
    viewer = frozenset({"{uuid-" + BOT + "}"})
    for author in (BOT, HUMAN):
        comment = {"content": {"raw": quoted}, "user": _FakeBitbucket._user_for(author)}
        comment["user"]["uuid"] = "{uuid-" + author + "}"
        assert not BitbucketPRProvider._is_ours(comment, MARKER, viewer)


def test_our_own_comment_is_ours_in_both_forms_and_only_for_our_account(settings) -> None:  # noqa: F811
    viewer = frozenset({"{uuid-" + BOT + "}"})
    html = _format_finding_body(_finding(0), MARKER)
    for raw in (html, markers.hide(html, "refdef"), markers.hide(html, "zwsp")):
        mine = {"content": {"raw": raw}, "user": {"uuid": "{uuid-" + BOT + "}"}}
        theirs = {"content": {"raw": raw}, "user": {"uuid": "{uuid-" + HUMAN + "}"}}
        assert BitbucketPRProvider._is_ours(mine, MARKER, viewer)
        assert not BitbucketPRProvider._is_ours(theirs, MARKER, viewer)
