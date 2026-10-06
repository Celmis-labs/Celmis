"""A push to a reviewed pull request is reviewed from the last reviewed commit on.

The orchestrator's `scope` stage asks `decide_scope`, and when the answer is
"incremental" it swaps the diff the agents read for the commits since the last
complete, posted review, keeping the whole PR on `pr.scope` for anchors and
the ledger. Every doubt falls back to the whole PR; a push that brings nothing
new ends quietly; a person who asks is never answered with a quiet skip.

The pull request row is a real one on sqlite, so `last_reviewed_sha` is read
the way production reads it.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewPullRequest
from src.review import pr_state
from src.review.agents.base import AgentRunResult, ReviewAgent
from src.review.diff import parse_unified_diff
from src.review.scope import CommitInfo, ReviewRequest
from src.review.stages import StageRecorder
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _finding,
    _keys,
    _orch,
    _Provider,
    _stage,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    _pr,
    env,  # noqa: F401
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


REVIEWED = "1a" * 20
NEWER = "2b" * 20
HEAD = "abcdef1234567890"

WHOLE_PR = """\
diff --git a/src/mod0.py b/src/mod0.py
--- a/src/mod0.py
+++ b/src/mod0.py
@@ -1,2 +1,5 @@
 line
+a = 1
+b = 2
+c = 3
 tail
diff --git a/src/mod1.py b/src/mod1.py
--- a/src/mod1.py
+++ b/src/mod1.py
@@ -1,1 +1,2 @@
 line
+x = 1
"""

INCREMENT = """\
diff --git a/src/mod0.py b/src/mod0.py
--- a/src/mod0.py
+++ b/src/mod0.py
@@ -2,3 +2,3 @@
 a = 1
-b = 2
+b = 22
 c = 3
diff --git a/from_target.py b/from_target.py
--- a/from_target.py
+++ b/from_target.py
@@ -1,1 +1,2 @@
 keep
+merged in from the target branch
"""


@pytest.fixture
def rows(monkeypatch):
    import src.review.issues as issues_mod

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(eng)
    monkeypatch.setattr(issues_mod, "_ENGINE", eng)
    yield eng
    eng.dispose()


class _Reader(ReviewAgent):
    """An agent that remembers what it was handed."""

    name = "defect"

    def __init__(self) -> None:
        self.seen: list = []

    def review(self, context) -> AgentRunResult:
        self.seen.append(context.pull_request)
        return AgentRunResult(agent="defect", findings=[_finding()],
                              tokens_in=1, tokens_out=1, model_used="m",
                              elapsed_seconds=0.1)


class _Incremental(_Provider):
    """A provider that can list commits and diff two of them."""

    def __init__(self, pr, *, commits="default", increment=INCREMENT) -> None:
        super().__init__(pr)
        self.commits = ([CommitInfo(sha=HEAD, parents=(NEWER,)),
                         CommitInfo(sha=NEWER, parents=(REVIEWED,)),
                         CommitInfo(sha=REVIEWED)]
                        if commits == "default" else commits)
        self.increment = increment
        self.diff_requests: list[tuple[str, str]] = []
        self.posted_batches: list = []

    def list_pr_commits(self, repo, pr_number):
        return self.commits

    def fetch_incremental_diff(self, repo, pr_number, base_sha, head_sha):
        self.diff_requests.append((base_sha, head_sha))
        return self.increment

    def our_inline_threads(self, pr, marker):
        return []

    def post_review(self, batch, dry_run=False):
        self.posted_batches.append(batch)
        return super().post_review(batch, dry_run=dry_run)


def _whole_pr():
    hunks, _ = parse_unified_diff(WHOLE_PR)
    return dataclasses.replace(_pr(), hunks=hunks, raw_diff=WHOLE_PR, head_sha=HEAD)


def _reviewed(rows, sha=REVIEWED):
    pr_state.register_push("ws-1", "github", "o/r", 1, sha, engine=rows)
    pr_state.mark_reviewed("ws-1", "github", "o/r", 1, sha, engine=rows)


def _run(monkeypatch, provider, *, request=None, **policy):
    reader = _Reader()
    orch = _orch(monkeypatch, [reader], policy={**POLICY, **policy})
    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=provider, stages=rec,
                         workspace_id="ws-1", request=request)
    return result, rec, reader


def test_a_push_is_reviewed_from_the_last_reviewed_commit_on(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(_whole_pr())

    result, rec, reader = _run(monkeypatch, provider)

    assert provider.diff_requests == [(REVIEWED, HEAD)]
    (seen,) = reader.seen
    assert {h.file_path for h in seen.hunks} == {"src/mod0.py"}, (
        "the agents read the increment, not mod1.py which no new commit touched")
    scope = result.batch.pull_request.scope
    assert scope is not None and scope.base_sha == REVIEWED and scope.new_commits == 2
    assert {h.file_path for h in scope.full_hunks} == {"src/mod0.py", "src/mod1.py"}
    assert result.batch.scope_mode == "incremental"


def test_the_scope_stage_says_what_was_read_and_since_when(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)

    _, rec, _ = _run(monkeypatch, _Incremental(_whole_pr()))

    keys = _keys(rec)
    assert keys.index("gate_cadence") < keys.index("scope") < keys.index("context")
    stage = _stage(rec, "scope")
    assert stage["status"] == "success"
    assert stage["meta"]["scope"] == "incremental"
    assert stage["meta"]["base_sha"] == REVIEWED[:12]
    assert stage["meta"]["new_commits"] == 2 and stage["meta"]["files"] == 1
    assert "2 new commits" in stage["reason"] and REVIEWED[:7] in stage["reason"]


def test_what_a_merge_pulled_in_from_the_target_branch_is_not_reviewed(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)

    result, _, reader = _run(monkeypatch, _Incremental(_whole_pr()))

    (seen,) = reader.seen
    assert "from_target.py" not in seen.raw_diff
    assert all(h.file_path != "from_target.py" for h in seen.hunks)


def test_a_first_review_reads_the_whole_pull_request(env, monkeypatch, rows):  # noqa: F811
    provider = _Incremental(_whole_pr())

    result, rec, reader = _run(monkeypatch, provider)

    assert provider.diff_requests == []
    assert result.batch.pull_request.scope is None
    assert {h.file_path for h in reader.seen[0].hunks} == {"src/mod0.py", "src/mod1.py"}
    assert _stage(rec, "scope")["meta"]["reason"] == "first_review"
    assert result.batch.scope_mode == "full"


def test_the_repository_setting_full_reads_the_whole_pull_request(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(_whole_pr())

    result, rec, reader = _run(monkeypatch, provider, review_scope="full")

    assert provider.diff_requests == []
    assert result.batch.pull_request.scope is None
    assert _stage(rec, "scope")["meta"]["reason"] == "setting_full"


def test_a_forced_review_reads_the_whole_pull_request(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(_whole_pr())

    result, _, _ = _run(monkeypatch, provider,
                        request=ReviewRequest(trigger="manual", force=True))

    assert result.batch.pull_request.scope is None and provider.diff_requests == []


def test_a_force_push_is_reviewed_whole_and_the_stage_says_why(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(
        _whole_pr(), commits=[CommitInfo(sha=HEAD, parents=(NEWER,)), CommitInfo(sha=NEWER)])

    result, rec, reader = _run(monkeypatch, provider)

    assert result.batch.pull_request.scope is None
    assert _stage(rec, "scope")["meta"]["reason"] == "history_rewritten"
    assert "rewritten" in _stage(rec, "scope")["reason"]
    assert len(reader.seen[0].hunks) == 2


def test_a_provider_that_cannot_list_commits_means_a_whole_review(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)

    result, rec, _ = _run(monkeypatch, _Incremental(_whole_pr(), commits=None))

    assert result.batch.pull_request.scope is None
    assert _stage(rec, "scope")["meta"]["reason"] == "commits_unavailable"


@pytest.mark.parametrize("increment", [None, "", "   \n"])
def test_an_increment_that_cannot_be_read_means_a_whole_review(env, monkeypatch, rows, increment):  # noqa: F811
    _reviewed(rows)

    result, rec, reader = _run(monkeypatch, _Incremental(_whole_pr(), increment=increment))

    assert result.batch.pull_request.scope is None
    assert len(reader.seen[0].hunks) == 2
    assert _stage(rec, "scope")["meta"]["reason"] == "increment_unreadable"
    assert result.batch.scope_mode == "full"


def test_an_increment_made_only_of_target_branch_changes_means_a_whole_review(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    only_target = INCREMENT[INCREMENT.index("diff --git a/from_target.py"):]

    result, _, _ = _run(monkeypatch, _Incremental(_whole_pr(), increment=only_target))

    assert result.batch.pull_request.scope is None


def test_a_push_that_is_only_merge_commits_ends_quietly(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(_whole_pr(), commits=[
        CommitInfo(sha=HEAD, parents=(REVIEWED, "9f" * 20)), CommitInfo(sha=REVIEWED)])

    result, rec, reader = _run(monkeypatch, provider)

    assert result.batch.run_status.value == "skipped"
    assert result.batch.scope_skip == "only_merge_commits"
    assert reader.seen == [] and provider.posted_batches == []
    assert _keys(rec)[-1] == "scope" and _stage(rec, "scope")["status"] == "skipped"


def test_no_new_commits_is_a_quiet_skip_for_a_push_event(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows, sha=HEAD)

    result, rec, reader = _run(monkeypatch, _Incremental(_whole_pr()))

    assert result.batch.run_status.value == "skipped"
    assert result.batch.scope_skip == "no_new_commits"
    assert reader.seen == []
    assert "no new commits" in _stage(rec, "scope")["reason"].lower()


@pytest.mark.parametrize("request_", [
    ReviewRequest(trigger="command"),
    ReviewRequest(trigger="manual", force=True),
])
def test_a_person_asking_for_an_unchanged_head_gets_a_review(env, monkeypatch, rows, request_):  # noqa: F811
    _reviewed(rows, sha=HEAD)

    result, _, reader = _run(monkeypatch, _Incremental(_whole_pr()), request=request_)

    assert result.batch.run_status.value != "skipped"
    assert len(reader.seen[0].hunks) == 2


def test_an_increment_made_only_of_ignored_files_falls_back_to_what_is_left_of_the_whole(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)

    result, rec, reader = _run(monkeypatch, _Incremental(_whole_pr()),
                               ignore_globs=["src/mod0.py"])

    # The repository's globs apply to the whole PR first; the increment then
    # has nothing the PR still shows, so the review is whole (not silent).
    assert result.batch.pull_request.scope is None
    assert [h.file_path for h in reader.seen[0].hunks] == ["src/mod1.py"]
    assert _stage(rec, "scope")["meta"]["reason"] == "increment_unreadable"


LOCKFILE_ONLY = """\
diff --git a/poetry.lock b/poetry.lock
--- a/poetry.lock
+++ b/poetry.lock
@@ -1,1 +1,2 @@
 keep
+new pin
"""


def test_a_push_of_only_lockfiles_ends_quietly_instead_of_reading_the_whole_pull_request(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    provider = _Incremental(_whole_pr(), increment=LOCKFILE_ONLY)

    result, rec, reader = _run(monkeypatch, provider)

    assert result.batch.run_status.value == "skipped"
    assert result.batch.scope_skip == "no_reviewable_new_changes"
    assert reader.seen == [] and provider.posted_batches == []
    assert _stage(rec, "scope")["status"] == "skipped"


def test_a_push_of_only_files_the_repository_ignores_ends_quietly(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    only_mod0 = INCREMENT[:INCREMENT.index("diff --git a/from_target.py")]

    result, _, reader = _run(monkeypatch, _Incremental(_whole_pr(), increment=only_mod0),
                             ignore_globs=["src/mod0.py"])

    assert result.batch.scope_skip == "no_reviewable_new_changes"
    assert reader.seen == []


def test_an_increment_that_is_the_whole_pull_request_is_read_as_a_whole_one(env, monkeypatch, rows):  # noqa: F811
    # What a provider answers when its "since" diff is really a merge-base diff.
    _reviewed(rows)

    result, rec, reader = _run(monkeypatch, _Incremental(_whole_pr(), increment=WHOLE_PR))

    assert result.batch.pull_request.scope is None
    assert len(reader.seen[0].hunks) == 2
    assert _stage(rec, "scope")["meta"]["reason"] == "increment_unreadable"


def test_an_incremental_run_still_lists_the_files_the_whole_pull_request_skipped(env, monkeypatch, rows):  # noqa: F811
    _reviewed(rows)
    pr = dataclasses.replace(_whole_pr(), skipped_files=["poetry.lock"])
    increment = INCREMENT + LOCKFILE_ONLY

    result, _, _ = _run(monkeypatch, _Incremental(pr, increment=increment))

    assert result.batch.pull_request.scope is not None
    assert "poetry.lock" in result.batch.skipped_files


def test_a_state_that_cannot_be_read_never_blocks_the_review(env, monkeypatch, rows):  # noqa: F811
    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(pr_state, "load", boom)

    result, rec, reader = _run(monkeypatch, _Incremental(_whole_pr()))

    assert result.batch.pull_request.scope is None
    assert len(reader.seen[0].hunks) == 2
    assert _stage(rec, "scope")["meta"]["reason"] == "first_review"
