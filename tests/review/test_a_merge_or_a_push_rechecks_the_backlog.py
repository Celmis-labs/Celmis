"""The webhooks that move a branch ask for a backlog recheck — and only then.

  * the PR-state extractors carry the target branch for all three providers;
  * a MERGE records the state and schedules one recheck of that branch; a
    close without a merge, a reopen and a branch with no backlog schedule none;
  * a push to a branch asks the same, under the same fail-closed tenant rule
    as every other dispatcher: an unbound repo, or a delivery signed for
    another workspace, does nothing;
  * the daily sweep rechecks each (workspace, repo, branch) with something to
    watch, and one branch failing does not end the pass.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewIssue
from src.review import issue_resolver, issues_sweep, webhook


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


# ─── The extractors ────────────────────────────────────────────────


def test_each_providers_state_event_names_the_target_branch() -> None:
    gh = webhook._extract_github_pr_state({
        "action": "closed", "repository": {"full_name": "o/r"},
        "pull_request": {"number": 4, "merged": True, "base": {"ref": "release/1"}}})
    gl = webhook._extract_gitlab_mr_state({
        "object_kind": "merge_request", "project": {"path_with_namespace": "g/p"},
        "object_attributes": {"action": "merge", "iid": 4, "target_branch": "develop"}})
    bb = webhook._extract_bitbucket_pr_state({
        "repository": {"full_name": "w/r"},
        "pullrequest": {"id": 4, "destination": {"branch": {"name": "main"}}}},
        "pullrequest:fulfilled")
    assert (gh["base_ref"], gl["base_ref"], bb["base_ref"]) == ("release/1", "develop", "main")
    assert (gh["state"], gl["state"], bb["state"]) == ("merged", "merged", "merged")


# ─── The dispatchers ───────────────────────────────────────────────


@pytest.fixture
def wired(monkeypatch):
    recorded: dict[str, list] = {"state": [], "scheduled": []}
    cfg = SimpleNamespace(workspace_id="ws-1", user_id="u")
    store = SimpleNamespace(config_for_repo=lambda p, r: cfg if r == "o/r" else None)
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: store)
    monkeypatch.setattr("src.review.issues.record_pr_state",
                        lambda **kw: recorded["state"].append(kw) or True)
    monkeypatch.setattr(issue_resolver, "has_backlog", lambda *a, **kw: recorded.get("backlog", True))

    async def schedule(*a, **kw):
        recorded["scheduled"].append((a, kw))
        return True

    monkeypatch.setattr(issue_resolver, "schedule_recheck", schedule)
    return recorded


def _merged(**kw):
    return {"provider": "github", "repo": "o/r", "number": 4, "state": "merged",
            "title": "t", "author": "a", "url": "u", "base_ref": "main", **kw}


def test_a_merge_records_the_state_and_schedules_one_recheck_of_its_branch(wired) -> None:
    asyncio.run(webhook._dispatch_pr_state(_merged(), expected_workspace_id="ws-1"))
    assert wired["state"][0]["base_ref"] == "main"
    [(args, kw)] = wired["scheduled"]
    assert args == ("ws-1", "github", "o/r", "main") and kw["merged_pr"] == 4


@pytest.mark.parametrize("state", ["closed", "open"])
def test_a_close_without_a_merge_and_a_reopen_schedule_nothing(wired, state) -> None:
    asyncio.run(webhook._dispatch_pr_state(_merged(state=state), expected_workspace_id="ws-1"))
    assert wired["state"] and wired["scheduled"] == []


def test_a_branch_with_no_backlog_is_not_scheduled(wired) -> None:
    wired["backlog"] = False
    asyncio.run(webhook._dispatch_pr_state(_merged(), expected_workspace_id="ws-1"))
    assert wired["scheduled"] == []


def test_the_branch_falls_back_to_the_one_the_ledger_stored(wired, monkeypatch) -> None:
    monkeypatch.setattr("src.review.issues.pr_base_ref", lambda **kw: "stored-branch")
    asyncio.run(webhook._dispatch_pr_state(_merged(base_ref=None), expected_workspace_id="ws-1"))
    assert wired["scheduled"][0][0][3] == "stored-branch"


def test_a_delivery_signed_for_another_workspace_does_nothing(wired) -> None:
    asyncio.run(webhook._dispatch_pr_state(_merged(), expected_workspace_id="ws-2"))
    asyncio.run(webhook._dispatch_issue_recheck("github", "o/r", "main",
                                                expected_workspace_id="ws-2"))
    assert wired["state"] == [] and wired["scheduled"] == []


def test_an_unbound_repository_does_nothing(wired) -> None:
    asyncio.run(webhook._dispatch_issue_recheck("github", "x/unbound", "main",
                                                expected_workspace_id=None))
    assert wired["scheduled"] == []


def test_a_push_to_a_branch_schedules_its_recheck(wired) -> None:
    asyncio.run(webhook._dispatch_issue_recheck("github", "o/r", "release/1.x",
                                                expected_workspace_id="ws-1"))
    [(args, kw)] = wired["scheduled"]
    assert args == ("ws-1", "github", "o/r", "release/1.x") and kw["reason"] == "push"


def test_a_scheduling_failure_never_reaches_the_webhook(wired, monkeypatch) -> None:
    async def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(issue_resolver, "schedule_recheck", boom)
    asyncio.run(webhook._dispatch_pr_state(_merged(), expected_workspace_id="ws-1"))


# ─── The daily sweep ───────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(eng)
    yield eng
    eng.dispose()


def _row(i, *, ws="ws-1", repo="o/r", base="main", status="open", source=None, merged=True, dup=None):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return ReviewIssue(
        id=i, workspace_id=ws, repo_slug=repo.replace("/", "-"), fingerprint=i, file_path="a.py",
        title="t", body="b", status=status, resolution_source=source, pr_provider="github",
        pr_repo=repo, pr_number=1, first_seen_at=now, last_seen_at=now,
        merged_at=now if merged else None, base_ref=base if merged else None, dup_of=dup)


def test_the_sweep_lists_each_branch_with_something_to_watch_once(engine) -> None:
    with Session(engine) as s:
        s.add_all([
            _row("1"), _row("2"),                                   # one branch, two issues
            _row("3", base="dev"),
            _row("4", status="fixed", source="auto_head_check", repo="o/other"),  # revert watch
            _row("5", status="fixed", source="manual"),             # a person's: not watched
            _row("6", merged=False),                                # not merged: not backlog
            _row("7", dup="1", base="x"),                           # a repeat: its original is
            _row("8", ws="ws-2"),
        ])
        s.commit()
    assert issues_sweep.backlog_branches(engine) == [
        ("ws-1", "github", "o/other", "main"),
        ("ws-1", "github", "o/r", "dev"),
        ("ws-1", "github", "o/r", "main"),
        ("ws-2", "github", "o/r", "main"),
    ]


def test_one_failing_branch_does_not_end_the_sweep(engine, monkeypatch) -> None:
    with Session(engine) as s:
        s.add_all([_row("1"), _row("3", base="dev")])
        s.commit()
    seen = []

    def recheck(ws, prov, repo, base, **kw):
        seen.append((base, kw["reason"]))
        if base == "dev":
            raise RuntimeError("provider down")
        return issue_resolver.RecheckResult("done", {"resolved": 2, "reopened": 1})

    monkeypatch.setattr(issue_resolver, "recheck_backlog", recheck)
    summary = asyncio.run(issues_sweep.sweep_once(stagger=0, engine=engine))
    assert sorted(seen) == [("dev", "sweep"), ("main", "sweep")]
    assert (summary["branches"], summary["done"], summary["unreadable"]) == (2, 1, 1)
    assert (summary["resolved"], summary["reopened"]) == (2, 1)


def test_the_sweep_can_be_switched_off_and_a_typo_cannot_kill_it(monkeypatch) -> None:
    monkeypatch.setenv("CELMIS_ISSUES_SWEEP_INTERVAL_HOURS", "0")
    issues_sweep.stop_issues_sweep()
    issues_sweep.start_issues_sweep()
    assert issues_sweep._TASK is None
    monkeypatch.setenv("CELMIS_ISSUES_SWEEP_INTERVAL_HOURS", "daily")
    assert issues_sweep._hours() == 24.0
