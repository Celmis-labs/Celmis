"""`git fetch` does not move HEAD, and the incremental indexer relied on it.

    # Pull latest before diffing so we compare against remote HEAD.
    subprocess.run(["git", "-C", path, "fetch", "--all", "--quiet"])
    head_sha = _git_head(repo_path)

The comment states an intention the code does not carry out. In a working
clone `fetch` advances `origin/<branch>` and leaves HEAD, the index and the
working tree exactly where they were. So `head_sha` is the OLD commit,
`prior_sha == head_sha` is true, and the pass records "unchanged" and returns
`{"status": "noop"}` — for a repository whose remote moved. The files that
were pushed are not even on disk to be parsed.

It survived because nothing called it. `run_index` had a queue handler and no
enqueuer: every call site used `index_repo_full`. The freshness check is what
makes this path reachable, and a re-index that indexes nothing is worse than
no re-index — the row records `last_indexed_at = now` while
`last_indexed_sha` stays old, so one column says "indexed just now" and the
next says "behind", and the daily sweep re-queues the same no-op for ever.

THESE TESTS USE REAL REPOSITORIES. A fake that commits into the local clone
moves HEAD by itself and cannot see this at all — which is exactly why the
existing incremental tests are green and this defect shipped anyway. Nothing
short of a real remote, a real push and a real second clone reproduces it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: Nothing about these repositories may come from the machine running the
#: tests. The first version let `git init` pick the default branch name, which
#: is `main` on this laptop and `master` on the CI runner — so the bare
#: remote's HEAD pointed at a branch the fixture never created, every clone
#: came out empty, and six tests died on `rev-parse HEAD` in CI while passing
#: locally. A test that reaches for git must not inherit the developer's git.
_ISOLATED = ("-c", "init.defaultBranch=main",
             "-c", "user.email=t@example.com",
             "-c", "user.name=T",
             "-c", "commit.gpgsign=false",
             "-c", "protocol.file.allow=always")

BRANCH = "main"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *_ISOLATED, *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def remote_and_clone(tmp_path: Path):
    """A bare remote one commit ahead of a working clone — the real situation."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", *_ISOLATED, "init", "-q", "--bare", str(remote)], check=True)
    # Say which branch the remote's HEAD names, rather than hoping the default
    # matches the one we push to. `init -b` would do it too but is newer than
    # some runners' git.
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD",
                    f"refs/heads/{BRANCH}"], check=True)

    author = tmp_path / "author"
    subprocess.run(["git", *_ISOLATED, "clone", "-q", str(remote), str(author)],
                   check=True)
    (author / "a.py").write_text("def a(): pass\n")
    _git("add", "-A", cwd=author)
    _git("commit", "-qm", "first", cwd=author)
    _git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=author)

    clone = tmp_path / "clone"
    subprocess.run(["git", *_ISOLATED, "clone", "-q", str(remote), str(clone)],
                   check=True)
    first = _git("rev-parse", "HEAD", cwd=clone)

    (author / "b.py").write_text("def b(): pass\n")
    _git("add", "-A", cwd=author)
    _git("commit", "-qm", "second", cwd=author)
    _git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=author)
    second = _git("rev-parse", "HEAD", cwd=author)

    return {"remote": remote, "clone": clone, "first": first, "second": second}


def test_fetch_alone_leaves_the_checkout_behind(remote_and_clone):
    """The git fact the indexer was built on, pinned so it cannot be re-assumed."""
    clone = remote_and_clone["clone"]
    subprocess.run(["git", "-C", str(clone), "fetch", "--all", "--quiet"], check=True)
    assert _git("rev-parse", "HEAD", cwd=clone) == remote_and_clone["first"]
    assert _git("rev-parse", f"origin/{BRANCH}", cwd=clone) == remote_and_clone["second"]
    assert not (clone / "b.py").exists()


def test_the_indexer_brings_the_checkout_to_the_remote(remote_and_clone):
    """What run_index must do before it can diff anything.

    Asserted on the checkout rather than on a return value: the point is that
    the pushed file is ON DISK, because that is what the extractor reads.
    """
    from src.sync.incremental import _advance_to_remote

    clone = remote_and_clone["clone"]
    moved = _advance_to_remote(clone)
    assert moved is True
    assert _git("rev-parse", "HEAD", cwd=clone) == remote_and_clone["second"]
    assert (clone / "b.py").exists(), "the new file is not on disk to be parsed"


def test_a_clone_already_current_is_left_alone(remote_and_clone):
    from src.sync.incremental import _advance_to_remote

    clone = remote_and_clone["clone"]
    _advance_to_remote(clone)
    before = _git("rev-parse", "HEAD", cwd=clone)
    assert _advance_to_remote(clone) is True
    assert _git("rev-parse", "HEAD", cwd=clone) == before


def test_local_edits_do_not_block_the_advance(remote_and_clone):
    """The indexer's clone is not a workspace; nobody's work lives there.

    `reset --hard` rather than a merge, for the same reason RepoSync does: a
    conflict on a machine with no human at it is a repository that stops
    updating and says nothing.
    """
    from src.sync.incremental import _advance_to_remote

    clone = remote_and_clone["clone"]
    (clone / "a.py").write_text("locally scribbled\n")
    assert _advance_to_remote(clone) is True
    assert _git("rev-parse", "HEAD", cwd=clone) == remote_and_clone["second"]


def test_a_detached_head_is_reported_not_forced(remote_and_clone):
    """Detached means somebody pinned it deliberately; moving it loses that."""
    from src.sync.incremental import _advance_to_remote

    clone = remote_and_clone["clone"]
    _git("checkout", "-q", "--detach", "HEAD", cwd=clone)
    assert _advance_to_remote(clone) is False


def test_a_directory_that_is_not_a_repository_is_false_not_a_crash(tmp_path):
    from src.sync.incremental import _advance_to_remote

    assert _advance_to_remote(tmp_path) is False


# ─── the whole pass, end to end ──────────────────────────────────────

def test_run_index_itself_brings_the_clone_forward(remote_and_clone, monkeypatch):
    """The defect as the product would have shipped it — driven through run_index.

    An earlier version of this test called `_advance_to_remote` by hand and
    then asserted on the result, which proves the helper works and nothing
    about whether anything calls it. Deleting the call from `run_index` left
    the whole file green. Now the test does what the worker does: hands
    `run_index` a slug and looks at the clone afterwards.
    """
    from src.config import get_settings
    from src.sync import incremental

    clone = remote_and_clone["clone"]
    settings = get_settings()
    monkeypatch.setattr(type(settings), "repo_path",
                        lambda self, slug: clone, raising=False)

    seen = {}

    def _spy(*a, **k):
        # A plain function, not `setdefault(...) or {...}`: setdefault returns
        # the stored value, which is truthy, so the `or` never evaluates and
        # the double returns True where a dict was expected.
        seen["indexed"] = True
        return {"files": 1}

    monkeypatch.setattr(incremental, "_run_incremental", _spy, raising=False)
    monkeypatch.setattr("src.repos.index_state.read_index_state",
                        lambda slug: type("S", (), {
                            "last_indexed_sha": remote_and_clone["first"]})())
    monkeypatch.setattr("src.repos.index_state.record_index_success",
                        lambda *a, **k: None)
    monkeypatch.setattr("src.repos.index_state.record_index_failure",
                        lambda *a, **k: None)
    unchanged = {}
    monkeypatch.setattr("src.repos.index_state.record_index_unchanged",
                        lambda slug, **kw: unchanged.setdefault("called", kw))

    result = incremental.run_index("github_acme-widgets",
                                   since_sha=remote_and_clone["first"])

    assert _git("rev-parse", "HEAD", cwd=clone) == remote_and_clone["second"], (
        "run_index did not advance the checkout; it would diff the old commit "
        "against itself"
    )
    assert (clone / "b.py").exists()
    assert result.get("status") != "noop", (
        f"a moved remote was reported as unchanged: {result}"
    )
    assert "called" not in unchanged
