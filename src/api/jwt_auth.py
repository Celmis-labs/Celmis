"""JWT issuance + validation for Celmis session tokens.

Tokens are short-lived (30 min) bearer JWTs signed HS256 with a server-side
secret. Contains user id + email + scopes claims.

The secret is the whole of the authentication system: anyone who knows it can
mint a token for any user, admin included. ``.env.example`` ships
``CELMIS_JWT_SECRET=replace-with-openssl-rand-hex-32`` and docker-compose
defaults the variable to ``change-me-in-production``, so a stack brought up
from the shipped files runs on a value that is public knowledge — and does so
silently, because any non-empty string used to be accepted.

Two gates, deliberately at different strengths:

  * :func:`_get_secret` refuses outright on a value that is *published* — the
    ``.env.example`` placeholders (via ``src.llm.keys._is_placeholder``, the
    same table the LLM keys use) and obvious markers like ``change-me``. No
    real deployment can hold one of those, so this cannot take a running site
    down; it can only stop a site that was never protected.

  * :func:`assert_secret_usable` adds the judgement call — "shorter than
    :data:`JWT_SECRET_MIN_LENGTH` is not a secret" — and belongs at STARTUP,
    where the operator reads the message, rather than on some request months
    later. Called from :func:`src.deployment.run_startup_checks`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt

from src.config import get_settings

logger = logging.getLogger(__name__)


JWT_ALGORITHM = "HS256"
#: Shorter than this is not a signing secret. 16 chars matches the floor the
#: ops token already enforces (src/api/deps.py); `openssl rand -hex 32`, which
#: every doc in this repo tells the operator to run, gives 64.
JWT_SECRET_MIN_LENGTH = 16
JWT_EXPIRE_MINUTES = 30 * 24 * 60  # 30 days — frontend session lifetime
JWT_ISSUER = "celmis"
JWT_AUDIENCE = "celmis-web"


_secret_cache: bytes | None = None

#: Substrings that only ever appear in a value somebody was told to replace.
#: Matched case-insensitively anywhere in the value, so
#: "change-me-in-production" (docker-compose's default) is caught by
#: "change-me". Kept narrow on purpose: a marker that could plausibly occur
#: inside a random secret would refuse to start a healthy install.
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "replace-me", "replace-with", "change-me", "changeme",
    "your-secret", "your-key-here", "placeholder", "insecure",
)


class WeakJwtSecretError(RuntimeError):
    """The configured JWT signing secret is a published placeholder."""


def secret_problem(value: str, *, check_length: bool = True) -> str | None:
    """Why ``value`` cannot sign session tokens, or ``None`` if it can.

    Pure and side-effect free so it can be unit-tested against every value
    this repo ships. ``check_length=False`` drops the "too short" judgement
    and leaves only the published-placeholder half — see the module docstring
    for why the two gates differ.
    """
    from src.llm.keys import _is_placeholder

    text = (value or "").strip()
    if not text:
        return "is empty"
    # A secret has no spaces. An INSTRUCTION does — and an instruction is
    # exactly what a .env ends up holding, because `KEY=   # openssl rand -hex 24`
    # is not a comment to a dotenv parser: everything after `=` is the value,
    # so the operator who copied .env.example verbatim is running with the
    # literal string "# openssl rand -hex 24" as their signing secret. It is
    # 22 characters, so the length floor passes it; it matches no placeholder
    # marker, so that gate passes it; and docker-compose's `${VAR:?}` only
    # fires on EMPTY, so that passes it too. Three guards, all defeated by a
    # value that is publicly readable in this repository.
    if any(ch.isspace() for ch in text):
        return (
            "contains whitespace — this is an instruction, not a secret. A "
            "dotenv file has no inline comments: everything after `=` is the "
            "value. Put the note on its own line above the variable"
        )
    if text.startswith("#"):
        return "starts with '#' — that is a comment being used as a value"
    low = text.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return (
                f"contains {marker!r} — that is the placeholder shipped in "
                f".env.example / docker-compose.yml, and it is public"
            )
    # Same table (and same <8-char floor) that blacklists a copy-pasted
    # provider key in src/llm/keys.py.
    if _is_placeholder(text):
        return "is one of the documented placeholder values"
    if check_length and len(text) < JWT_SECRET_MIN_LENGTH:
        return (
            f"is {len(text)} characters; a signing secret needs at least "
            f"{JWT_SECRET_MIN_LENGTH}"
        )
    return None


def _refuse(problem: str) -> WeakJwtSecretError:
    return WeakJwtSecretError(
        f"CELMIS_JWT_SECRET {problem}. Anyone holding this value can mint a "
        f"session token for any user, including an admin. Generate one with "
        f"`openssl rand -hex 32` and set CELMIS_JWT_SECRET, or unset it "
        f"entirely to have Celmis generate and store its own."
    )


def assert_secret_usable() -> None:
    """Raise :class:`WeakJwtSecretError` unless the configured secret is real.

    Startup gate — the stricter of the two, length included. A secret Celmis
    generated for itself (the ``jwt.key`` file) always passes, so an install
    that never set the variable is never blocked by this.
    """
    env_secret = os.environ.get("CELMIS_JWT_SECRET", "").strip()
    if not env_secret:
        return  # no env secret → the generated key file is used; that is fine
    problem = secret_problem(env_secret)
    if problem:
        raise _refuse(problem)


def _get_secret() -> bytes:
    """Resolve JWT signing secret.

    Priority:
        1. CELMIS_JWT_SECRET env var
        2. ~/code-analysis/secrets/jwt.key — auto-generated 32 bytes

    Raises :class:`WeakJwtSecretError` when the env var holds a value that is
    published in this repository — no token may be signed or verified with a
    secret the whole internet has.
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    env_secret = os.environ.get("CELMIS_JWT_SECRET", "").strip()
    if env_secret:
        # Published placeholders only — the length judgement is made once, at
        # startup, by `assert_secret_usable`.
        problem = secret_problem(env_secret, check_length=False)
        if problem:
            raise _refuse(problem)
        _secret_cache = env_secret.encode("utf-8")
        return _secret_cache

    settings = get_settings()
    key_file: Path = settings.workspace_dir / "secrets" / "jwt.key"
    if key_file.exists():
        _secret_cache = key_file.read_bytes().strip()
        return _secret_cache

    key_file.parent.mkdir(parents=True, exist_ok=True)
    new_secret = secrets.token_urlsafe(32).encode("utf-8")
    key_file.write_bytes(new_secret)
    # Best effort: a filesystem that refuses chmod (Windows share, some
    # container volumes) still gets the key written.
    with contextlib.suppress(OSError):
        key_file.chmod(0o600)
    logger.info("jwt_secret_generated path=%s", key_file)
    _secret_cache = new_secret
    return _secret_cache


def issue_token(
    *, user_id: str, email: str, scopes: list[str] | None = None,
) -> tuple[str, datetime]:
    """Issue a fresh JWT for the given user.

    Returns (token, expires_at).
    """
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "sub": user_id,
        "email": email,
        "scopes": scopes or [],
    }
    token = jwt.encode(payload, _get_secret(), algorithm=JWT_ALGORITHM)
    return token, exp


def _get_previous_secret() -> bytes | None:
    """Stage 21 — key rotation. During a rotation window the OLD secret
    lives in CELMIS_JWT_SECRET_PREVIOUS; tokens signed with it still
    verify, while all NEW tokens are signed with the current secret.

    Rotation procedure (documented in docs/KEY_ROTATION.md):
        1. Set CELMIS_JWT_SECRET_PREVIOUS=<current secret>
        2. Set CELMIS_JWT_SECRET=<new secret>
        3. Restart. Existing sessions stay valid until natural expiry.
        4. After the longest token TTL passes (30 days), unset _PREVIOUS.
    """
    prev = os.environ.get("CELMIS_JWT_SECRET_PREVIOUS", "").strip()
    return prev.encode("utf-8") if prev else None


def decode_token(token: str) -> dict:
    """Verify + decode JWT. Tries current secret first, then the
    rotation-window previous secret. Raises jwt.InvalidTokenError when
    neither verifies."""
    kwargs = dict(
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
    )
    try:
        return jwt.decode(token, _get_secret(), **kwargs)
    except jwt.InvalidSignatureError:
        prev = _get_previous_secret()
        if prev is None:
            raise
        payload = jwt.decode(token, prev, **kwargs)
        logger.info("jwt_verified_with_previous_secret sub=%s", payload.get("sub"))
        return payload
