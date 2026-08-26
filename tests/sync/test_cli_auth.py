"""CLI auth commands smoke tests.

Mock'аємо GitHub/GitLab API верifications щоб не вимагати network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from src.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    # Force-set master key для encrypted store (per-test isolation)
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())

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


# ─── github auth ────────────────────────────────────────────────────


class TestGitHubAuth:
    def test_save_credentials_after_verify(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        """Mock authenticate() success → saved + show у list."""
        from src.sync.github_api import GitHubCredentials

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(
                token="ghp_fake",
                login="octocat",
            )
            result = runner.invoke(app, [
                "auth", "github",
                "--token", "ghp_fake",
            ])

        assert result.exit_code == 0, result.output
        assert "octocat" in result.output
        assert "default" in result.output

    def test_save_with_custom_label(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        from src.sync.github_api import GitHubCredentials

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(token="t", login="u")
            result = runner.invoke(app, [
                "auth", "github",
                "--token", "t",
                "--label", "work",
            ])

        assert result.exit_code == 0
        assert "work" in result.output

    def test_invalid_token_exits_1(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        from src.sync.github_api import GitHubAuthError

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.side_effect = GitHubAuthError("Bad credentials")
            result = runner.invoke(app, [
                "auth", "github", "--token", "invalid",
            ])

        assert result.exit_code == 1
        assert "Authentication failed" in result.output


class TestGitLabAuth:
    def test_save_credentials_after_verify(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        from src.sync.gitlab_api import GitLabCredentials

        with patch("src.sync.gitlab_api.authenticate") as mock_auth:
            mock_auth.return_value = GitLabCredentials(
                token="glpat-fake",
                username="konstantin",
                user_id=42,
            )
            result = runner.invoke(app, [
                "auth", "gitlab",
                "--token", "glpat-fake",
            ])

        assert result.exit_code == 0
        assert "konstantin" in result.output


# ─── auth list ─────────────────────────────────────────────────────


class TestAuthList:
    def test_empty(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["auth", "list"])
        assert result.exit_code == 0
        assert "No credentials" in result.output

    def test_after_save(self, runner: CliRunner, isolated_workspace) -> None:
        from src.sync.github_api import GitHubCredentials

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(token="t", login="user1")
            runner.invoke(app, ["auth", "github", "--token", "t"])

        result = runner.invoke(app, ["auth", "list"])
        assert result.exit_code == 0
        assert "github" in result.output
        assert "user1" in result.output

    def test_multiple_accounts(self, runner: CliRunner, isolated_workspace) -> None:
        """github work + github personal — обидва відображаються."""
        from src.sync.github_api import GitHubCredentials

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(token="t1", login="user1")
            runner.invoke(app, ["auth", "github", "--token", "t1", "--label", "work"])
            mock_auth.return_value = GitHubCredentials(token="t2", login="user2")
            runner.invoke(app, [
                "auth", "github", "--token", "t2", "--label", "personal",
            ])

        result = runner.invoke(app, ["auth", "list"])
        assert "work" in result.output
        assert "personal" in result.output
        assert "user1" in result.output
        assert "user2" in result.output


class TestAuthDelete:
    def test_delete_existing(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        from src.sync.github_api import GitHubCredentials

        with patch("src.sync.github_api.authenticate") as mock_auth:
            mock_auth.return_value = GitHubCredentials(token="t", login="u")
            runner.invoke(app, ["auth", "github", "--token", "t"])

        result = runner.invoke(app, ["auth", "delete", "github", "--yes"])
        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_delete_nonexistent(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        result = runner.invoke(app, ["auth", "delete", "github", "--yes"])
        assert result.exit_code == 1
