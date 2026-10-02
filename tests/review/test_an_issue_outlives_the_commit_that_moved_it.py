"""A finding has an identity across runs, and "fixed" is earned, not assumed.

`review_issues` keys a finding by sha256(rule_id | file | normalised title) per
pull request. The line is NOT in it: a push that adds lines above a defect
moves the defect without fixing it. These tests pin that, the rule for
"fixed in a subsequent commit" (the issue was not found again AND its file
changed between the two reviewed heads), and the cases that must leave an
issue open: same head, partial run, the finder that raised it did not run,
no previous diff to compare against.

The persistence half runs on sqlite with the real models — the suite has no
Postgres, and JSONB is rendered as JSON on sqlite for the test only.
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
from src.review.issues import (
    ExistingIssue,
    apply_feedback,
    categorize,
    file_section_hashes,
    fingerprint,
    found_issues,
    plan_sync,
    record_pr_state,
    record_review_run,
)
from src.review.models import (
    Finding,
    FindingSeverity,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


# ─── identity ────────────────────────────────────────────────────────


def test_the_fingerprint_ignores_the_line_and_the_digits_in_the_title() -> None:
    a = fingerprint("defect.null", "src/a.py", "Null deref on line 42 in `load()`")
    b = fingerprint("defect.null", "src/a.py", "null deref on line 57 in load()")
    assert a == b


def test_the_fingerprint_still_tells_findings_apart() -> None:
    base = fingerprint("defect.null", "src/a.py", "Null deref")
    assert base != fingerprint("defect.null", "src/b.py", "Null deref")
    assert base != fingerprint("defect.race", "src/a.py", "Null deref")
    assert base != fingerprint("defect.null", "src/a.py", "Unclosed file")


def test_a_shifted_finding_maps_to_the_same_issue() -> None:
    f1 = Finding(file_path="src/a.py", line=10, title="Unchecked return",
                 rule_id="defect.ret", agent="defect")
    f2 = Finding(file_path="src/a.py", line=31, title="Unchecked return",
                 rule_id="defect.ret", agent="defect")
    assert found_issues([f1])[0].fingerprint == found_issues([f2])[0].fingerprint


@pytest.mark.parametrize(("agent", "rule", "title", "category"), [
    ("security", "sec.x", "anything", "security"),
    ("defect", "defect.sqli", "SQL injection via f-string", "security"),
    ("defect", "defect.n1", "N+1 query in loop", "performance"),
    ("defect", "defect.naming", "Inconsistent naming", "style"),
    ("structural", "struct.x", "Large module", "maintainability"),
    ("defect", "defect.duplication", "Duplicated block", "maintainability"),
    ("defect", "defect.x", "Off by one", "bug"),
    ("breaking_change", "bc.sig", "Signature changed", "bug"),
    ("compliance", "comp.x", "Missing licence header", "other"),
])
def test_categories(agent, rule, title, category) -> None:
    assert categorize(agent, rule, title) == category


def test_diff_sections_hash_per_file_and_ignore_the_index_line() -> None:
    one = (
        "diff --git a/a.py b/a.py\nindex 111..222 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.py b/b.py\nindex 333..444 100644\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1 +1 @@\n-p\n+q\n"
    )
    two = one.replace("index 111..222", "index 999..888").replace("+q", "+r")
    h1, h2 = file_section_hashes(one), file_section_hashes(two)
    assert set(h1) == {"a.py", "b.py"}
    assert h1["a.py"] == h2["a.py"], "only the blob ids moved"
    assert h1["b.py"] != h2["b.py"]


# ─── the plan ────────────────────────────────────────────────────────


def _existing(fp: str, path: str = "src/a.py", agent: str = "defect",
              status: str = "open", source: str | None = None) -> ExistingIssue:
    return ExistingIssue(id=fp, fingerprint=fp, file_path=path, agent=agent,
                         status=status, resolution_source=source)


def _plan(existing, found=(), **kw):
    args = dict(run_complete=True, head_sha="h2", prev_head_sha="h1",
                prev_file_hashes={"src/a.py": "old"},
                new_file_hashes={"src/a.py": "new"})
    args.update(kw)
    return plan_sync(list(existing), list(found), **args)


def test_an_unrepeated_issue_in_a_changed_file_is_fixed() -> None:
    assert _plan([_existing("x")]).fixed == ["x"]


def test_an_unrepeated_issue_in_an_untouched_file_stays_open() -> None:
    plan = _plan([_existing("x")], new_file_hashes={"src/a.py": "old"})
    assert plan.fixed == []


@pytest.mark.parametrize("kw", [
    {"head_sha": "h1"},                  # the same head reviewed again
    {"run_complete": False},             # a stage did not answer
    {"prev_file_hashes": None},          # nothing to compare against
    {"new_file_hashes": {}},             # no diff on the new run
    {"agents_not_run": ["defect"]},      # its finder did not look
])
def test_uncertainty_leaves_the_issue_open(kw) -> None:
    assert _plan([_existing("x")], **kw).fixed == []


def test_a_refound_auto_fix_is_a_regression_but_a_human_decision_stands() -> None:
    found = found_issues([Finding(file_path="src/a.py", line=1, title="t",
                                  rule_id="r", agent="defect")])
    fp = found[0].fingerprint
    auto = _plan([_existing(fp, status="fixed", source="auto_next_commit")], found)
    assert auto.refound[0][2] is True
    manual = _plan([_existing(fp, status="dismissed", source="manual")], found)
    assert manual.refound[0][2] is False


# ─── persistence ─────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(eng)
    ReviewPullRequest.__table__.create(eng)
    yield eng
    eng.dispose()


def _diff(body: str) -> str:
    return (
        "diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n"
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n" + body
    )


def _result(head: str, raw: str, findings: list[Finding], failed=()) -> SimpleNamespace:
    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="Add cache",
        description="", author="dana", base_ref="main", base_sha="b",
        head_ref="feat/cache", head_sha=head, state="open",
        url="https://github.com/acme/api/pull/7", raw_diff=raw,
    )
    batch = ReviewBatch(pull_request=pr, findings=findings,
                        verdict=ReviewVerdict.COMMENT)
    batch.agents_run = ["defect", "security"]
    batch.agents_failed = list(failed)
    return SimpleNamespace(batch=batch, posted=True, provider_response={})


def _f(line: int, title: str = "Unchecked return") -> Finding:
    return Finding(file_path="src/a.py", line=line, title=title,
                   severity=FindingSeverity.ERROR, rule_id="defect.ret",
                   agent="defect", body="b", suggestion="fix")


def _issues(engine) -> list[ReviewIssue]:
    with Session(engine) as s:
        return list(s.execute(select(ReviewIssue)).scalars())


def _pr_row(engine) -> ReviewPullRequest:
    with Session(engine) as s:
        return s.execute(select(ReviewPullRequest)).scalar_one()


def test_a_run_then_a_fixing_commit(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    [issue] = _issues(engine)
    assert (issue.status, issue.category, issue.first_seen_sha) == ("open", "bug", "h1")
    assert issue.pr_url == "https://github.com/acme/api/pull/7"
    pr = _pr_row(engine)
    assert (pr.reviews_count, pr.last_review_status, pr.head_sha) == (1, "complete", "h1")

    # Push 2 shifts the line: the same issue, seen again.
    record_review_run(_result("h2", _diff("-a\n+c\n"), [_f(31)]),
                      run_id="r2", workspace_id="ws", status="complete",
                      engine=engine)
    [issue] = _issues(engine)
    assert (issue.status, issue.occurrences, issue.line) == ("open", 2, 31)
    assert issue.last_seen_sha == "h2" and issue.first_run_id == "r1"

    # Push 3 changes the file and the finding is gone: fixed by that commit.
    record_review_run(_result("h3", _diff("-a\n+d\n"), []),
                      run_id="r3", workspace_id="ws", status="complete",
                      engine=engine)
    [issue] = _issues(engine)
    assert issue.status == "fixed"
    assert issue.resolution_source == "auto_next_commit"
    assert issue.fixed_in_sha == "h3"
    assert _pr_row(engine).reviews_count == 3


def test_a_failed_or_skipped_run_moves_nothing(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), []),
                      run_id="r2", workspace_id="ws", status="skipped",
                      engine=engine)
    [issue] = _issues(engine)
    assert issue.status == "open"
    pr = _pr_row(engine)
    assert pr.last_review_status == "skipped" and pr.head_sha == "h1", (
        "a skipped run must not move the baseline the next fix check reads"
    )


def test_a_partial_run_records_but_fixes_nothing(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), [], failed=["security"]),
                      run_id="r2", workspace_id="ws", status="partial",
                      engine=engine)
    assert _issues(engine)[0].status == "open"


def test_closing_unmerged_resolves_and_merging_does_not(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    assert record_pr_state(workspace_id="ws", provider="github", repo="acme/api",
                           number=7, state="merged", engine=engine)
    assert _issues(engine)[0].status == "open"
    assert _pr_row(engine).state == "merged"
    record_pr_state(workspace_id="ws", provider="github", repo="acme/api",
                    number=7, state="closed", engine=engine)
    issue = _issues(engine)[0]
    assert (issue.status, issue.resolution_source) == ("resolved", "pr_closed")


def test_dismissing_the_finding_dismisses_the_issue_and_undo_reopens(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    n = apply_feedback(workspace_id="ws", run_id="r1", state="dismissed",
                       file_path="src/a.py", title="Unchecked return",
                       rule_id="defect.ret", engine=engine)
    assert n == 1 and _issues(engine)[0].status == "dismissed"
    apply_feedback(workspace_id="ws", run_id="r1", state=None,
                   file_path="src/a.py", title="Unchecked return",
                   rule_id="defect.ret", engine=engine)
    assert _issues(engine)[0].status == "open"


def test_the_ledger_never_breaks_the_review(caplog) -> None:
    """No DATABASE_URL, a broken engine: a warning, never an exception."""
    class _Broken:
        def connect(self, *a, **kw):
            raise RuntimeError("db down")

    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=_Broken())
    assert "review_issues_sync_failed" in caplog.text
