"""Getting an MCP token without a shell on the server.

Until this existed the only way was `analyzer mcp issue-token`, which needs
MCP_JWT_SECRET and SSH. Connecting an editor to Celmis is something a developer
does on their own laptop, so "ask an administrator to SSH in" was not an
instruction — it was a description of a gap with a page around it.

The OAuth flow that .well-known/oauth-authorization-server advertises has not
shipped; that endpoint says so itself. This is the bridge until it does.

What the tests are actually about is the scope ceiling. An MCP client is
software on a laptop driven by a language model, and a token minted from a
browser click must not be able to write a review policy or register a
repository. The way to guarantee that is to never let the caller name the
scope — so most of this file is about the ways a caller might try.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ROUTER = (SRC / "api" / "routers" / "mcp_access.py").read_text(encoding="utf-8")


def test_the_token_is_read_only():
    from src.api.routers.mcp_access import _TOKEN_SCOPES

    assert _TOKEN_SCOPES, "a token with no scopes can do nothing at all"
    for scope in _TOKEN_SCOPES:
        assert scope.startswith("read:"), (
            f"{scope} is not read-only; a browser-issued token must not be "
            "able to change anything"
        )


def test_the_caller_cannot_name_the_scope():
    """The request model must have no scope field. A ceiling the caller can
    raise is not a ceiling."""
    from src.api.routers.mcp_access import McpTokenIn

    fields = set(McpTokenIn.model_fields)
    assert "scope" not in fields and "scopes" not in fields, (
        f"the request model exposes scopes: {fields}"
    )
    # And the issue call must pass the constant, not anything from the payload.
    assert "scopes=list(_TOKEN_SCOPES)" in ROUTER


def test_write_scopes_stay_with_the_cli():
    """The server supports write:reviews / write:policies / write:repos. None
    of them may be reachable from a browser click."""
    for scope in ("write:reviews", "write:policies", "write:repos"):
        assert scope not in ROUTER, f"{scope} is reachable from the browser"


def test_the_token_is_bound_to_the_caller_and_their_workspace():
    """Issued against the user's own identity, so what the MCP server answers
    is already filtered by what that user can see — and against the workspace
    they were looking at rather than whichever one would be the default."""
    assert "subject=user.id" in ROUTER
    assert '"workspace_id": workspace_id' in ROUTER
    assert "Depends(current_workspace_id)" in ROUTER
    assert "Depends(get_current_user)" in ROUTER


def test_a_missing_secret_says_what_to_set():
    """MCP_JWT_SECRET is the one prerequisite an operator has to configure.
    Naming it is the difference between a five-minute fix and a support
    thread — a 500 with a stack trace is neither."""
    assert "503" in ROUTER
    assert "MCP_JWT_SECRET" in ROUTER


def test_the_url_carries_the_trailing_slash():
    """Without it Starlette answers 307, and a redirected POST is not something
    the MCP streamable-HTTP client is guaranteed to follow. This is the single
    most common reason a correct-looking config does not connect."""
    assert '/mcp/' in ROUTER
    assert re.search(r'url=f"\{base\}/mcp/"', ROUTER), (
        "the advertised URL lost its trailing slash"
    )


def test_the_token_lives_long_enough_to_be_worth_pasting():
    """It goes into a config file that is edited rarely. An hour-long token
    would make the feature useless; an eternal one would make a leak
    permanent."""
    from src.api.routers.mcp_access import _TOKEN_TTL_SECONDS

    assert 7 * 24 * 3600 <= _TOKEN_TTL_SECONDS <= 90 * 24 * 3600


def test_the_route_is_registered():
    """A router nobody includes is a 404 with tests passing."""
    main = (SRC / "api" / "main.py").read_text(encoding="utf-8")
    assert "mcp_access" in main


def test_the_issued_token_actually_verifies(monkeypatch):
    """End to end through the real signer and the real verifier: a token this
    endpoint mints must be one the MCP server accepts, with the scopes it
    claims and no others."""
    import jwt as pyjwt

    secret = "test-secret-long-enough-for-hs256-aaaaaaaaaaaa"
    monkeypatch.setenv("MCP_JWT_SECRET", secret)
    from src.api.routers.mcp_access import _TOKEN_SCOPES, _TOKEN_TTL_SECONDS
    from src.mcp_server.auth import JwtConfig, issue_token

    token = issue_token(
        JwtConfig.from_env(), subject="user-42", scopes=list(_TOKEN_SCOPES),
        client_id="celmis-mcp-client", expires_in=_TOKEN_TTL_SECONDS,
        extra_claims={"workspace_id": "ws-9"},
    )
    claims = pyjwt.decode(token, secret, algorithms=["HS256"],
                          options={"verify_aud": False})
    assert claims["sub"] == "user-42"
    assert claims["workspace_id"] == "ws-9"
    granted = str(claims.get("scope", "")).split()
    assert set(granted) == set(_TOKEN_SCOPES)
    assert not any(s.startswith("write:") for s in granted)


# ─── the page ────────────────────────────────────────────────────────

WEB = ROOT / "web"
PAGE = (WEB / "app" / "(app)" / "settings" / "mcp" / "page.tsx").read_text(encoding="utf-8")


def test_the_page_gives_both_halves():
    """The config is the part somebody has to understand; the token is the part
    they have to copy. A page with one and not the other is a riddle."""
    assert "/api/mcp/token" in PAGE, "the page cannot issue a token"
    assert "mcpServers" in PAGE, "no config to paste"
    assert "claude mcp add" in PAGE, "no Claude Code path"


def test_the_page_says_how_to_tell_it_worked():
    """Every setup guide ends at "now it is configured" and leaves the reader
    with no way to know."""
    assert "mcp.verifyTitle" in PAGE
    assert "mcp.troubleTitle" in PAGE


def test_copying_reports_failure():
    """navigator.clipboard is unavailable over plain HTTP on some browsers, and
    this install runs on http://. A copy button that silently does nothing, on
    the one page whose whole job is "copy this", is worse than no button."""
    assert "mcp.copyFailed" in PAGE
    assert "catch" in PAGE


def test_the_tab_is_reachable():
    tabs = (WEB / "components" / "section-tabs.tsx").read_text(encoding="utf-8")
    assert '"/settings/mcp"' in tabs, "the page exists and nothing links to it"


@pytest.mark.parametrize("locale", sorted(
    p.stem for p in (WEB / "lib" / "i18n" / "messages").glob("*.json")))
def test_every_locale_has_the_page_strings(locale):
    import json

    messages = WEB / "lib" / "i18n" / "messages"
    en = json.loads((messages / "en.json").read_text(encoding="utf-8"))
    data = json.loads((messages / f"{locale}.json").read_text(encoding="utf-8"))
    keys = [k for k in en if k.startswith("mcp.")]
    assert keys, "the page has no strings"
    missing = [k for k in keys if k not in data]
    assert not missing, f"{locale} is missing {missing}"
