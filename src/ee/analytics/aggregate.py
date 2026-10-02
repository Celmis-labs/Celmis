# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Review analytics — the numbers behind /analytics, as pure functions.

Two sources, read by the router and handed in here as plain dicts so the
arithmetic can be tested without either database:

  - runs   — the SQLite `review_runs` rows of the workspace in the window
             (status, elapsed_seconds, cost_usd, severity counts, started_at);
  - issues — the Postgres `review_issues` rows touched in the window, each
             carrying the state of its pull request (`pr_state`).

Cost is `review_runs.cost_usd`, not the `llm_spend` ledger: the ledger is
tagged surface="review" only by the API agents and the verifier, so a
workspace on the Claude Code engine — which bills per session and records its
cost on the run — would read as free there. A run whose cost is unknown (a
model with no price) is NULL on the row; it is counted in
`runs_with_unknown_cost` and left out of the average rather than read as $0.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

ALLOWED_WINDOWS = (7, 30, 90)
SEVERITIES = ("critical", "error", "warning", "info")
CATEGORIES = ("bug", "security", "performance", "maintainability", "style", "other")

#: Run statuses that mean "a review happened" — the agents read the diff.
_REVIEWED = ("complete", "partial")


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _days(since: datetime, now: datetime) -> list[date]:
    start = since.date()
    n = (now.date() - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def summarize(
    runs: Iterable[dict[str, Any]],
    issues: Iterable[dict[str, Any]],
    *,
    days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    runs = [r for r in runs if (_parse_ts(r.get("started_at")) or now) >= since]
    issues = list(issues)

    finished = [r for r in runs if r.get("status") not in ("queued", "running")]
    status_counts = Counter(str(r.get("status") or "unknown") for r in finished)

    times = [
        float(r["elapsed_seconds"]) for r in finished
        if r.get("status") in _REVIEWED and r.get("elapsed_seconds")
    ]
    costs = [float(r["cost_usd"]) for r in finished if r.get("cost_usd") is not None]
    unknown_cost = sum(
        1 for r in finished
        if r.get("cost_usd") is None and r.get("status") in _REVIEWED
    )

    findings_by_severity = {
        "critical": sum(int(r.get("critical") or 0) for r in finished),
        "error": sum(int(r.get("error_count") or 0) for r in finished),
        "warning": sum(int(r.get("warning") or 0) for r in finished),
        "info": sum(int(r.get("info") or 0) for r in finished),
    }

    def in_window(ts: Any) -> bool:
        dt = _parse_ts(ts)
        return dt is not None and dt >= since

    opened = [i for i in issues if in_window(i.get("first_seen_at"))]
    closed = [i for i in issues
              if i.get("status") != "open" and in_window(i.get("closed_at"))]
    fixed = [i for i in closed if i.get("status") == "fixed"]
    fixed_auto = [i for i in fixed if i.get("resolution_source") == "auto_next_commit"]
    dismissed = [i for i in closed if i.get("status") == "dismissed"]

    # "Found by Celmis and fixed in a later commit" vs "not fixed", over the
    # issues FOUND in the window — so the two add up against the same base.
    opened_fixed_auto = sum(
        1 for i in opened
        if i.get("status") == "fixed"
        and i.get("resolution_source") == "auto_next_commit"
    )
    opened_fixed_any = sum(1 for i in opened if i.get("status") == "fixed")
    open_on_merged = sum(
        1 for i in opened
        if i.get("status") == "open" and i.get("pr_state") == "merged"
    )
    still_open = sum(1 for i in opened if i.get("status") == "open")
    opened_dismissed = sum(1 for i in opened if i.get("status") == "dismissed")
    actionable = len(opened) - opened_dismissed
    fix_rate = round(100.0 * opened_fixed_any / actionable, 1) if actionable else None

    by_category = Counter(str(i.get("category") or "other") for i in opened)

    # Daily series — every day of the window, zeros included, so a chart's
    # x-axis is time and not "days something happened".
    day_list = _days(since, now)
    reviews_by_day: Counter[str] = Counter()
    findings_by_day: Counter[str] = Counter()
    for r in finished:
        dt = _parse_ts(r.get("started_at"))
        if dt is None:
            continue
        key = dt.date().isoformat()
        reviews_by_day[key] += 1
        findings_by_day[key] += int(r.get("findings_count") or 0)
    opened_by_day: Counter[str] = Counter()
    for i in opened:
        dt = _parse_ts(i.get("first_seen_at"))
        if dt is not None:
            opened_by_day[dt.date().isoformat()] += 1
    fixed_by_day: Counter[str] = Counter()
    for i in fixed:
        dt = _parse_ts(i.get("closed_at"))
        if dt is not None:
            fixed_by_day[dt.date().isoformat()] += 1

    total_cost = round(sum(costs), 6)
    return {
        "days": days,
        "since": since.isoformat(),
        "cost_basis": "review_runs.cost_usd",
        "reviews": {
            "total": len(finished),
            "by_status": {k: status_counts.get(k, 0)
                          for k in ("complete", "partial", "skipped", "failed")},
        },
        "review_time_seconds": {
            "avg": round(sum(times) / len(times), 1) if times else None,
            "p50": percentile(times, 50),
            "p90": percentile(times, 90),
            "samples": len(times),
        },
        "cost_usd": {
            "total": total_cost,
            "avg_per_review": round(total_cost / len(costs), 6) if costs else None,
            "runs_with_cost": len(costs),
            "runs_with_unknown_cost": unknown_cost,
        },
        "findings_by_severity": findings_by_severity,
        "issues_by_category": {c: by_category.get(c, 0) for c in CATEGORIES},
        "issues": {
            "opened": len(opened),
            "closed": len(closed),
            "fixed": len(fixed),
            "fixed_auto": len(fixed_auto),
            "dismissed": len(dismissed),
        },
        "outcomes": {
            "found": len(opened),
            "fixed_in_next_commits": opened_fixed_auto,
            "fixed_any": opened_fixed_any,
            "open_on_merged_prs": open_on_merged,
            "still_open": still_open,
            "dismissed": opened_dismissed,
            "fix_rate_pct": fix_rate,
        },
        "daily": [
            {
                "date": d.isoformat(),
                "reviews": reviews_by_day.get(d.isoformat(), 0),
                "findings": findings_by_day.get(d.isoformat(), 0),
                "issues_opened": opened_by_day.get(d.isoformat(), 0),
                "issues_fixed": fixed_by_day.get(d.isoformat(), 0),
            }
            for d in day_list
        ],
    }
