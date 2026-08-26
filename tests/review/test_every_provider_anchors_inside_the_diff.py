"""All three providers snap an anchor onto a line the diff carries.

THE ORIGINAL DEFECT was measured on GitHub, where it is loudest: GitHub
validates a review as ONE object, so a single refused anchor 422s the batch.
`github:celmis-bench/discourse-graphite#18` is the one PR of fourteen whose
findings never reached the pull request — findings 4, posted 0, status
"complete" — because one anchor pointed at line 117 of a 104-line file.

`_snap_to_span` fixed that, in the GitHub provider only. GitLab and Bitbucket
send `finding.line` straight into `position[new_line]` and `inline.to`, and
they post PER FINDING — so an unanchorable line there costs one comment
instead of all of them, is recorded as nothing but a `failed` counter, and was
therefore never noticed. Quieter, not absent.

The helpers now live in the shared provider base, so a fix measured on one
provider is not a fix for one provider.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.review.models import Hunk, PullRequest
from src.review.providers.base import _anchorable_ranges, _snap_to_span

PROVIDERS = ["github", "gitlab", "bitbucket"]


def pr_with_hunk() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=1, title="t",
        description="", author="a", base_ref="main", base_sha="a" * 40,
        head_ref="f", head_sha="b" * 40, state="open",
        hunks=[Hunk(file_path="src/app.py", old_file_path="src/app.py",
                    old_start=10, old_count=5, new_start=10, new_count=5,
                    content="")],
    )


def test_a_line_inside_the_hunk_is_left_alone():
    ranges = _anchorable_ranges(pr_with_hunk())

    assert _snap_to_span(12, ranges[("src/app.py", "RIGHT")]) == 12


def test_a_line_past_the_hunk_snaps_back_to_it():
    ranges = _anchorable_ranges(pr_with_hunk())

    assert _snap_to_span(117, ranges[("src/app.py", "RIGHT")]) == 14


def test_a_line_before_the_hunk_snaps_forward():
    ranges = _anchorable_ranges(pr_with_hunk())

    assert _snap_to_span(1, ranges[("src/app.py", "RIGHT")]) == 10


def test_a_file_the_diff_does_not_touch_is_returned_unchanged():
    """Nowhere to snap TO. Dropping it would lose the finding; the 422
    fallback folds it into the summary instead."""
    assert _snap_to_span(42, []) == 42


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_provider_snaps_before_it_posts(provider: str):
    """Reads the module with ast, so a comment mentioning the helper cannot
    satisfy this — only a call can."""
    module = __import__(f"src.review.providers.{provider}", fromlist=["x"])
    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "_snap_to_span" in called, f"{provider} posts a raw finding.line"
    assert "_anchorable_ranges" in called


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_provider_keeps_its_own_copy(provider: str):
    """Three copies of an anchoring rule drift, and the drift is invisible
    until a review silently loses a comment on one provider only."""
    module = __import__(f"src.review.providers.{provider}", fromlist=["x"])
    tree = ast.parse(inspect.getsource(module))
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_snap_to_span" not in defined
    assert "_anchorable_ranges" not in defined
