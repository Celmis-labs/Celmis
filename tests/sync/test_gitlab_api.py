"""Tests для GitLabClient (unit + mock)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.sync.gitlab_api import (
    GitLabAuthError,
    GitLabClient,
    GitLabScopeError,
    _parse_project,
    _replace_page_param,
    authenticate,
)

# ─── _replace_page_param ────────────────────────────────────────────


class TestPageParam:
    def test_replace_existing(self) -> None:
        url = "https://gitlab.com/api/v4/projects?page=1&per_page=100"
        assert _replace_page_param(url, "2") == (
            "https://gitlab.com/api/v4/projects?page=2&per_page=100"
        )

    def test_add_to_query(self) -> None:
        url = "https://gitlab.com/api/v4/projects?per_page=100"
        assert "page=3" in _replace_page_param(url, "3")

    def test_add_to_url_without_query(self) -> None:
        url = "https://gitlab.com/api/v4/projects"
        result = _replace_page_param(url, "5")
        assert result.endswith("?page=5")


# ─── _parse_project ─────────────────────────────────────────────────


class TestProjectParsing:
    def test_full_data(self) -> None:
        data = {
            "id": 12345,
            "path_with_namespace": "group/sub/project",
            "name": "project",
            "description": "Test project",
            "default_branch": "main",
            "visibility": "public",
            "archived": False,
            "last_activity_at": "2026-04-15T10:00:00Z",
            "http_url_to_repo": "https://gitlab.com/group/sub/project.git",
            "ssh_url_to_repo": "git@gitlab.com:group/sub/project.git",
            "topics": ["python", "api"],
        }
        info = _parse_project(data)
        assert info.id == 12345
        assert info.full_path == "group/sub/project"
        assert info.name == "project"
        assert info.visibility == "public"
        assert info.topics == ["python", "api"]

    def test_fork_detection(self) -> None:
        forked = _parse_project({
            "id": 1, "path_with_namespace": "x/y", "name": "y",
            "forked_from_project": {"id": 99},
        })
        not_forked = _parse_project({
            "id": 1, "path_with_namespace": "x/y", "name": "y",
        })
        assert forked.fork is True
        assert not_forked.fork is False

    def test_visibility_default(self) -> None:
        info = _parse_project({
            "id": 1, "path_with_namespace": "x/y", "name": "y",
        })
        assert info.visibility == "private"


# ─── Error handling ─────────────────────────────────────────────────


def _client_with_mock(handler):
    transport = httpx.MockTransport(handler)
    client = GitLabClient(token="glpat-fake")
    client._http.close()
    client._http = httpx.Client(
        transport=transport,
        timeout=10.0,
        headers={
            "PRIVATE-TOKEN": "glpat-fake",
            "Accept": "application/json",
            "User-Agent": "code-analyzer/0.1",
        },
    )
    return client


class TestErrorHandling:
    def test_401_raises(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Unauthorized"})

        client = _client_with_mock(handler)
        with pytest.raises(GitLabAuthError, match="invalid"):
            client.fetch_current_user()
        client.close()

    def test_403_raises(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        client = _client_with_mock(handler)
        with pytest.raises(GitLabScopeError):
            client.fetch_current_user()
        client.close()

    def test_get_project_404_returns_none(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Project Not Found"})

        client = _client_with_mock(handler)
        assert client.get_project("ghost/repo") is None
        client.close()


class TestSuccessPath:
    def test_fetch_user(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert "user" in str(req.url)
            return httpx.Response(200, json={
                "id": 1, "username": "konstantin", "name": "Konstantin",
            })

        client = _client_with_mock(handler)
        user = client.fetch_current_user()
        assert user["username"] == "konstantin"
        client.close()

    def test_get_project_with_full_path(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # Url-encoded slash check
            assert "group%2Fsubgroup%2Fproject" in str(req.url)
            return httpx.Response(200, json={
                "id": 100,
                "path_with_namespace": "group/subgroup/project",
                "name": "project",
            })

        client = _client_with_mock(handler)
        info = client.get_project("group/subgroup/project")
        assert info is not None
        assert info.full_path == "group/subgroup/project"
        client.close()

    def test_get_project_by_id(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert "/projects/12345" in str(req.url)
            return httpx.Response(200, json={
                "id": 12345,
                "path_with_namespace": "g/p",
                "name": "p",
            })

        client = _client_with_mock(handler)
        info = client.get_project(12345)
        assert info is not None
        assert info.id == 12345
        client.close()

    def test_pagination_via_x_next_page(self) -> None:
        page_count = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            page_count["n"] += 1
            if page_count["n"] == 1:
                return httpx.Response(
                    200,
                    headers={"X-Next-Page": "2"},
                    json=[
                        {
                            "id": i, "path_with_namespace": f"g/p{i}",
                            "name": f"p{i}",
                        }
                        for i in range(50)
                    ],
                )
            # Page 2 — no X-Next-Page → stop
            return httpx.Response(
                200,
                headers={"X-Next-Page": ""},
                json=[
                    {
                        "id": i + 50, "path_with_namespace": f"g/p{i+50}",
                        "name": f"p{i+50}",
                    }
                    for i in range(20)
                ],
            )

        client = _client_with_mock(handler)
        projects = client.list_user_projects(limit=200)
        assert len(projects) == 70  # 50 + 20
        assert page_count["n"] == 2
        client.close()


class TestAuthenticate:
    def test_authenticate(self) -> None:
        with patch("src.sync.gitlab_api.GitLabClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.fetch_current_user.return_value = {
                "username": "konstantin", "id": 42,
            }
            creds = authenticate("glpat-test-token")
            assert creds.token == "glpat-test-token"
            assert creds.username == "konstantin"
            assert creds.user_id == 42
