"""Ask the code answered from whatever was indexed last time somebody looked.

The incremental indexer has existed since it was written: `run_index` walks
`git diff last_sha..HEAD`, deletes symbols of files that went away, adds the
new ones, drops stale Qdrant points. A handler for it was registered with the
queue. And nothing ever enqueued the job — every call site enqueued
`index_repo_full`, the webhook parsed pull-request events only, and the poller
looked for pull requests to review rather than commits to index. A complete
mechanism, connected to nothing.

So a repository indexed on Tuesday answered Friday's questions from Tuesday's
code, and said nothing about it. Not an error, not a warning: a confident
answer from a graph nobody had told it was stale.

WHAT THESE TESTS HOLD.

Three ways in, one decision. A daily sweep, a push webhook and a button all
call `check_repo`, so a schedule, a provider and a person cannot reach three
different conclusions about what "current" means.

Four outcomes, not two. Up to date, behind, never indexed, and *could not
tell*. The fourth is the one that matters: a check that cannot reach the
remote and renders as "no new changes" is worse than no check at all, because
it answers wrongly with the authority of a fresh timestamp. `known` exists so
callers cannot write `state != "behind"` and mean "nothing changed".

The check is recorded EVERY time, including when nothing changed. That write
is the entire difference between "indexed three days ago" and "checked an hour
ago, unchanged", and only the second answers what a person came to ask.
"""
from __future__ import annotations

import pytest

from src.repos import freshness
from src.repos.freshness import check_repo


class _State:
    def __init__(self, sha=None):
        self.last_indexed_sha = sha
        self.last_checked_at = None
        self.last_remote_sha = None
        self.last_check_error = None
        self.last_indexed_at = None
        self.last_full_rebuild_at = None
        self.last_indexed_files = 0
        self.last_error = None
        self.last_error_at = None


@pytest.fixture
def wired(monkeypatch):
    """A repo whose remote and index are both under the test's control."""
    calls = {"checks": [], "enqueued": []}

    def _record(repo_slug, *, remote_sha, error=None):
        calls["checks"].append({"repo": repo_slug, "sha": remote_sha, "error": error})

    def _enqueue(**kw):
        calls["enqueued"].append(kw)
        return "job-1"

    monkeypatch.setattr("src.repos.index_state.record_remote_check", _record)
    monkeypatch.setattr("src.sync.queue.enqueue", _enqueue)
    return calls


def _with(monkeypatch, *, indexed, remote=None, boom=None):
    monkeypatch.setattr("src.repos.index_state.read_index_state",
                        lambda slug: _State(indexed) if indexed is not None else None)

    def _head(slug, *, workspace_id, user_id="default"):
        if boom:
            raise RuntimeError(boom)
        return remote

    monkeypatch.setattr(freshness, "remote_head", _head)


# ─── the four answers ────────────────────────────────────────────────

def test_an_unmoved_branch_is_up_to_date(monkeypatch, wired):
    _with(monkeypatch, indexed="a" * 40, remote="a" * 40)
    r = check_repo("repo", workspace_id="ws")
    assert r.state == "up_to_date"
    assert r.changed is False and r.known is True
    assert wired["enqueued"] == [], "nothing moved; nothing to re-index"


def test_a_moved_branch_is_behind_and_queues_a_reindex(monkeypatch, wired):
    _with(monkeypatch, indexed="a" * 40, remote="b" * 40)
    r = check_repo("repo", workspace_id="ws")
    assert r.state == "behind" and r.changed is True
    assert r.reindex_job_id == "job-1"
    assert len(wired["enqueued"]) == 1


def test_the_reindex_is_incremental_and_says_where_to_start(monkeypatch, wired):
    """The full path re-parses the whole repository because one file changed.

    `since_sha` is what lets `run_index` walk a diff instead: without it the
    incremental indexer has no left-hand side and falls back to a rebuild,
    which is the expensive thing this whole mechanism exists to avoid doing
    daily.
    """
    from src.sync.queue import KIND_INDEX_REPO, KIND_INDEX_REPO_FULL

    _with(monkeypatch, indexed="a" * 40, remote="b" * 40)
    check_repo("repo", workspace_id="ws")
    job = wired["enqueued"][0]
    assert job["kind"] == KIND_INDEX_REPO
    assert job["kind"] != KIND_INDEX_REPO_FULL
    assert job["payload"]["since_sha"] == "a" * 40
    assert job["payload"]["repo_slug"] == "repo"


def test_a_repo_with_no_recorded_revision_is_not_called_current(monkeypatch, wired):
    """`never_indexed` rather than `up_to_date`.

    Nothing to compare against is not the same as nothing having changed, and
    a screen that says "up to date" about a repository it has never indexed is
    making that up.
    """
    _with(monkeypatch, indexed=None, remote="b" * 40)
    r = check_repo("repo", workspace_id="ws")
    assert r.state == "never_indexed"
    assert wired["enqueued"] == []


def test_a_remote_that_cannot_be_reached_is_its_own_answer(monkeypatch, wired):
    _with(monkeypatch, indexed="a" * 40, boom="Could not read from remote repository")
    r = check_repo("repo", workspace_id="ws")
    assert r.state == "unreachable"
    assert r.known is False, (
        "a caller writing `not result.changed` would report 'no new changes' "
        "for a remote nobody could reach"
    )
    assert wired["enqueued"] == []
    assert r.detail and "remote" in r.detail


def test_unreachable_is_not_behind_and_not_up_to_date(monkeypatch, wired):
    """The trap this shape exists to close.

    `state != "behind"` is the natural way to write "nothing changed" and it
    is wrong: unreachable is also not behind.
    """
    _with(monkeypatch, indexed="a" * 40, boom="timeout")
    r = check_repo("repo", workspace_id="ws")
    assert r.state not in ("behind", "up_to_date")


# ─── what gets written down ──────────────────────────────────────────

def test_a_check_that_found_nothing_is_still_recorded(monkeypatch, wired):
    """Otherwise the row cannot tell "unchanged" from "nobody looked"."""
    _with(monkeypatch, indexed="a" * 40, remote="a" * 40)
    check_repo("repo", workspace_id="ws")
    assert len(wired["checks"]) == 1
    assert wired["checks"][0]["sha"] == "a" * 40
    assert wired["checks"][0]["error"] is None


def test_a_failed_check_records_the_reason_and_no_sha(monkeypatch, wired):
    """The previous answer is still the last thing the remote actually said.

    Writing None over it would turn a stale-but-true fact into a silence that
    reads as "never checked".
    """
    _with(monkeypatch, indexed="a" * 40, boom="403 Forbidden")
    check_repo("repo", workspace_id="ws")
    assert wired["checks"][0]["sha"] is None
    assert "403" in wired["checks"][0]["error"]


def test_reindex_can_be_asked_for_without_the_consequence(monkeypatch, wired):
    _with(monkeypatch, indexed="a" * 40, remote="b" * 40)
    r = check_repo("repo", workspace_id="ws", reindex=False)
    assert r.state == "behind"
    assert r.reindex_job_id is None
    assert wired["enqueued"] == []


def test_a_queue_that_refuses_does_not_lose_the_finding(monkeypatch, wired):
    """The branch DID move, and that stays true if the follow-up cannot be queued.

    Written after the first version of this test asserted the test double
    raised, which proves nothing about the code. `enqueue` was outside the
    try; a database hiccup would have turned "behind" into an exception, and
    from the sweep's point of view into "no new changes".
    """
    _with(monkeypatch, indexed="a" * 40, remote="b" * 40)

    def _boom(**kw):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("src.sync.queue.enqueue", _boom)
    r = check_repo("repo", workspace_id="ws")
    assert r.state == "behind", "the answer was lost with the enqueue"
    assert r.reindex_job_id is None


def test_a_check_records_before_it_queues(monkeypatch, wired):
    """Order matters when the queue is the thing that fails.

    Recording after enqueueing would leave a repository that is behind with no
    record of ever having been checked — invisible on the page and invisible
    to the next sweep's reasoning.
    """
    order = []
    monkeypatch.setattr("src.repos.index_state.record_remote_check",
                        lambda *a, **k: order.append("record"))
    monkeypatch.setattr("src.sync.queue.enqueue",
                        lambda **k: order.append("enqueue") or "job-1")
    _with(monkeypatch, indexed="a" * 40, remote="b" * 40)
    check_repo("repo", workspace_id="ws")
    assert order == ["record", "enqueue"]
