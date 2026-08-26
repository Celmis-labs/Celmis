"""Prometheus metrics (Stage 21).

Exposes `/metrics` in Prometheus text format. Two data sources:

  1. HTTP middleware counters — request totals + latency histogram,
     labelled by method / path-class / status. Path-class (not raw path)
     keeps cardinality bounded.
  2. A custom collector that queries operational state at scrape time:
     sync_jobs by status, review_runs aggregates (last 24h), workspace
     count. Scrape-time queries keep the numbers honest without a
     background sampler.

The endpoint is intentionally auth-free (standard Prometheus practice —
protect at the network layer / scrape config). It exposes only counts,
never payloads.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Histogram,
        generate_latest,
    )
    from prometheus_client.core import GaugeMetricFamily
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


if _PROM_AVAILABLE:
    HTTP_REQUESTS = Counter(
        "celmis_http_requests_total",
        "HTTP requests",
        ["method", "path_class", "status"],
    )
    HTTP_LATENCY = Histogram(
        "celmis_http_request_duration_seconds",
        "HTTP request latency",
        ["method", "path_class"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )

    class _OpsCollector:
        """Scrape-time gauges from Postgres + SQLite stores."""

        def collect(self):  # noqa: ANN201
            # sync_jobs per status
            g_jobs = GaugeMetricFamily(
                "celmis_sync_jobs", "Jobs in queue by status", labels=["status"],
            )
            try:
                from src.sync.queue import stats
                for status, n in stats().items():
                    g_jobs.add_metric([status], n)
            except Exception as exc:  # noqa: BLE001
                logger.debug("metrics_jobs_failed err=%s", exc)
            yield g_jobs

            # review_runs last 24h
            g_runs = GaugeMetricFamily(
                "celmis_review_runs_24h", "Review runs in last 24h by status",
                labels=["status"],
            )
            g_cost = GaugeMetricFamily(
                "celmis_review_cost_usd_24h", "LLM cost (USD) last 24h",
            )
            try:
                import sqlite3
                from datetime import datetime, timedelta

                from src.api.review_runs import get_review_run_store
                store = get_review_run_store()
                since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
                with sqlite3.connect(store.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    for r in conn.execute(
                        "SELECT status, COUNT(*) AS n FROM review_runs "
                        "WHERE started_at >= ? GROUP BY status", (since,),
                    ):
                        g_runs.add_metric([r["status"]], r["n"])
                    row = conn.execute(
                        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM review_runs "
                        "WHERE started_at >= ?", (since,),
                    ).fetchone()
                    g_cost.add_metric([], float(row["c"] or 0))
            except Exception as exc:  # noqa: BLE001
                logger.debug("metrics_runs_failed err=%s", exc)
            yield g_runs
            yield g_cost

    # ValueError == already registered (module re-import in tests).
    with contextlib.suppress(ValueError):
        REGISTRY.register(_OpsCollector())


def _classify(path: str) -> str:
    # Mirror the rate-limiter classes + a few extra read buckets.
    if path.startswith("/oauth"):
        return "oauth"
    if path.startswith("/api/reviews"):
        return "reviews"
    if path.startswith("/mcp"):
        return "mcp"
    if path.startswith("/api/qa") or path.startswith("/api/chats"):
        return "qa"
    if path.startswith("/webhook"):
        return "webhook"
    if path.startswith("/api"):
        return "api"
    return "other"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _PROM_AVAILABLE:
            return await call_next(request)
        path = request.url.path
        if path == "/metrics":
            return await call_next(request)
        t0 = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            cls = _classify(path)
            HTTP_REQUESTS.labels(request.method, cls, status).inc()
            HTTP_LATENCY.labels(request.method, cls).observe(time.perf_counter() - t0)


def metrics_endpoint() -> Response:
    if not _PROM_AVAILABLE:
        return Response("prometheus_client not installed\n", status_code=501)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


__all__ = ["MetricsMiddleware", "metrics_endpoint"]
