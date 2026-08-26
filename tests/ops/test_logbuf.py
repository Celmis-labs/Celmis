"""The ops ring buffer has to catch the records worth debugging.

Regression: an endpoint returned 500 and the buffer held nothing at all.
uvicorn logs unhandled exceptions on `uvicorn.error`, which sets
propagate=False — so a handler on the root logger never sees them, and the
one class of record you actually go looking for is the one that is missing.
"""

from __future__ import annotations

import logging

from src.ops import logbuf


def _reset() -> None:
    logbuf._installed = False
    logbuf._buf.clear()
    for name in ("", *logbuf._EXTRA_LOGGERS):
        lg = logging.getLogger(name)
        for h in [h for h in lg.handlers if isinstance(h, logbuf.RingBufferHandler)]:
            lg.removeHandler(h)


def test_captures_uvicorn_error_tracebacks():
    _reset()
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.propagate = False          # what uvicorn itself does
    uvicorn_error.setLevel(logging.INFO)
    try:
        logbuf.install()
        try:
            raise ValueError("boom in an endpoint")
        except ValueError:
            uvicorn_error.exception("Exception in ASGI application")

        records = logbuf.tail(limit=10)
        assert records, "uvicorn.error record never reached the buffer"
        entry = records[-1]
        assert entry["level"] == "ERROR"
        assert "boom in an endpoint" in entry.get("exc", "")
    finally:
        _reset()


def test_access_log_is_not_captured():
    """One line per request would evict everything else from the buffer."""
    assert "uvicorn.access" not in logbuf._EXTRA_LOGGERS


def test_root_logger_still_captured():
    _reset()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.INFO)   # pytest pins root at WARNING by default
    try:
        logbuf.install()
        logging.getLogger("src.something").info("hello ops")
        assert any("hello ops" in r["message"] for r in logbuf.tail(limit=10))
    finally:
        root.setLevel(previous)
        _reset()
