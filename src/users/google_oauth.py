"""Google OAuth 2.0 + OIDC flow for CLI app login.

Stage 16.3 (May 2026, OAuth 2.1 + PKCE).

Flow type: **Authorization Code with PKCE** (recommended for public clients
such as CLI/desktop apps since 2024). Does not require a client secret.

Workflow:
    1. App generates code_verifier + code_challenge (PKCE pair)
    2. Opens browser to the Google auth URL with the challenge
    3. User authorizes in the browser → redirect to loopback
       (http://127.0.0.1:PORT)
    4. App's local server catches authorization code
    5. App exchanges code + verifier → ID token + access token
    6. Validates ID token signature + claims via the google-auth library
    7. Extracts 'sub' (stable user ID) + email + name
    8. Creates/updates User in the local UserStore (auth_method=GOOGLE_OAUTH)

Required env vars:
    GOOGLE_OAUTH_CLIENT_ID — public client ID (NOT a secret for PKCE flow)

Optional:
    GOOGLE_OAUTH_REDIRECT_PORT — loopback port (default 8765)

Setup steps for users:
    1. Create an OAuth client in the Google Cloud Console:
       https://console.cloud.google.com/apis/credentials
    2. Type: 'Desktop app' (PKCE-friendly)
    3. Copy Client ID → export GOOGLE_OAUTH_CLIENT_ID=<...>
    4. Run: analyzer auth google
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import logging
import os
import secrets
import socketserver
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import httpx

from src.http import build_client

logger = logging.getLogger(__name__)


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DEFAULT_LOOPBACK_PORT = 8765
DEFAULT_SCOPES = ["openid", "email", "profile"]


class GoogleOAuthError(Exception):
    """OAuth flow failed."""


@dataclass
class GoogleUserInfo:
    """Verified user info after a successful OAuth flow."""

    sub: str  # Stable Google user ID (use as User.google_sub)
    email: str
    name: str = ""
    email_verified: bool = False
    picture_url: str = ""


# ─── PKCE pair ───────────────────────────────────────────────────


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) per RFC 7636.

    code_verifier: 43-128 chars, A-Z/a-z/0-9/-/./_/~ (we use 64 random bytes
    base64-urlsafe-encoded → ~86 chars).

    code_challenge: SHA-256(verifier) base64-urlsafe (no padding).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ─── Loopback callback server ────────────────────────────────────


class _CallbackResult:
    """Shared state between HTTP handler thread and main thread."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_handler(expected_state: str, result: _CallbackResult):
    """Build BaseHTTPRequestHandler subclass capturing OAuth callback."""

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            received_state = (params.get("state") or [""])[0]
            if received_state != expected_state:
                result.error = "state_mismatch"
                self._respond_error("Invalid state parameter (CSRF protection failed).")
                result.event.set()
                return

            if "error" in params:
                result.error = (params.get("error") or ["unknown"])[0]
                self._respond_error(f"OAuth error: {result.error}")
                result.event.set()
                return

            code = (params.get("code") or [""])[0]
            if not code:
                result.error = "no_code"
                self._respond_error("Missing authorization code.")
                result.event.set()
                return

            result.code = code
            self._respond_success()
            result.event.set()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # Silent logging — so we don't spam stdout
            return

        def _respond_success(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;"
                "padding:60px'>"
                "<h2>Authentication successful</h2>"
                "<p>You can close this tab and return to the CLI.</p>"
                "</body></html>"
            )
            self.wfile.write(html.encode("utf-8"))

        def _respond_error(self, msg: str) -> None:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h2>OAuth Error</h2><p>{msg}</p></body></html>"
                .encode()
            )

    return CallbackHandler


def _start_callback_server(
    port: int, expected_state: str, result: _CallbackResult,
) -> socketserver.TCPServer:
    handler = _make_handler(expected_state, result)
    server = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ─── OAuth flow ──────────────────────────────────────────────────


def run_google_oauth_flow(
    *,
    client_id: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
    timeout_seconds: int = 180,
) -> GoogleUserInfo:
    """Run the full Authorization Code + PKCE flow with Google.

    Args:
        client_id: OAuth public client ID (Google Console → Credentials).
                   Default: GOOGLE_OAUTH_CLIENT_ID env var.
        port: loopback port for the redirect URI. Default 8765.
        open_browser: whether to auto-open the browser. Default True.
        timeout_seconds: max wait for user authorization.

    Returns: GoogleUserInfo with verified sub + email.

    Raises: GoogleOAuthError if the flow fails (timeout, denied, network).
    """
    cid = client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    if not cid:
        raise GoogleOAuthError(
            "GOOGLE_OAUTH_CLIENT_ID env var not set. "
            "Setup OAuth client at https://console.cloud.google.com/apis/credentials "
            "(type: 'Desktop app'), then export GOOGLE_OAUTH_CLIENT_ID=<your_id>"
        )

    cb_port = port or int(os.environ.get("GOOGLE_OAUTH_REDIRECT_PORT", DEFAULT_LOOPBACK_PORT))
    redirect_uri = f"http://127.0.0.1:{cb_port}/callback"

    # PKCE pair + state
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    auth_params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(DEFAULT_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",  # request refresh token
        "prompt": "consent",        # force consent screen
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    # Start callback server
    result = _CallbackResult()
    try:
        server = _start_callback_server(cb_port, state, result)
    except OSError as exc:
        raise GoogleOAuthError(
            f"Cannot bind loopback port {cb_port}: {exc}. "
            f"Set GOOGLE_OAUTH_REDIRECT_PORT to a free port."
        ) from exc

    try:
        if open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(auth_url)

        logger.info("oauth_browser_opened url=%s", auth_url[:100])

        # Wait for callback
        if not result.event.wait(timeout=timeout_seconds):
            raise GoogleOAuthError(
                f"OAuth timeout after {timeout_seconds}s — no callback received. "
                "If the browser did not open, copy the URL manually from the logs."
            )

        if result.error:
            raise GoogleOAuthError(f"OAuth callback failed: {result.error}")

        if not result.code:
            raise GoogleOAuthError("OAuth callback missing 'code' parameter")
    finally:
        server.shutdown()
        server.server_close()

    # Exchange code → tokens
    token_data = _exchange_code(
        code=result.code,
        verifier=verifier,
        client_id=cid,
        redirect_uri=redirect_uri,
    )

    # Validate ID token + extract user info
    return _validate_and_extract(token_data, expected_audience=cid)


def _exchange_code(
    *, code: str, verifier: str, client_id: str, redirect_uri: str,
) -> dict[str, Any]:
    """POST /token endpoint exchange."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    try:
        with build_client(timeout=30.0) as client:
            response = client.post(
                GOOGLE_TOKEN_URL,
                data=payload,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"Token exchange network error: {exc}") from exc

    if response.status_code != 200:
        raise GoogleOAuthError(
            f"Token exchange failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    data = response.json()
    if "error" in data:
        raise GoogleOAuthError(
            f"Token exchange error: {data.get('error')}: "
            f"{data.get('error_description', '')}"
        )
    return data


def _validate_and_extract(
    token_data: dict[str, Any], *, expected_audience: str,
) -> GoogleUserInfo:
    """Validate ID token signature + extract sub/email/name.

    Uses the google-auth library (already in dependencies via google-genai).
    """
    id_token = token_data.get("id_token")
    if not id_token:
        raise GoogleOAuthError("Token response missing id_token")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as id_token_lib
    except ImportError as exc:
        raise GoogleOAuthError(
            "google-auth library required for ID token verification"
        ) from exc

    try:
        claims = id_token_lib.verify_oauth2_token(
            id_token,
            google_requests.Request(),
            expected_audience,
        )
    except ValueError as exc:
        raise GoogleOAuthError(f"ID token validation failed: {exc}") from exc

    # claims includes: iss, aud, sub, email, name, picture, email_verified, ...
    sub = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").lower()
    if not sub or not email:
        raise GoogleOAuthError("ID token missing sub or email claim")

    return GoogleUserInfo(
        sub=sub,
        email=email,
        name=str(claims.get("name") or ""),
        email_verified=bool(claims.get("email_verified")),
        picture_url=str(claims.get("picture") or ""),
    )
