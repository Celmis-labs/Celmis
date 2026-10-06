"""The backlog belongs to the branch a PR really landed on.

Second review of the issues backlog:

  * a PR retargeted after its last review (a stacked PR, once its parent
    merged) is stamped with the branch the merge webhook names;
  * a repeat of an issue is linked only inside one target branch, and never
    closed or reopened through a canonical issue of another;
  * a revert reopens an auto-fixed issue only where the issue was raised, not
    for the same line of text somewhere else in the file;
  * the checks are bounded: the prompt's region in characters, the pass's
    candidates by how long ago each was looked at, and the settings by the
    repository's own policy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.db.models import (
    RepoReviewPolicy,
    ReviewIssue,
    ReviewIssueRecheckState,
    ReviewPullRequest,
    WorkspaceReviewDefaults,
)
from src.review import issue_resolver as resolver
from src.review.issues import record_pr_state, record_review_run
from tests.review.test_a_merged_pr_leaves_its_issues_on_the_backlog import (
    REPO,
    WS,
    _FixAll,
    _issues,
    _merge,
    _recheck,
    _result,
    _review,
)
from tests.review.test_an_issue_is_fixed_only_when_the_target_branch_says_so import (
    LINE,
    FakeProvider,
    _cand,
    _view,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    for table in (ReviewIssue, ReviewPullRequest, ReviewIssueRecheckState,
                  RepoReviewPolicy, WorkspaceReviewDefaults):
        table.__table__.create(eng)
    yield eng
    eng.dispose()


def _review_into(engine, number: int, base: str, head: str = "h1") -> None:
    from tests.review.test_a_merged_pr_leaves_its_issues_on_the_backlog import _finding

    result = _result(number, head, [_finding()])
    result.batch.pull_request.base_ref = base
    record_review_run(result, run_id=f"r{number}{head}", workspace_id=WS,
                      status="complete", engine=engine)


def _by_pr(engine) -> dict[int, ReviewIssue]:
    return {i.pr_number: i for i in _issues(engine)}


# ─── A retarget before the merge ───────────────────────────────────


def test_a_merge_stamps_the_branch_it_landed_on_not_the_one_last_reviewed(engine) -> None:
    _review_into(engine, 7, "feature/parent")
    assert record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=7,
                          state="merged", base_ref="main", engine=engine)
    [issue] = _issues(engine)
    assert issue.base_ref == "main"
    with Session(engine) as s:
        assert s.execute(select(ReviewPullRequest.base_ref)).scalar_one() == "main"
    provider = FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2")
    assert resolver.has_backlog(WS, "github", REPO, "main", engine=engine)
    _recheck(engine, provider, _FixAll())
    assert _issues(engine)[0].status == "fixed"


def test_an_open_pr_keeps_the_branch_its_review_stored(engine) -> None:
    _review_into(engine, 7, "feature/parent")
    record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=7,
                    state="closed", base_ref="main", engine=engine)
    with Session(engine) as s:
        assert s.execute(select(ReviewPullRequest.base_ref)).scalar_one() == "feature/parent"


# ─── A repeat on another branch ────────────────────────────────────


def test_a_repeat_on_another_branch_is_not_linked_to_the_issue_of_this_one(engine) -> None:
    _review_into(engine, 7, "main")
    _merge(engine, 7)
    _review_into(engine, 8, "release-1.x", head="k1")
    assert _by_pr(engine)[8].dup_of is None


def test_a_repeat_on_the_same_branch_is_still_linked(engine) -> None:
    _review_into(engine, 7, "main")
    _merge(engine, 7)
    _review_into(engine, 8, "main", head="k1")
    assert _by_pr(engine)[8].dup_of == _by_pr(engine)[7].id


def test_fixing_the_issue_on_one_branch_does_not_close_its_repeat_on_another(engine) -> None:
    # Linked while both PRs targeted main; the second is retargeted at its merge.
    _review_into(engine, 7, "main")
    _merge(engine, 7)
    _review_into(engine, 8, "main", head="k1")
    assert _by_pr(engine)[8].dup_of is not None
    record_pr_state(workspace_id=WS, provider="github", repo=REPO, number=8,
                    state="merged", base_ref="release-1.x", engine=engine)
    repeat = _by_pr(engine)[8]
    assert repeat.dup_of is None and repeat.base_ref == "release-1.x"
    _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"), _FixAll())
    assert _by_pr(engine)[7].status == "fixed"
    assert _by_pr(engine)[8].status == "open"


def test_a_repeat_on_the_same_branch_follows_the_fix_of_its_canonical(engine) -> None:
    _review_into(engine, 7, "main")
    _merge(engine, 7)
    _review_into(engine, 8, "main", head="k1")
    _merge(engine, 8)
    _recheck(engine, FakeProvider({("h2", "src/a.py"): "x\n"}, head="h2"), _FixAll())
    assert {i.status for i in _issues(engine)} == {"fixed"}


# ─── A revert, and the same text elsewhere ─────────────────────────


def _fixed(**kw):
    kw.setdefault("snippet", f"import os\nimport sys\n{LINE}")
    return _cand(status="fixed", resolution_source="auto_next_commit", **kw)


def test_the_same_line_far_from_the_fixed_place_is_not_a_revert() -> None:
    far = "def other():\n    helper_one()\n    helper_two()\n    " + LINE + "\n    return None\n"
    check = resolver.plan_head_check(_fixed(), _view(far), llm_verify=True)
    assert check.action == "keep"


def test_the_line_back_among_its_old_neighbours_is_a_revert() -> None:
    back = f"x\nimport os\nimport sys\n{LINE}\n"
    check = resolver.plan_head_check(_fixed(), _view(back), llm_verify=True)
    assert check.action == "reopen"


def test_an_issue_with_no_recorded_neighbours_is_reopened_on_the_line_alone() -> None:
    check = resolver.plan_head_check(
        _fixed(snippet=LINE), _view(f"x\n{LINE}\n"), llm_verify=True)
    assert check.action == "reopen"


# ─── Bounded work ──────────────────────────────────────────────────


def test_a_minified_line_cannot_put_megabytes_into_the_prompt() -> None:
    text = "\n".join("var a=" + "x" * 50_000 for _ in range(40))
    _, region = resolver.head_region(text, _cand(anchor="var a"))
    assert len(region) <= resolver.REGION_MAX_CHARS


def test_a_pass_that_cannot_hold_the_whole_backlog_moves_on_each_time(engine) -> None:
    _review(engine, 7)
    _merge(engine, 7)
    long_ago = datetime.now(UTC) - timedelta(days=3)
    with Session(engine) as s:
        base = s.execute(select(ReviewIssue)).scalar_one()
        for n, checked in ((101, long_ago), (102, None), (103, datetime.now(UTC))):
            s.add(ReviewIssue(
                workspace_id=WS, repo_slug=base.repo_slug, fingerprint=f"f{n}",
                file_path="src/a.py", agent="defect", severity="warning", title=f"t{n}",
                body="", status="open", pr_provider="github", pr_repo=REPO,
                pr_number=n, occurrences=1, first_seen_at=base.first_seen_at,
                last_seen_at=base.last_seen_at, merged_at=base.merged_at,
                base_ref="main", last_checked_at=checked))
        base.last_checked_at = datetime.now(UTC)
        s.commit()
        got = resolver.load_candidates(
            s, workspace_id=WS, pr_provider="github", pr_repo=REPO,
            base_ref="main", limit=2)
    assert [c.title for c in got] == ["t102", "t101"]


# ─── The repository's own setting ──────────────────────────────────


def _policy(engine, **kw) -> None:
    from src.sync.git_providers import parse_repo_url

    slug = parse_repo_url(f"github:{REPO}").slug
    with engine.begin() as conn:
        conn.execute(RepoReviewPolicy.__table__.insert().values(
            repo_slug=slug, workspace_id=WS, enabled=True, prompt_template="",
            folder_rules=[], agent_prompt_overrides={}, mcp_sources=[], **kw))


def test_a_repository_that_turned_resolution_off_stays_off_without_a_binding(engine) -> None:
    _policy(engine, issues_auto_resolve=False)
    cfg = resolver.issue_settings("github", REPO, WS, engine)
    assert cfg.auto_resolve is False


def test_a_workspace_default_applies_when_the_repository_says_nothing(engine) -> None:
    with engine.begin() as conn:
        conn.execute(WorkspaceReviewDefaults.__table__.insert().values(
            workspace_id=WS, issues_announce_resolved=False))
    cfg = resolver.issue_settings("github", REPO, WS, engine)
    assert cfg.announce is False and cfg.auto_resolve is True


def test_settings_that_cannot_be_read_skip_the_pass_instead_of_enabling_it() -> None:
    broken = create_engine("sqlite://")  # no tables
    assert resolver.issue_settings("github", REPO, WS, broken).auto_resolve is False
