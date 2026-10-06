"""The ledger and the pull-request list through the review scope.

* A run the scope ended before reading anything (no new commits, merge commits
  only) is not a review: the PR keeps the status, run and count of the review
  it had, so the list does not turn a reviewed PR into a skipped one. Any other
  skip still shows as a skip.
* The baseline of the next incremental review moves only for a complete review
  whose comments were posted: a dry run, a partial run and a refused post leave
  it where it was, so the next review covers their gap.
* An incremental review hashes and anchors the WHOLE PR, so an issue in a file
  the new commits never touched is not judged and stays open.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewIssue, ReviewPullRequest
from src.review.issues import record_review_run
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
    ScopeInfo,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(eng)
    ReviewPullRequest.__table__.create(eng)
    yield eng
    eng.dispose()


def _section(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n{body}")


A_V1, A_V2 = _section("src/a.py", "-a\n+b\n"), _section("src/a.py", "-a\n+c\n")
B_V1 = _section("src/b.py", "-x\n+y\n")


def _hunk(path: str) -> Hunk:
    return Hunk(file_path=path, old_file_path=path, old_start=1, old_count=1,
                new_start=1, new_count=1, content="@@ -1 +1 @@\n")


def _finding(path: str, line: int = 1) -> Finding:
    return Finding(file_path=path, line=line, title="Unchecked return",
                   severity=FindingSeverity.ERROR, rule_id="defect.ret",
                   agent="defect", body="b", suggestion="fix")


def _result(head: str, *, raw: str, hunks: list[str], findings=(), scope=None,
            scope_skip=None, posted=True) -> SimpleNamespace:
    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="Add cache",
        description="", author="dana", base_ref="main", base_sha="b",
        head_ref="feat/cache", head_sha=head, state="open",
        url="https://github.com/acme/api/pull/7", raw_diff=raw,
        hunks=[_hunk(p) for p in hunks], scope=scope,
    )
    batch = ReviewBatch(pull_request=pr, findings=list(findings),
                        verdict=ReviewVerdict.COMMENT)
    batch.agents_run = ["defect"]
    batch.scope_skip = scope_skip
    return SimpleNamespace(batch=batch, posted=posted, provider_response={})


def _row(engine) -> ReviewPullRequest:
    with Session(engine) as s:
        return s.execute(select(ReviewPullRequest)).scalar_one()


def _issues(engine) -> list[ReviewIssue]:
    with Session(engine) as s:
        return list(s.execute(select(ReviewIssue)).scalars())


def _record(engine, result, run_id, status="complete") -> None:
    assert record_review_run(result, run_id=run_id, workspace_id="ws", status=status,
                             engine=engine) is True


# ─── a scope skip is not a review ────────────────────────────────────


def test_a_scope_skip_leaves_the_reviewed_pr_as_it_was(engine) -> None:
    _record(engine, _result("h1", raw=A_V1, hunks=["src/a.py"], findings=[_finding("src/a.py")]),
            "r1")

    _record(engine, _result("h1", raw=A_V1, hunks=[], scope_skip="no_new_commits", posted=False),
            "r2", status="skipped")

    row = _row(engine)
    assert (row.reviews_count, row.last_review_status, row.last_run_id) == (1, "complete", "r1")
    assert row.last_reviewed_sha == "h1"
    assert [i.status for i in _issues(engine)] == ["open"]


def test_any_other_skip_still_shows_as_a_skip(engine) -> None:
    _record(engine, _result("h1", raw=A_V1, hunks=["src/a.py"]), "r1")

    _record(engine, _result("h2", raw=A_V2, hunks=[], posted=False), "r2", status="skipped")

    row = _row(engine)
    assert (row.reviews_count, row.last_review_status, row.last_run_id) == (2, "skipped", "r2")


def test_a_scope_skip_does_not_create_a_row_that_was_never_reviewed(engine) -> None:
    _record(engine, _result("h1", raw=A_V1, hunks=[], scope_skip="only_merge_commits",
                            posted=False), "r1", status="skipped")

    row = _row(engine)
    assert row.reviews_count == 0 and row.last_reviewed_sha is None


# ─── the baseline ────────────────────────────────────────────────────


def test_only_a_complete_posted_review_moves_the_baseline(engine) -> None:
    _record(engine, _result("h1", raw=A_V1, hunks=["src/a.py"]), "r1")
    assert _row(engine).last_reviewed_sha == "h1"

    # A dry run: complete, nothing posted.
    _record(engine, _result("h2", raw=A_V2, hunks=["src/a.py"], posted=False), "r2")
    assert _row(engine).last_reviewed_sha == "h1", "a dry run must not move the baseline"

    # A partial run: an agent failed, its gap stays to be covered.
    _record(engine, _result("h3", raw=A_V2, hunks=["src/a.py"]), "r3", status="partial")
    assert _row(engine).last_reviewed_sha == "h1", "a partial run must not move it either"

    _record(engine, _result("h4", raw=A_V2, hunks=["src/a.py"]), "r4")
    assert _row(engine).last_reviewed_sha == "h4"


# ─── the whole pull request, also for an increment ───────────────────


def _incremental(head: str, *, increment: str, whole: str, findings=()):
    scope = ScopeInfo(base_sha="h1", new_commits=1, full_raw_diff=whole,
                      full_hunks=[_hunk("src/a.py"), _hunk("src/b.py")])
    return _result(head, raw=increment, hunks=["src/a.py"], findings=findings, scope=scope)


def test_an_issue_in_a_file_the_new_commits_never_touched_stays_open(engine) -> None:
    whole_v1 = A_V1 + B_V1
    _record(engine, _result("h1", raw=whole_v1, hunks=["src/a.py", "src/b.py"],
                            findings=[_finding("src/b.py")]), "r1")
    before = _row(engine).file_hashes

    _record(engine, _incremental("h2", increment=A_V2, whole=A_V2 + B_V1), "r2")

    [issue] = _issues(engine)
    assert issue.status == "open", "the increment never read src/b.py, so it cannot judge it"
    after = _row(engine).file_hashes
    assert set(after) == set(before) == {"src/a.py", "src/b.py"}, (
        "the file hashes describe the whole pull request, not the increment")
    assert after["src/b.py"] == before["src/b.py"]
    assert after["src/a.py"] != before["src/a.py"]


def test_an_incremental_review_that_found_something_records_it_on_the_pull_request(engine) -> None:
    _record(engine, _result("h1", raw=A_V1 + B_V1, hunks=["src/a.py", "src/b.py"]), "r1")

    _record(engine, _incremental("h2", increment=A_V2, whole=A_V2 + B_V1,
                                 findings=[_finding("src/a.py")]), "r2")

    [issue] = _issues(engine)
    assert (issue.file_path, issue.status, issue.first_seen_sha) == ("src/a.py", "open", "h2")
    row = _row(engine)
    assert (row.reviews_count, row.last_reviewed_sha) == (2, "h2")
