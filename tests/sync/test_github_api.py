"""Tests для GitHubClient (unit + smoke integration)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.sync.github_api import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubClient,
    GitHubScopeError,
    _parse_link_next,
    _parse_repo,
    authenticate,
)

# ─── _parse_link_next ────────────────────────────────────────────────


class TestLinkParsing:
    def test_no_link(self) -> None:
        assert _parse_link_next(None) is None
        assert _parse_link_next("") is None

    def test_single_next(self) -> None:
        link = '<https://api.github.com/user/repos?page=2>; rel="next"'
        assert _parse_link_next(link) == "https://api.github.com/user/repos?page=2"

    def test_multi_rel_links(self) -> None:
        link = (
            '<https://api.github.com/user/repos?page=2>; rel="next", '
            '<https://api.github.com/user/repos?page=10>; rel="last"'
        )
        assert _parse_link_next(link) == "https://api.github.com/user/repos?page=2"

    def test_no_next_link(self) -> None:
        link = '<https://api.github.com/user/repos?page=10>; rel="last"'
        assert _parse_link_next(link) is None


# ─── _parse_repo ────────────────────────────────────────────────────


class TestRepoParsing:
    def test_full_data(self) -> None:
        data = {
            "full_name": "octocat/Hello-World",
            "name": "Hello-World",
            "owner": {"login": "octocat"},
            "description": "A test repo",
            "language": "Python",
            "private": False,
            "archived": False,
            "fork": False,
            "default_branch": "main",
            "size": 108,
            "updated_at": "2026-04-15T10:00:00Z",
            "clone_url": "https://github.com/octocat/Hello-World.git",
            "ssh_url": "git@github.com:octocat/Hello-World.git",
            "topics": ["python", "example"],
        }
        info = _parse_repo(data)
        assert info.full_name == "octocat/Hello-World"
        assert info.owner == "octocat"
        assert info.name == "Hello-World"
        assert info.description == "A test repo"
        assert info.language == "Python"
        assert info.private is False
        assert info.size_kb == 108
        assert info.topics == ["python", "example"]

    def test_minimal_data(self) -> None:
        """Missing fields → defaults."""
        data = {
            "full_name": "x/y",
            "name": "y",
            "owner": {"login": "x"},
        }
        info = _parse_repo(data)
        assert info.description == ""
        assert info.private is False
        assert info.default_branch == "main"
        assert info.topics == []

    def test_null_fields(self) -> None:
        """description може бути None — гарантовано."""
        data = {
            "full_name": "x/y",
            "name": "y",
            "owner": {"login": "x"},
            "description": None,
            "language": None,
            "topics": None,
        }
        info = _parse_repo(data)
        assert info.description == ""
        assert info.language == ""
        assert info.topics == []


# ─── Error handling (через mock transport) ──────────────────────────


def _client_with_mock(handler):
    """Create GitHubClient з httpx MockTransport handler."""
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="fake-token")
    # Заміняємо internal http client на mock
    client._http.close()
    client._http = httpx.Client(
        transport=transport,
        timeout=10.0,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer fake-token",
            "User-Agent": "code-analyzer/0.1",
        },
    )
    return client


class TestErrorHandling:
    def test_401_unauthorized_raises(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        client = _client_with_mock(handler)
        with pytest.raises(GitHubAuthError, match="invalid or expired"):
            client.fetch_current_user()
        client.close()

    def test_403_rate_limited(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1234567890",
                },
                json={"message": "API rate limit exceeded"},
            )

        client = _client_with_mock(handler)
        with pytest.raises(GitHubAPIError, match="Rate limit"):
            client.fetch_current_user()
        client.close()

    def test_403_scope(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "4900"},
                json={"message": "Insufficient scope"},
            )

        client = _client_with_mock(handler)
        with pytest.raises(GitHubScopeError):
            client.fetch_current_user()
        client.close()

    def test_get_repo_404_returns_none(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _client_with_mock(handler)
        assert client.get_repo("ghost", "repo") is None
        client.close()


class TestSuccessPath:
    def test_fetch_user(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert "user" in str(req.url)
            return httpx.Response(200, json={"login": "octocat", "id": 1})

        client = _client_with_mock(handler)
        user = client.fetch_current_user()
        assert user["login"] == "octocat"
        client.close()

    def test_get_repo_success(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "full_name": "octocat/Hello",
                "name": "Hello",
                "owner": {"login": "octocat"},
                "default_branch": "main",
            })

        client = _client_with_mock(handler)
        info = client.get_repo("octocat", "Hello")
        assert info is not None
        assert info.full_name == "octocat/Hello"
        client.close()

    def test_pagination(self) -> None:
        """Multi-page response — pagination через Link header."""
        page = {"calls": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            page["calls"] += 1
            if page["calls"] == 1:
                return httpx.Response(
                    200,
                    headers={
                        "Link": '<https://api.github.com/user/repos?page=2>; rel="next"',
                    },
                    json=[
                        {"full_name": f"x/r{i}", "name": f"r{i}", "owner": {"login": "x"}}
                        for i in range(50)
                    ],
                )
            # Page 2 — no Link header → stop
            return httpx.Response(
                200,
                json=[
                    {"full_name": f"x/r{i+50}", "name": f"r{i+50}", "owner": {"login": "x"}}
                    for i in range(20)
                ],
            )

        client = _client_with_mock(handler)
        repos = client.list_user_repos(limit=200)
        assert len(repos) == 70  # 50 + 20
        assert page["calls"] == 2
        client.close()

    def test_pagination_respects_limit(self) -> None:
        """limit=10 cuts off after first 10."""
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"full_name": f"x/r{i}", "name": f"r{i}", "owner": {"login": "x"}}
                    for i in range(100)
                ],
            )

        client = _client_with_mock(handler)
        repos = client.list_user_repos(limit=10)
        assert len(repos) == 10
        client.close()


class TestAuthenticate:
    def test_authenticate_returns_creds(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"login": "octocat", "id": 1})

        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.fetch_current_user.return_value = {"login": "octocat", "id": 1}
            creds = authenticate("ghp_test_token")
            assert creds.token == "ghp_test_token"
            assert creds.login == "octocat"

    def test_authenticate_strips_whitespace(self) -> None:
        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.fetch_current_user.return_value = {"login": "user", "id": 1}
            creds = authenticate("  ghp_token  \n")
            assert creds.token == "ghp_token"
