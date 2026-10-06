"""A merge turns a PR's open issues into a backlog, and the backlog is rechecked.

Three things are pinned on sqlite with the real models:

  * the FATE of each issue is frozen when its PR merges or closes
    (`close_outcome`: implemented | unimplemented | dismissed | abandoned) and
    nothing later moves it — a fix that lands months afterwards is a
    `resolved_later` event, not a retroactive "implemented";
  * `recheck_backlog` closes an issue only against the target branch's head,
    re-reads nothing when that head has not moved, never overrules a person,
    and puts an issue back when a revert brings its line back;
  * a repeat of a backlog issue on another PR points at it (`dup_of`) and
    follows its fate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewIssue, ReviewIssueRecheckState, ReviewPullRequest
from src.review import issue_resolver as resolver
from src.review import outcome_hooks
from src.review.issues import (
    implementation_stats,
    record_pr_state,
    record_review_run,
)
from src.review.models import (
    EarlierIssues,
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
    ScopeInfo,
)
from src.review.providers.base import PathCommit
from tests.review.test_an_issue_is_fixed_only_when_the_target_branch_says_so import (
    LINE,
    FakeProvider,
)

WS = "ws"
REPO = "acme/api"


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    for table in (ReviewIssue, ReviewPullRequest, ReviewIssueRecheckState):
        table.__table__.create(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def heard():
    seen: list[outcome_hooks.IssueOutcome] = []
    outcome_hooks.register_outcome_listener(seen.append)
    yield seen
    outcome_hooks.unregister_outcome_listener(seen.append)


def _diff(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,3 @@\n{body}")


def _result(number: int, head: str, findings, *, path="src/a.py", state="open"):
    body = f"+import os\n+import sys\n+{LINE}\n"
    pr = PullRequest(
        provider="github", repo=REPO, number=number, title=f"PR {number}",
        description="", author="dana", base_ref="main", base_sha="b",
        head_ref=f"feat/{number}", head_sha=head, state=state,
        url=f"https://github.com/{REPO}/pull/{number}", raw_diff=_diff(path, body),
        hunks=[Hunk(file_path=path, old_file_path=path, old_start=0, old_count=0,
                    new_start=1, new_count=3, content="@@ -0,0 +1,3 @@\n")],
    )
    batch = ReviewBatch(pull_request=pr, findings=findings, verdict=ReviewVerdict.COMMENT)
    batch.agents_run = ["defect"]
    return SimpleNamespace(batch=batch, posted=True, provider_response={})


def _finding(title="Total may overflow", path="src/a.py", line=3) -> Finding:
    return Finding(file_path=path, line=line, title=title, severity=FindingSeverity.ERROR,
                   rule_id="defect.sum", agent="defect", body="b", suggestion="fix")


def _review(engine, number=7, head="h1", findings=None, **kw):
    record_review_run(_result(number, head, findings if findings is not None else [_finding()], **kw),
                      run_id=f"r{number}{head}", workspace_id=WS, status="complete",
                      engine=engine)


def _issues(engine) -> list[ReviewIssue]:
    with Session(engine) as s:
        return list(s.execute(select(ReviewIssue).order_by(ReviewIssue.pr_number)).scalars())


def _merge(engine, number=7):
    assert record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=number,
                           state="merged", engine=engine)


def _settings(**kw) -> resolver.IssueSettings:
    return resolver.IssueSettings(**{"max_llm": 8, **kw})


def _recheck(engine, provider, verifier=None, **kw):
    return resolver.recheck_backlog(
        WS, "github", REPO, "main", provider=provider, engine=engine,
        settings=kw.pop("settings", _settings()),
        verifier_factory=(lambda: verifier) if verifier is not None else None, **kw)


# ─── The fate is frozen at the merge ───────────────────────────────


def test_a_merge_freezes_each_issues_fate_and_stamps_the_backlog_columns(engine) -> None:
    _review(engine, findings=[_finding("Open one"), _finding("Dismissed one", line=2)])
    with Session(engine) as s:
        dismissed = s.execute(select(ReviewIssue).where(
            ReviewIssue.title == "Dismissed one")).scalar_one()
        dismissed.status = "dismissed"
        s.commit()
    _merge(engine)
    by_title = {i.title: i for i in _issues(engine)}
    assert by_title["Open one"].close_outcome == "unimplemented"
    assert by_title["Dismissed one"].close_outcome == "dismissed"
    for i in by_title.values():
        assert i.merged_at is not None and i.base_ref == "main"
    assert by_title["Open one"].status == "open"  # backlog = open + merged_at


def test_a_pr_closed_unmerged_abandons_its_issues_and_a_reopen_undoes_it(engine) -> None:
    _review(engine)
    record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=7,
                    state="closed", engine=engine)
    [i] = _issues(engine)
    assert (i.status, i.close_outcome, i.merged_at) == ("resolved", "abandoned", None)
    record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=7,
                    state="open", engine=engine)
    [i] = _issues(engine)
    assert (i.status, i.close_outcome) == ("open", None)


def test_the_implementation_rate_counts_only_what_the_merge_froze(engine) -> None:
    rows = [{"close_outcome": o} for o in
            ["implemented"] * 3 + ["unimplemented"] + ["dismissed"] * 5 + ["abandoned"] * 2 + [None]]
    stats = implementation_stats(rows)
    assert stats["implementation_rate"] == pytest.approx(0.75)
    assert (stats["dismissed"], stats["abandoned"]) == (5, 2)
    assert implementation_stats([])["implementation_rate"] is None


def test_a_review_that_arrives_after_the_merge_still_freezes_its_issues(engine) -> None:
    record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=7,
                    state="merged", base_ref="main", engine=engine)
    _review(engine, state="merged")
    [i] = _issues(engine)
    assert i.merged_at is not None and i.close_outcome == "unimplemented"


# ─── The recheck ───────────────────────────────────────────────────


def _backlog(engine, **kw) -> ReviewIssue:
    _review(engine, **kw)
    _merge(engine)
    [i] = _issues(engine)
    return i


def test_a_later_fix_resolves_the_issue_without_rewriting_its_frozen_fate(engine, heard) -> None:
    _backlog(engine)
    fix = PathCommit(sha="c2", subject="Fix (pull request #12)", date="2026-09-20T00:00:00Z")
    old = PathCommit(sha="c1", subject="Add", date="2026-09-02T00:00:00Z")
    provider = FakeProvider(
        {("h2", "src/a.py"): "import os\n", ("c1", "src/a.py"): f"{LINE}\n",
         ("c2", "src/a.py"): "import os\n"},
        head="h2", commits={"src/a.py": [fix, old]})
    res = _recheck(engine, provider, _FixAll())
    assert res.status == "done" and res.counts["resolved"] == 1
    [i] = _issues(engine)
    assert (i.status, i.resolution_source) == ("fixed", "auto_head_check")
    assert (i.fixed_by_pr_number, i.fixed_in_sha) == (12, "c2")
    assert i.close_outcome == "unimplemented"       # frozen at the merge
    assert [e.kind for e in heard if e.issue_id == i.id] == ["unimplemented", "resolved_later"]
    assert heard[-1].fixed_by_pr_number == 12


class _FixAll:
    def __init__(self):
        self.calls = 0

    def verify(self, path, items):
        self.calls += 1
        return {sid: ("fixed", "gone") for sid, *_ in items}


def test_a_head_that_has_not_moved_costs_no_reads(engine) -> None:
    _backlog(engine)
    provider = FakeProvider({("h1", "src/a.py"): f"import os\n{LINE}\n"})
    _recheck(engine, provider)
    first = len(provider.reads)
    assert first == 1
    again = _recheck(engine, provider)
    assert len(provider.reads) == first
    assert again.status == "done"
    with Session(engine) as s:
        state = s.get(ReviewIssueRecheckState, (WS, "github", REPO, "main"))
        assert state.last_head_sha == "h1" and state.last_result["checked"] == 0


def test_a_line_still_on_the_branch_leaves_the_issue_open_with_no_model_call(engine) -> None:
    _backlog(engine)
    verifier = _FixAll()
    _recheck(engine, FakeProvider({("h1", "src/a.py"): f"import os\n{LINE}\n"}), verifier)
    [i] = _issues(engine)
    assert (i.status, i.last_checked_sha) == ("open", "h1")
    assert verifier.calls == 0


def test_an_unreadable_branch_changes_nothing_and_is_retried_next_time(engine) -> None:
    _backlog(engine)
    res = _recheck(engine, FakeProvider(fail=True), _FixAll())
    assert res.status == "unreadable"
    [i] = _issues(engine)
    assert (i.status, i.last_checked_sha) == ("open", None)


def test_a_person_who_dismissed_the_issue_is_not_overruled(engine) -> None:
    issue = _backlog(engine)
    with Session(engine) as s:
        row = s.get(ReviewIssue, issue.id)
        row.status, row.resolution_source = "dismissed", "manual"
        s.commit()
    _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"), _FixAll())
    [i] = _issues(engine)
    assert (i.status, i.resolution_source) == ("dismissed", "manual")


def test_a_revert_that_brings_the_line_back_reopens_an_auto_fixed_issue(engine, heard) -> None:
    _backlog(engine)
    _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"), _FixAll())
    assert _issues(engine)[0].status == "fixed"
    res = _recheck(engine, FakeProvider(
        {("h3", "src/a.py"): f"x\nimport os\nimport sys\n{LINE}\n"}, head="h3"))
    assert res.counts["reopened"] == 1
    [i] = _issues(engine)
    assert (i.status, i.resolution_source, i.fixed_by_pr_number) == ("open", None, None)
    assert heard[-1].kind == "reopened"


def test_a_manual_fix_is_not_reopened_by_a_revert(engine) -> None:
    issue = _backlog(engine)
    with Session(engine) as s:
        row = s.get(ReviewIssue, issue.id)
        row.status, row.resolution_source = "fixed", "manual"
        s.commit()
    _recheck(engine, FakeProvider({("h2", "src/a.py"): f"{LINE}\n"}, head="h2"))
    assert _issues(engine)[0].status == "fixed"


def test_the_setting_that_turns_resolution_off_turns_the_recheck_off(engine) -> None:
    _backlog(engine)
    provider = FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2")
    res = _recheck(engine, provider, _FixAll(), settings=_settings(auto_resolve=False))
    assert res.status == "disabled" and provider.reads == []


def test_a_branch_being_rechecked_answers_busy_instead_of_judging_twice(engine) -> None:
    _backlog(engine)
    key = (WS, "github", REPO, "main")
    resolver._RUNNING.add(key)
    try:
        assert _recheck(engine, FakeProvider({}, head="h2")).status == "busy"
    finally:
        resolver._RUNNING.discard(key)
    assert _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"),
                    _FixAll()).status == "done"


def test_a_branch_with_no_backlog_is_nothing_to_do(engine) -> None:
    _review(engine)       # not merged: not backlog
    assert _recheck(engine, FakeProvider({}, head="h2")).status == "nothing"


# ─── Repeats on another PR ─────────────────────────────────────────


def test_a_repeat_on_another_pr_points_at_the_backlog_issue_and_follows_its_fate(engine) -> None:
    canonical = _backlog(engine)
    _review(engine, number=8, head="k1")
    dup = next(i for i in _issues(engine) if i.pr_number == 8)
    assert dup.dup_of == canonical.id and dup.status == "open"
    # PR 8 merges too: it is a backlog duplicate now.
    _merge(engine, number=8)
    _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"), _FixAll())
    by_pr = {i.pr_number: i for i in _issues(engine)}
    assert by_pr[7].status == "fixed" and by_pr[8].status == "fixed"
    assert by_pr[8].resolution_source == "auto_head_check"


def test_a_repeat_is_not_checked_on_its_own(engine) -> None:
    _backlog(engine)
    _review(engine, number=8, head="k1")
    _merge(engine, number=8)
    provider = FakeProvider({("h2", "src/a.py"): f"{LINE}\n"}, head="h2")
    _recheck(engine, provider)
    assert len(provider.reads) == 1


# ─── The review's own stage ────────────────────────────────────────


def _pr_obj(number=9, files=("src/a.py",)) -> PullRequest:
    return PullRequest(
        provider="github", repo=REPO, number=number, title="t", description="",
        author="a", base_ref="main", base_sha="b", head_ref="f", head_sha="s",
        state="open", url="u",
        hunks=[Hunk(file_path=p, old_file_path=p, old_start=1, old_count=1,
                    new_start=1, new_count=1, content="@@") for p in files])


def test_the_stage_finds_a_fix_on_the_files_the_pr_changes_and_writes_nothing(engine) -> None:
    _backlog(engine)
    provider = FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2")
    found = resolver.plan_review_resolutions(
        _pr_obj(), provider=provider, workspace_id=WS, settings=_settings(),
        verifier_factory=lambda: _FixAll(), engine=engine)
    assert isinstance(found, EarlierIssues)
    assert [r["title"] for r in found.resolved] == ["Total may overflow"]
    assert found.resolutions and _issues(engine)[0].status == "open"


def test_the_stage_ignores_issues_on_files_the_pr_does_not_touch(engine) -> None:
    _backlog(engine)
    found = resolver.plan_review_resolutions(
        _pr_obj(files=("src/other.py",)), provider=FakeProvider({}, head="h2"),
        workspace_id=WS, settings=_settings(), engine=engine)
    assert found is None


def test_an_incremental_review_checks_the_files_of_the_whole_pull_request(engine) -> None:
    """The agents read only the new commits, but a fix an earlier push of this
    PR brought is still a fix: the stage looks at every file of the PR."""
    _backlog(engine)
    whole = _pr_obj(files=("src/a.py", "src/b.py"))
    pr = _pr_obj(files=("src/b.py",))  # the increment touches b only
    pr.scope = ScopeInfo(base_sha="1" * 40, new_commits=1, full_hunks=whole.hunks)
    provider = FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2")
    found = resolver.plan_review_resolutions(
        pr, provider=provider, workspace_id=WS, settings=_settings(),
        verifier_factory=lambda: _FixAll(), engine=engine)
    assert [r["title"] for r in found.resolved] == ["Total may overflow"]


def test_what_the_stage_found_is_written_when_the_run_is_recorded(engine) -> None:
    _backlog(engine)
    provider = FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2")
    found = resolver.plan_review_resolutions(
        _pr_obj(), provider=provider, workspace_id=WS, settings=_settings(),
        verifier_factory=lambda: _FixAll(), engine=engine)
    result = _result(9, "s1", [])
    result.batch.earlier_issues = found
    record_review_run(result, run_id="r9", workspace_id=WS, status="complete", engine=engine)
    first = next(i for i in _issues(engine) if i.pr_number == 7)
    assert first.status == "fixed" and first.resolution_source == "auto_head_check"


def test_the_stage_is_off_when_resolution_is_off(engine) -> None:
    _backlog(engine)
    assert resolver.plan_review_resolutions(
        _pr_obj(), provider=FakeProvider({}, head="h2"), workspace_id=WS,
        settings=_settings(auto_resolve=False), engine=engine) is None


def test_an_unreadable_branch_is_counted_by_the_stage_not_resolved(engine) -> None:
    _backlog(engine)
    found = resolver.plan_review_resolutions(
        _pr_obj(), provider=FakeProvider(fail=True), workspace_id=WS,
        settings=_settings(), engine=engine)
    assert (found.unreadable, found.resolved, found.resolutions) == (1, [], [])


# ─── What the comment says ─────────────────────────────────────────


def _batch(resolved, lang=None, announce=True):
    b = SimpleNamespace(review_language=lang)
    b.earlier_issues = SimpleNamespace(resolved=resolved, announce=announce)
    return b


ITEM = {"id": "i", "title": "Total may overflow @bob", "file": "src/a.py", "pr": 12, "sha": "c2"}


def test_the_comment_lists_the_resolved_issues_with_the_pr_that_fixed_them() -> None:
    text = resolver.earlier_issues_section(_batch([ITEM]))
    assert "Resolved 1 earlier issue" in text
    assert "fixed in PR #12" in text and "`src/a.py`" in text
    assert "@bob" not in text.replace("@​bob", "")   # no ping from a bot comment


def test_the_comment_is_written_in_the_review_language() -> None:
    assert "Закрито 1 раніше знайдену проблему" in resolver.earlier_issues_section(
        _batch([ITEM], "uk"))


def test_a_repository_that_does_not_announce_gets_no_section() -> None:
    assert resolver.earlier_issues_section(_batch([ITEM], announce=False)) == ""
    assert resolver.earlier_issues_section(_batch([])) == ""
    assert resolver.earlier_issues_section(SimpleNamespace(earlier_issues=None)) == ""


def test_a_long_list_is_cut_with_a_count_of_the_rest() -> None:
    many = [{**ITEM, "id": str(n), "title": f"t{n}"} for n in range(12)]
    text = resolver.earlier_issues_section(_batch(many))
    assert text.count("\n- ") == resolver.MAX_LISTED + 1
    assert "and 4 more" in text


def test_the_comment_carries_no_raw_html() -> None:
    assert "<" not in resolver.earlier_issues_section(_batch([ITEM]))


def test_a_burst_of_merges_is_one_recheck_that_knows_every_merged_pr(monkeypatch) -> None:
    import asyncio

    calls: list[tuple] = []
    monkeypatch.setattr(resolver, "recheck_backlog",
                        lambda *a, **kw: calls.append((a, kw)))

    async def go() -> list[bool]:
        started = [await resolver.schedule_recheck(WS, "github", REPO, "main",
                                                   merged_pr=n, delay=0.05)
                   for n in (5, 6, 7)]
        await asyncio.sleep(0.3)
        return started

    assert asyncio.run(go()) == [True, False, False]
    assert len(calls) == 1
    assert calls[0][1]["merged_prs"] == [5, 6, 7]
    assert calls[0][0][:4] == (WS, "github", REPO, "main")


def test_a_trigger_without_a_branch_does_nothing() -> None:
    import asyncio

    assert asyncio.run(resolver.schedule_recheck(WS, "github", REPO, "")) is False
