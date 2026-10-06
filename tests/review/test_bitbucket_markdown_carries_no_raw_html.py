"""Bitbucket Cloud shows raw HTML as text.

Everything the provider writes — the finding comments, the summary (rich and
compact), the lifecycle comments, the description — must reach Bitbucket as
markdown: no `<sub>`, `<details>`, `<summary>` tag, no `&amp;`, no HTML-comment
marker. GitHub and GitLab keep the tags they render fine; only the Bitbucket
boundary converts, and code the reviewed repository contains is never touched.
"""

from __future__ import annotations

import re

import httpx
import pytest

from src.review import markers
from src.review.markers import bitbucket_flavour
from src.review.models import FindingSeverity
from src.review.pr_actions import compose_description
from src.review.providers.base import (
    _format_feedback_comment,
    _format_started_comment,
    _format_status_comment,
    _format_summary,
)
from src.review.providers.bitbucket import BitbucketPRProvider
from tests.review.test_a_rerun_does_not_double_the_comments import (
    MARKER,
    _FakeBitbucket,
    _patch_client,
)
from tests.review.test_the_pr_says_what_it_does import (
    _batch,
    _Bitbucket,
    _bitbucket,
    _finding,
    _pr,
    settings,  # noqa: F401 — fixture
)

RAW_HTML = re.compile(
    r"</?(sub|sup|details|summary|br|b|i|p|div|span|table|tr|td|th|a)\b[^>]*>|<!--|&amp;",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _fresh_marker_style():
    markers.reset_style_fallback()
    yield
    markers.reset_style_fallback()


def _assert_clean(text: str) -> None:
    found = RAW_HTML.search(text)
    assert found is None, f"raw HTML {found.group(0)!r} in:\n{text[:600]}"


def _batch_with_everything():
    batch = _batch(
        _pr("bitbucket", "ws/r", 3),
        [_finding(FindingSeverity.WARNING), _finding(FindingSeverity.CRITICAL, line=12)],
    )
    batch.rich_summary = True
    batch.pr_overview = "Adds a cache & a lock."
    return batch


def test_every_comment_a_bitbucket_review_posts_is_markdown_only(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    provider.post_review(_batch_with_everything())
    assert len(fake.comments) >= 2
    for body in fake.bodies():
        _assert_clean(body)


def test_the_compact_summary_is_markdown_only_too(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    batch = _batch_with_everything()
    batch.rich_summary = False
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    provider.post_review(batch)
    for body in fake.bodies():
        _assert_clean(body)


def test_the_lifecycle_comments_are_markdown_only(settings) -> None:  # noqa: F811
    pr = _pr("bitbucket", "ws/r", 3)
    for text in (
        _format_started_comment(pr, agents=["defect"], started_at="now", marker=MARKER),
        _format_status_comment(pr, outcome="skipped", reason="a & b", marker=MARKER),
        _format_feedback_comment(pr, reason="draft", marker=MARKER),
    ):
        fake = _FakeBitbucket()
        provider = BitbucketPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(fake))
        provider.upsert_status_comment(pr, text)
        _assert_clean(fake.bodies()[0])


def test_the_description_is_markdown_only_with_the_original_folded(settings) -> None:  # noqa: F811
    fake = _Bitbucket()
    provider = _bitbucket(fake)
    pr = _pr("bitbucket", "ws/r", 3)
    result = provider.update_description(
        pr, lambda current: compose_description(
            current, insights="## 🤖 Celmis summary\n\nOverview.", commit="abc1234",
            existing_mode="complement", new_commits_mode="replace",
            complement=lambda author, ours: f"{ours}\n\n(merged with the author's text)",
        ))
    assert result["written"]
    _assert_clean(fake.description)
    assert "Author's words." in fake.description


def test_the_folded_original_is_read_back_after_the_conversion(settings) -> None:  # noqa: F811
    fake = _Bitbucket()
    pr = _pr("bitbucket", "ws/r", 3)

    def write(commit: str, insights: str):
        return _bitbucket(fake).update_description(
            pr, lambda current: compose_description(
                current, insights=insights, commit=commit,
                existing_mode="complement", new_commits_mode="replace",
                complement=lambda author, ours: f"{ours}\n\nmerged: {author}",
            ))

    assert write("aaaaaaa1", "## 🤖 Celmis summary\n\nfirst")["written"]
    assert write("bbbbbbb2", "## 🤖 Celmis summary\n\nsecond")["written"]
    # The author's original survives a second merge, once, not wrapped again.
    assert fake.description.count("Author's words.") >= 1
    assert "first" not in fake.description and "second" in fake.description
    _assert_clean(fake.description)


def test_github_keeps_the_tags_it_renders(settings) -> None:  # noqa: F811
    summary = _format_summary(_batch_with_everything(), MARKER)
    assert "<details>" in summary and "<sub>" in summary


def test_code_in_a_comment_is_never_rewritten() -> None:
    text = (
        "see <sub>this</sub> and `<sub>code</sub>`\n\n"
        "```html\n<details><summary>x</summary></details>\n<sub>y</sub>\n```\n"
    )
    out = bitbucket_flavour(text)
    assert "see _this_" in out
    assert "`<sub>code</sub>`" in out
    assert "```html\n<details><summary>x</summary></details>\n<sub>y</sub>\n```" in out


def test_the_conversion_is_idempotent() -> None:
    text = "<sub>a</sub>\n\n<details>\n<summary>S &amp; T</summary>\n\nbody\n</details>"
    once = bitbucket_flavour(text)
    assert once == "_a_\n\n**S & T**\n\nbody"
    assert bitbucket_flavour(once) == once
