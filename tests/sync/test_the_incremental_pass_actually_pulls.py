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
    # A file in a SUBDIRECTORY, because that is where the read-only mode bites:
    # unlinking needs write on the containing directory, and `_chmod_readonly`
    # leaves the repository root alone while setting every directory under it
    # to 0550. A fixture with only top-level files cannot see the difference.
    (author / "src").mkdir()
    (author / "src" / "one.py").write_text("ONE = 1\n")
    _git("add", "-A", cwd=author)
    _git("commit", "-qm", "first", cwd=author)
    _git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=author)

    clone = tmp_path / "clone"
    subprocess.run(["git", *_ISOLATED, "clone", "-q", str(remote), str(clone)],
                   check=True)
    first = _git("rev-parse", "HEAD", cwd=clone)

    (author / "b.py").write_text("def b(): pass\n")
    (author / "src" / "one.py").write_text("ONE = 2\n")
    (author / "src" / "two.py").write_text("TWO = 2\n")
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


# ─── the filesystem the clone actually lives on ──────────────────────
#
# Everything above runs against a writable clone, and every one of those tests
# passed while `_advance_to_remote` could not move a single production
# repository. `RepoSync.clone_or_update` ends with `_chmod_readonly` so that
# analysers physically cannot edit the code: directories under the root become
# 0550, files 0440. Unlinking a file needs write on its DIRECTORY, so
# `git reset --hard` died on the first path inside `src/` —
#
#   error: unable to unlink old 'src/contract.ts': Permission denied
#   fatal: Could not reset index file to revision 969fa59
#
# measured on a copy of a production clone. `RepoSync._pull` brackets its own
# pull with `_chmod_writable`; this path did not.
#
# The fixture calls the project's own `_chmod_readonly` rather than setting
# modes by hand, so it keeps tracking whatever that function decides to do.


@pytest.fixture
def readonly_clone(remote_and_clone):
    """The clone as `RepoSync` leaves it: read-only, nested directories and all."""
    from src.sync.clone import _chmod_readonly, _chmod_writable

    clone = remote_and_clone["clone"]
    _chmod_readonly(clone)
    try:
        yield remote_and_clone
    finally:
        # tmp_path cleanup cannot remove a 0550 directory either.
        _chmod_writable(clone)


def test_the_advance_moves_a_read_only_clone(readonly_clone):
    """The production shape, which is the only shape that matters."""
    from src.sync.incremental import _advance_to_remote

    clone = readonly_clone["clone"]
    assert (clone / "src" / "one.py").read_text() == "ONE = 1\n"

    assert _advance_to_remote(clone) is True, (
        "the read-only tree stopped the reset; every freshness re-index is a "
        "no-op and the answers come from the old code"
    )
    assert _git("rev-parse", "HEAD", cwd=clone) == readonly_clone["second"]
    assert (clone / "src" / "two.py").exists(), (
        "the pushed file is not on disk to be parsed"
    )
    assert (clone / "src" / "one.py").read_text() == "ONE = 2\n", (
        "the file that CHANGED still holds its old contents"
    )


def test_the_tree_is_read_only_again_afterwards(readonly_clone):
    """The safeguard is borrowed for the reset, not spent on it."""
    import stat

    from src.sync.incremental import _advance_to_remote

    clone = readonly_clone["clone"]
    _advance_to_remote(clone)

    src_mode = stat.S_IMODE((clone / "src").stat().st_mode)
    file_mode = stat.S_IMODE((clone / "src" / "two.py").stat().st_mode)
    assert not src_mode & stat.S_IWUSR, f"src/ left writable ({src_mode:o})"
    assert not file_mode & stat.S_IWUSR, f"src/two.py left writable ({file_mode:o})"


def test_a_failed_advance_still_restores_the_safeguard(readonly_clone, monkeypatch):
    """A reset that dies must not leave the tree open behind it."""
    import stat
    import subprocess as sp

    from src.sync import incremental

    real = sp.run

    def explode(cmd, *a, **kw):
        if isinstance(cmd, list) and "reset" in cmd:
            raise OSError("git vanished")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(incremental.subprocess, "run", explode)
    clone = readonly_clone["clone"]
    assert incremental._advance_to_remote(clone) is False
    assert not stat.S_IMODE((clone / "src").stat().st_mode) & stat.S_IWUSR


def test_a_directory_that_is_not_a_repository_is_left_as_it_was(tmp_path):
    """Only undo what this call did.

    An early return happens before anything is made writable, so the restore
    must not run — otherwise asking about a plain directory would silently
    make it read-only.
    """
    import stat

    from src.sync.incremental import _advance_to_remote

    plain = tmp_path / "not-a-repo"
    (plain / "sub").mkdir(parents=True)
    (plain / "sub" / "f.txt").write_text("x")
    before = stat.S_IMODE((plain / "sub").stat().st_mode)

    assert _advance_to_remote(plain) is False
    assert stat.S_IMODE((plain / "sub").stat().st_mode) == before, (
        "a directory that was never touched came back read-only"
    )


def test_a_fetch_that_failed_is_not_an_advance(remote_and_clone):
    """"Could not reach the remote" and "nothing changed" are opposite answers.

    The fetch ran with check=False, so an unreachable remote left
    `origin/<branch>` at whatever it said last time — and the reset then
    succeeded onto that stale ref and returned True. The caller writes
    last_indexed_at = now for code nobody fetched.
    """
    from src.sync.incremental import _advance_to_remote

    clone = remote_and_clone["clone"]
    _git("remote", "set-url", "origin",
         str(clone.parent / "there-is-no-remote-here.git"), cwd=clone)

    assert _advance_to_remote(clone) is False, (
        "an unreachable remote was reported as a successful advance"
    )
    # And it did not move onto the stale ref it still had on disk.
    assert _git("rev-parse", "HEAD", cwd=clone) == remote_and_clone["first"]
