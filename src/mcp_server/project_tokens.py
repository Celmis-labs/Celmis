"""Project-scoped MCP tokens: one project, search only, expiring.

A superadmin hands an outside tool (a coding agent, a script) a token that can
search ONE project and nothing else. The token is opaque — ``cmcp_`` plus 256
random bits — and shown once; only its SHA-256 is stored (``mcp_project_tokens``).

The verifier looks the hash up on EVERY call, without a cache: revoking the row
or letting it expire ends the token on the next request. The project and
workspace come from the row, never from a tool argument.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

PREFIX = "cmcp_"
#: The one scope such a token carries.
SCOPE = "read:project_search"
#: The only tools a project token may see or call.
TOOLS = frozenset({"search_project", "ask_project"})
#: ``AccessToken.client_id`` of a verified project token: ``mcp-project:<row id>``.
CLIENT_PREFIX = "mcp-project:"

MIN_TTL_SECONDS = 60 * 60
MAX_TTL_SECONDS = 90 * 24 * 60 * 60
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_LABEL = 120

EXPIRED = "This project token has expired. Ask the administrator for a new one."
REVOKED = "This project token was revoked. Ask the administrator for a new one."
UNKNOWN = "This project token is not recognised."


class ProjectTokenError(ValueError):
    """A request to issue a token that cannot be honoured."""


@dataclass(frozen=True)
class ProjectTokenView:
    id: str
    workspace_id: str
    project_id: str
    label: str
    scopes: tuple[str, ...]
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    def problem(self, now: datetime | None = None) -> str:
        """Why the token may not be used now, or "" when it may."""
        now = now or datetime.now(UTC)
        if self.revoked_at is not None:
            return REVOKED
        if self.expires_at <= now:
            return EXPIRED
        return ""


def is_project_token(raw: str | None) -> bool:
    return bool(raw) and raw.startswith(PREFIX)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _view(row) -> ProjectTokenView:  # noqa: ANN001
    return ProjectTokenView(
        id=row.id, workspace_id=row.workspace_id, project_id=str(row.project_id),
        label=row.label or "", scopes=tuple(row.scopes or ()),
        created_by=row.created_by or "", created_at=_aware(row.created_at),
        expires_at=_aware(row.expires_at), revoked_at=_aware(row.revoked_at),
        last_used_at=_aware(row.last_used_at),
    )


def clamp_ttl(ttl_seconds: int | None) -> int:
    """The requested lifetime, or the default; ProjectTokenError outside 1 h … 90 d."""
    if ttl_seconds is None:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ProjectTokenError("ttl_seconds must be a whole number") from None
    if value < MIN_TTL_SECONDS or value > MAX_TTL_SECONDS:
        raise ProjectTokenError(
            "The token lifetime must be between 1 hour and 90 days "
            f"({MIN_TTL_SECONDS}..{MAX_TTL_SECONDS} seconds).")
    return value


def mint(session, *, workspace_id: str, project_id: str, label: str,  # noqa: ANN001
         ttl_seconds: int | None, created_by: str) -> tuple[str, ProjectTokenView]:
    """Create the row and return ``(token, view)``. The token is not recoverable
    afterwards. The caller commits the session."""
    from src.db.models import McpProjectToken

    ttl = clamp_ttl(ttl_seconds)
    raw = PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = McpProjectToken(
        id=str(uuid.uuid4()), token_hash=hash_token(raw), workspace_id=workspace_id,
        project_id=project_id, label=(label or "").strip()[:MAX_LABEL],
        scopes=[SCOPE], created_by=created_by or "", created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(row)
    session.flush()
    return raw, _view(row)


def lookup_hash(raw: str) -> ProjectTokenView | None:
    """The row behind a presented token, straight from the database (no cache).
    A database that cannot be read raises: the caller refuses."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import McpProjectToken

    with Session(_sync_engine()) as s:
        row = s.execute(select(McpProjectToken).where(
            McpProjectToken.token_hash == hash_token(raw))).scalar_one_or_none()
        return _view(row) if row is not None else None


def lookup_id(token_id: str) -> ProjectTokenView | None:
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import McpProjectToken

    with Session(_sync_engine()) as s:
        row = s.get(McpProjectToken, token_id)
        return _view(row) if row is not None else None


def touch(token_id: str) -> None:
    """Stamp ``last_used_at``. Bookkeeping: never raises."""
    try:
        from sqlalchemy.orm import Session

        from src.access.resolver import _sync_engine
        from src.db.models import McpProjectToken

        with Session(_sync_engine()) as s:
            row = s.get(McpProjectToken, token_id)
            if row is not None:
                row.last_used_at = datetime.now(UTC)
                s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("mcp_project_token_touch_failed err=%s", type(exc).__name__)


def list_rows(session, project_id: str) -> list[ProjectTokenView]:  # noqa: ANN001
    from sqlalchemy import select

    from src.db.models import McpProjectToken

    rows = session.execute(
        select(McpProjectToken).where(McpProjectToken.project_id == project_id)
        .order_by(McpProjectToken.created_at.desc())).scalars().all()
    return [_view(r) for r in rows]


def revoke(session, project_id: str, token_id: str) -> bool:  # noqa: ANN001
    """Revoke a token of this project. False when there is none. The caller commits."""
    from src.db.models import McpProjectToken

    row = session.get(McpProjectToken, token_id)
    if row is None or str(row.project_id) != str(project_id):
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    return True


def current_view() -> ProjectTokenView | None:
    """The project token behind the MCP call in flight, re-read from the
    database; None when the call is not made with a project token."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001
        return None
    client = str(getattr(token, "client_id", "") or "")
    if token is None or not client.startswith(CLIENT_PREFIX):
        return None
    try:
        return lookup_id(client[len(CLIENT_PREFIX):])
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("mcp_project_token_lookup_failed err=%s", type(exc).__name__)
        return None
