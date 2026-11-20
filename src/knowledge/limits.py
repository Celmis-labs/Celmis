"""The limits of knowledge packs, and where each one comes from.

Six limits shape what a workspace may keep:

    archive_bytes        the compressed upload
    max_files            files KEPT in one version
    file_bytes           one file
    total_bytes          all kept files, uncompressed
    packs_per_workspace  packs a workspace holds
    keep_versions        versions retained per pack (the current one always stays)

Each resolves, per workspace::

    workspace override (workspace_pack_limits, NULL = inherit)
      > install default (CELMIS_PACK_* environment variables)
      > built-in (the constants below)

and no layer may exceed the hard CEILINGS, which exist so that a typo cannot
turn a pack upload into a denial of service. The archive ceiling is the size of
a request body that reaches the server before anyone is signed in, so it is the
operator's call, not a workspace admin's: it stays at the built-in 5 MB until
the operator raises it (``CELMIS_PACK_MAX_ARCHIVE_CEILING_BYTES``), and the
request-body cap for pack uploads follows it. The others are constants of the
build, generous enough for a workspace admin to raise into.

Read the limits through :func:`resolve` / :func:`resolve_sync`, never from the
built-in constants: a constant is only what an install that sets nothing gets.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, fields
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.db.models import WorkspacePackLimits

logger = logging.getLogger(__name__)

MB = 1024 * 1024


@dataclass(frozen=True)
class PackLimits:
    archive_bytes: int
    max_files: int
    file_bytes: int
    total_bytes: int
    packs_per_workspace: int
    keep_versions: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


FIELDS: tuple[str, ...] = tuple(f.name for f in fields(PackLimits))

#: What an install that sets nothing gets (the limits before they were configurable).
BUILTIN = PackLimits(
    archive_bytes=5 * MB, max_files=400, file_bytes=512 * 1024, total_bytes=8 * MB,
    packs_per_workspace=200, keep_versions=20)

#: The most any layer may ask for.
_CEILING_CONSTANTS = PackLimits(
    archive_bytes=5 * MB, max_files=5000, file_bytes=4 * MB, total_bytes=200 * MB,
    packs_per_workspace=2000, keep_versions=100)

#: Environment variable of the install default of each limit.
ENV_NAMES: dict[str, str] = {
    "archive_bytes": "CELMIS_PACK_MAX_ARCHIVE_BYTES",
    "max_files": "CELMIS_PACK_MAX_FILES",
    "file_bytes": "CELMIS_PACK_MAX_FILE_BYTES",
    "total_bytes": "CELMIS_PACK_MAX_TOTAL_BYTES",
    "packs_per_workspace": "CELMIS_PACK_MAX_PACKS",
    "keep_versions": "CELMIS_PACK_KEEP_VERSIONS",
}
CEILING_ENV = "CELMIS_PACK_MAX_ARCHIVE_CEILING_BYTES"

#: Where a resolved value came from.
SOURCES = ("workspace", "install", "builtin")


def ceilings() -> PackLimits:
    """The hard maxima: constants, except the archive one, which the operator may set."""
    raw = os.environ.get(CEILING_ENV, "").strip()
    try:
        archive = int(raw) if raw else _CEILING_CONSTANTS.archive_bytes
    except ValueError:
        archive = _CEILING_CONSTANTS.archive_bytes
    return PackLimits(**{**_CEILING_CONSTANTS.as_dict(), "archive_bytes": max(1, archive)})


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("pack_limit_env_ignored name=%s reason=not_an_integer", name)
        return None
    if value < 1:
        logger.warning("pack_limit_env_ignored name=%s reason=below_one", name)
        return None
    return value


def install_values() -> dict[str, int]:
    """The install-wide values that are set (clamped to the ceilings)."""
    top = ceilings().as_dict()
    out: dict[str, int] = {}
    for name in FIELDS:
        value = _env_int(ENV_NAMES[name])
        if value is not None:
            out[name] = min(value, top[name])
    return out


def install_defaults() -> PackLimits:
    """What a workspace without an override gets: the install value, else the built-in."""
    builtin = BUILTIN.as_dict()
    return PackLimits(**{n: install_values().get(n, builtin[n]) for n in FIELDS})


@dataclass(frozen=True)
class Resolved:
    limits: PackLimits
    sources: dict[str, str]          # field -> "workspace" | "install" | "builtin"

    def payload(self) -> dict[str, Any]:
        return {"limits": self.limits.as_dict(), "sources": dict(self.sources),
                "ceilings": ceilings().as_dict(), "defaults": install_defaults().as_dict(),
                "builtin": BUILTIN.as_dict()}


def _layer(row: Any) -> Resolved:
    top = ceilings().as_dict()
    install = install_values()
    builtin = BUILTIN.as_dict()
    values: dict[str, int] = {}
    sources: dict[str, str] = {}
    for name in FIELDS:
        own = getattr(row, name, None) if row is not None else None
        if own is not None:
            values[name], sources[name] = min(int(own), top[name]), "workspace"
        elif name in install:
            values[name], sources[name] = install[name], "install"
        else:
            values[name], sources[name] = min(builtin[name], top[name]), "builtin"
    return Resolved(PackLimits(**values), sources)


def resolved_from(row: Any) -> Resolved:
    """Resolve from an already-loaded override row (or None)."""
    return _layer(row)


async def resolve_full(session: AsyncSession, ws_id: str) -> Resolved:
    row = await session.get(WorkspacePackLimits, ws_id)
    return _layer(row)


async def resolve(session: AsyncSession, ws_id: str) -> PackLimits:
    """The limits that apply to workspace ``ws_id`` right now."""
    return (await resolve_full(session, ws_id)).limits


def resolve_sync(s: Session | None, ws_id: str) -> PackLimits:
    """:func:`resolve` for blocking code; opens its own session when ``s`` is None.
    Any failure answers the install defaults."""
    try:
        if s is not None:
            return _layer(s.scalar(select(WorkspacePackLimits).where(
                WorkspacePackLimits.workspace_id == ws_id))).limits
        from src.access.resolver import _sync_engine

        with Session(_sync_engine()) as own:
            return resolve_sync(own, ws_id)
    except Exception as exc:  # noqa: BLE001 - fall back to the install layer
        logger.warning("pack_limits_unreadable err=%s", type(exc).__name__)
        return install_defaults()


def body_cap() -> int | None:
    """The request-body cap of a pack upload once the operator raised the archive ceiling:
    that ceiling plus a megabyte of envelope. ``None`` while it is the built-in one, so the
    large-body cap (5 MB) holds exactly as before. The router still holds each upload to
    its workspace's own archive limit."""
    top = ceilings().archive_bytes
    return top + MB if top > BUILTIN.archive_bytes else None


class LimitError(ValueError):
    """A value is not acceptable for a limit."""


def validate(values: dict[str, Any]) -> dict[str, int | None]:
    """Check an override payload: each present key is None (inherit) or an integer
    in ``1..ceiling``. Unknown keys are refused. Raises :class:`LimitError`."""
    top = ceilings().as_dict()
    out: dict[str, int | None] = {}
    for key, value in values.items():
        if key not in FIELDS:
            raise LimitError(f"Unknown limit {key!r}.")
        if value is None:
            out[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise LimitError(f"{key} must be a whole number.")
        if value < 1:
            raise LimitError(f"{key} must be at least 1.")
        if value > top[key]:
            raise LimitError(f"{key} cannot exceed {top[key]}.")
        out[key] = value
    return out


__all__ = [
    "BUILTIN", "CEILING_ENV", "ENV_NAMES", "FIELDS", "LimitError", "PackLimits", "Resolved",
    "body_cap", "ceilings", "install_defaults", "install_values", "resolve", "resolve_full",
    "resolve_sync", "resolved_from", "validate",
]
