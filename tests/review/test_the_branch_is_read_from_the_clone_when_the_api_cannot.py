"""The target branch is read from the local clone when the provider's API fails.

A token without repository read scope or a rate limit must not make every
backlog issue unreadable when the clone already holds the branch. The fallback
reads what the clone has (it never fetches), runs git as a list of arguments
with the path after `--`, refuses a branch name an option parser could read,
and still answers "unavailable" (never "fixed") when neither source can.
"""

from __future__ import annotations

import subprocess

import pytest

from src.review import issue_content
from src.review.issue_content import (
    ContentSource,
    ContentUnavailable,
    pr_number_from_commit_subject,
    pr_url_for,
)
from src.review.providers.base import PullRequestProviderError

CYR = "каталог/звіт.py"


class _Down:
    name = "github"

    def _boom(self, *a, **kw):
        raise PullRequestProviderError("GitHub 403")

    branch_head_sha = read_file_at = commits_touching = file_change_in_commit = _boom


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "каталог").mkdir()
    (tmp_path / CYR).write_text("one = 1\n", encoding="utf-8")
    (tmp_path / "-rf").write_text("dash\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "Add files")
    _git(tmp_path, "mv", CYR, "каталог/renamed.py")
    _git(tmp_path, "rm", "-q", "--", "-rf")
    _git(tmp_path, "commit", "-q", "-m", "Move and drop (pull request #3)")
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head)
    monkeypatch.setattr(issue_content, "_clone_dir", lambda slug: tmp_path)
    return tmp_path, head


def _source() -> ContentSource:
    return ContentSource(_Down(), "o/r", local_slug="o-r", pr_provider="github")


def test_the_head_comes_from_the_clone_when_the_api_refuses(clone) -> None:
    _path, head = clone
    assert _source().head_sha("main") == head


def test_a_file_is_read_by_sha_and_a_missing_one_is_none(clone) -> None:
    _path, head = clone
    src = _source()
    assert src.read(head, "каталог/renamed.py") == "one = 1\n"
    assert src.read(head, CYR) is None


def test_a_path_that_looks_like_an_option_is_still_a_path(clone) -> None:
    path, head = clone
    first = subprocess.run(["git", "-C", str(path), "rev-list", "--max-parents=0", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.split()[0]
    assert _source().read(first, "-rf") == "dash\n"


def test_history_and_the_kind_of_change_come_from_the_clone(clone) -> None:
    _path, head = clone
    src = _source()
    [newest, *_] = src.history(head, "каталог/renamed.py")
    assert newest.sha == head and "pull request #3" in newest.subject
    moved = src.change(head, CYR)
    assert (moved.status, moved.path) == ("renamed", "каталог/renamed.py")
    assert src.change(head, "-rf").status == "deleted"


def test_a_branch_name_git_could_read_as_an_option_is_refused(clone) -> None:
    for bad in ("--upload-pack=x", "-x", "a..b", "a b", "a~1", ""):
        with pytest.raises(ContentUnavailable):
            _source().head_sha(bad)


def test_with_no_clone_and_no_api_the_branch_is_unavailable_never_fixed(monkeypatch) -> None:
    monkeypatch.setattr(issue_content, "_clone_dir", lambda slug: None)
    with pytest.raises(ContentUnavailable):
        _source().head_sha("main")


def test_a_sha_the_clone_does_not_have_is_unavailable(clone) -> None:
    with pytest.raises(ContentUnavailable):
        _source().read("0" * 40, "a.py")


# ─── Which PR a commit came from ───────────────────────────────────


@pytest.mark.parametrize(("subject", "provider", "number"), [
    ("Merged in feat/x (pull request #1234)", "bitbucket", 1234),
    ("Fix the thing (#56)", "github", 56),
    ("Merge pull request #7 from a/b", "github", 7),
    ("Resolve it (!88)", "gitlab", 88),
    ("See merge request grp/proj!9", "gitlab", 9),
    ("Bump to v2024", "github", None),
    ("", "github", None),
    (None, "bitbucket", None),
])
def test_the_pr_a_commit_names_is_read_from_its_subject(subject, provider, number) -> None:
    assert pr_number_from_commit_subject(subject, provider) == number


def test_a_pr_address_is_built_for_each_provider() -> None:
    assert pr_url_for("github", "o/r", 5) == "https://github.com/o/r/pull/5"
    assert pr_url_for("gitlab", "g/p", 5, base_url="https://git.example/") == \
        "https://git.example/g/p/-/merge_requests/5"
    assert pr_url_for("bitbucket", "w/r", 5) == "https://bitbucket.org/w/r/pull-requests/5"
    assert pr_url_for("other", "w/r", 5) is None
