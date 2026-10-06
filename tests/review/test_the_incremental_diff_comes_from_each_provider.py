"""What an incremental review reads comes from the provider's own two-commit APIs.

Per provider: the commits of the pull request (with parents, across pages;
a list that may be cut short is "unknown", never a short list that proves a
force-push), and the diff between the last reviewed commit and the head. Any
failure is None, and the orchestrator then reviews the whole pull request.

  GitHub     GET /pulls/{n}/commits, GET /compare/{base}...{head} (diff media type)
  GitLab     GET /merge_requests/{n}/commits, GET /repository/compare?straight=true
  Bitbucket  GET /pullrequests/{n}/commits, GET /diff/{head}..{base}?merge=false
             (answered with a 302 like the pull request's own diff)
"""

from __future__ import annotations

import httpx
import pytest

from src.review.providers import bitbucket as bb_mod
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from src.review.scope import MAX_LISTED_COMMITS
from tests.review.test_providers import _patch_client

BASE = "a" * 40
HEAD = "b" * 40
DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


class _Recorder:
    """A transport that answers from a table of {path-suffix: response} and
    remembers every request."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for suffix, answer in self.routes.items():
            if url.split("#")[0].endswith(suffix) or suffix in url:
                return answer(request) if callable(answer) else answer
        return httpx.Response(404, text="not routed: " + url)


def _wire(provider, routes: dict) -> _Recorder:
    rec = _Recorder(routes)
    _patch_client(provider, httpx.MockTransport(rec))
    return rec


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(bb_mod.time, "sleep", lambda s: None)


# ─── GitHub ──────────────────────────────────────────────────────────


def _gh_commit(sha: str, *parents: str) -> dict:
    return {"sha": sha, "parents": [{"sha": p} for p in parents],
            "commit": {"message": "msg", "committer": {"date": "2026-01-01T00:00:00Z"}}}


def test_github_lists_the_commits_with_their_parents() -> None:
    provider = GitHubPRProvider(token="t")
    rec = _wire(provider, {"/pulls/7/commits?per_page=100": httpx.Response(
        200, json=[_gh_commit(HEAD, BASE), _gh_commit(BASE)])})

    commits = provider.list_pr_commits("acme/shop", 7)

    assert [(c.sha, c.parents) for c in commits] == [(HEAD, (BASE,)), (BASE, ())]
    assert "/repos/acme/shop/pulls/7/commits" in str(rec.requests[0].url)


def test_github_follows_the_pages_of_a_long_list() -> None:
    provider = GitHubPRProvider(token="t")
    page2 = "https://api.github.com/repositories/1/pulls/7/commits?page=2"

    def first(request):
        return httpx.Response(200, json=[_gh_commit(HEAD, BASE)],
                              headers={"Link": f'<{page2}>; rel="next"'})

    _wire(provider, {"/pulls/7/commits?per_page=100": first,
                     "page=2": httpx.Response(200, json=[_gh_commit(BASE)])})

    assert [c.sha for c in provider.list_pr_commits("acme/shop", 7)] == [HEAD, BASE]


def test_github_does_not_pretend_a_cut_short_list_is_the_history() -> None:
    provider = GitHubPRProvider(token="t")
    many = [_gh_commit(f"{i:040x}") for i in range(MAX_LISTED_COMMITS)]
    _wire(provider, {"/pulls/7/commits": httpx.Response(200, json=many)})

    assert provider.list_pr_commits("acme/shop", 7) is None


def test_github_commits_that_cannot_be_read_are_unknown() -> None:
    provider = GitHubPRProvider(token="t")
    _wire(provider, {"/pulls/7/commits": httpx.Response(500, text="boom")})

    assert provider.list_pr_commits("acme/shop", 7) is None


def test_github_compares_with_the_diff_media_type() -> None:
    provider = GitHubPRProvider(token="t")
    rec = _wire(provider, {f"/compare/{BASE}...{HEAD}": httpx.Response(200, text=DIFF)})

    assert provider.fetch_incremental_diff("acme/shop", 7, BASE, HEAD) == DIFF
    (request,) = rec.requests
    assert request.headers["Accept"] == "application/vnd.github.v3.diff"


@pytest.mark.parametrize("status", [404, 422, 500])
def test_github_compare_that_fails_is_none(status) -> None:
    provider = GitHubPRProvider(token="t")
    _wire(provider, {"/compare/": httpx.Response(status, text="nope")})

    assert provider.fetch_incremental_diff("acme/shop", 7, BASE, HEAD) is None


# ─── GitLab ──────────────────────────────────────────────────────────


def _gl_commit(sha: str, *parents: str) -> dict:
    return {"id": sha, "parent_ids": list(parents), "message": "msg",
            "committed_date": "2026-01-01T00:00:00Z"}


def test_gitlab_lists_the_commits_across_pages() -> None:
    provider = GitLabPRProvider(token="t")

    def first(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[_gl_commit(BASE)])
        return httpx.Response(200, json=[_gl_commit(HEAD, BASE)],
                              headers={"X-Next-Page": "2"})

    rec = _wire(provider, {"/merge_requests/9/commits": first})

    commits = provider.list_pr_commits("group/sub/proj", 9)

    assert [(c.sha, c.parents) for c in commits] == [(HEAD, (BASE,)), (BASE, ())]
    assert "/projects/group%2Fsub%2Fproj/merge_requests/9/commits" in str(rec.requests[0].url)


def test_gitlab_commits_that_cannot_be_read_are_unknown() -> None:
    provider = GitLabPRProvider(token="t")
    _wire(provider, {"/merge_requests/9/commits": httpx.Response(403, text="no")})

    assert provider.list_pr_commits("group/proj", 9) is None


def test_gitlab_compare_is_rebuilt_as_a_unified_diff() -> None:
    provider = GitLabPRProvider(token="t")
    body = {"diffs": [
        {"old_path": "x.py", "new_path": "x.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"},
        {"old_path": "n.py", "new_path": "n.py", "new_file": True, "diff": "@@ -0,0 +1 @@\n+z\n"},
    ]}
    rec = _wire(provider, {"/repository/compare": httpx.Response(200, json=body)})

    text = provider.fetch_incremental_diff("group/proj", 9, BASE, HEAD)

    params = rec.requests[0].url.params
    assert (params["from"], params["to"], params["straight"]) == (BASE, HEAD, "true")
    assert "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@" in text
    assert "--- /dev/null\n+++ b/n.py" in text


@pytest.mark.parametrize("body", [
    {"diffs": [{"new_path": "x.py", "old_path": "x.py", "diff": "", "too_large": True}]},
    {"diffs": [{"new_path": "x.py", "old_path": "x.py", "diff": "", "collapsed": True}]},
    {"diffs": [], "compare_timeout": True},
    {"commit": {}},
])
def test_gitlab_compare_that_was_cut_short_is_none(body) -> None:
    provider = GitLabPRProvider(token="t")
    _wire(provider, {"/repository/compare": httpx.Response(200, json=body)})

    assert provider.fetch_incremental_diff("group/proj", 9, BASE, HEAD) is None


# ─── Bitbucket ───────────────────────────────────────────────────────

API = "https://api.bitbucket.org/2.0"
REPO = f"{API}/repositories/ws/r"


def _bb_commit(sha: str, *parents: str) -> dict:
    return {"hash": sha, "parents": [{"hash": p} for p in parents], "message": "msg",
            "date": "2026-01-01T00:00:00+00:00"}


def test_bitbucket_lists_the_commits_across_pages() -> None:
    provider = BitbucketPRProvider(token="t")
    page2 = f"{REPO}/pullrequests/4/commits?page=2"
    _wire(provider, {
        "/pullrequests/4/commits?pagelen=50": httpx.Response(
            200, json={"values": [_bb_commit(HEAD, BASE)], "next": page2}),
        "page=2": httpx.Response(200, json={"values": [_bb_commit(BASE)]}),
    })

    commits = provider.list_pr_commits("ws/r", 4)

    assert [(c.sha, c.parents) for c in commits] == [(HEAD, (BASE,)), (BASE, ())]


def test_bitbucket_commits_that_cannot_be_read_are_unknown() -> None:
    provider = BitbucketPRProvider(token="t")
    _wire(provider, {"/pullrequests/4/commits": httpx.Response(500, text="boom")})

    assert provider.list_pr_commits("ws/r", 4) is None


def test_bitbucket_diffs_two_commits_without_a_merge_base_and_follows_the_redirect() -> None:
    provider = BitbucketPRProvider(token="t")
    final = f"{REPO}/diff/{HEAD[:12]}..{BASE[:12]}?merge=false&from=x"
    rec = _wire(provider, {
        f"/diff/{HEAD}..{BASE}?merge=false": httpx.Response(302, headers={"Location": final}),
        "from=x": httpx.Response(200, text=DIFF),
    })

    assert provider.fetch_incremental_diff("ws/r", 4, BASE, HEAD) == DIFF
    first = str(rec.requests[0].url)
    assert first.endswith(f"/diff/{HEAD}..{BASE}?merge=false"), (
        "head first, base second, and never the PR's merge base")
    assert len(rec.requests) == 2


def test_bitbucket_diff_that_fails_is_none() -> None:
    provider = BitbucketPRProvider(token="t")
    _wire(provider, {"/diff/": httpx.Response(404, text="gone")})

    assert provider.fetch_incremental_diff("ws/r", 4, BASE, HEAD) is None


def test_bitbucket_redirect_off_the_api_is_none_not_followed() -> None:
    provider = BitbucketPRProvider(token="t")
    rec = _wire(provider, {"/diff/": httpx.Response(
        302, headers={"Location": "https://evil.example.com/steal"})})

    assert provider.fetch_incremental_diff("ws/r", 4, BASE, HEAD) is None
    assert all("evil.example.com" not in str(r.url) for r in rec.requests)
