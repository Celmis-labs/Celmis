"""Cheap in-process counters the resource sampler snapshots every interval.

Thread-safe, zero dependencies. `llm_calls`/`llm_tokens_*` are cumulative;
the sampler stores per-interval deltas so the history answers "how many LLM
requests went out between 14:30 and 14:31". `reviews_running` is a live gauge
(inc on review start, dec in finally) so parallel-vs-sequential review load
is visible per sample.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()

_counters = {
    "llm_calls": 0,
    "llm_tokens_in": 0,
    "llm_tokens_out": 0,
}
_gauges = {
    "reviews_running": 0,
}


def record_llm_call(tokens_in: int = 0, tokens_out: int = 0) -> None:
    with _lock:
        _counters["llm_calls"] += 1
        _counters["llm_tokens_in"] += int(tokens_in or 0)
        _counters["llm_tokens_out"] += int(tokens_out or 0)


def review_started() -> None:
    with _lock:
        _gauges["reviews_running"] += 1


def review_finished() -> None:
    with _lock:
        _gauges["reviews_running"] = max(0, _gauges["reviews_running"] - 1)


def snapshot() -> dict:
    with _lock:
        return {**_counters, **_gauges}


__all__ = ["record_llm_call", "review_started", "review_finished", "snapshot"]
