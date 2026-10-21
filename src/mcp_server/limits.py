"""How much of the server one MCP token may use.

A dev tool opens graph stores and runs ``git grep`` over clones on disk. The
size of an answer is budgeted, but the work to produce it was not: a handful of
parallel ``grep`` / ``map`` calls from one token could keep every core and the
disk busy for everybody else. Three limits, all per token (a person with two
tokens has two budgets, a stolen token cannot spend anybody else's):

* **rate**: at most ``CELMIS_MCP_RATE_PER_MINUTE`` calls in any 60 seconds
  (default 120, ``0`` switches it off);
* **concurrency**: at most ``CELMIS_MCP_MAX_CONCURRENT`` calls running at once
  (default 4); a call that cannot start within a few seconds is refused as busy
  instead of queueing without end;
* **time**: a call that runs longer than ``CELMIS_MCP_CALL_TIMEOUT_SECONDS``
  (default 60) is answered with a timeout; the work behind it stops being waited
  for (a thread cannot be killed, but it no longer holds the caller or a slot).
  A tool that writes an answer with a model (``ask``) does not run to that
  limit: :func:`answer_deadline` gives it the time the call has left minus a
  margin, so it ends with its own "took too long, narrow it" message, a
  finished answer, instead of being cut by this envelope with a generic one.

The key is a digest of the bearer token, so nothing here stores a secret and no
database is touched on the hot path. Counters are per process, which is the
right granularity for a limit whose purpose is to protect this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

RATE_DEFAULT = 120
CONCURRENT_DEFAULT = 4
TIMEOUT_DEFAULT = 60.0
SLOT_WAIT_SECONDS = 5.0
#: Keys remembered at once; beyond it the oldest idle ones are dropped.
MAX_KEYS = 4096

RATE_LIMITED = "rate limit reached for this MCP token; retry in {wait}s"
BUSY = "too many calls running for this MCP token; retry shortly"
ANSWER_TOO_SLOW = "The answer took too long (more than {seconds}s) — narrow the question or the repositories."
TIMED_OUT = "the call took longer than {seconds}s and was stopped; narrow it (repo=, path_glob=, a longer pattern)"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def rate_per_minute() -> int:
    return _env_int("CELMIS_MCP_RATE_PER_MINUTE", RATE_DEFAULT)


def max_concurrent() -> int:
    return _env_int("CELMIS_MCP_MAX_CONCURRENT", CONCURRENT_DEFAULT)


def call_timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("CELMIS_MCP_CALL_TIMEOUT_SECONDS", TIMEOUT_DEFAULT)))
    except (TypeError, ValueError):
        return TIMEOUT_DEFAULT


#: The longest a tool that writes an answer with a model may take when no call
#: envelope applies (the web assistant, a direct caller). Inside an MCP call the
#: answer gets less: see :func:`answer_deadline`.
ANSWER_CEILING_S = 150.0
#: What :func:`answer_deadline` leaves for everything after the answer is written:
#: redacting it, framing it, sending it. A tenth of the budget would be too little
#: on a slow link; a quarter of a long budget is more than it needs.
ANSWER_MARGIN_MAX_S = 10.0
ANSWER_MARGIN_SHARE = 0.25
#: Never a deadline shorter than this: a model call cannot start and finish in less.
ANSWER_MIN_S = 1.0


def answer_deadline(ceiling: float = ANSWER_CEILING_S) -> float:
    """Seconds a tool that writes an answer with a model may wait for it.

    Outside an MCP call, or with the call timeout switched off (``0``): the
    ``ceiling``. Inside one: what is left of the envelope's time, less a margin
    (the smaller of ``ANSWER_MARGIN_MAX_S`` and a quarter of the call timeout),
    never more than the ceiling and never less than ``ANSWER_MIN_S``. So with
    the default 60 s the answer is given up on at about 50 s, the tool reports
    "took too long, narrow it" itself, and the envelope's own cut (60 s) is the
    backstop it should never be the one to use. Derived, not configured: the
    two numbers cannot disagree because there is only one.
    """
    from src.mcp_server import callctx

    rec = callctx.current()
    due = getattr(rec, "deadline", None)
    if due is None:
        return float(ceiling)
    margin = min(ANSWER_MARGIN_MAX_S, ANSWER_MARGIN_SHARE * call_timeout())
    left = due - time.monotonic() - margin
    return float(max(ANSWER_MIN_S, min(ceiling, left)))


def answer_too_slow(deadline: float) -> str:
    """The message of an answer that was given up on after ``deadline`` seconds.

    The number is ROUNDED, not cut: the deadline is worked out a moment after
    the call began (49.97 for a nominal 50), and ``int()`` of that said "more
    than 49s" while the documentation says 50."""
    return ANSWER_TOO_SLOW.format(seconds=max(1, round(deadline)))


_LOCK = threading.Lock()
_CALLS: dict[str, deque[float]] = {}
_SEMS: dict[tuple[str, int], tuple[asyncio.Semaphore, int]] = {}


def reset() -> None:
    """Forget every counter (tests)."""
    with _LOCK:
        _CALLS.clear()
        _SEMS.clear()


def key_for_request() -> str:
    """A digest of the bearer token of the request being served ("" when the
    server runs without authentication: then one shared budget applies)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
        raw = getattr(tok, "token", "") or ""
    except Exception:  # noqa: BLE001
        raw = ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24] if raw else "anonymous"


def check_rate(key: str, *, now: float | None = None) -> int:
    """Count one call for ``key``. Returns 0 when allowed, else the seconds to wait."""
    limit = rate_per_minute()
    if limit <= 0:
        return 0
    t = time.monotonic() if now is None else now
    with _LOCK:
        if len(_CALLS) >= MAX_KEYS and key not in _CALLS:
            for k in [k for k, d in _CALLS.items() if not d or t - d[-1] > 60][:MAX_KEYS // 2]:
                _CALLS.pop(k, None)
        window = _CALLS.setdefault(key, deque())
        while window and t - window[0] >= 60.0:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(60.0 - (t - window[0])) + 1)
        window.append(t)
    return 0


def _semaphore(key: str) -> asyncio.Semaphore | None:
    size = max_concurrent()
    if size <= 0:
        return None
    # One semaphore per (token, event loop): a semaphore belongs to the loop
    # that first waits on it.
    k = (key, id(asyncio.get_running_loop()))
    with _LOCK:
        have = _SEMS.get(k)
        if have is not None and have[1] == size:
            return have[0]
        if len(_SEMS) >= MAX_KEYS:
            _SEMS.clear()
        sem = asyncio.Semaphore(size)
        _SEMS[k] = (sem, size)
        return sem


class Busy(RuntimeError):
    """No slot became free in time."""


@asynccontextmanager
async def slot(key: str):  # noqa: ANN201
    """One of the token's concurrent-call slots, or :class:`Busy`."""
    sem = _semaphore(key)
    if sem is None:
        yield
        return
    try:
        await asyncio.wait_for(sem.acquire(), timeout=SLOT_WAIT_SECONDS)
    except TimeoutError:
        raise Busy(BUSY) from None
    try:
        yield
    finally:
        sem.release()
