"""Tests для review domain model."""

from __future__ import annotations

from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)


class TestPullRequest:
    def test_repo_slug_normalization(self) -> None:
        pr = PullRequest(
            provider="github", repo="pallets/click", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )
        assert pr.repo_slug == "github_pallets-click"

    def test_changed_files_dedup_preserves_order(self) -> None:
        pr = PullRequest(
            provider="github", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
            hunks=[
                Hunk(file_path="a.py", old_file_path="a.py",
                     old_start=1, old_count=1, new_start=1, new_count=2,
                     content="@@ -1,1 +1,2 @@\n line\n+added\n"),
                Hunk(file_path="b.py", old_file_path="b.py",
                     old_start=1, old_count=1, new_start=1, new_count=2,
                     content="@@ -1,1 +1,2 @@\n line\n+added\n"),
                Hunk(file_path="a.py", old_file_path="a.py",
                     old_start=10, old_count=1, new_start=10, new_count=2,
                     content="@@ -10,1 +10,2 @@\n line\n+added\n"),
            ],
        )
        assert pr.changed_files == ["a.py", "b.py"]


class TestHunkLineCounts:
    def test_added_removed_lines(self) -> None:
        h = Hunk(
            file_path="x.py", old_file_path="x.py",
            old_start=1, old_count=2, new_start=1, new_count=4,
            content=(
                "@@ -1,2 +1,4 @@\n"
                " context\n"
                "-removed\n"
                "+added1\n"
                "+added2\n"
                "+added3\n"
            ),
        )
        assert h.added_lines == 3
        assert h.removed_lines == 1


class TestFinding:
    def test_dedup_key_stable(self) -> None:
        f1 = Finding(file_path="a.py", line=10, rule_id="sec.injection")
        f2 = Finding(file_path="a.py", line=10, rule_id="sec.injection",
                     body="different body text")
        assert f1.dedup_key == f2.dedup_key

    def test_dedup_key_distinguishes_lines(self) -> None:
        f1 = Finding(file_path="a.py", line=10, rule_id="r")
        f2 = Finding(file_path="a.py", line=11, rule_id="r")
        assert f1.dedup_key != f2.dedup_key


class TestReviewBatchVerdict:
    def _make_pr(self) -> PullRequest:
        return PullRequest(
            provider="github", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )

    def test_clean_review_approves(self) -> None:
        # `agents_run` is load-bearing: "clean" means agents looked and found
        # nothing. A batch with no findings AND no agents is not clean, it is
        # unreviewed, and compute_verdict answers SKIPPED for it — this test
        # used to construct exactly that batch and demand APPROVE, pinning the
        # green-tick-on-an-unreviewed-PR bug in place. See
        # test_a_review_nobody_ran_is_not_an_approval.
        batch = ReviewBatch(
            pull_request=self._make_pr(),
            agents_run=["architect", "security", "quality"],
        )
        assert batch.compute_verdict() == ReviewVerdict.APPROVE

    def test_critical_finding_requests_changes(self) -> None:
        batch = ReviewBatch(
            pull_request=self._make_pr(),
            findings=[Finding(
                file_path="a.py", line=1,
                severity=FindingSeverity.CRITICAL, rule_id="r",
            )],
        )
        assert batch.compute_verdict() == ReviewVerdict.REQUEST_CHANGES

    def test_three_errors_request_changes(self) -> None:
        batch = ReviewBatch(
            pull_request=self._make_pr(),
            findings=[
                Finding(file_path="a.py", line=i,
                        severity=FindingSeverity.ERROR, rule_id=f"r{i}")
                for i in range(3)
            ],
        )
        assert batch.compute_verdict() == ReviewVerdict.REQUEST_CHANGES

    def test_warnings_only_comment(self) -> None:
        batch = ReviewBatch(
            pull_request=self._make_pr(),
            findings=[Finding(
                file_path="a.py", line=1,
                severity=FindingSeverity.WARNING, rule_id="r",
            )],
        )
        assert batch.compute_verdict() == ReviewVerdict.COMMENT

    def test_severity_counts(self) -> None:
        batch = ReviewBatch(
            pull_request=self._make_pr(),
            findings=[
                Finding(file_path="a.py", line=1, severity=FindingSeverity.CRITICAL, rule_id="r1"),
                Finding(file_path="a.py", line=2, severity=FindingSeverity.ERROR, rule_id="r2"),
                Finding(file_path="a.py", line=3, severity=FindingSeverity.WARNING, rule_id="r3"),
                Finding(file_path="a.py", line=4, severity=FindingSeverity.INFO, rule_id="r4"),
            ],
        )
        assert batch.critical_count == 1
        assert batch.error_count == 1
        assert batch.warning_count == 1
        assert batch.info_count == 1
