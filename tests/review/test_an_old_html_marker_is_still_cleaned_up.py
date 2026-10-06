"""Comments written before markers were hidden carry `<!-- … -->` on Bitbucket.

They are still ours: found by the marker and the author, deleted or rewritten
on the next run — and the rewrite is already in the hidden form, so the
migration costs nothing and needs no sweep.
"""

from __future__ import annotations

import pytest

from src.review import markers
from src.review.markers import has_marker
from src.review.providers.base import STATUS_IN_PROGRESS_MARK, _format_finding_body
from tests.review.test_a_rerun_does_not_double_the_comments import (
    HUMAN,
    MARKER,
    _batch,
    _bitbucket,
    _FakeBitbucket,
    _finding,
    settings,  # noqa: F401 — fixture
)


@pytest.fixture(autouse=True)
def _fresh_marker_style():
    markers.reset_style_fallback()
    yield
    markers.reset_style_fallback()


def test_an_old_inline_comment_with_an_html_marker_is_deleted_on_the_next_run(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    old = fake.add_comment(_format_finding_body(_finding(0), MARKER), inline=True)
    assert "<!-- celmis:review:under-test -->" in fake.bodies()[0]
    provider = _bitbucket(fake)
    provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
    provider.close()
    assert old not in fake.ids()


def test_an_old_summary_with_an_html_marker_is_rewritten_in_place_and_hidden(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    original = fake.add_comment(f"{MARKER}\nold summary")
    provider = _bitbucket(fake)
    result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=0))
    provider.close()
    summaries = [c for c in fake.comments if not c.get("inline")]
    assert [c["id"] for c in summaries] == [original] == [result["summary_comment_id"]]
    raw = summaries[0]["content"]["raw"]
    assert "<!--" not in raw and has_marker(raw, MARKER)
    assert "old summary" not in raw


def test_an_old_placeholder_with_html_marks_is_still_a_placeholder(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    cid = fake.add_comment(f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n## 🔄 reviewing…")
    provider = _bitbucket(fake)
    pr = _batch("bitbucket", "ws/r", 3).pull_request
    written = provider.upsert_status_comment(
        pr, "## skipped", create=False, only_if_in_progress=True)
    provider.close()
    assert written == cid


def test_an_old_comment_of_somebody_else_is_still_not_ours(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    theirs = fake.add_comment(f"{MARKER}\ncopied by hand", author=HUMAN)
    provider = _bitbucket(fake)
    provider.post_review(_batch("bitbucket", "ws/r", 3, findings=0))
    provider.close()
    assert theirs in fake.ids()
