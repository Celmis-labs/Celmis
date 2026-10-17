"""What agents did with a pack in the last 30 days — counts and times, never people.

The MCP tools write one ``mcp_call_log`` row per call, and ``packs`` holds the
ids of the packs the call served: ``"<pack_id>"`` for every pack touched, and
``"<pack_id>/<skill>"`` for every skill whose body was returned. This module
reads those rows back as one small object:

    {"calls_30d": 12, "last_used_at": "2026-10-16T09:30:00+00:00",
     "skills": [{"skill": "billing-api", "opens_30d": 7, "last_used_at": "..."}]}

There is deliberately no person in it, no token and no distinct-people count:
who used a pack is not something a pack's maintainers are told in the community
edition. Richer analytics belong to a separate (enterprise) module that can read
the same ``mcp_call_log.packs`` column; this one stays small and stable.

The scan is bounded (:data:`MAX_ROWS_SCANNED`, newest first) and the SQL only
narrows to rows that mention the pack id; the Python then matches list entries
exactly, so an id that merely appears inside another string counts for nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import KnowledgeItem, McpCallLog
from src.knowledge.timestamps import iso_utc

USAGE_DAYS = 30
#: Rows read for one pack, newest first. A pack busier than this reports the
#: most recent stretch (the cap protects the database, it is not a feature).
MAX_ROWS_SCANNED = 100_000
#: Skills listed in the object.
MAX_SKILLS = 200


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _iso(moment: datetime | None) -> str | None:
    return iso_utc(moment)


def summarise(rows: Iterable[tuple[datetime | None, Any]], pack_id: str, *,
              skills: Iterable[str] = ()) -> dict[str, Any]:
    """The usage object for ``pack_id`` from ``(ts, packs)`` rows.

    A row counts as one call when its list holds the pack id or a
    ``"<pack_id>/<skill>"`` entry; each distinct skill named in it counts one
    open. ``skills`` are listed even with no opens (a skill nobody opened is
    what a maintainer wants to see); a skill seen in the log but not in
    ``skills`` (an older version had it) is listed too.
    """
    prefix = f"{pack_id}/"
    opens: dict[str, int] = {name: 0 for name in skills}
    last_by_skill: dict[str, datetime | None] = dict.fromkeys(opens)
    calls = 0
    last: datetime | None = None
    for ts, packs in rows:
        if not isinstance(packs, list):
            continue
        moment = _aware(ts)
        touched = False
        opened: set[str] = set()
        for entry in packs:
            if not isinstance(entry, str):
                continue
            if entry == pack_id:
                touched = True
            elif entry.startswith(prefix) and len(entry) > len(prefix):
                touched = True
                opened.add(entry[len(prefix):])
        if not touched:
            continue
        calls += 1
        last = _later(last, moment)
        for name in opened:
            opens[name] = opens.get(name, 0) + 1
            last_by_skill[name] = _later(last_by_skill.get(name), moment)
    ordered = sorted(opens, key=lambda name: (-opens[name], name))[:MAX_SKILLS]
    return {
        "calls_30d": calls,
        "last_used_at": _iso(last),
        "skills": [{"skill": name, "opens_30d": opens[name],
                    "last_used_at": _iso(last_by_skill.get(name))} for name in ordered],
    }


async def pack_usage(session: AsyncSession, ws_id: str, pack_id: str,
                     current_version_id: str | None) -> dict[str, Any]:
    """The usage object of one pack of workspace ``ws_id``.

    The caller has already decided that the person may see it (maintainers
    only); nothing here checks.
    """
    cutoff = datetime.now(UTC) - timedelta(days=USAGE_DAYS)
    marker = f'%"{pack_id}%'            # an id is a validated uuid: no wildcard in it
    rows = (await session.execute(
        select(McpCallLog.ts, McpCallLog.packs)
        .where(McpCallLog.workspace_id == ws_id, McpCallLog.ts >= cutoff,
               McpCallLog.packs.is_not(None), cast(McpCallLog.packs, Text).like(marker))
        .order_by(McpCallLog.ts.desc()).limit(MAX_ROWS_SCANNED))).tuples().all()
    names: list[str] = []
    if current_version_id:
        names = sorted({n for n in (await session.scalars(
            select(KnowledgeItem.skill).where(
                KnowledgeItem.version_id == current_version_id,
                KnowledgeItem.pack_id == pack_id, KnowledgeItem.kind == "skill"))).all() if n})
    return summarise(rows, pack_id, skills=names)


__all__ = ["MAX_ROWS_SCANNED", "MAX_SKILLS", "USAGE_DAYS", "pack_usage", "summarise"]
