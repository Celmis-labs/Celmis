"""Tests для bulk repo discovery commands.

Mock'аємо GitHubClient/GitLabClient щоб не запускати network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from src.cli import app
from src.sync.github_api import GitHubRepoInfo
from src.sync.gitlab_api import GitLabProjectInfo


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    for var in ("GITHUB_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import src.credentials.store as cs
    import src.groups.manager as gm
    from src.config import get_settings
    get_settings.cache_clear()
    cs._default_store = None
    gm._default_manager = None

    yield tmp_path

    get_settings.cache_clear()
    cs._default_store = None
    gm._default_manager = None


def _make_repo(
    full_name: str, archived: bool = False, fork: bool = False,
) -> GitHubRepoInfo:
    return GitHubRepoInfo(
        full_name=full_name,
        owner=full_name.split("/")[0],
        name=full_name.split("/")[1],
        archived=archived,
        fork=fork,
    )


def _make_project(
    path: str, archived: bool = False, fork: bool = False,
) -> GitLabProjectInfo:
    return GitLabProjectInfo(
        id=hash(path) % 1_000_000,
        full_path=path,
        name=path.split("/")[-1],
        archived=archived,
        fork=fork,
    )


# ─── add-from-github-org ─────────────────────────────────────────────


class TestAddFromGitHubOrg:
    def _setup_github_creds(self, runner: CliRunner) -> None:
        """Save GitHub creds для bulk command."""
        from src.sync.github_api import GitHubCredentials
        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(
                token="ghp_token", login="user",
            )
            runner.invoke(app, ["auth", "github", "--token", "ghp_token"])

    def test_bulk_add_filters_archived_and_forks(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        """Default skip_archived + skip_forks → 2 з 4 repos додано."""
        self._setup_github_creds(runner)
        runner.invoke(app, ["group", "create", "acme"])

        repos = [
            _make_repo("acme/api"),
            _make_repo("acme/old", archived=True),
            _make_repo("acme/forked", fork=True),
            _make_repo("acme/frontend"),
        ]
        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.list_org_repos.return_value = repos
            result = runner.invoke(app, [
                "group", "add-from-github-org", "acme", "acme",
            ])

        assert result.exit_code == 0
        assert "Added 2 repos" in result.output  # api + frontend

        # Verify через group show
        # Reset singleton to read fresh state
        import src.groups.manager as gm
        from src.groups import get_group_manager
        gm._default_manager = None
        g = get_group_manager().load("acme")
        assert "github:acme/api" in g.repos
        assert "github:acme/frontend" in g.repos
        assert not any("old" in r for r in g.repos)
        assert not any("forked" in r for r in g.repos)

    def test_include_archived_flag(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        self._setup_github_creds(runner)
        runner.invoke(app, ["group", "create", "g"])

        repos = [
            _make_repo("org/active"),
            _make_repo("org/old", archived=True),
        ]
        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.list_org_repos.return_value = repos
            result = runner.invoke(app, [
                "group", "add-from-github-org", "g", "org",
                "--include-archived",
            ])

        assert result.exit_code == 0
        assert "Added 2 repos" in result.output

    def test_no_credentials_fails_clearly(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, [
            "group", "add-from-github-org", "g", "org",
        ])
        assert result.exit_code == 1
        assert "No GitHub credentials" in result.output
        assert "analyzer auth github" in result.output

    def test_nonexistent_group_fails(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        self._setup_github_creds(runner)
        result = runner.invoke(app, [
            "group", "add-from-github-org", "ghost", "org",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_empty_org_no_failure(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        self._setup_github_creds(runner)
        runner.invoke(app, ["group", "create", "g"])

        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.list_org_repos.return_value = []
            result = runner.invoke(app, [
                "group", "add-from-github-org", "g", "empty-org",
            ])

        assert result.exit_code == 0
        assert "No repos found" in result.output

    def test_duplicate_skipped(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        """Re-running same command — no double-add."""
        self._setup_github_creds(runner)
        runner.invoke(app, ["group", "create", "g"])
        # Pre-add one repo
        runner.invoke(app, ["group", "add", "g", "github:org/api"])

        repos = [_make_repo("org/api"), _make_repo("org/web")]
        with patch("src.sync.github_api.GitHubClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.list_org_repos.return_value = repos
            result = runner.invoke(app, [
                "group", "add-from-github-org", "g", "org",
            ])

        assert result.exit_code == 0
        # 1 added (web), 1 skipped (api duplicate)
        assert "Added 1 repos" in result.output
        assert "skipped 1" in result.output


# ─── add-from-gitlab-group ──────────────────────────────────────────


class TestAddFromGitLabGroup:
    def _setup_gitlab_creds(self, runner: CliRunner) -> None:
        from src.sync.gitlab_api import GitLabCredentials
        with patch("src.sync.gitlab_api.authenticate") as mock_auth:
            mock_auth.return_value = GitLabCredentials(
                token="glpat-test", username="user", user_id=1,
            )
            runner.invoke(app, ["auth", "gitlab", "--token", "glpat-test"])

    def test_bulk_add_with_subgroups(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        self._setup_gitlab_creds(runner)
        runner.invoke(app, ["group", "create", "g"])

        projects = [
            _make_project("acme/api"),
            _make_project("acme/sub/auth"),
            _make_project("acme/sub/billing"),
        ]
        with patch("src.sync.gitlab_api.GitLabClient") as mock_client:
            instance = mock_client.return_value.__enter__.return_value
            instance.list_group_projects.return_value = projects
            result = runner.invoke(app, [
                "group", "add-from-gitlab-group", "g", "acme",
            ])

        assert result.exit_code == 0
        assert "Added 3" in result.output

        import src.groups.manager as gm
        gm._default_manager = None
        from src.groups import get_group_manager
        g = get_group_manager().load("g")
        assert "gitlab:acme/api" in g.repos
        assert "gitlab:acme/sub/auth" in g.repos
        assert "gitlab:acme/sub/billing" in g.repos

    def test_no_credentials_fails(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, [
            "group", "add-from-gitlab-group", "g", "acme",
        ])
        assert result.exit_code == 1
        assert "No GitLab credentials" in result.output
