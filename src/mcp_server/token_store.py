"""The grants behind MCP tokens: issue, look up, change, revoke.

A token is a signed claim set plus a row in ``mcp_tokens``. The signature says
"we issued this"; the row says what it may do *now* — which repositories,
whether it may write, whether it is revoked or expired. The verifier and the
identity resolver consult the row (through a cache of at most
:data:`CACHE_TTL_SECONDS`), so changing the row changes an already-issued
token without reissuing it, and a revoked token stops working within the
cache window.

The token value is returned once by :func:`mint` and never stored.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

#: How long a looked-up row is trusted. This is the revocation window.
CACHE_TTL_SECONDS = 30.0
#: ``last_used_at`` is written at most this often per token.
TOUCH_EVERY_SECONDS = 60.0

KINDS = ("pat", "cli", "self", "oauth_grant")
#: Short-lived grants the server mints for its own loopback calls (the in-app
#: agent, the claude-engine review, documentation generation). Never listed,
#: never issued through the API, and like ``self`` they cannot exceed the
#: acting person: the holder's own access is the ceiling.
INTERNAL_KIND = "internal"
PROFILES = ("dev", "full")

#: Read scopes every token carries; ``write:*`` only when the row allows writing.
READ_SCOPES = ("read:graph", "read:groups", "read:reviews")
DEV_SCOPES = ("read:code",)
WRITE_SCOPES = ("write:repos", "write:config", "write:reviews")

#: A repo pattern: slug alphabet plus the glob characters. Nothing that could
#: be mistaken for a path traversal or a regex.
_PATTERN_RE = re.compile(r"^[A-Za-z0-9._/\-*?\[\]]{1,200}$")
MAX_PATTERNS = 200


class TokenError(ValueError):
    """A request to issue or change a token that cannot be honoured."""


@dataclass(frozen=True)
class TokenView:
    id: str
    kind: str
    workspace_id: str
    user_id: str
    issued_by: str
    label: str
    patterns: tuple[str, ...]
    allow_write: bool
    scopes: tuple[str, ...]
    profile: str
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime | None = None
    last_used_at: datetime | None = None

    @property
    def authoritative(self) -> bool:
        """The repo list grants access by itself (superadmin-issued) — as
        opposed to a self-service token, which can never exceed its holder."""
        return self.kind not in ("self", INTERNAL_KIND)

    def problem(self, now: datetime | None = None) -> str | None:
        """Why this grant cannot be used right now, or None."""
        now = now or datetime.now(UTC)
        if self.revoked_at is not None:
            return "This MCP token was revoked. Ask the administrator for a new one."
        if _aware(self.expires_at) <= now:
            return "This MCP token has expired. Ask the administrator for a new one."
        return None


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _view(row) -> TokenView:  # noqa: ANN001
    return TokenView(
        id=row.id, kind=row.kind, workspace_id=row.workspace_id, user_id=row.user_id,
        issued_by=row.issued_by or "", label=row.label or "",
        patterns=tuple(str(p) for p in (row.repo_patterns or [])),
        allow_write=bool(row.allow_write),
        scopes=tuple(str(s) for s in (row.scopes or [])),
        profile=row.profile or "dev",
        expires_at=_aware(row.expires_at),
        revoked_at=_aware(row.revoked_at) if row.revoked_at else None,
        created_at=_aware(row.created_at) if row.created_at else None,
        last_used_at=_aware(row.last_used_at) if row.last_used_at else None,
    )


# ════════════════════════════════════════════════════════════════════
# Settings
# ════════════════════════════════════════════════════════════════════


def _setting(name: str, default):  # noqa: ANN001, ANN202
    try:
        from src.config import get_settings

        return getattr(get_settings(), name)
    except Exception:  # noqa: BLE001
        return default


def self_service_enabled() -> bool:
    return bool(_setting("celmis_mcp_self_service", False))


def max_days() -> int:
    try:
        return max(1, int(_setting("celmis_mcp_token_max_days", 90)))
    except (TypeError, ValueError):
        return 90


def legacy_tokens_accepted() -> bool:
    return str(_setting("celmis_mcp_legacy_tokens", "refuse")).strip().lower() == "accept"


def may_issue(user) -> bool:  # noqa: ANN001
    """Who may issue tokens: the superadmin; with ``platform_admin`` any global admin."""
    from src.users.roles import is_superadmin

    if is_superadmin(user):
        return True
    who = str(_setting("celmis_mcp_token_issuers", "superadmin")).strip().lower()
    return who == "platform_admin" and bool(getattr(user, "is_admin", False))


# ════════════════════════════════════════════════════════════════════
# Validation
# ════════════════════════════════════════════════════════════════════


def normalise_patterns(patterns: Iterable[str]) -> list[str]:
    """The repo list, validated and de-duplicated. At least one entry."""
    out: list[str] = []
    for raw in patterns or []:
        p = str(raw or "").strip()
        if not p:
            continue
        if not _PATTERN_RE.match(p) or ".." in p:
            raise TokenError(f"{p[:60]!r} is not a repository slug or glob")
        if p not in out:
            out.append(p)
    if not out:
        raise TokenError("a token needs at least one repository (a slug or a glob)")
    if len(out) > MAX_PATTERNS:
        raise TokenError(f"at most {MAX_PATTERNS} repository entries")
    return out


def clamp_days(days: int | None) -> int:
    cap = max_days()
    try:
        n = int(days if days is not None else min(30, cap))
    except (TypeError, ValueError) as exc:
        raise TokenError("expires_in_days must be a number") from exc
    if n < 1:
        raise TokenError("expires_in_days must be at least 1")
    return min(n, cap)


# ════════════════════════════════════════════════════════════════════
# Cache
# ════════════════════════════════════════════════════════════════════

_CACHE: dict[str, tuple[float, TokenView | None]] = {}
_CACHE_LOCK = threading.Lock()
_TOUCHED: dict[str, float] = {}
_CACHE_CAP = 4096


def invalidate(token_id: str | None = None) -> None:
    """Forget a cached row (this process only — others catch up within the TTL)."""
    with _CACHE_LOCK:
        if token_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(token_id, None)


def _engine():  # noqa: ANN202
    from src.access.resolver import _sync_engine

    return _sync_engine()


def lookup(token_id: str) -> TokenView | None:
    """The grant behind ``token_id`` (the JWT's ``jti``), or None when there is
    none. A database that cannot be read raises: the caller refuses."""
    if not token_id:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(token_id)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    with Session(_engine()) as s:
        row = s.get(McpToken, token_id)
        view = _view(row) if row is not None else None
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_CAP:
            _CACHE.clear()
        _CACHE[token_id] = (now, view)
    return view


def lookup_fresh(session, token_id: str) -> TokenView | None:  # noqa: ANN001
    """The grant behind ``token_id`` straight from the database (no cache)."""
    from src.db.models import McpToken

    row = session.get(McpToken, token_id)
    return _view(row) if row is not None else None


def touch(token_id: str, ip: str | None = None) -> None:
    """Record a use — at most once a minute per token, off the request path."""
    now = time.monotonic()
    with _CACHE_LOCK:
        if now - _TOUCHED.get(token_id, -1e9) < TOUCH_EVERY_SECONDS:
            return
        _TOUCHED[token_id] = now
        if len(_TOUCHED) > _CACHE_CAP:
            _TOUCHED.clear()

    def _write() -> None:
        try:
            from sqlalchemy.orm import Session

            from src.db.models import McpToken

            with Session(_engine()) as s:
                row = s.get(McpToken, token_id)
                if row is None:
                    return
                row.last_used_at = datetime.now(UTC)
                if ip:
                    row.last_used_ip = ip[:64]
                s.commit()
        except Exception as exc:  # noqa: BLE001 — bookkeeping only
            logger.debug("mcp_token_touch_failed err=%s", type(exc).__name__)

    threading.Thread(target=_write, name="mcp-token-touch", daemon=True).start()


# ════════════════════════════════════════════════════════════════════
# Issue / change / revoke
# ════════════════════════════════════════════════════════════════════


def scopes_for(profile: str, allow_write: bool) -> list[str]:
    scopes = list(DEV_SCOPES if profile == "dev" else READ_SCOPES)
    if profile == "dev":
        # The dev endpoint is read-only; a write switch is meaningless there.
        return scopes
    if allow_write:
        scopes.extend(WRITE_SCOPES)
    return scopes


def _scopes_granted(profile: str, allow_write: bool, write_scopes: Iterable[str] | None) -> list[str]:
    scopes = scopes_for(profile, allow_write)
    if write_scopes is None:
        return scopes
    keep = set(write_scopes)
    return [s for s in scopes if not s.startswith("write:") or s in keep]


def create_row(
    session,  # noqa: ANN001
    *,
    kind: str,
    workspace_id: str,
    user_id: str,
    issued_by: str,
    label: str,
    patterns: Iterable[str],
    allow_write: bool = False,
    profile: str = "dev",
    expires_in_days: int | None = None,
    token_id: str | None = None,
    write_scopes: Iterable[str] | None = None,
):
    """Insert a grant row and return it (committed).

    ``write_scopes`` narrows what a write-capable token carries to exactly the
    write scopes it was granted (a self-service token asked for one of them);
    left out, a write token carries all of them."""
    from src.db.models import McpToken

    if kind not in KINDS and kind != INTERNAL_KIND:
        raise TokenError(f"unknown token kind {kind!r}")
    if profile not in PROFILES:
        raise TokenError(f"profile must be one of {', '.join(PROFILES)}")
    if profile == "dev" and allow_write:
        raise TokenError("the dev profile is read-only: a write token needs profile 'full'")
    days = clamp_days(expires_in_days)
    row = McpToken(
        id=token_id or str(uuid.uuid4()), kind=kind, workspace_id=workspace_id,
        user_id=user_id, issued_by=issued_by, label=(label or "")[:80],
        repo_patterns=normalise_patterns(patterns), allow_write=bool(allow_write),
        scopes=_scopes_granted(profile, allow_write, write_scopes), profile=profile,
        created_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(days=days),
    )
    session.add(row)
    session.commit()
    return row


def sign(row) -> str:  # noqa: ANN001
    """The bearer token for a grant row. The only place the value exists."""
    from src.mcp_server.auth import JwtConfig, issue_token

    expires = _aware(row.expires_at)
    seconds = max(1, int((expires - datetime.now(UTC)).total_seconds()))
    typ = {"oauth_grant": "oauth"}.get(row.kind, row.kind)
    return issue_token(
        JwtConfig.from_env(),
        subject=row.user_id,
        scopes=list(row.scopes or []),
        client_id=(row.label or "celmis-mcp-client")[:64],
        expires_in=seconds,
        extra_claims={"workspace_id": row.workspace_id},
        jti=row.id, typ=typ,
    )


def mint(**kwargs) -> tuple[str, TokenView]:  # noqa: ANN003
    """Create a grant and sign its token. Returns ``(token, view)``."""
    from sqlalchemy.orm import Session

    with Session(_engine()) as s:
        row = create_row(s, **kwargs)
        token = sign(row)
        view = _view(row)
    invalidate(view.id)
    return token, view


def mint_internal(*, user_id: str, workspace_id: str, ttl_seconds: int,
                  label: str = "celmis-internal") -> str:
    """A bearer token for the server's own loopback MCP calls.

    The row is bound to the workspace and the acting person, carries read
    scopes only, and lists ``*`` so the person's own access is the whole
    ceiling (like a self-service token, never wider). It lives ``ttl_seconds``;
    expired internal rows are swept on the next mint.
    """
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    ttl = max(60, int(ttl_seconds))
    now = datetime.now(UTC)
    with Session(_engine()) as s:
        s.execute(delete(McpToken).where(
            McpToken.kind == INTERNAL_KIND,
            McpToken.expires_at < now - timedelta(days=1)))
        s.commit()
        row = McpToken(
            id=str(uuid.uuid4()), kind=INTERNAL_KIND, workspace_id=workspace_id or "default",
            user_id=user_id, issued_by="internal", label=label[:80],
            repo_patterns=["*"], allow_write=False,
            scopes=scopes_for("full", False), profile="full",
            created_at=now, expires_at=now + timedelta(seconds=ttl),
        )
        s.add(row)
        s.commit()
        return sign(row)


def list_rows(session, *, user_id: str | None = None, workspace_id: str | None = None,  # noqa: ANN001
              status: str | None = None, kinds: Iterable[str] | None = None) -> list[TokenView]:
    from sqlalchemy import select

    from src.db.models import McpToken

    stmt = select(McpToken).order_by(McpToken.created_at.desc())
    if user_id:
        stmt = stmt.where(McpToken.user_id == user_id)
    if workspace_id:
        stmt = stmt.where(McpToken.workspace_id == workspace_id)
    if kinds:
        stmt = stmt.where(McpToken.kind.in_(list(kinds)))
    views = [_view(r) for r in session.execute(stmt).scalars().all()]
    now = datetime.now(UTC)
    if status == "active":
        views = [v for v in views if v.problem(now) is None]
    elif status == "revoked":
        views = [v for v in views if v.revoked_at is not None]
    elif status == "expired":
        views = [v for v in views if v.revoked_at is None and _aware(v.expires_at) <= now]
    return views


def revoke(session, token_id: str, by: str):  # noqa: ANN001, ANN201
    from src.db.models import McpToken

    row = session.get(McpToken, token_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        row.revoked_by = by
        session.commit()
    invalidate(token_id)
    return row


def revoke_all_for_user(session, user_id: str, by: str) -> int:  # noqa: ANN001
    """Revoke every live token of one person, in every workspace (account
    deactivated or erased). Returns how many were revoked."""
    from sqlalchemy import select

    from src.db.models import McpToken

    rows = session.execute(
        select(McpToken).where(
            McpToken.user_id == user_id, McpToken.revoked_at.is_(None))
    ).scalars().all()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
        row.revoked_by = by
    if rows:
        session.commit()
    invalidate()
    return len(rows)


def update_row(session, token_id: str, *, patterns: Iterable[str] | None = None,  # noqa: ANN001
               expires_in_days: int | None = None, allow_write: bool | None = None,
               label: str | None = None):
    """Change a grant. Takes effect on the next verification — the token is not
    reissued, because the repo list is looked up on the server.

    Only changes that can take effect are accepted. The signed token carries
    its own expiry and its own scopes, so a grant can be *narrowed* (fewer
    repos, an earlier expiry, writing switched off) or revoked, but not
    widened: a later expiry or switching writing on needs a new token. A
    self-service token's repos are its holder's own access, never a list."""
    from src.db.models import McpToken

    row = session.get(McpToken, token_id)
    if row is None:
        return None
    if patterns is not None:
        if row.kind in ("self", INTERNAL_KIND):
            raise TokenError(
                "a self-service token has no repository list of its own: it "
                "reaches what its holder reaches. Revoke it, or issue a listed token.")
        row.repo_patterns = normalise_patterns(patterns)
    if expires_in_days is not None:
        new_expiry = datetime.now(UTC) + timedelta(days=clamp_days(expires_in_days))
        if new_expiry > _aware(row.expires_at):
            raise TokenError(
                "an issued token cannot be extended: its expiry is part of the "
                "signed token. Issue a new one.")
        row.expires_at = new_expiry
    if allow_write is not None:
        if row.profile == "dev" and allow_write:
            raise TokenError("the dev profile is read-only")
        if allow_write and not row.allow_write:
            raise TokenError(
                "writing cannot be switched on for an issued token: its scopes "
                "are part of the signed token. Issue a new one with write allowed.")
        row.allow_write = bool(allow_write)
        if not row.allow_write:
            # Only switching writing OFF recomputes: a token that was granted
            # one write scope keeps exactly that one.
            row.scopes = scopes_for(row.profile, False)
    if label is not None:
        row.label = label[:80]
    session.commit()
    invalidate(token_id)
    return row


def active_grant(session, *, user_id: str, workspace_id: str):  # noqa: ANN001, ANN201
    """The user's active ``oauth_grant`` row in the workspace, or None."""
    from sqlalchemy import select

    from src.db.models import McpToken

    rows = session.execute(
        select(McpToken).where(
            McpToken.kind == "oauth_grant", McpToken.user_id == user_id,
            McpToken.workspace_id == workspace_id, McpToken.revoked_at.is_(None),
        ).order_by(McpToken.created_at.desc())
    ).scalars().all()
    now = datetime.now(UTC)
    for r in rows:
        if _aware(r.expires_at) > now:
            return r
    return None


__all__ = [
    "CACHE_TTL_SECONDS", "INTERNAL_KIND", "TokenError", "TokenView", "active_grant", "clamp_days",
    "create_row", "invalidate", "legacy_tokens_accepted", "list_rows", "lookup",
    "max_days", "may_issue", "mint", "mint_internal", "normalise_patterns", "revoke",
    "self_service_enabled", "sign", "touch", "update_row",
]
