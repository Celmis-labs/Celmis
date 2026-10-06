"""A review that found backlog issues fixed on the target branch says so in the
completed comment (compact and rich), in the PR's language, and stays silent
when there is nothing to say or the repository asked for silence. The cap on
the model calls a pass may spend is checked at the API edge, 0 to 50."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.schemas import ReviewPolicyIn, WorkspaceReviewDefaultsIn
from src.review.issue_resolver import attach_earlier_issues
from src.review.models import EarlierIssues, PullRequest, ReviewBatch
from src.review.providers.base import _format_rich_summary, _format_summary

ITEM = {"id": "i", "title": "Total may overflow", "file": "src/a.py", "pr": 12, "sha": "c2"}


def _batch(resolved, *, announce=True, lang=None) -> ReviewBatch:
    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="t", description="d",
        author="alice", base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open", hunks=[])
    b = ReviewBatch(pull_request=pr, review_language=lang)
    attach_earlier_issues(b, EarlierIssues(resolved=resolved, announce=announce))
    return b


@pytest.mark.parametrize("render", [_format_summary, _format_rich_summary])
def test_the_comment_lists_what_the_review_found_fixed(render) -> None:
    text = render(_batch([ITEM]), "<!-- m -->")
    assert "Total may overflow" in text and "`src/a.py`" in text


@pytest.mark.parametrize("render", [_format_summary, _format_rich_summary])
def test_the_comment_is_silent_when_nothing_was_fixed_or_the_repo_said_so(render) -> None:
    assert "Total may overflow" not in render(_batch([]), "<!-- m -->")
    assert "Total may overflow" not in render(_batch([ITEM], announce=False), "<!-- m -->")


def test_the_comment_speaks_the_language_of_the_pull_request() -> None:
    assert "раніше знайден" in _format_summary(_batch([ITEM], lang="uk"), "<!-- m -->")


@pytest.mark.parametrize("model", [ReviewPolicyIn, WorkspaceReviewDefaultsIn])
def test_the_resolve_budget_is_bounded_at_the_api_edge(model) -> None:
    assert model(issues_resolve_max_llm=0).issues_resolve_max_llm == 0
    assert model(issues_resolve_max_llm=50).issues_resolve_max_llm == 50
    assert model().issues_resolve_max_llm is None
    for bad in (-1, 51):
        with pytest.raises(ValidationError):
            model(issues_resolve_max_llm=bad)
