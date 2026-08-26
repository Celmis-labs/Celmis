"""Tests для MCP OAuth 2.1 / JWT Bearer auth (Stage 15.3)."""

from __future__ import annotations

import time

import jwt
import pytest

from src.mcp_server.auth import (
    DEFAULT_AUDIENCE,
    DEFAULT_ISSUER,
    JwtConfig,
    JwtConfigError,
    JwtTokenVerifier,
    issue_token,
)


@pytest.fixture
def config() -> JwtConfig:
    return JwtConfig(secret="test-secret-key-do-not-use-in-prod-32-chars-or-more")


# ─── JwtConfig ─────────────────────────────────────────────────────


class TestJwtConfig:
    def test_from_env_missing_secret_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("MCP_JWT_SECRET", raising=False)
        # CELMIS_JWT_SECRET is the documented fallback, so "no secret"
        # means neither is set. Without this the test passes alone and
        # fails in a full run, whenever another module has loaded .env.
        monkeypatch.delenv("CELMIS_JWT_SECRET", raising=False)
        with pytest.raises(JwtConfigError, match="MCP_JWT_SECRET"):
            JwtConfig.from_env()

    def test_from_env_with_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_JWT_SECRET", "my-secret-256bits-long-enough")
        config = JwtConfig.from_env()
        assert config.secret == "my-secret-256bits-long-enough"
        assert config.issuer == DEFAULT_ISSUER
        assert config.audience == DEFAULT_AUDIENCE
        assert config.algorithm == "HS256"

    def test_from_env_overrides(self, monkeypatch) -> None:
        # Not "secret": that is one of the documented placeholders, and the
        # config now refuses them. This test is about issuer/audience.
        monkeypatch.setenv("MCP_JWT_SECRET", "c4f2a70b9e5d18634ac7f0e2b9d51a86")
        monkeypatch.setenv("MCP_JWT_ISSUER", "my-app")
        monkeypatch.setenv("MCP_JWT_AUDIENCE", "my-mcp")
        monkeypatch.setenv("MCP_JWT_ALGORITHM", "HS512")
        config = JwtConfig.from_env()
        assert config.issuer == "my-app"
        assert config.audience == "my-mcp"
        assert config.algorithm == "HS512"

    def test_empty_secret_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_JWT_SECRET", "   ")  # whitespace only
        monkeypatch.delenv("CELMIS_JWT_SECRET", raising=False)
        with pytest.raises(JwtConfigError):
            JwtConfig.from_env()


# ─── issue_token ───────────────────────────────────────────────────


class TestIssueToken:
    def test_basic_token(self, config: JwtConfig) -> None:
        token = issue_token(config, subject="user-1", scopes=["read:graph"])
        # Decode and verify structure
        payload = jwt.decode(
            token, config.secret, algorithms=[config.algorithm],
            audience=config.audience, issuer=config.issuer,
        )
        assert payload["sub"] == "user-1"
        assert payload["aud"] == config.audience
        assert payload["iss"] == config.issuer
        assert payload["scope"] == "read:graph"
        assert "iat" in payload
        assert "exp" in payload

    def test_multiple_scopes(self, config: JwtConfig) -> None:
        token = issue_token(
            config, subject="u",
            scopes=["read:graph", "read:groups", "write:groups"],
        )
        payload = jwt.decode(
            token, config.secret, algorithms=[config.algorithm],
            audience=config.audience, issuer=config.issuer,
        )
        assert payload["scope"] == "read:graph read:groups write:groups"

    def test_expiration_set(self, config: JwtConfig) -> None:
        before = int(time.time())
        token = issue_token(config, subject="u", expires_in=120)
        after = int(time.time())
        payload = jwt.decode(
            token, config.secret, algorithms=[config.algorithm],
            audience=config.audience, issuer=config.issuer,
        )
        assert before + 120 <= payload["exp"] <= after + 121

    def test_extra_claims(self, config: JwtConfig) -> None:
        token = issue_token(
            config, subject="u",
            extra_claims={"tenant_id": "t-42", "custom": "value"},
        )
        payload = jwt.decode(
            token, config.secret, algorithms=[config.algorithm],
            audience=config.audience, issuer=config.issuer,
        )
        assert payload["tenant_id"] == "t-42"
        assert payload["custom"] == "value"

    def test_extra_claims_cant_override_standard(self, config: JwtConfig) -> None:
        """Cannot inject custom 'iss' or 'sub' through extra_claims."""
        token = issue_token(
            config, subject="legitimate-user",
            extra_claims={"sub": "MALICIOUS", "iss": "EVIL"},
        )
        payload = jwt.decode(
            token, config.secret, algorithms=[config.algorithm],
            audience=config.audience, issuer=config.issuer,
        )
        assert payload["sub"] == "legitimate-user"
        assert payload["iss"] == config.issuer


# ─── JwtTokenVerifier ─────────────────────────────────────────────


class TestJwtTokenVerifier:
    @pytest.fixture
    def verifier(self, config: JwtConfig) -> JwtTokenVerifier:
        return JwtTokenVerifier(config)

    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        token = issue_token(
            config, subject="user-42",
            scopes=["read:graph", "read:groups"],
            client_id="my-client",
        )

        result = await verifier.verify_token(token)
        assert result is not None
        assert result.token == token
        assert result.client_id == "my-client"
        assert result.scopes == ["read:graph", "read:groups"]
        assert result.resource == config.audience

    @pytest.mark.asyncio
    async def test_empty_token_returns_none(
        self, verifier: JwtTokenVerifier,
    ) -> None:
        assert await verifier.verify_token("") is None
        assert await verifier.verify_token("   ") is None

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_none(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        # Sign з different secret
        bad_config = JwtConfig(secret="different-secret-32-chars-minimum-len")
        token = issue_token(bad_config, subject="u")

        result = await verifier.verify_token(token)
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        # Issue token expired 10 секунд тому
        now = int(time.time())
        payload = {
            "iss": config.issuer, "aud": config.audience,
            "sub": "u", "iat": now - 100, "exp": now - 10,
            "scope": "", "client_id": "c",
        }
        expired = jwt.encode(payload, config.secret, algorithm=config.algorithm)

        result = await verifier.verify_token(expired)
        assert result is None

    @pytest.mark.asyncio
    async def test_wrong_audience_returns_none(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        now = int(time.time())
        payload = {
            "iss": config.issuer, "aud": "different-audience",
            "sub": "u", "iat": now, "exp": now + 100,
            "scope": "", "client_id": "c",
        }
        wrong_aud = jwt.encode(payload, config.secret, algorithm=config.algorithm)

        result = await verifier.verify_token(wrong_aud)
        assert result is None

    @pytest.mark.asyncio
    async def test_wrong_issuer_returns_none(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        now = int(time.time())
        payload = {
            "iss": "different-issuer", "aud": config.audience,
            "sub": "u", "iat": now, "exp": now + 100,
            "scope": "", "client_id": "c",
        }
        wrong_iss = jwt.encode(payload, config.secret, algorithm=config.algorithm)

        result = await verifier.verify_token(wrong_iss)
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_token_returns_none(
        self, verifier: JwtTokenVerifier,
    ) -> None:
        result = await verifier.verify_token("not.a.valid.jwt")
        assert result is None
        result = await verifier.verify_token("garbage")
        assert result is None

    @pytest.mark.asyncio
    async def test_scope_list_format(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        """Token з scope як list (replaces space-separated string)."""
        now = int(time.time())
        payload = {
            "iss": config.issuer, "aud": config.audience,
            "sub": "u", "iat": now, "exp": now + 100,
            "scope": ["read", "write"], "client_id": "c",
        }
        token = jwt.encode(payload, config.secret, algorithm=config.algorithm)

        result = await verifier.verify_token(token)
        assert result is not None
        assert result.scopes == ["read", "write"]

    @pytest.mark.asyncio
    async def test_no_scope_claim(
        self, config: JwtConfig, verifier: JwtTokenVerifier,
    ) -> None:
        """Token без scope claim — empty scopes list."""
        now = int(time.time())
        payload = {
            "iss": config.issuer, "aud": config.audience,
            "sub": "u", "iat": now, "exp": now + 100,
            "client_id": "c",
        }
        token = jwt.encode(payload, config.secret, algorithm=config.algorithm)

        result = await verifier.verify_token(token)
        assert result is not None
        assert result.scopes == []


# ─── Server integration ──────────────────────────────────────────


class TestServerIntegration:
    def test_build_server_no_auth(self) -> None:
        """build_server() без enable_auth — works без env."""
        from src.mcp_server.server import build_server
        mcp = build_server()
        assert mcp.name == "code-analyzer"

    def test_build_server_with_auth_requires_secret(self, monkeypatch) -> None:
        """enable_auth=True без MCP_JWT_SECRET — raises."""
        monkeypatch.delenv("MCP_JWT_SECRET", raising=False)
        # CELMIS_JWT_SECRET is the documented fallback, so "no secret"
        # means neither is set. Without this the test passes alone and
        # fails in a full run, whenever another module has loaded .env.
        monkeypatch.delenv("CELMIS_JWT_SECRET", raising=False)
        from src.mcp_server.server import build_server

        with pytest.raises(JwtConfigError):
            build_server(enable_auth=True)

    def test_build_server_with_auth_valid_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-32-chars-or-more-please")
        from src.mcp_server.server import build_server

        mcp = build_server(enable_auth=True)
        assert mcp.name == "code-analyzer"


# ─── CLI smoke test ──────────────────────────────────────────────


class TestCliIssueTokenCommand:
    def test_issue_token_no_secret_fails(self, monkeypatch) -> None:
        from typer.testing import CliRunner

        from src.cli import app

        monkeypatch.delenv("MCP_JWT_SECRET", raising=False)
        # CELMIS_JWT_SECRET is the documented fallback, so "no secret"
        # means neither is set. Without this the test passes alone and
        # fails in a full run, whenever another module has loaded .env.
        monkeypatch.delenv("CELMIS_JWT_SECRET", raising=False)
        runner = CliRunner()
        result = runner.invoke(app, ["mcp", "issue-token"])
        assert result.exit_code == 1
        assert "MCP_JWT_SECRET" in result.output

    def test_issue_token_with_secret_outputs_jwt(self, monkeypatch) -> None:
        from typer.testing import CliRunner

        from src.cli import app

        monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-32-chars-or-more-please")
        runner = CliRunner()
        result = runner.invoke(
            app, ["mcp", "issue-token", "--subject", "test-user"],
        )
        assert result.exit_code == 0
        # Output має бути JWT — 3 base64-encoded parts split by '.'
        token = result.output.strip()
        parts = token.split(".")
        assert len(parts) == 3
        # Verify it parses
        decoded = jwt.decode(
            token, "test-secret-32-chars-or-more-please",
            algorithms=["HS256"],
            audience=DEFAULT_AUDIENCE,
            issuer=DEFAULT_ISSUER,
        )
        assert decoded["sub"] == "test-user"
