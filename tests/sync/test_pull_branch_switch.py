"""RepoSync._pull must be able to CHANGE the checked-out branch.

Clones are made with `--single-branch`, which leaves `remote.origin.fetch`
pointing at one branch only. Two consequences this module pins down, because
both silently produce a repo indexed from the wrong code:

  1. `git checkout <other>` fails outright ("pathspec did not match") — the
     remote-tracking ref was never fetched, and checkout's DWIM only consults
     the configured (narrow) refspec.
  2. Even once switched, a plain `origin.fetch()` keeps updating only the
     ORIGINAL branch, so the new branch would freeze at the commit it had when
     it was first fetched.

Local bare repo only — no network.
"""

from __future__ import annotations

import subprocess

import pytest

from src.sync.clone import RepoSync

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git binary not available",
)


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _head(path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)


@pytest.fixture()
def clone(tmp_path):
    """A `main`-only single-branch clone of a bare repo that also has `dev`."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-b", "main", str(src))
    _git("config", "user.email", "t@example.com", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / "a.txt").write_text("main\n")
    _git("add", ".", cwd=src)
    _git("commit", "-m", "main1", cwd=src)
    _git("checkout", "-b", "dev", cwd=src)
    (src / "a.txt").write_text("dev\n")
    _git("commit", "-am", "dev1", cwd=src)
    _git("checkout", "main", cwd=src)

    bare = tmp_path / "bare.git"
    _git("clone", "--bare", str(src), str(bare))

    work = tmp_path / "work"
    _git("clone", "--depth", "50", "--single-branch", "--branch", "main",
         str(bare), str(work))
    return src, bare, work


def test_pull_switches_branch_on_single_branch_clone(clone):
    _src, _bare, work = clone
    assert _head(work) == "main"

    RepoSync()._pull(work, "dev", None)

    assert _head(work) == "dev"
    assert (work / "a.txt").read_text().strip() == "dev"


def test_switched_branch_keeps_advancing(clone):
    """The narrow refspec must not freeze the new branch at its first fetch."""
    src, bare, work = clone
    RepoSync()._pull(work, "dev", None)

    _git("checkout", "dev", cwd=src)
    (src / "a.txt").write_text("dev2\n")
    _git("commit", "-am", "dev2", cwd=src)
    _git("push", str(bare), "dev", cwd=src)

    result = RepoSync()._pull(work, "dev", None)

    assert (work / "a.txt").read_text().strip() == "dev2"
    assert result.changed is True


def test_missing_branch_keeps_current_checkout(clone):
    """A stale branch in the config must not fail the whole sync."""
    _src, _bare, work = clone

    RepoSync()._pull(work, "no-such-branch", None)

    assert _head(work) == "main"
    assert (work / "a.txt").read_text().strip() == "main"
