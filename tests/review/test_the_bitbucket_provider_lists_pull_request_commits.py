"""Every provider can say what the commits of a pull request are called.

A team that writes the task key in its commits but not in the title or the
branch is found through them; the list is asked for only when nothing else
names a task, so the call must be cheap, bounded and unable to fail a review.
The base class answers "none" for a provider that cannot say.
"""

from __future__ import annotations

import httpx
import pytest

from src.review.models import PullRequest
from src.review.providers.base import PullRequestProvider
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.test_providers import _patch_client


def _pr(repo: str, number: int = 5) -> PullRequest:
    return PullRequest(
        provider="x", repo=repo, number=number, title="t", description="", author="a",
        base_ref="main", base_sha="b", head_ref="feat", head_sha="h", state="open", hunks=[])


def _serve(provider, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    _patch_client(provider, httpx.MockTransport(wrapped))
    return seen


def test_bitbucket_lists_the_messages_of_the_pull_requests_commits():
    p = BitbucketPRProvider(token="fake")
    seen = _serve(p, lambda r: httpx.Response(200, json={"values": [
        {"message": "PROJ-6066 newest"}, {"message": ""}, {"message": "older"}]}))
    assert p.fetch_commit_messages(_pr("ws/r"), limit=10) == ["PROJ-6066 newest", "older"]
    assert str(seen[0].url).endswith("/repositories/ws/r/pullrequests/5/commits?pagelen=10")


def test_github_lists_them_newest_first_though_it_answers_oldest_first():
    p = GitHubPRProvider(token="fake")
    seen = _serve(p, lambda r: httpx.Response(200, json=[
        {"commit": {"message": "old"}}, {"commit": {"message": "PROJ-1 new"}}]))
    assert p.fetch_commit_messages(_pr("o/n")) == ["PROJ-1 new", "old"]
    assert seen[0].url.path == "/repos/o/n/pulls/5/commits"


def test_github_reaches_the_newest_commits_of_a_pull_request_with_more_than_one_page():
    p = GitHubPRProvider(token="fake")

    def pages(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        first = (page - 1) * 100
        count = 100 if page < 3 else 50                 # 250 commits, oldest first
        return httpx.Response(200, json=[
            {"commit": {"message": f"c{n}"}} for n in range(first, first + count)])

    seen = _serve(p, pages)
    found = p.fetch_commit_messages(_pr("o/n"), limit=3)
    assert found == ["c249", "c248", "c247"]
    assert len(seen) == 3


def test_github_stops_asking_for_pages_when_one_is_not_full():
    p = GitHubPRProvider(token="fake")
    seen = _serve(p, lambda r: httpx.Response(200, json=[{"commit": {"message": "only"}}]))
    assert p.fetch_commit_messages(_pr("o/n")) == ["only"]
    assert len(seen) == 1


def test_gitlab_lists_the_messages_of_the_merge_requests_commits():
    p = GitLabPRProvider(token="fake")
    seen = _serve(p, lambda r: httpx.Response(200, json=[{"message": "PROJ-2 first"}, {"message": "x"}]))
    assert p.fetch_commit_messages(_pr("group/proj")) == ["PROJ-2 first", "x"]
    assert "/merge_requests/5/commits" in str(seen[0].url)


@pytest.mark.parametrize("provider", [
    BitbucketPRProvider(token="fake"), GitHubPRProvider(token="fake"),
    GitLabPRProvider(token="fake"),
])
@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(500, json={}),
    lambda r: httpx.Response(404, json={}),
    lambda r: httpx.Response(200, text="not json"),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_a_provider_that_cannot_answer_gives_no_commits_not_an_error(provider, answer):
    _serve(provider, answer)
    assert provider.fetch_commit_messages(_pr("a/b")) == []


def test_the_limit_bounds_what_is_returned():
    p = GitLabPRProvider(token="fake")
    _serve(p, lambda r: httpx.Response(200, json=[{"message": f"m{n}"} for n in range(30)]))
    assert len(p.fetch_commit_messages(_pr("a/b"), limit=5)) == 5


def test_a_provider_that_cannot_say_answers_with_nothing():
    assert PullRequestProvider.fetch_commit_messages(object(), _pr("a/b")) == []
