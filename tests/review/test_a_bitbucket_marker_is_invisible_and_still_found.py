"""Bitbucket shows raw HTML as text, so a marker written as `<!-- … -->` would
sit in every comment as visible noise. The code keeps writing the HTML form;
the Bitbucket provider hides it on the way out and reveals it on the way in.

Pinned here: nothing the provider stores carries an HTML comment, every marker
is still found by `has_marker` (comment, finding, description block), a re-run
rewrites in place instead of stacking, and when Bitbucket renders a hidden
marker as text anyway the process falls back to zero-width markers by itself.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from src.review import markers
from src.review.markers import has_marker, hide, reveal
from src.review.models import FindingSeverity
from src.review.pr_actions import compose_description
from src.review.providers.base import (
    STATUS_IN_PROGRESS_MARK,
    _format_finding_body,
    _format_started_comment,
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


@pytest.fixture(autouse=True)
def _fresh_marker_style():
    markers.reset_style_fallback()
    yield
    markers.reset_style_fallback()


def _stored(fake: _FakeBitbucket) -> list[dict]:
    return fake.comments


# ─── the module itself ──────────────────────────────────────────────

SAMPLE = (
    f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n\n## Title\n\nbody text\n\n"
    "<!-- celmis:finding fp=0123456789abcdef sha=0123456789ab -->"
)


@pytest.mark.parametrize("style", ["refdef", "zwsp"])
def test_a_hidden_marker_reads_back_as_the_html_one(style, settings) -> None:  # noqa: F811
    hidden = hide(SAMPLE, style)
    assert "<!--" not in hidden
    assert reveal(hidden) == reveal(hide(reveal(hidden), style))
    assert has_marker(hidden, MARKER)
    assert has_marker(hidden, "status") and has_marker(hidden, "finding")
    assert markers.parse_finding_marker(hidden) == ("0123456789abcdef", "0123456789ab")


@pytest.mark.parametrize("style", ["refdef", "zwsp"])
def test_hiding_twice_changes_nothing(style, settings) -> None:  # noqa: F811
    once = hide(SAMPLE, style)
    assert hide(once, style) == once
    assert reveal(reveal(once)) == reveal(once)


def test_the_html_style_leaves_the_text_alone() -> None:
    assert hide(SAMPLE, "html") == SAMPLE


def test_a_comment_of_a_person_is_not_rewritten_as_a_marker(settings) -> None:  # noqa: F811
    text = "<!-- todo: ask Dana -->\n\n[//]: # (a note to self)\n\nbody"
    assert hide(text, "refdef") == text
    assert reveal(text) == text


def test_a_marker_in_the_middle_of_a_line_is_only_text(settings) -> None:  # noqa: F811
    text = f"before {MARKER} after"
    assert hide(text, "refdef") == text
    assert not has_marker(hide(text, "refdef"), MARKER)


def test_a_cut_comment_keeps_every_marker_whole(settings) -> None:  # noqa: F811
    body = f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n" + ("x" * 5000)
    out = markers.fit(body, 400)
    assert len(out) <= 400
    assert has_marker(out, MARKER) and has_marker(out, "status")
    assert "[//]: # (celmis:review:under-test)" in out


# ─── what the provider stores ───────────────────────────────────────

def test_nothing_the_provider_stores_carries_an_html_comment(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    provider = _bitbucket_for(fake)
    provider.post_review(_full_batch())
    assert _stored(fake), "nothing was posted"
    for body in fake.bodies():
        assert "<!--" not in body
        assert has_marker(body, MARKER)


def test_the_summary_and_the_findings_are_found_again_by_the_next_run(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    for _ in range(2):
        provider = _bitbucket_for(fake)
        provider.post_review(_full_batch())
        provider.close()
    inline = [c for c in fake.comments if c.get("inline")]
    summaries = [c for c in fake.comments if not c.get("inline")]
    assert len(inline) == 1, "the inline comment of run one was not cleaned up"
    assert len(summaries) == 1, "a second summary was stacked"
    assert has_marker(summaries[0]["content"]["raw"], MARKER)


def test_the_lifecycle_comment_is_found_by_its_hidden_status_mark(settings) -> None:  # noqa: F811
    fake = _FakeBitbucket()
    pr = _pr("bitbucket", "ws/r", 3)
    first = _bitbucket_for(fake)
    body = _format_started_comment(pr, agents=["defect"], started_at="now", marker=MARKER)
    cid = first.upsert_status_comment(pr, body)
    assert cid is not None and "<!--" not in fake.bodies()[0]
    assert has_marker(fake.bodies()[0], STATUS_IN_PROGRESS_MARK)
    first.close()
    # A later run (a new provider, nothing cached) finds the placeholder by the
    # hidden mark alone and rewrites it, never adding a note beside it.
    later = _bitbucket_for(fake)
    again = later.upsert_status_comment(
        pr, "## skipped\n", create=False, only_if_in_progress=True)
    assert again == cid
    assert len(fake.comments) == 1
    later.close()


# ─── the description block ──────────────────────────────────────────

def _write_description(fake: _Bitbucket, *, commit: str, insights: str = "## 🤖 Celmis summary\n\nov",
                       mode: str = "replace") -> dict:
    provider = _bitbucket(fake)
    pr = _pr("bitbucket", "ws/r", 3)
    return provider.update_description(
        pr, lambda current: compose_description(
            current, insights=insights, commit=commit,
            existing_mode="append", new_commits_mode=mode,
        ))


def test_the_description_block_is_hidden_found_and_rewritten_not_stacked(settings) -> None:  # noqa: F811
    fake = _Bitbucket()
    assert _write_description(fake, commit="aaaaaaa1")["written"]
    first = fake.description
    assert "<!--" not in first
    assert first.startswith("Author's words.")
    assert _write_description(fake, commit="bbbbbbb2", insights="## 🤖 Celmis summary\n\nnew")["written"]
    assert fake.description.count("Celmis summary") == 2  # heading + the stamp line
    assert "new" in fake.description and "\n\nov" not in fake.description
    assert fake.description.startswith("Author's words.")


def test_a_description_written_with_html_markers_is_migrated_on_the_next_write(settings) -> None:  # noqa: F811
    fake = _Bitbucket()
    fake.description = (
        "Author's words.\n\n<!-- celmis:summary:start -->\n## 🤖 Celmis summary\n\nold\n\n"
        "<sub>Celmis summary · commit `aaaaaaa` · 2026-01-01 10:00 UTC</sub>\n"
        "<!-- celmis:summary:commit=aaaaaaa1 -->\n<!-- celmis:summary:end -->"
    )
    assert _write_description(fake, commit="bbbbbbb2", insights="## 🤖 Celmis summary\n\nnew")["written"]
    assert "<!--" not in fake.description
    assert "old" not in fake.description and "new" in fake.description
    assert fake.description.count("## 🤖 Celmis summary") == 1


def test_a_description_that_lost_its_markers_is_repaired_by_its_heading(settings) -> None:  # noqa: F811
    """A person edited the description in Bitbucket's own editor, which does not
    know the hidden lines: the markers are gone, the block is not."""
    fake = _Bitbucket()
    fake.description = "Author's words.\n\n## 🤖 Celmis summary\n\nstale overview\n"
    assert _write_description(fake, commit="ccccccc3", insights="## 🤖 Celmis summary\n\nfresh")["written"]
    assert fake.description.count("## 🤖 Celmis summary") == 1
    assert "stale overview" not in fake.description and "fresh" in fake.description
    assert fake.description.startswith("Author's words.")


def test_the_description_is_cut_to_the_provider_limit_without_cutting_a_marker(settings) -> None:  # noqa: F811
    fake = _Bitbucket()
    huge = "## 🤖 Celmis summary\n\n" + ("word " * 20_000)
    assert _write_description(fake, commit="ddddddd4", insights=huge)["written"]
    assert len(fake.description) <= BitbucketPRProvider.description_max_chars
    assert has_marker(fake.description, "summary")
    stored = reveal(fake.description)
    assert stored.count("<!-- celmis:summary:start -->") == 1
    assert stored.count("<!-- celmis:summary:end -->") == 1


# ─── Bitbucket renders the marker anyway ────────────────────────────

class _RenderingBitbucket(_FakeBitbucket):
    """Answers every comment write with `content.html`, rendered the way a
    Bitbucket that does NOT understand `[//]: # (x)` would: as visible text."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        resp = super().__call__(request)
        if request.method in ("POST", "PUT") and resp.status_code in (200, 201):
            raw = json.loads(request.content)["content"]["raw"]
            html = "".join(f"<p>{line}</p>" for line in raw.splitlines() if line.strip())
            data = resp.json()
            data["content"] = {"raw": raw, "html": html}
            return httpx.Response(resp.status_code, json=data)
        return resp


def test_a_marker_that_shows_up_in_the_rendered_html_switches_to_zero_width(settings) -> None:  # noqa: F811
    fake = _RenderingBitbucket()
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    provider.post_review(_full_batch())
    assert markers.marker_style() == "zwsp"
    for body in fake.bodies():
        assert "[//]: #" not in body and "<!--" not in body
        assert has_marker(body, MARKER)


# ─── helpers ────────────────────────────────────────────────────────

def _bitbucket_for(fake: _FakeBitbucket) -> BitbucketPRProvider:
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


def _full_batch():
    batch = _batch(_pr("bitbucket", "ws/r", 3), [_finding(FindingSeverity.WARNING)])
    batch.rich_summary = True
    batch.pr_overview = "Adds a cache in front of the lookup."
    return batch


def test_a_finding_body_carries_the_marker_on_a_line_of_its_own(settings) -> None:  # noqa: F811
    body = _format_finding_body(_finding(), MARKER)
    assert re.search(r"^<!-- celmis:review:under-test -->$", body, re.MULTILINE)
