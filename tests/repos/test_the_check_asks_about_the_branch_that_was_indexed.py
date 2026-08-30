"""Freshness asked the remote about a branch the clone was not standing on.

`remote_head` fell through to `HEAD` — the PROVIDER DEFAULT — whenever the
repository was registered without an explicit branch. But
`RepoSync.clone_or_update` takes `branch="dev"` by default and only falls back
to the default branch when `dev` does not exist, and `_advance_to_remote`
resets onto the remote counterpart of whatever branch the clone is standing
on.

So a repository that has a `dev` branch and was added without naming one was
indexed from `dev` and compared against `main`. Two shas that never converge:
it reads as behind for ever, re-indexes every day, and every re-index leaves
it behind again — a permanent, silent, self-renewing loop.

The invariant these tests hold is not "ask for dev". It is that the ref this
check asks the remote about is the ref the advance would move the clone to.
One decision, not two that happen to agree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.repos import freshness

_ISOLATED = ("-c", "init.defaultBranch=main",
             "-c", "user.email=t@example.com",
             "-c", "user.name=T",
             "-c", "commit.gpgsign=false",
             "-c", "protocol.file.allow=always")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *_ISOLATED, *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True).stdout.strip()


class _Cfg:
    def __init__(self, branch: str | None) -> None:
        self.branch = branch
        self.url = "https://github.com/acme/thing.git"
        self.provider = "github"


class _Store:
    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def get_in_workspace(self, _ws, _slug):
        return self._cfg

    def get(self, _uid, _slug):
        return self._cfg


@pytest.fixture
def clone_on_dev(tmp_path, monkeypatch):
    """A remote with main AND dev, and a clone standing on dev."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", *_ISOLATED, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True)

    author = tmp_path / "author"
    subprocess.run(["git", *_ISOLATED, "clone", "-q", str(remote), str(author)],
                   check=True)
    (author / "a.py").write_text("on main\n")
    _git("add", "-A", cwd=author)
    _git("commit", "-qm", "main commit", cwd=author)
    _git("push", "-q", "origin", "HEAD:main", cwd=author)

    _git("checkout", "-qb", "dev", cwd=author)
    (author / "a.py").write_text("on dev\n")
    _git("commit", "-qam", "dev commit", cwd=author)
    _git("push", "-q", "origin", "HEAD:dev", cwd=author)

    repos = tmp_path / "repos"
    repos.mkdir()
    clone = repos / "acme-thing"
    subprocess.run(["git", *_ISOLATED, "clone", "-q", "-b", "dev",
                    str(remote), str(clone)], check=True)

    class _Settings:
        def repo_path(self, slug):
            return repos / slug

    monkeypatch.setattr("src.config.get_settings", lambda: _Settings())
    return {"clone": clone, "remote": remote}


def _asked_ref(monkeypatch, cfg_branch, slug="acme-thing") -> str:
    """The ref `remote_head` puts to the remote."""
    seen: dict = {}

    def fake_ls_remote(url, ref, env_extra):
        seen["ref"] = ref
        return "0" * 40

    monkeypatch.setattr(freshness, "_run_ls_remote", fake_ls_remote)
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store",
                        lambda: _Store(_Cfg(cfg_branch)))
    monkeypatch.setattr("src.credentials.resolve_git_credential",
                        lambda *a, **k: None)
    freshness.remote_head(slug, workspace_id="ws-1")
    return seen["ref"]


def test_it_asks_about_the_branch_the_clone_is_standing_on(clone_on_dev, monkeypatch):
    assert _asked_ref(monkeypatch, None) == "refs/heads/dev", (
        "the check asked about the provider default while the clone — the "
        "thing that was actually indexed — was standing on dev"
    )


def test_a_configured_branch_still_wins(clone_on_dev, monkeypatch):
    """Somebody said which branch. That is not a guess to be second-guessed."""
    assert _asked_ref(monkeypatch, "release") == "refs/heads/release"


def test_with_no_clone_it_falls_back_to_the_provider_default(monkeypatch, tmp_path):
    class _Settings:
        def repo_path(self, slug):
            return tmp_path / "nothing-here" / slug

    monkeypatch.setattr("src.config.get_settings", lambda: _Settings())
    assert _asked_ref(monkeypatch, None) == "HEAD"


def test_a_detached_clone_falls_back_to_the_provider_default(clone_on_dev, monkeypatch):
    """Detached names no branch, so there is nothing better to ask for."""
    _git("checkout", "-q", "--detach", "HEAD", cwd=clone_on_dev["clone"])
    assert _asked_ref(monkeypatch, None) == "HEAD"


def test_the_check_and_the_advance_name_the_same_ref(clone_on_dev, monkeypatch):
    """The invariant, held end to end rather than by two matching guesses.

    The advance is what moves the clone; the check is what decides whether it
    should. If they can name different branches, the repository oscillates
    between "behind" and a re-index that does not fix it.
    """
    from src.sync.incremental import _advance_to_remote

    asked = _asked_ref(monkeypatch, None)

    clone = clone_on_dev["clone"]
    branch_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone)
    assert _advance_to_remote(clone) is True
    branch_after = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone)

    assert branch_after == branch_before == "dev"
    assert asked == f"refs/heads/{branch_after}", (
        f"the check asked for {asked} and the advance moved {branch_after}"
    )
    assert (clone / "a.py").read_text() == "on dev\n", (
        "the advance pulled the wrong branch's content onto disk"
    )
