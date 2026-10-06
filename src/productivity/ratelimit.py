"""The sync's own API budget: a token bucket per credential, and 429 handling.

WHY OUR OWN BUCKET. Bitbucket allows roughly 1000 requests an hour per user,
and the token the sync reads with is the same one that posts reviews. A
backfill that spends the hour starves live reviews. The bucket sits well under
the provider's limit (`rate_per_hour`, default 500) and is shared by every
sync of the same credential in the process.

TWO WAYS TO STOP. A short wait is slept off. A wait longer than `max_wait`
(the rest of the job's time budget) is not slept: the call raises
`RateLimited(until)` and the engine saves its cursor, records the time in
`productivity_sync_state.rate_limited_until` and re-queues itself for then —
a worker slot is never parked for an hour.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

#: The most that is ever slept in one acquire when the caller gives no limit.
DEFAULT_MAX_WAIT = 20.0


class RateLimited(Exception):
    """The provider (or our own bucket) says: not before `until`."""

    def __init__(self, until: datetime, reason: str = "rate limited") -> None:
        super().__init__(f"{reason}; retry after {until.isoformat()}")
        self.until = until
        self.reason = reason


def fingerprint(provider: str, secret: str, email: str = "") -> str:
    """Which credential this is, without keeping it: the key of a bucket."""
    digest = hashlib.sha256(f"{provider}\0{email}\0{secret}".encode()).hexdigest()
    return f"{provider}:{digest[:16]}"


class RateGate:
    """Token bucket. Refills `rate_per_hour / 3600` tokens a second, holds at most `burst`."""

    def __init__(
        self, rate_per_hour: int, *, burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._clock, self._sleep, self._now = clock, sleep, now
        self._lock = threading.Lock()
        self.configure(rate_per_hour, burst)
        self._tokens = float(self._burst)
        self._at = clock()
        #: Set by `penalise` after a 429: nothing is allowed before this.
        self._blocked_until = 0.0

    def configure(self, rate_per_hour: int, burst: int | None = None) -> None:
        self.rate_per_hour = max(1, int(rate_per_hour))
        self._per_second = self.rate_per_hour / 3600.0
        # A burst of a tenth of an hour: a page of calls goes through at once,
        # a backfill does not.
        self._burst = max(1, int(burst if burst is not None else min(50, self.rate_per_hour // 10 or 1)))

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self._burst), self._tokens + (now - self._at) * self._per_second)
        self._at = now

    def acquire(self, max_wait: float = DEFAULT_MAX_WAIT) -> None:
        """Take one token, sleeping for it; `RateLimited` when that wait exceeds `max_wait`."""
        with self._lock:
            self._refill()
            wait = max(0.0, self._blocked_until - self._clock())
            if self._tokens < 1.0:
                wait = max(wait, (1.0 - self._tokens) / self._per_second)
            if wait > max_wait:
                raise RateLimited(self._now() + timedelta(seconds=wait))
            # Reserve now, sleep outside the lock's critical decision: the
            # token is spent either way, so a second caller waits behind it.
            self._tokens -= 1.0
        if wait > 0:
            self._sleep(wait)

    def penalise(self, retry_after: float) -> datetime:
        """The provider answered 429: block everything for `retry_after` seconds. Returns when it ends."""
        retry_after = max(1.0, float(retry_after))
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + retry_after)
        return self._now() + timedelta(seconds=retry_after)


_GATES: dict[str, RateGate] = {}
_GATES_LOCK = threading.Lock()


def gate_for(key: str, rate_per_hour: int) -> RateGate:
    """The process-wide gate of one credential (see `fingerprint`)."""
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = _GATES[key] = RateGate(rate_per_hour)
        elif gate.rate_per_hour != rate_per_hour:
            gate.configure(rate_per_hour)
        return gate


def reset_gates() -> None:
    """For tests."""
    with _GATES_LOCK:
        _GATES.clear()


def parse_retry_after(value: str | None, *, default: float = 60.0) -> float:
    """Seconds from a `Retry-After` header: delta-seconds or an HTTP date. Never below 1, never above a day."""
    if value is None or not str(value).strip():
        return default
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return default
    return min(86400.0, max(1.0, seconds))
