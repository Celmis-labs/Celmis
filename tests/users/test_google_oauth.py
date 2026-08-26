"""Tests для Google OAuth flow — PKCE generation + helpers + CLI smoke."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import patch

import pytest

from src.users.google_oauth import (
    GoogleOAuthError,
    GoogleUserInfo,
    _generate_pkce_pair,
    run_google_oauth_flow,
)

# ─── PKCE pair generation ─────────────────────────────────────────


class TestPkcePair:
    def test_pair_format(self) -> None:
        verifier, challenge = _generate_pkce_pair()
        # Per RFC 7636: 43-128 chars, A-Z/a-z/0-9/-/./_/~
        assert 43 <= len(verifier) <= 128
        # Base64url-decoded length matches SHA-256 = 32 bytes
        # Base64 без padding: ceil(32/3) * 4 - 2 = 43 chars
        assert len(challenge) == 43

    def test_challenge_is_sha256_of_verifier(self) -> None:
        verifier, challenge = _generate_pkce_pair()
        expected = hashlib.sha256(verifier.encode("ascii")).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
        assert challenge == expected_b64

    def test_pairs_are_unique(self) -> None:
        """Each call → different verifier (random)."""
        v1, _ = _generate_pkce_pair()
        v2, _ = _generate_pkce_pair()
        assert v1 != v2


# ─── Flow error handling ────────────────────────────────────────


class TestFlowErrors:
    def test_no_client_id_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        with pytest.raises(GoogleOAuthError, match="GOOGLE_OAUTH_CLIENT_ID"):
            run_google_oauth_flow()

    def test_explicit_client_id_overrides_env(self, monkeypatch) -> None:
        """Якщо client_id passed explicitly — env var ignored."""
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        # Will fail later due to network/timeout, but client_id check passes
        with pytest.raises(GoogleOAuthError) as exc_info:
            # 1-second timeout щоб не wait forever
            run_google_oauth_flow(
                client_id="explicit-client-id",
                open_browser=False,
                timeout_seconds=1,
            )
        # Не "GOOGLE_OAUTH_CLIENT_ID not set"
        assert "GOOGLE_OAUTH_CLIENT_ID" not in str(exc_info.value)


# ─── GoogleUserInfo dataclass ───────────────────────────────────


class TestGoogleUserInfo:
    def test_dataclass_fields(self) -> None:
        info = GoogleUserInfo(
            sub="g-12345",
            email="alice@example.com",
            name="Alice",
            email_verified=True,
        )
        assert info.sub == "g-12345"
        assert info.email == "alice@example.com"
        assert info.name == "Alice"
        assert info.email_verified is True
        assert info.picture_url == ""


# ─── CLI smoke ──────────────────────────────────────────────────


class TestCliCommand:
    @pytest.fixture
    def isolated_workspace(self, tmp_path, monkeypatch):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
        import src.users.store as us
        from src.config import get_settings
        get_settings.cache_clear()
        us._default_store = None
        yield tmp_path
        get_settings.cache_clear()
        us._default_store = None

    def test_oauth_creates_new_user(self, isolated_workspace) -> None:
        """Successful OAuth → new user у store з google_sub."""
        from typer.testing import CliRunner

        from src.cli import app

        mock_info = GoogleUserInfo(
            sub="g-new-user",
            email="newbie@example.com",
            name="Newbie",
            email_verified=True,
        )

        runner = CliRunner()
        with patch(
            "src.users.google_oauth.run_google_oauth_flow",
            return_value=mock_info,
        ):
            result = runner.invoke(app, ["auth", "google", "--no-browser"])

        assert result.exit_code == 0
        assert "Created new user" in result.output
        assert "newbie@example.com" in result.output

        # Verify saved
        from src.users import get_user_store
        store = get_user_store()
        u = store.get_by_google_sub("g-new-user")
        assert u is not None
        assert u.email == "newbie@example.com"
        assert u.has_google is True

    def test_oauth_returns_existing_user(self, isolated_workspace) -> None:
        """User з тим же google_sub returned without duplicate creation."""
        from typer.testing import CliRunner

        from src.cli import app
        from src.users import User, UserAuthMethod, get_user_store

        # Pre-create existing user
        store = get_user_store()
        existing = User(
            id="u-1",
            email="returning@example.com",
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="g-returning",
            name="Returning",
        )
        store.create(existing)
        initial_count = store.count()

        # Mock OAuth повертає same google_sub
        mock_info = GoogleUserInfo(
            sub="g-returning", email="returning@example.com", name="Returning",
        )

        runner = CliRunner()
        with patch(
            "src.users.google_oauth.run_google_oauth_flow",
            return_value=mock_info,
        ):
            result = runner.invoke(app, ["auth", "google", "--no-browser"])

        assert result.exit_code == 0
        assert "Welcome back" in result.output
        assert store.count() == initial_count  # no new user created

    def test_oauth_links_to_existing_email(self, isolated_workspace) -> None:
        """Email matches existing password user → link Google."""
        from typer.testing import CliRunner

        from src.cli import app
        from src.users import User, UserAuthMethod, get_user_store
        from src.users.password import hash_password

        store = get_user_store()
        existing = User(
            id="u-1",
            email="alice@example.com",
            auth_method=UserAuthMethod.PASSWORD,
            password_hash=hash_password("pwd"),
            name="Alice",
        )
        store.create(existing)

        mock_info = GoogleUserInfo(
            sub="g-alice", email="alice@example.com", name="Alice Renamed",
        )

        runner = CliRunner()
        with patch(
            "src.users.google_oauth.run_google_oauth_flow",
            return_value=mock_info,
        ):
            result = runner.invoke(app, ["auth", "google", "--no-browser"])

        assert result.exit_code == 0
        assert "Linked Google account" in result.output

        u = store.get_by_email("alice@example.com")
        assert u.google_sub == "g-alice"
        assert u.auth_method == UserAuthMethod.BOTH
        assert u.has_password is True
        assert u.has_google is True

    def test_oauth_failure_propagated(self, isolated_workspace) -> None:
        """OAuth error → exit 1 + clear message."""
        from typer.testing import CliRunner

        from src.cli import app

        runner = CliRunner()
        with patch(
            "src.users.google_oauth.run_google_oauth_flow",
            side_effect=GoogleOAuthError("OAuth flow failed: client denied"),
        ):
            result = runner.invoke(app, ["auth", "google", "--no-browser"])

        assert result.exit_code == 1
        assert "client denied" in result.output
