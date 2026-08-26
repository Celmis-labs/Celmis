"""Tests для PR providers (GitHub/GitLab/Bitbucket) — через httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from src.review.models import (
    Finding,
    FindingSeverity,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.providers import get_provider_for
from src.review.providers.base import (
    PullRequestProviderError,
    _format_finding_body,
    _format_summary,
)
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider

SAMPLE_DIFF = """diff --git a/src/foo.py b/src/foo.py
index 1234567..abcdefg 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,7 @@
 def existing_func():
-    return 1
+    return 2
+    # New behavior 1
+    # New behavior 2

 def other_func():
     pass
"""


def _patch_client(provider, transport: httpx.MockTransport) -> None:
    """Replace internal httpx.Client with mock-backed одне."""
    provider._http.close()
    provider._http = httpx.Client(
        transport=transport,
        timeout=10.0,
        headers={"User-Agent": "code-analyzer/0.1"},
    )


# ─── Factory ────────────────────────────────────────────────────────


class TestFactory:
    def test_known_providers_explicit_token(self) -> None:
        """Передаючи token напряму — bypass credential store lookup."""
        # Дозвіл — створювати кожний з провайдерів безпосередньо з explicit token
        gh = GitHubPRProvider(token="fake")
        assert gh.name == "github"
        gh.close()

        gl = GitLabPRProvider(token="fake")
        assert gl.name == "gitlab"
        gl.close()

        bb = BitbucketPRProvider(token="fake")
        assert bb.name == "bitbucket"
        bb.close()

    def test_unknown_provider(self) -> None:
        with pytest.raises(ValueError):
            get_provider_for("svn")


# ─── Body formatting ────────────────────────────────────────────────


class TestFormatting:
    def test_finding_body_includes_severity_emoji(self) -> None:
        f = Finding(
            file_path="a.py", line=1,
            severity=FindingSeverity.CRITICAL,
            title="SQL injection risk",
            body="User input concatenated.",
            agent="security",
            rule_id="sec.sql-inject",
        )
        body = _format_finding_body(f)
        assert "🔴" in body  # critical emoji
        assert "SQL injection risk" in body
        assert "User input" in body
        assert "security" in body
        assert "sec.sql-inject" in body

    def test_finding_with_suggestion(self) -> None:
        f = Finding(
            file_path="a.py", line=1, body="Use parameterized query",
            suggestion="cursor.execute(query, [user_id])",
            rule_id="r",
        )
        body = _format_finding_body(f)
        assert "```suggestion" in body
        assert "cursor.execute" in body

    def test_summary_includes_marker(self) -> None:
        pr = PullRequest(
            provider="github", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )
        batch = ReviewBatch(pull_request=pr, verdict=ReviewVerdict.APPROVE)
        summary = _format_summary(batch, marker="<!-- code-analyzer:review -->")
        assert "<!-- code-analyzer:review -->" in summary
        assert "APPROVED" in summary

    def test_summary_with_findings_shows_counts(self) -> None:
        pr = PullRequest(
            provider="github", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )
        batch = ReviewBatch(
            pull_request=pr,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            findings=[
                Finding(file_path="a.py", line=1, severity=FindingSeverity.CRITICAL, rule_id="r1"),
                Finding(file_path="a.py", line=2, severity=FindingSeverity.WARNING, rule_id="r2"),
            ],
        )
        summary = _format_summary(batch, marker="X")
        assert "Critical:** 1" in summary
        assert "Warning:** 1" in summary


# ─── GitHub provider ────────────────────────────────────────────────


class TestGitHubProvider:
    def test_split_repo(self) -> None:
        assert GitHubPRProvider._split_repo("owner/name") == ("owner", "name")

    def test_split_repo_invalid_raises(self) -> None:
        with pytest.raises(PullRequestProviderError):
            GitHubPRProvider._split_repo("just-name")

    def test_parse_link_next(self) -> None:
        link = '<https://api.github.com/repos/o/r/issues/1/comments?page=2>; rel="next"'
        url = GitHubPRProvider._parse_link_next(link)
        assert url == "https://api.github.com/repos/o/r/issues/1/comments?page=2"

    def test_fetch_pull_request(self) -> None:
        meta_payload = {
            "title": "Test PR",
            "body": "Description",
            "user": {"login": "alice"},
            "base": {"ref": "main", "sha": "base123"},
            "head": {"ref": "feat", "sha": "head456"},
            "state": "open",
            "draft": False,
            "html_url": "https://github.com/o/r/pull/1",
        }

        def handler(req: httpx.Request) -> httpx.Response:
            accept = req.headers.get("accept", "").lower()
            if "diff" in accept:
                return httpx.Response(200, text=SAMPLE_DIFF)
            return httpx.Response(200, json=meta_payload)

        provider = GitHubPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        pr = provider.fetch_pull_request("o/r", 1)
        assert pr.title == "Test PR"
        assert pr.author == "alice"
        assert pr.base_sha == "base123"
        assert pr.head_sha == "head456"
        assert len(pr.hunks) == 1
        provider.close()

    def test_post_review_dry_run(self) -> None:
        provider = GitHubPRProvider(token="fake")
        _patch_client(
            provider,
            httpx.MockTransport(
                lambda req: httpx.Response(200, json={"id": 1})
            ),
        )

        pr = PullRequest(
            provider="github", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )
        batch = ReviewBatch(
            pull_request=pr,
            verdict=ReviewVerdict.COMMENT,
            findings=[Finding(file_path="a.py", line=1, rule_id="r")],
        )
        result = provider.post_review(batch, dry_run=True)
        assert result["dry_run"] is True
        assert "would_post" in result
        assert result["would_post"]["event"] == "COMMENT"
        provider.close()

    def test_find_existing_marker_comment(self) -> None:
        """The marker alone no longer identifies our comment — the author has
        to match too — so the fake serves GET /user and every comment carries
        a `user`, the way the real API returns them."""
        comments_page = [
            {"id": 1, "body": "regular comment", "user": {"login": "alice"}},
            {
                "id": 42,
                "body": "<!-- code-analyzer:review -->\nSome content",
                "user": {"login": "celmis-bot"},
            },
            {"id": 3, "body": "another", "user": {"login": "bob"}},
        ]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/user":
                return httpx.Response(200, json={"login": "celmis-bot"})
            return httpx.Response(200, json=comments_page)

        provider = GitHubPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        cid = provider.find_existing_review_comment(
            "o/r", 1, "<!-- code-analyzer:review -->",
        )
        assert cid == 42
        provider.close()

    def test_find_marker_returns_none_if_absent(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/user":
                return httpx.Response(200, json={"login": "celmis-bot"})
            return httpx.Response(
                200, json=[{"id": 1, "body": "regular", "user": {"login": "alice"}}],
            )

        provider = GitHubPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        assert provider.find_existing_review_comment("o/r", 1, "MARKER") is None
        provider.close()

    def test_our_own_marker_under_somebody_elses_name_is_not_ours(self) -> None:
        """A comment can carry our marker without being ours: the marker is an
        HTML comment, and quoting a comment copies its raw markdown. The author
        is the half of the predicate that cannot be copied."""
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/user":
                return httpx.Response(200, json={"login": "celmis-bot"})
            return httpx.Response(200, json=[
                {"id": 7, "body": "> MARKER\n\nno it isn't",
                 "user": {"login": "alice"}},
            ])

        provider = GitHubPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        assert provider.find_existing_review_comment("o/r", 1, "MARKER") is None
        provider.close()


# ─── GitLab provider ────────────────────────────────────────────────


class TestGitLabProvider:
    def test_replace_page_param_idempotent(self) -> None:
        url = "https://gitlab.com/api/v4/projects?per_page=100"
        result = GitLabPRProvider._replace_page_param(url, "3")
        assert "page=3" in result
        # per_page лишається unchanged
        assert "per_page=100" in result

    def test_fetch_merge_request(self) -> None:
        meta_payload = {
            "title": "Add feature",
            "description": "Body",
            "author": {"username": "bob"},
            "target_branch": "main",
            "source_branch": "feat/x",
            "diff_refs": {"base_sha": "b1", "head_sha": "h1", "start_sha": "s1"},
            "state": "opened",
            "draft": False,
            "web_url": "https://gitlab.com/o/r/-/merge_requests/5",
        }

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/raw_diffs" in url:
                return httpx.Response(200, text=SAMPLE_DIFF)
            return httpx.Response(200, json=meta_payload)

        provider = GitLabPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        pr = provider.fetch_pull_request("group/proj", 5)
        assert pr.title == "Add feature"
        assert pr.author == "bob"
        assert pr.head_sha == "h1"
        provider.close()


# ─── Bitbucket provider ────────────────────────────────────────────


class TestBitbucketProvider:
    def test_split_repo(self) -> None:
        assert BitbucketPRProvider._split_repo("ws/repo") == ("ws", "repo")

    def test_split_repo_invalid(self) -> None:
        with pytest.raises(PullRequestProviderError):
            BitbucketPRProvider._split_repo("just-name")

    def test_fetch_pull_request(self) -> None:
        meta_payload = {
            "title": "Add feature",
            "description": "Body",
            "author": {"nickname": "carol"},
            "source": {
                "branch": {"name": "feat/x"},
                "commit": {"hash": "head789"},
            },
            "destination": {
                "branch": {"name": "main"},
                "commit": {"hash": "base000"},
            },
            "state": "OPEN",
            "links": {"html": {"href": "https://bitbucket.org/ws/r/pull-requests/3"}},
        }

        def handler(req: httpx.Request) -> httpx.Response:
            if str(req.url).endswith("/diff"):
                return httpx.Response(200, text=SAMPLE_DIFF)
            return httpx.Response(200, json=meta_payload)

        provider = BitbucketPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(handler))

        pr = provider.fetch_pull_request("ws/r", 3)
        assert pr.title == "Add feature"
        assert pr.author == "carol"
        assert pr.head_sha == "head789"
        assert pr.state == "open"
        provider.close()


# ─── Guarded egress ─────────────────────────────────────────────────


class TestGuardedEgress:
    """GitHub and Bitbucket built raw httpx.Clients — two of the sites that
    made the egress allowlist decorative (deployment.UNGUARDED_HTTP_SITES).
    Both go through src.http.build_client now, so a request to a host outside
    the allowlist dies inside the transport before a socket is opened — the
    thing a raw client never did.
    """

    def _providers(self):
        # The Bitbucket constructor has two auth branches (Basic via email,
        # Bearer without) and each builds its own client, so each is a
        # separate chance to reintroduce a raw one.
        return [
            GitHubPRProvider(token="fake"),
            BitbucketPRProvider(token="fake"),
            BitbucketPRProvider(token="fake", email="bot@example.com"),
        ]

    def test_every_client_refuses_a_host_off_the_allowlist(self) -> None:
        from src.http import EgressBlockedError

        for provider in self._providers():
            try:
                with pytest.raises(EgressBlockedError):
                    provider._http.get("https://evil.example.com/exfil")
            finally:
                provider.close()

    def test_each_provider_names_its_own_api_host(self) -> None:
        """The extra_allowed_hosts exception is derived from the API base
        constant, so it can only ever name the host the requests go to —
        api.github.com / api.bitbucket.org keep answering even on an install
        that trimmed the shipped allowlist."""
        from src.review.providers import bitbucket, github

        assert github._api_host() == ("api.github.com",)
        assert bitbucket._api_host() == ("api.bitbucket.org",)


# ─── Cross-provider safety check ───────────────────────────────────


class TestCrossProvider:
    def test_post_review_rejects_wrong_provider(self) -> None:
        gh = GitHubPRProvider(token="fake")
        _patch_client(gh, httpx.MockTransport(lambda r: httpx.Response(200)))

        gitlab_pr = PullRequest(
            provider="gitlab", repo="o/r", number=1,
            title="t", description="d", author="u",
            base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
            state="open",
        )
        batch = ReviewBatch(pull_request=gitlab_pr)
        with pytest.raises(PullRequestProviderError, match="non-github"):
            gh.post_review(batch, dry_run=True)
        gh.close()
