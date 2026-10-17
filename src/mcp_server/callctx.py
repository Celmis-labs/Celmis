"""The record of the MCP call in flight.

One :class:`CallRecord` per tool call, held in a ContextVar so that code deep
inside a tool (the access scope, the redactor) can say what it touched without
the call chain carrying it. The call envelope
(:mod:`src.mcp_server.call_envelope`) opens the record, the tool body and the
redaction layer add to it, and :mod:`src.mcp_server.audit` writes it down.

Append-only by design: other modules add helpers here, nobody changes the
meaning of an existing one.

The record never holds argument values, results or secrets — only the tool
name, the repos and knowledge packs touched (by id), sizes and counters.
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class CallRecord:
    tool: str
    profile: str = "full"
    started: float = field(default_factory=time.monotonic)
    repos: set[str] = field(default_factory=set)
    #: Knowledge packs the call read from: ``<pack uuid>`` for a pack touched and
    #: ``<pack uuid>/<skill name>`` for a skill whose body was returned. Ids and
    #: validated skill names only, never a title, a path or a query.
    packs: set[str] = field(default_factory=set)
    #: ok | denied | error
    status: str = "ok"
    result_bytes: int = 0
    result_items: int = 0
    redactions: dict[str, int] = field(default_factory=dict)
    #: One-way digest of the arguments; lets two calls be told apart unread.
    args_hash: str = ""
    duration_ms: int = 0


_CURRENT: ContextVar[CallRecord | None] = ContextVar("mcp_call_record", default=None)


def begin(tool: str, profile: str = "full") -> CallRecord:
    rec = CallRecord(tool=tool, profile=profile)
    _CURRENT.set(rec)
    return rec


def current() -> CallRecord | None:
    return _CURRENT.get()


def note_repos(*slugs: str) -> None:
    rec = _CURRENT.get()
    if rec is not None:
        rec.repos.update(s for s in slugs if s)


#: A skill name as ingest accepts it (``src/knowledge/ingest.py``).
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def pack_entry(value: object) -> str | None:
    """The canonical form of ``<pack uuid>`` or ``<pack uuid>/<skill name>``, or
    None for anything else. This is the whole vocabulary of ``mcp_call_log.packs``:
    a caller that passes the wrong thing (a title, a path, a query) logs nothing."""
    if not isinstance(value, str):
        return None
    head, slash, skill = value.partition("/")
    try:
        ident = str(uuid.UUID(head))
    except ValueError:
        return None
    if not slash:
        return ident
    return f"{ident}/{skill}" if _SKILL_NAME.fullmatch(skill) else None


def note_packs(*ids: object) -> None:
    """Record the knowledge packs a call read from. Accepts pack ids (or
    ``<pack id>/<skill>`` entries) or one iterable of them; anything that is not
    one is ignored, so a caller cannot put a title, a path or a query into the
    log by passing the wrong thing."""
    rec = _CURRENT.get()
    if rec is None:
        return
    for item in ids:
        for one in (item if isinstance(item, list | tuple | set | frozenset) else (item,)):
            entry = pack_entry(one)
            if entry is not None:
                rec.packs.add(entry)


def note_skill(pack_id: object, skill: object) -> None:
    """Record that the body of ``skill`` of pack ``pack_id`` was returned: both
    the pack and the ``<pack id>/<skill>`` entry."""
    if isinstance(pack_id, str) and isinstance(skill, str):
        note_packs(pack_id, f"{pack_id}/{skill}")


def note_redactions(stats) -> None:  # noqa: ANN001 — a RedactionStats or a plain mapping
    rec = _CURRENT.get()
    if rec is None or not stats:
        return
    counts = getattr(stats, "counts", None)
    if counts is None and isinstance(stats, dict):
        counts = stats
    for label, n in (counts or {}).items():
        rec.redactions[str(label)] = rec.redactions.get(str(label), 0) + int(n)


def set_status(code: str) -> None:
    rec = _CURRENT.get()
    if rec is not None:
        # `denied` is not downgraded by a later generic `error`.
        if rec.status == "denied" and code == "error":
            return
        rec.status = code


def reset() -> None:
    _CURRENT.set(None)
