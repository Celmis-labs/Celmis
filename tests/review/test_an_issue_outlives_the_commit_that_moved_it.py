"""A finding has an identity across runs, and "fixed" is earned, not assumed.

`review_issues` keys a finding by sha256(rule_id | file | normalised title) per
pull request. The line is NOT in it: a push that adds lines above a defect
moves the defect without fixing it. These tests pin that, the rule for
"fixed in a subsequent commit" (the issue was not found again AND its file
changed between the two reviewed heads), and the cases that must leave an
issue open: same head, no stage answered, the finder that raised it did not
run (failed — what makes a run partial — or skipped), no previous diff to
compare against. A partial run still judges the issues of the agents that
did answer: it moves the baseline, so not judging there lost those fixes.

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
    Hunk,
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
    args = dict(run_reviewed=True, head_sha="h2", prev_head_sha="h1",
                prev_file_hashes={"src/a.py": "old"},
                new_file_hashes={"src/a.py": "new"},
                reviewed_files={"src/a.py"})
    args.update(kw)
    return plan_sync(list(existing), list(found), **args)


def test_an_unrepeated_issue_in_a_changed_file_is_fixed() -> None:
    assert _plan([_existing("x")]).fixed == ["x"]


def test_an_unrepeated_issue_in_an_untouched_file_stays_open() -> None:
    plan = _plan([_existing("x")], new_file_hashes={"src/a.py": "old"})
    assert plan.fixed == []


@pytest.mark.parametrize("kw", [
    {"head_sha": "h1"},                  # the same head reviewed again
    {"run_reviewed": False},             # no stage answered
    {"prev_file_hashes": None},          # nothing to compare against
    {"new_file_hashes": {}},             # no diff on the new run
    {"agents_not_run": ["defect"]},      # its finder did not look
    {"reviewed_files": None},            # not known what was looked at
    {"reviewed_files": set()},           # the file was skipped / ignored
    # The file left the diff altogether: before set, after missing.
    {"new_file_hashes": {"src/b.py": "x"}, "reviewed_files": {"src/b.py"}},
])
def test_uncertainty_leaves_the_issue_open(kw) -> None:
    assert _plan([_existing("x")], **kw).fixed == []


def test_a_rule_the_deny_list_hid_this_run_is_not_a_fix() -> None:
    e = ExistingIssue(id="x", fingerprint="x", file_path="src/a.py",
                      agent="defect", status="open", resolution_source=None,
                      rule_id="defect.ret")
    assert _plan([e], hidden_rules=["defect.ret"]).fixed == []
    assert _plan([e], hidden_rules=["other.rule"]).fixed == ["x"]


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


def _result(head: str, raw: str, findings: list[Finding], failed=(),
            reviewed=("src/a.py",), suppressed: dict | None = None) -> SimpleNamespace:
    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="Add cache",
        description="", author="dana", base_ref="main", base_sha="b",
        head_ref="feat/cache", head_sha=head, state="open",
        url="https://github.com/acme/api/pull/7", raw_diff=raw,
        # What reached the agents. A file the run skipped is in `raw_diff`
        # but not here.
        hunks=[Hunk(file_path=p, old_file_path=p, old_start=1, old_count=1,
                    new_start=1, new_count=1, content="@@ -1 +1 @@\n")
               for p in reviewed],
    )
    batch = ReviewBatch(pull_request=pr, findings=findings,
                        verdict=ReviewVerdict.COMMENT)
    batch.dropped_by_rule = dict(suppressed or {})
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


def _sec(line: int) -> Finding:
    return Finding(file_path="src/a.py", line=line, title="Token in log",
                   severity=FindingSeverity.ERROR, rule_id="sec.log",
                   agent="security", body="b")


def test_a_partial_run_judges_what_its_answering_agents_looked_at(engine) -> None:
    """h2 fixes the defect, and the security agent fails on h2. The defect
    agent DID look: its issue is fixed by h2. The security issue is not
    judged — its finder did not look. The baseline moves to h2 either way,
    so a fix not judged here would never be judged at all."""
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10), _sec(3)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), [], failed=["security"]),
                      run_id="r2", workspace_id="ws", status="partial",
                      engine=engine)
    by_agent = {i.agent: i for i in _issues(engine)}
    assert (by_agent["defect"].status, by_agent["defect"].fixed_in_sha) == ("fixed", "h2")
    assert by_agent["security"].status == "open"
    # h3 is complete and leaves a.py as h2 had it: nothing more to judge, and
    # the defect fix is not lost.
    record_review_run(_result("h3", _diff("-a\n+c\n"), [_sec(3)]),
                      run_id="r3", workspace_id="ws", status="complete",
                      engine=engine)
    by_agent = {i.agent: i for i in _issues(engine)}
    assert by_agent["defect"].status == "fixed"
    assert by_agent["security"].status == "open"


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


def test_reopening_the_pr_reopens_what_the_close_resolved(engine) -> None:
    kw = dict(workspace_id="ws", provider="github", repo="acme/api", number=7,
              engine=engine)
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_pr_state(state="closed", **kw)
    assert _issues(engine)[0].status == "resolved"
    record_pr_state(state="open", **kw)
    issue = _issues(engine)[0]
    assert (issue.status, issue.resolution_source, issue.closed_at) == ("open", None, None)
    # The defect is found again on the reopened PR: still open, seen twice.
    record_review_run(_result("h2", _diff("-a\n+c\n"), [_f(10)]),
                      run_id="r2", workspace_id="ws", status="complete",
                      engine=engine)
    issue = _issues(engine)[0]
    assert (issue.status, issue.occurrences) == ("open", 2)


def test_a_reopen_does_not_undo_a_persons_resolution(engine) -> None:
    kw = dict(workspace_id="ws", provider="github", repo="acme/api", number=7,
              engine=engine)
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    with Session(engine) as s:
        row = s.execute(select(ReviewIssue)).scalar_one()
        row.status, row.resolution_source = "resolved", "manual"
        s.commit()
    record_pr_state(state="closed", **kw)
    record_pr_state(state="open", **kw)
    assert (_issues(engine)[0].status, _issues(engine)[0].resolution_source) == (
        "resolved", "manual")


def test_a_refound_close_resolution_reopens_only_on_an_open_pr() -> None:
    found = found_issues([_f(10)])
    fp = found[0].fingerprint
    closed = [_existing(fp, status="resolved", source="pr_closed")]
    assert _plan(closed, found).refound[0][2] is True
    assert _plan(closed, found, pr_open=False).refound[0][2] is False


def test_a_review_finishing_after_the_close_files_its_issues_resolved(engine) -> None:
    """The close landed while the review ran: the run's snapshot still says
    open, the row says closed — and the row is newer."""
    record_pr_state(workspace_id="ws", provider="github", repo="acme/api",
                    number=7, state="closed", engine=engine)
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    issue = _issues(engine)[0]
    assert (issue.status, issue.resolution_source) == ("resolved", "pr_closed")
    assert issue.closed_at is not None
    assert _pr_row(engine).state == "closed"


def test_feedback_on_a_run_between_the_first_and_the_latest_reaches_the_issue(
    engine,
) -> None:
    for i in (1, 2, 3):
        record_review_run(_result(f"h{i}", _diff(f"-a\n+{i}\n"), [_f(10)]),
                          run_id=f"r{i}", workspace_id="ws", status="complete",
                          engine=engine)
    kw = dict(workspace_id="ws", file_path="src/a.py", title="Unchecked return",
              rule_id="defect.ret", engine=engine)
    assert apply_feedback(run_id="r2", state="dismissed",
                          pr=("github", "acme/api", 7), **kw) == 1
    assert _issues(engine)[0].status == "dismissed"
    # Another PR's coordinates match nothing.
    assert apply_feedback(run_id="r2", state=None,
                          pr=("github", "acme/api", 8), **kw) == 0
    assert apply_feedback(run_id="r2", state=None,
                          pr=("github", "acme/api", 7), **kw) == 1
    assert _issues(engine)[0].status == "open"


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


def test_a_file_the_run_skipped_is_not_fixed_by_its_changed_hash(engine) -> None:
    """The file grew past the size limit (or matched a new ignore glob): its
    section is still in raw_diff, so its hash moved, but no agent read it."""
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), [], reviewed=()),
                      run_id="r2", workspace_id="ws", status="complete",
                      engine=engine)
    assert _issues(engine)[0].status == "open"


def test_a_finding_the_deny_list_hid_is_not_a_fix(engine) -> None:
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), [],
                              suppressed={"defect.ret": 1}),
                      run_id="r2", workspace_id="ws", status="complete",
                      engine=engine)
    assert _issues(engine)[0].status == "open"


def test_flipping_a_dismissal_to_accepted_reopens(engine) -> None:
    """The review page has no "clear" — it flips the verdict. That flip
    must undo a dismissal the same way clearing does."""
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    kw = dict(workspace_id="ws", run_id="r1", file_path="src/a.py",
              title="Unchecked return", rule_id="defect.ret", engine=engine)
    apply_feedback(state="dismissed", **kw)
    assert _issues(engine)[0].status == "dismissed"
    assert apply_feedback(state="accepted", **kw) == 1
    assert _issues(engine)[0].status == "open"


def test_a_pr_row_written_first_by_the_webhook_is_reused(engine) -> None:
    """The row is inserted ON CONFLICT DO NOTHING and then read, so a second
    writer finds the first one's row instead of colliding on the unique key."""
    assert record_pr_state(workspace_id="ws", provider="github", repo="acme/api",
                           number=7, state="open", engine=engine)
    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=engine)
    record_review_run(_result("h2", _diff("-a\n+c\n"), [_f(10)]),
                      run_id="r2", workspace_id="ws", status="complete",
                      engine=engine)
    pr = _pr_row(engine)
    assert (pr.reviews_count, pr.head_sha) == (2, "h2")


def test_a_review_that_posted_is_not_failed_by_its_own_bookkeeping(monkeypatch) -> None:
    """Recording raised after a posted review: the queue writer used to mark
    the run failed, count the PR's review twice and re-raise into the queue."""
    import asyncio

    import src.api.review_runs as runs_mod
    import src.review.issues as issues_mod
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod
    from src.sync.handlers import handle_review

    result = _result("h1", _diff("-a\n+b\n"), [_f(10)])
    updates: list[dict] = []
    failed_calls: list[dict] = []

    class _Store:
        def insert(self, row):
            pass

        def update(self, run_id, **kw):
            updates.append(kw)

    class _Orch:
        _last_drift_facts = None

        def review(self, *a, **kw):
            return result

    def _boom(*a, **kw):
        raise RuntimeError("sqlite is locked")

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", _Orch)
    monkeypatch.setattr(providers_mod, "get_provider_for",
                        lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runs_mod, "get_review_run_store", lambda: _Store())
    monkeypatch.setattr(runs_mod, "record_completed_review", _boom)
    monkeypatch.setattr(issues_mod, "record_failed_review",
                        lambda **kw: failed_calls.append(kw))

    asyncio.run(handle_review({"payload": {
        "provider": "github", "repo": "acme/api", "pr_number": 7,
        "user_id": "u", "workspace_id": "ws",
    }}))
    assert failed_calls == []
    assert updates and all(u.get("status") != "failed" for u in updates)
    assert updates[-1]["finished"] is True


def test_the_ledger_never_breaks_the_review(caplog) -> None:
    """No DATABASE_URL, a broken engine: a warning, never an exception."""
    class _Broken:
        def connect(self, *a, **kw):
            raise RuntimeError("db down")

    record_review_run(_result("h1", _diff("-a\n+b\n"), [_f(10)]),
                      run_id="r1", workspace_id="ws", status="complete",
                      engine=_Broken())
    assert "review_issues_sync_failed" in caplog.text


# ─── the flagged line, not the file ─────────────────────────────────────
#
# The first live run: one commit fixed `refund` in app/orders.py, the model did
# not repeat a divide-by-zero further down the same file, and the file's
# section hash had moved — so the untouched divide-by-zero was called fixed.

_DIFF_V1 = """diff --git a/app/orders.py b/app/orders.py
--- a/app/orders.py
+++ b/app/orders.py
@@ -18,0 +19,8 @@
+def refund(total_cents: int, refunded_cents: int) -> int:
+    remaining = total_cents + refunded_cents
+    return remaining
+
+
+def average_item_price(items):
+    return subtotal(items) // len(items)
+
"""

_DIFF_V2 = _DIFF_V1.replace(
    "remaining = total_cents + refunded_cents", "remaining = total_cents - refunded_cents")


def _orders_plan(existing, found=()):
    from src.review.issues import anchors_present, file_section_hashes, plan_sync
    return plan_sync(
        existing, list(found), run_reviewed=True, head_sha="b", prev_head_sha="a",
        prev_file_hashes=file_section_hashes(_DIFF_V1),
        new_file_hashes=file_section_hashes(_DIFF_V2),
        reviewed_files={"app/orders.py"},
        new_anchors=anchors_present(_DIFF_V2),
    )


def test_the_anchor_is_the_flagged_lines_text():
    from src.review.issues import anchor_at
    assert anchor_at(_DIFF_V1, "app/orders.py", 25) == "return subtotal(items) // len(items)"
    assert anchor_at(_DIFF_V1, "app/orders.py", 20) == "remaining = total_cents + refunded_cents"
    assert anchor_at(_DIFF_V1, "app/other.py", 20) is None
    assert anchor_at(_DIFF_V1, "app/orders.py", None) is None


def test_an_untouched_line_in_a_changed_file_stays_open():
    from src.review.issues import ExistingIssue, anchor_at
    div = ExistingIssue(id="div", fingerprint="f-div", file_path="app/orders.py",
                        agent="defect", status="open", resolution_source=None,
                        anchor=anchor_at(_DIFF_V1, "app/orders.py", 25))
    refund = ExistingIssue(id="ref", fingerprint="f-ref", file_path="app/orders.py",
                           agent="defect", status="open", resolution_source=None,
                           anchor=anchor_at(_DIFF_V1, "app/orders.py", 20))
    plan = _orders_plan([div, refund])
    assert plan.fixed == ["ref"]


def test_an_issue_without_an_anchor_keeps_the_file_rule():
    # Rows written before the anchor existed: the old rule still decides.
    from src.review.issues import ExistingIssue
    old = ExistingIssue(id="old", fingerprint="f-old", file_path="app/orders.py",
                        agent="defect", status="open", resolution_source=None)
    assert _orders_plan([old]).fixed == ["old"]


# ─── a re-worded finding is the issue it already is ─────────────────────
#
# Live: the same divide-by-zero came back as "Potential ZeroDivisionError on
# empty list" after "Division by zero on empty totals list", pointing one line
# lower — a second row for one defect.


def _found(title, line, *, agent="defect", path="app/orders.py", diff=_DIFF_V1):
    from src.review.issues import FoundIssue, fingerprint, near_lines
    return FoundIssue(
        fingerprint=fingerprint(None, path, title), file_path=path, line=line,
        agent=agent, rule_id=None, category="bug", severity="error",
        title=title, body="", suggestion=None,
        near=near_lines(diff, path, line))


def _open(id_, title, line, *, agent="defect"):
    from src.review.issues import ExistingIssue, anchor_at, fingerprint
    return ExistingIssue(
        id=id_, fingerprint=fingerprint(None, "app/orders.py", title),
        file_path="app/orders.py", agent=agent, status="open",
        resolution_source=None, anchor=anchor_at(_DIFF_V1, "app/orders.py", line))


def _same_head(existing, found):
    from src.review.issues import plan_sync
    return plan_sync(existing, found, run_reviewed=True, head_sha="a",
                     prev_head_sha="a", prev_file_hashes={}, new_file_hashes={})


def test_a_reworded_finding_one_line_off_refinds_the_issue():
    plan = _same_head([_open("div", "Division by zero on empty totals list", 25)],
                      [_found("Potential ZeroDivisionError on empty list", 24)])
    assert plan.create == []
    assert [r[0] for r in plan.refound] == ["div"]


def test_another_agent_or_a_far_line_is_a_new_issue():
    plan = _same_head([_open("div", "Division by zero on empty totals list", 25)],
                      [_found("Potential ZeroDivisionError", 24, agent="security"),
                       _found("Refund adds instead of subtracting", 20)])
    assert len(plan.create) == 2 and plan.refound == []


def test_one_issue_is_claimed_once_and_exact_matches_win():
    title = "Division by zero on empty totals list"
    plan = _same_head([_open("div", title, 25)],
                      [_found("Potential ZeroDivisionError on empty list", 24),
                       _found(title, 25)])
    assert [r[0] for r in plan.refound] == ["div"]
    assert [f.title for f in plan.create] == ["Potential ZeroDivisionError on empty list"]


def test_a_reworded_refind_is_not_called_fixed_on_the_next_head():
    from src.review.issues import file_section_hashes, plan_sync
    plan = plan_sync(
        [_open("div", "Division by zero on empty totals list", 25)],
        [_found("Potential ZeroDivisionError on empty list", 24, diff=_DIFF_V2)],
        run_reviewed=True, head_sha="b", prev_head_sha="a",
        prev_file_hashes=file_section_hashes(_DIFF_V1),
        new_file_hashes=file_section_hashes(_DIFF_V2),
        reviewed_files={"app/orders.py"})
    assert plan.fixed == [] and [r[0] for r in plan.refound] == ["div"]


# ─── what an anchor may be, and what a re-wording must share ────────────


def test_a_trivial_or_repeated_or_context_line_is_no_anchor():
    from src.review.issues import anchor_at
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,5 @@\n"
            " def keep_this_context_line():\n"
            "+    }\n"
            "+    total = compute_the_total(items)\n"
            "+    total = compute_the_total(items)\n"
            "+    unique_and_meaningful_line = 1\n")
    assert anchor_at(diff, "x.py", 1) is None   # context: may leave the diff
    assert anchor_at(diff, "x.py", 2) is None   # "}" says nothing
    assert anchor_at(diff, "x.py", 3) is None   # repeated in the file's diff
    assert anchor_at(diff, "x.py", 5) == "unique_and_meaningful_line = 1"


def test_an_added_line_that_starts_with_plus_plus_keeps_the_numbering():
    from src.review.issues import anchor_at
    diff = ("diff --git a/post.md b/post.md\nnew file mode 100644\n--- /dev/null\n"
            "+++ b/post.md\n@@ -0,0 +1,4 @@\n"
            "++++\n+title = \"hello world post\"\n++++\n+the actual body sentence\n")
    assert anchor_at(diff, "post.md", 2) == 'title = "hello world post"'
    assert anchor_at(diff, "post.md", 4) == "the actual body sentence"


def test_a_different_rule_next_to_the_anchor_is_a_new_issue():
    from src.review.issues import ExistingIssue, FoundIssue, anchor_at, near_lines
    e = ExistingIssue(id="a", fingerprint="fp-a", file_path="app/orders.py",
                      agent="defect", status="open", resolution_source=None,
                      rule_id="defect.zero", anchor=anchor_at(_DIFF_V1, "app/orders.py", 25))
    f = FoundIssue(fingerprint="fp-b", file_path="app/orders.py", line=24,
                   agent="defect", rule_id="defect.overflow", category="bug",
                   severity="error", title="Overflow", body="", suggestion=None,
                   near=near_lines(_DIFF_V1, "app/orders.py", 24))
    plan = _same_head([e], [f])
    assert plan.refound == [] and len(plan.create) == 1


def test_fixed_line_agents_never_match_by_position():
    from src.review.issues import ExistingIssue, FoundIssue, near_lines
    e = ExistingIssue(id="c", fingerprint="fp-c", file_path="app/orders.py",
                      agent="compliance", status="open", resolution_source=None,
                      anchor="def refund(total_cents: int, refunded_cents: int) -> int:")
    f = FoundIssue(fingerprint="fp-d", file_path="app/orders.py", line=19,
                   agent="compliance", rule_id=None, category="other",
                   severity="error", title="Another policy", body="", suggestion=None,
                   near=near_lines(_DIFF_V1, "app/orders.py", 19))
    plan = _same_head([e], [f])
    assert plan.refound == [] and len(plan.create) == 1


def test_a_reworded_refind_is_rekeyed_so_feedback_reaches_it(engine) -> None:
    body = "+    totals_divided = sum(totals) / len(totals)\n"
    record_review_run(_result("h1", _diff(body), [_f(1, "Division by zero")]),
                      run_id="r1", workspace_id="ws", status="complete", engine=engine)
    record_review_run(_result("h1", _diff(body), [_f(1, "Possible ZeroDivisionError")]),
                      run_id="r2", workspace_id="ws", status="complete", engine=engine)
    [issue] = _issues(engine)
    assert issue.title == "Possible ZeroDivisionError" and issue.occurrences == 2
    n = apply_feedback(workspace_id="ws", run_id="r2", state="dismissed",
                       file_path="src/a.py", title="Possible ZeroDivisionError",
                       rule_id="defect.ret", engine=engine)
    assert n == 1 and _issues(engine)[0].status == "dismissed"
