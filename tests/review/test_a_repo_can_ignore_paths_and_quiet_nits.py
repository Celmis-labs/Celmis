"""Two per-repo review settings: paths to ignore, and the lowest severity worth
a comment.

Ignore globs (`RepoReviewPolicy.ignore_globs`) are applied in the orchestrator,
after the policy loads — the diff was parsed in the provider before any policy
existed. What they drop joins `skipped_files`, so a PR the globs cover entirely
still gets the "all changed files were filtered out" skip rather than a
review of nothing.

The comment threshold (`comment_min_severity`) keeps findings below it out of
the PR's inline comments and NOTHING else: they are still in `batch.findings`,
still counted in the summary, still stored. The summary says how many were
shown and how many were held back, so the thread count and the severity counts
never silently disagree.

And the fix that rides along: breaking-change and compliance findings were
appended after the prefilter's severity sort, so with more findings than the
inline cap a critical one at the tail was cut while warnings posted.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from src.review import settings as settings_module
from src.review.ignore_globs import filter_raw_diff, path_ignored, validate_ignore_globs
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.orchestrator import ReviewOrchestrator, _sort_by_severity
from src.review.providers import gitlab as gitlab_module
from src.review.providers.base import _format_summary
from src.review.providers.gitlab import GitLabPRProvider
from src.review.settings import ReviewSettings

# ─── globs ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("glob", "path", "hit"), [
    ("*.snap", "web/__snapshots__/a.test.tsx.snap", True),
    ("*.snap", "a.snap", True),
    ("*.snap", "a.snapx", False),
    ("docs/**", "docs/guide/intro.md", True),
    ("docs/**", "api/docs/intro.md", False),       # anchored at the root
    ("/docs/**", "docs/a.md", True),
    ("migrations/*.py", "migrations/0001_init.py", True),
    ("migrations/*.py", "migrations/sub/0001.py", False),  # * stays in a segment
    ("**/fixtures/**", "tests/unit/fixtures/big.json", True),
    ("**/fixtures/**", "fixtures/big.json", True),
    ("vendor/", "vendor/lib/x.go", True),
    ("vendor/", "src/vendor.go", False),
    ("CHANGELOG.md", "packages/a/CHANGELOG.md", True),
])
def test_glob_rules(glob: str, path: str, hit: bool) -> None:
    assert path_ignored(path, [glob]) is hit


def test_validation_cleans_and_refuses_what_cannot_work() -> None:
    assert validate_ignore_globs([" docs/** ", "", "*.snap", "docs/**"]) == [
        "docs/**", "*.snap",
    ]
    for bad in ("!keep.py", "**", "/", "*", "# comment"):
        with pytest.raises(ValueError):
            validate_ignore_globs([bad])
    with pytest.raises(ValueError):
        validate_ignore_globs(["a"] * 201)


DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n"
    "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
    "diff --git a/docs/guide.md b/docs/guide.md\n"
    "index 3..4 100644\n--- a/docs/guide.md\n+++ b/docs/guide.md\n"
    "@@ -1,1 +1,2 @@\n # Guide\n+more\n"
)


def test_the_raw_diff_loses_exactly_the_ignored_sections() -> None:
    out = filter_raw_diff(DIFF, ["docs/**"])
    assert "src/app.py" in out
    assert "docs/guide.md" not in out
    assert filter_raw_diff(DIFF, []) == DIFF


def _hunk(path: str) -> Hunk:
    return Hunk(file_path=path, old_file_path=path, old_start=1, old_count=1,
                new_start=1, new_count=2, content="@@ -1,1 +1,2 @@\n+x\n")


def _pr(hunks: list[Hunk], raw: str = DIFF) -> PullRequest:
    return PullRequest(
        provider="gitlab", repo="g/p", number=5, title="t", description="",
        author="bob", base_ref="main", base_sha="b", head_ref="feat",
        head_sha="h", state="open", hunks=hunks, raw_diff=raw,
    )


def test_ignored_hunks_leave_and_are_named_as_skipped() -> None:
    pr = _pr([_hunk("src/app.py"), _hunk("docs/guide.md"), _hunk("docs/guide.md")])
    filtered = ReviewOrchestrator._apply_ignore_globs(pr, ["docs/**"])
    assert [h.file_path for h in pr.hunks] == ["src/app.py"]
    assert pr.skipped_files == ["docs/guide.md (ignore glob)"]
    assert "docs/guide.md" not in filtered


def test_a_pr_the_globs_cover_entirely_is_skipped_with_the_reason() -> None:
    """The orchestrator end to end, up to the no-hunks gate."""
    pr = _pr([_hunk("docs/guide.md")])

    class _Provider:
        def fetch_pull_request(self, repo, number):
            return pr

    orch = ReviewOrchestrator(agents=[])
    orch._load_policy = lambda slug: {  # type: ignore[method-assign]
        "enabled": True, "target_branches": [], "ignore_globs": ["docs/**"],
        "comment_min_severity": "error",
    }
    orch._build_context = lambda *a, **kw: type(  # type: ignore[method-assign]
        "Ctx", (), {"cross_repo_callers_count": 0, "graph_note": None})()
    result = orch.review("gitlab", "g/p", 5, post_comments=False, provider=_Provider())
    batch = result.batch
    assert batch.verdict == ReviewVerdict.SKIPPED
    assert "filtered out" in batch.summary
    assert "docs/guide.md (ignore glob)" in batch.summary
    assert batch.comment_min_severity == "error"


# ─── threshold ───────────────────────────────────────────────────────


def _finding(sev: FindingSeverity, n: int) -> Finding:
    return Finding(file_path="src/app.py", line=2, severity=sev,
                   title=f"finding {n}", body="b", agent="defect",
                   rule_id=f"defect.r{n}")


def _batch(threshold: str | None) -> ReviewBatch:
    findings = [
        _finding(FindingSeverity.CRITICAL, 1),
        _finding(FindingSeverity.ERROR, 2),
        _finding(FindingSeverity.WARNING, 3),
        _finding(FindingSeverity.WARNING, 4),
        _finding(FindingSeverity.INFO, 5),
    ]
    b = ReviewBatch(pull_request=_pr([_hunk("src/app.py")]), findings=findings,
                    verdict=ReviewVerdict.REQUEST_CHANGES)
    b.agents_run = ["defect"]
    b.comment_min_severity = threshold
    return b


@pytest.mark.parametrize(("threshold", "posted"), [
    (None, 5), ("info", 5), ("warning", 4), ("error", 2), ("critical", 1),
    ("nonsense", 5),  # a typo in a row must not silence the review
])
def test_the_threshold_decides_what_is_postable(threshold, posted) -> None:
    b = _batch(threshold)
    assert len(b.postable_findings) == posted
    assert len(b.findings) == 5, "findings under the threshold are kept"


@pytest.fixture
def cfg(monkeypatch) -> ReviewSettings:
    c = ReviewSettings(replace_on_synchronize=False, max_inline_comments=20)
    monkeypatch.setattr(gitlab_module, "get_review_settings", lambda: c)
    monkeypatch.setattr(settings_module, "get_review_settings", lambda: c)
    return c


def _post(batch: ReviewBatch) -> tuple[list[str], str]:
    """Run GitLab's post_review against a fake; return (discussion bodies,
    summary body)."""
    discussions: list[str] = []
    notes: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET":
            return httpx.Response(200, json=[])
        body = req.content.decode()
        if url.endswith("/discussions"):
            discussions.append(parse_qs(body).get("body", [""])[0])
            return httpx.Response(201, json={"id": "d"})
        if "/notes" in url:
            try:
                notes.append(json.loads(body).get("body", ""))
            except ValueError:
                notes.append(parse_qs(body).get("body", [""])[0])
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(200, json={})

    provider = GitLabPRProvider(token="fake")
    provider._http.close()
    provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider.post_review(batch)
    finally:
        provider.close()
    return discussions, (notes[-1] if notes else "")


def test_only_findings_over_the_threshold_become_comments(cfg) -> None:
    discussions, summary = _post(_batch("error"))
    assert len(discussions) == 2
    assert all("finding 1" in d or "finding 2" in d for d in discussions)
    # The counts stay honest: all five are counted, and the gap is explained.
    assert "**Warning:** 2" in summary and "**Info:** 1" in summary
    assert "**2** shown inline" in summary
    assert "3 below the comment threshold (critical + error)" in summary


def test_with_no_threshold_everything_posts_and_nothing_is_explained(cfg) -> None:
    discussions, summary = _post(_batch(None))
    assert len(discussions) == 5
    assert "shown inline" not in summary


def test_the_inline_cap_is_said_too(cfg) -> None:
    cfg.max_inline_comments = 1
    b = _batch("warning")
    assert len(b.inline_findings(cfg.max_inline_comments)) == 1
    summary = _format_summary(b, marker="<!-- m -->")
    assert "**1** shown inline" in summary
    assert "1 below the comment threshold (warning and above)" in summary
    assert "3 over the 1-comment limit" in summary


def test_a_late_critical_is_sorted_ahead_of_the_cap() -> None:
    """breaking_change appends after the prefilter's sort; the re-sort puts
    it first, so `inline_findings(cap)` cannot cut it."""
    warnings = [_finding(FindingSeverity.WARNING, n) for n in range(25)]
    late = _finding(FindingSeverity.CRITICAL, 99)
    late.agent = "breaking_change"
    ordered = _sort_by_severity([*warnings, late])
    assert ordered[0] is late
    assert ordered[1:] == warnings, "the sort is stable within a severity"
