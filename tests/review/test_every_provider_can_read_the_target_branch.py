"""The issues backlog reads the TARGET BRANCH through each provider.

Five calls: the branch head sha, a file at a sha, the commits that touched a
path, what one commit did to a path, and a commit's web address. Pinned here
against mocked HTTP for GitHub, GitLab and Bitbucket:

  * a missing file is `None` (it is a fact), any other failure raises (it is
    "could not look", which the resolver reads as unreadable, never fixed);
  * a Cyrillic or spaced path is percent-encoded, not mangled;
  * a file over the size cap is refused rather than read half;
  * a rename and a deletion are told apart from an edit;
  * Bitbucket's 302 on file contents is followed (to its own API only).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest

from src.review.providers.base import MAX_FILE_BYTES, PullRequestProviderError
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.test_providers import _patch_client

CYR = "src/каталог/звіт 1.py"


def _provider(cls, handler):
    p = cls(token="fake")
    _patch_client(p, httpx.MockTransport(handler))
    return p


def _json(data, status=200):
    return httpx.Response(status, json=data)


# ─── GitHub ────────────────────────────────────────────────────────


def test_github_reads_the_branch_head_and_a_file_at_a_sha() -> None:
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if "/branches/" in req.url.path:
            return _json({"commit": {"sha": "abc123"}})
        return httpx.Response(200, text="print(1)\n")

    p = _provider(GitHubPRProvider, handler)
    assert p.branch_head_sha("o/r", "release/1.x") == "abc123"
    assert p.read_file_at("o/r", "abc123", CYR) == "print(1)\n"
    file_req = seen[-1]
    assert unquote(file_req.url.path).endswith(CYR)
    assert "%D0%BA" in file_req.url.raw_path.decode()   # encoded on the wire
    assert file_req.url.params["ref"] == "abc123"


def test_github_says_none_for_a_missing_file_and_raises_for_a_refusal() -> None:
    p = _provider(GitHubPRProvider, lambda r: httpx.Response(404, json={}))
    assert p.read_file_at("o/r", "sha", "a.py") is None
    p = _provider(GitHubPRProvider, lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(PullRequestProviderError):
        p.read_file_at("o/r", "sha", "a.py")
    with pytest.raises(PullRequestProviderError):
        p.branch_head_sha("o/r", "main")


def test_a_file_over_the_cap_is_refused_not_cut() -> None:
    big = httpx.Response(200, content=b"x" * (MAX_FILE_BYTES + 1))
    p = _provider(GitHubPRProvider, lambda r: big)
    with pytest.raises(PullRequestProviderError, match="not read"):
        p.read_file_at("o/r", "sha", "a.py")


def test_github_lists_the_commits_that_touched_a_path_since_a_date() -> None:
    captured = {}

    def handler(req):
        captured.update(dict(req.url.params))
        return _json([{"sha": "c2", "html_url": "https://x/c2", "commit": {
            "message": "Fix totals (#12)\n\nlong body", "committer": {"date": "2026-09-20T10:00:00Z"}}}])

    p = _provider(GitHubPRProvider, handler)
    [c] = p.commits_touching("o/r", "main", "a.py",
                             since=datetime(2026, 9, 1, tzinfo=UTC), limit=5)
    assert (c.sha, c.subject) == ("c2", "Fix totals (#12)")
    assert captured["path"] == "a.py" and captured["sha"] == "main"
    assert captured["since"].startswith("2026-09-01")


def test_github_tells_a_rename_and_a_deletion_from_an_edit() -> None:
    files = [
        {"filename": "new.py", "previous_filename": "old.py", "status": "renamed"},
        {"filename": "gone.py", "status": "removed"},
        {"filename": "same.py", "status": "modified"},
    ]
    p = _provider(GitHubPRProvider, lambda r: _json({"files": files}))
    renamed = p.file_change_in_commit("o/r", "c", "old.py")
    assert (renamed.status, renamed.path, renamed.previous_path) == ("renamed", "new.py", "old.py")
    assert p.file_change_in_commit("o/r", "c", "gone.py").status == "deleted"
    assert p.file_change_in_commit("o/r", "c", "same.py").status == "modified"
    assert p.file_change_in_commit("o/r", "c", "other.py") is None


# ─── GitLab ────────────────────────────────────────────────────────


def test_gitlab_encodes_the_project_branch_and_path() -> None:
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if "/branches/" in req.url.path:
            return _json({"commit": {"id": "d34d"}})
        return httpx.Response(200, text="ok\n")

    p = _provider(GitLabPRProvider, handler)
    assert p.branch_head_sha("grp/sub/proj", "feat/ж") == "d34d"
    assert p.read_file_at("grp/sub/proj", "d34d", CYR) == "ok\n"
    raw = seen[-1].url.raw_path.decode()
    assert "grp%2Fsub%2Fproj" in raw and "%2F" in raw.split("/files/")[1]


def test_gitlab_reads_deletions_and_renames_from_the_commit_diff() -> None:
    diff = [
        {"old_path": "a.py", "new_path": "b.py", "renamed_file": True},
        {"old_path": "c.py", "new_path": "c.py", "deleted_file": True},
    ]
    p = _provider(GitLabPRProvider, lambda r: _json(diff))
    assert p.file_change_in_commit("g/p", "s", "a.py").path == "b.py"
    assert p.file_change_in_commit("g/p", "s", "c.py").status == "deleted"
    assert p.commit_url("g/p", "s").endswith("/g/p/-/commit/s")


# ─── Bitbucket ─────────────────────────────────────────────────────

BB = "https://api.bitbucket.org/2.0/repositories/ws/r"


def test_bitbucket_follows_the_redirect_of_a_file_read() -> None:
    hops: list[str] = []

    def handler(req):
        hops.append(req.url.path)
        if "/src/" in req.url.path and "redirected" not in req.url.path:
            return httpx.Response(302, headers={
                "location": f"{BB}/src/redirected/file"})
        return httpx.Response(200, text="hello\n")

    p = _provider(BitbucketPRProvider, handler)
    assert p.read_file_at("ws/r", "abc", CYR) == "hello\n"
    assert len(hops) == 2


def test_bitbucket_will_not_follow_a_redirect_to_another_host() -> None:
    p = _provider(BitbucketPRProvider, lambda r: httpx.Response(
        302, headers={"location": "https://evil.example/steal"}))
    with pytest.raises(PullRequestProviderError, match="another host"):
        p.read_file_at("ws/r", "abc", "a.py")


def test_bitbucket_reads_the_branch_and_the_diffstat() -> None:
    def handler(req):
        if "/refs/branches/" in req.url.path:
            return _json({"target": {"hash": "f00d"}})
        return _json({"values": [
            {"status": "removed", "old": {"path": "gone.py"}, "new": None},
            {"status": "renamed", "old": {"path": "a.py"}, "new": {"path": "b.py"}},
        ]})

    p = _provider(BitbucketPRProvider, handler)
    assert p.branch_head_sha("ws/r", "main") == "f00d"
    assert p.file_change_in_commit("ws/r", "c", "gone.py").status == "deleted"
    assert p.file_change_in_commit("ws/r", "c", "a.py").path == "b.py"
    assert p.commit_url("ws/r", "c") == "https://bitbucket.org/ws/r/commits/c"


def test_an_invalid_repo_name_is_an_error_not_a_request() -> None:
    p = _provider(BitbucketPRProvider, lambda r: pytest.fail("no request expected"))
    with pytest.raises(PullRequestProviderError):
        p.branch_head_sha("no-slash", "main")
