"""Integration test: clone flow використовує saved credentials з store.

Перевіряє end-to-end: користувач saves token via `analyzer auth github`,
потім RepoSync.clone_or_update() автоматично використовує цей token.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.sync.clone import _build_url_for_clone, _resolve_token
from src.sync.git_providers import GitProvider, ParsedRepo


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    # Clear potentially-leaked env tokens
    for var in ("GITHUB_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import src.credentials.store as cs
    from src.config import get_settings
    get_settings.cache_clear()
    cs._default_store = None

    yield tmp_path

    get_settings.cache_clear()
    cs._default_store = None


class TestCredentialStoreUsage:
    def test_token_resolved_from_store(self, isolated_workspace) -> None:
        """Saved token resolves via _resolve_token()."""
        from src.config import get_settings
        from src.credentials import get_credential_store

        store = get_credential_store()
        store.save(
            provider="github",
            secret="ghp_stored_token",
            metadata={"login": "user"},
        )

        token = _resolve_token(GitProvider.GITHUB, get_settings())
        assert token == "ghp_stored_token"

    def test_env_var_fallback(self, isolated_workspace, monkeypatch) -> None:
        """Якщо store empty — env var як fallback."""
        from src.config import get_settings

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env_token")
        token = _resolve_token(GitProvider.GITHUB, get_settings())
        assert token == "ghp_env_token"

    def test_store_priority_over_env(self, isolated_workspace, monkeypatch) -> None:
        """Store credentials мають вищий priority за env."""
        from src.config import get_settings
        from src.credentials import get_credential_store

        store = get_credential_store()
        store.save(provider="github", secret="store_token", metadata={})
        monkeypatch.setenv("GITHUB_TOKEN", "env_token")

        token = _resolve_token(GitProvider.GITHUB, get_settings())
        assert token == "store_token"

    def test_no_token_returns_none(self, isolated_workspace) -> None:
        from src.config import get_settings
        token = _resolve_token(GitProvider.GITHUB, get_settings())
        assert token is None


class TestBuildUrlIntegration:
    def test_authenticated_url_with_stored_creds(self, isolated_workspace) -> None:
        """build_url_for_clone використовує stored token у URL."""
        from src.config import get_settings
        from src.credentials import get_credential_store

        store = get_credential_store()
        store.save(provider="github", secret="ghp_secret", metadata={})

        repo = ParsedRepo(
            provider=GitProvider.GITHUB, owner="foo", name="bar",
        )
        url, mode = _build_url_for_clone(repo, get_settings())
        # The mode is a log label, and it stopped being the generic
        # "stored_creds" when `describe_auth` started naming the auth SHAPE —
        # which is the thing an operator reading a failed clone needs.
        assert mode == "github:token-url"
        assert "ghp_secret" in url
        assert "github.com/foo/bar.git" in url

    def test_anonymous_when_no_creds(self, isolated_workspace) -> None:
        from src.config import get_settings

        repo = ParsedRepo(
            provider=GitProvider.GITHUB, owner="pallets", name="click",
        )
        url, mode = _build_url_for_clone(repo, get_settings())
        assert mode == "anonymous"
        assert "ghp_" not in url
        assert url == "https://github.com/pallets/click.git"

    def test_explicit_api_token_overrides_stored(self, isolated_workspace) -> None:
        """Explicit api_token у clone call > stored credentials."""
        from src.config import get_settings
        from src.credentials import get_credential_store

        store = get_credential_store()
        store.save(provider="github", secret="stored_token", metadata={})

        repo = ParsedRepo(
            provider=GitProvider.GITHUB, owner="foo", name="bar",
        )
        url, mode = _build_url_for_clone(
            repo, get_settings(), api_token="explicit_override",
        )
        assert mode == "token"
        assert "explicit_override" in url
        assert "stored_token" not in url
