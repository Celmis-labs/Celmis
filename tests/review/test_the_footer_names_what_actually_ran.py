"""The provenance line under a review describes that review.

THE DEFECT. The footer was a constant:

    Powered by Code Analyzer · context: tree-sitter graph + cross-repo edges
    + Gemini 3 Pro/Flash

Every review carried it — including every review run entirely by the Claude
Code engine, with `agents_run=["claude_code"]` and
`cost_source=claude_code_subscription`. So the line under a Claude review named
Gemini, and the line under a review with no repo group claimed cross-repo
edges it never had.

Provenance is the part of a machine-written comment a reader uses to decide how
much to trust the rest of it. A constant there is not a small lie.

SEPARATELY, the same block printed `tokens: 0/0` beside a real $0.21. The
Claude Code engine bills by subscription and never populates those fields, so
the zero was an absent measurement rendered as a measured zero.
"""

from __future__ import annotations

from src.review.models import PullRequest, ReviewBatch


def batch(**kw) -> ReviewBatch:
    base = dict(
        pull_request=PullRequest(
            provider="github", repo="acme/api", number=1, title="t",
            description="", author="a", base_ref="main", base_sha="a" * 40,
            head_ref="f", head_sha="b" * 40, state="open",
        ),
        elapsed_seconds=12.3,
    )
    base.update(kw)
    return ReviewBatch(**base)


def _footer(b: ReviewBatch) -> str:
    from src.review.providers.base import _format_summary
    return _format_summary(b, marker="<!-- test -->")


def test_a_claude_review_does_not_name_gemini():
    body = _footer(batch(agents_run=["claude_code"]))

    assert "Gemini" not in body
    assert "claude_code" in body


def test_a_review_without_cross_repo_edges_does_not_claim_them():
    body = _footer(batch(agents_run=["architect"], cross_repo_callers=0))

    assert "cross-repo edges" not in body


def test_a_review_with_cross_repo_edges_says_so():
    body = _footer(batch(agents_run=["architect"], cross_repo_callers=4))

    assert "cross-repo edges" in body


def test_the_graph_is_always_named_because_it_is_always_used():
    assert "tree-sitter graph" in _footer(batch(agents_run=["architect"]))


def test_absent_token_counts_are_omitted_not_printed_as_zero():
    """A missing line is honest; "tokens: 0/0" beside a real cost is not."""
    body = _footer(batch(agents_run=["claude_code"], tokens_in=0, tokens_out=0))

    assert "tokens:" not in body
    assert "Analysis time" in body


def test_real_token_counts_are_still_shown():
    body = _footer(batch(agents_run=["architect"], tokens_in=1234, tokens_out=567))

    assert "tokens: 1,234/567" in body


def test_a_run_with_no_agents_still_renders():
    """Reachable for skipped and failed runs, which post a real comment."""
    body = _footer(batch(agents_run=[]))

    assert "agents: none" in body
