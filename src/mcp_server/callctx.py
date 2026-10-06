"""The record of the MCP call in flight.

One :class:`CallRecord` per tool call, held in a ContextVar so that code deep
inside a tool (the access scope, the redactor) can say what it touched without
the call chain carrying it. The call envelope
(:mod:`src.mcp_server.call_envelope`) opens the record, the tool body and the
redaction layer add to it, and :mod:`src.mcp_server.audit` writes it down.

Append-only by design: other modules add helpers here, nobody changes the
meaning of an existing one.

The record never holds argument values, results or secrets — only the tool
name, the repos touched, sizes and counters.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class CallRecord:
    tool: str
    profile: str = "full"
    started: float = field(default_factory=time.monotonic)
    repos: set[str] = field(default_factory=set)
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
