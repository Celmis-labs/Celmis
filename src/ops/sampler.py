"""Resource sampler — one row of usage history every CELMIS_SAMPLE_SECONDS.

Answers, from real data, the sizing questions the docs need: how much
RAM/CPU at time X, how many reviews/jobs ran in parallel, how many LLM calls
went out per interval. Retention CELMIS_SAMPLE_RETENTION_DAYS (default 14).

Runs as one asyncio task in the api process; all blocking work is tiny
(psutil reads + one INSERT). Disable with CELMIS_DISABLE_SAMPLER=1.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_SAMPLE_SECONDS = int(os.environ.get("CELMIS_SAMPLE_SECONDS", "30"))
_RETENTION_DAYS = int(os.environ.get("CELMIS_SAMPLE_RETENTION_DAYS", "14"))

_task: asyncio.Task | None = None
_prev_counters: dict = {}
_prev_http: int = 0


def _http_requests_total() -> int:
    """Total HTTP requests served, from the existing Prometheus counter."""
    try:
        from src.api.metrics import HTTP_REQUESTS  # labeled Counter
        total = 0.0
        for metric in HTTP_REQUESTS.collect():
            for s in metric.samples:
                if s.name.endswith("_total"):
                    total += s.value
        return int(total)
    except Exception:  # noqa: BLE001
        return 0


async def _sample_once(psutil_proc) -> None:
    global _prev_counters, _prev_http
    import psutil
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from src.db import async_session
    from src.db.models import AgentSession, ResourceSample
    from src.ops.telemetry import snapshot

    cpu = psutil_proc.cpu_percent(interval=None)          # since last call
    rss_mb = psutil_proc.memory_info().rss / (1024 * 1024)
    sys_mem = psutil.virtual_memory().percent
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0

    snap = snapshot()
    deltas = {
        k: max(0, snap[k] - _prev_counters.get(k, 0))
        for k in ("llm_calls", "llm_tokens_in", "llm_tokens_out")
    }
    _prev_counters = snap
    http_total = _http_requests_total()
    http_delta = max(0, http_total - _prev_http) if _prev_http else 0
    _prev_http = http_total

    # Queue + agent-session gauges from the DB (cheap indexed counts).
    jobs_running = jobs_pending = sessions_running = 0
    try:
        from sqlalchemy import text
        async with async_session() as s:
            rows = (await s.execute(text(
                "SELECT status, count(*) FROM sync_jobs "
                "WHERE status IN ('running','pending') GROUP BY status"
            ))).all()
            by = {status: n for status, n in rows}
            jobs_running = int(by.get("running", 0))
            jobs_pending = int(by.get("pending", 0))
            sessions_running = int((await s.execute(
                select(sa_func.count()).select_from(AgentSession)
                .where(AgentSession.status == "running")
            )).scalar_one())
    except Exception as exc:  # noqa: BLE001
        logger.debug("sampler_db_counts_failed err=%s", exc)

    async with async_session() as s:
        s.add(ResourceSample(
            cpu_pct=round(cpu, 1), rss_mb=round(rss_mb, 1),
            sys_mem_pct=round(sys_mem, 1), load1=round(load1, 2),
            reviews_running=snap["reviews_running"],
            jobs_running=jobs_running, jobs_pending=jobs_pending,
            agent_sessions_running=sessions_running,
            llm_calls=deltas["llm_calls"],
            llm_tokens_in=deltas["llm_tokens_in"],
            llm_tokens_out=deltas["llm_tokens_out"],
            http_requests=http_delta,
        ))
        await s.commit()


async def _cleanup() -> None:
    from sqlalchemy import delete

    from src.db import async_session
    from src.db.models import ResourceSample

    cutoff = datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)
    async with async_session() as s:
        await s.execute(delete(ResourceSample).where(ResourceSample.ts < cutoff))
        await s.commit()


async def _loop() -> None:
    import psutil
    proc = psutil.Process()
    proc.cpu_percent(interval=None)   # prime the counter
    ticks = 0
    while True:
        await asyncio.sleep(_SAMPLE_SECONDS)
        try:
            await _sample_once(proc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resource_sample_failed err=%s", exc)
        ticks += 1
        if ticks % max(1, 3600 // _SAMPLE_SECONDS) == 0:   # ~hourly
            try:
                await _cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sample_cleanup_failed err=%s", exc)


def start_sampler() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_event_loop().create_task(_loop(), name="resource-sampler")
        logger.info("resource_sampler_started interval=%ss retention=%sd",
                    _SAMPLE_SECONDS, _RETENTION_DAYS)


__all__ = ["start_sampler"]
