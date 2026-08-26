"""The agent's changes have to actually leave the box.

Everything upstream of this is invisible if the push is broken: the session
reports a branch, the branch is not on the remote, and the workspace is
deleted a moment later. So this drives `commit_and_push` against a real bare
repository and then reads the result back out of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.agent.workspace import AgentWorkspace, commit_and_push


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def remote_and_clone(tmp_path: Path):
    """A bare 'remote' with one commit, plus a working clone of it."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)],
                   check=True, capture_output=True)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", cwd=seed)
    _git("push", str(remote), "main", cwd=seed)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)],
                   check=True, capture_output=True)
    return remote, clone


def _workspace(clone: Path, remote: Path, session_id: str) -> AgentWorkspace:
    return AgentWorkspace(
        session_id=session_id,
        repo_dir=clone,
        home_dir=clone.parent / "home",
        push_url=str(remote),
        clean_url="https://github.com/acme/widget.git",
        default_branch="main",
    )


def test_edits_reach_the_remote(remote_and_clone):
    remote, clone = remote_and_clone
    (clone / "new_file.py").write_text("print('from the agent')\n")

    pushed = commit_and_push(_workspace(clone, remote, "abcdef12-rest-of-uuid"))

    assert pushed is not None, "commit_and_push reported nothing to push"
    assert pushed["branch"] == "celmis-agent/abcdef12"

    # The claim is only worth as much as the remote's own view of it.
    refs = _git("ls-remote", "--heads", str(remote), cwd=clone)
    assert "refs/heads/celmis-agent/abcdef12" in refs

    on_remote = _git("--no-pager", "show",
                     "celmis-agent/abcdef12:new_file.py", cwd=remote)
    assert on_remote == "print('from the agent')"
    assert _git("rev-parse", "celmis-agent/abcdef12", cwd=remote) == pushed["commit"]


def test_modifications_and_deletions_travel_too(remote_and_clone):
    """`git add -A` — not `add .` — or deletions never leave the clone."""
    remote, clone = remote_and_clone
    (clone / "README.md").write_text("edited by the agent\n")
    (clone / "gone.txt").write_text("x\n")
    _git("add", "-A", cwd=clone)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "pre", cwd=clone)
    (clone / "gone.txt").unlink()

    pushed = commit_and_push(_workspace(clone, remote, "11112222-x"))

    assert pushed is not None
    files = _git("--no-pager", "ls-tree", "--name-only", "-r",
                 pushed["branch"], cwd=remote).split()
    assert "gone.txt" not in files
    assert _git("--no-pager", "show", f"{pushed['branch']}:README.md",
                cwd=remote) == "edited by the agent"


def test_no_changes_means_no_branch(remote_and_clone):
    """An advisory session must not litter the repo with empty branches."""
    remote, clone = remote_and_clone

    assert commit_and_push(_workspace(clone, remote, "deadbeef-x")) is None
    assert "celmis-agent" not in _git("ls-remote", "--heads", str(remote), cwd=clone)


def test_compare_url_points_at_the_pushed_branch(remote_and_clone):
    remote, clone = remote_and_clone
    (clone / "f.txt").write_text("1\n")

    pushed = commit_and_push(_workspace(clone, remote, "cafe0001-x"))

    assert pushed["compare_url"] == (
        "https://github.com/acme/widget/compare/main...celmis-agent/cafe0001?expand=1"
    )
