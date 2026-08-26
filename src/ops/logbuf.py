"""In-memory ring buffer of recent log records + a logging handler.

Why this exists: the box's SSH port is not reachable from every network, so
`docker logs` is not always an option. This keeps the last N records in
process memory and exposes them over an admin-only HTTP endpoint, which
makes production debugging possible from anywhere the web UI works.

Deliberately bounded and lossy — it is a debugging aid, not an audit trail
(that one is `src/security/audit.py`, persisted to disk).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

_MAX_RECORDS = int(os.environ.get("CELMIS_LOG_BUFFER_SIZE", "3000"))

_buf: deque[dict[str, Any]] = deque(maxlen=_MAX_RECORDS)
_lock = threading.Lock()
_installed = False


class RingBufferHandler(logging.Handler):
    """Appends every formatted record to the module-level ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — never break the app on a bad log call
            msg = str(getattr(record, "msg", ""))
        entry = {
            "ts": datetime.fromtimestamp(
                record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg[:4000],
            "module": f"{record.module}:{record.lineno}",
        }
        if record.exc_info:
            with contextlib.suppress(Exception):
                entry["exc"] = self.format(record)[-4000:]
        with _lock:
            _buf.append(entry)


# Loggers that do NOT propagate to root, so the root handler alone misses
# them. `uvicorn.error` is the one that matters: it carries the traceback of
# every unhandled exception, i.e. exactly the 500s worth debugging. Without
# it a failing endpoint shows up as "Internal Server Error" in the browser
# and leaves nothing whatsoever in the log. `uvicorn.access` is deliberately
# not here — one request line per hit would push everything else out of a
# 3000-record buffer within minutes.
_EXTRA_LOGGERS = ("uvicorn", "uvicorn.error", "gunicorn.error")


def install() -> None:
    """Attach the handler to the root logger and to the non-propagating
    server loggers. Idempotent."""
    global _installed
    if _installed:
        return
    handler = RingBufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    for name in _EXTRA_LOGGERS:
        lg = logging.getLogger(name)
        if not lg.propagate:
            lg.addHandler(handler)
    _installed = True


def tail(
    *,
    limit: int = 200,
    level: str | None = None,
    contains: str | None = None,
    logger_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-last slice of the buffer, filtered."""
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_level = order.get((level or "").upper(), 0)
    needle = (contains or "").lower()
    with _lock:
        items = list(_buf)
    out = []
    for e in items:
        if min_level and order.get(e["level"], 0) < min_level:
            continue
        if needle and needle not in e["message"].lower() \
                and needle not in e["logger"].lower():
            continue
        if logger_prefix and not e["logger"].startswith(logger_prefix):
            continue
        out.append(e)
    return out[-limit:]


def stats() -> dict[str, Any]:
    with _lock:
        items = list(_buf)
    counts: dict[str, int] = {}
    for e in items:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
    return {
        "buffered": len(items),
        "capacity": _MAX_RECORDS,
        "by_level": counts,
        "oldest": items[0]["ts"] if items else None,
        "newest": items[-1]["ts"] if items else None,
    }


__all__ = ["install", "tail", "stats", "RingBufferHandler"]
