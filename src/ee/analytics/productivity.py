# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Productivity metrics — cycle time, DORA, PR size, activity, as pure functions.

The sync engine (src/productivity, AGPL) keeps normalised pull requests,
review events and deployments; the router reads them and hands them in here as
plain dicts, so the arithmetic is testable without a database — the same split
as `aggregate.py`, whose `percentile` this reuses.

Definitions (each is shown to the reader as a tooltip, so they are written
down once, here):

  coding   = created_at − first_commit_at                      (clamped ≥ 0)
  pickup   = first human review − created_at                   (clamped ≥ 0)
  review   = merged_at − first human review                    (clamped ≥ 0)
  cycle    = merged_at − first_commit_at (created_at when the first commit
             is unknown); only for a PR whose detail has been read, since an
             unread PR's merge time can be the list's last-update time

A "human review" is the first comment, approval or review by somebody who is
neither the author nor a bot; the sync decides that and stores it as
`first_review_at`. A PR merged with no such act BEFORE the merge is counted
as `merged_without_review` and left out of pickup and review. A PR whose
detail has not been read yet (`detail_state == "none"`) is counted as
`not_enriched` and left out of everything that needs the detail — an unread
PR is unknown, not unreviewed.

Cohorts: cycle metrics use the PRs MERGED in the window, "opened" uses the
PRs CREATED in it, lead time uses the PRs DEPLOYED to production in it.
Medians with p75 and p90 are reported, never means. Every headline figure
also carries the same figure for the previous equal period.

Change failure rate counts SETTLED deployments only (older than the
repository's failure window): a deployment still inside the window can fail
tomorrow, so it is in neither the numerator nor the denominator.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.ee.analytics.aggregate import _parse_ts, percentile

ALLOWED_WINDOWS = (15, 30, 90, 180)
DEFAULT_WINDOW = 30
BUCKETS = ("week", "day")
SORTS = ("cycle", "size", "pickup", "review")

#: Upper bounds in changed lines (additions + deletions); the last is open.
SIZE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("xs", 10), ("s", 50), ("m", 250), ("l", 1000), ("xl", None),
)
#: A per-person median needs at least this many merged PRs to mean anything.
MIN_SAMPLE = 3
#: What the overview lists as a deployment timeline, newest kept.
MAX_TIMELINE = 400

_BUGFIX_KINDS = frozenset({"bugfix", "hotfix", "revert"})
_DAY = 86400.0


# ─── small helpers ───────────────────────────────────────────────────

def _secs(later: datetime | None, earlier: datetime | None) -> float | None:
    """Seconds from `earlier` to `later`, clamped at zero; None when either is unknown."""
    if later is None or earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds())


def _stats(values: Iterable[float]) -> dict[str, Any]:
    vals = list(values)
    return {
        "n": len(vals),
        "p50": percentile(vals, 50), "p75": percentile(vals, 75), "p90": percentile(vals, 90),
    }


def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


def _delta(current: float | None, previous: float | None) -> float | None:
    """Relative change against the previous period; None when it cannot be said."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def _figure(current: float | None, previous: float | None, **extra: Any) -> dict[str, Any]:
    return {"value": current, "previous": previous, "delta": _delta(current, previous), **extra}


def size_bucket(lines: int) -> str:
    """`xs` ≤ 10, `s` ≤ 50, `m` ≤ 250, `l` ≤ 1000, `xl` above."""
    for name, limit in SIZE_BUCKETS:
        if limit is None or lines <= limit:
            return name
    return SIZE_BUCKETS[-1][0]


def lines_changed(pr: dict[str, Any]) -> int | None:
    """Additions + deletions; None when the diffstat has not been read."""
    if pr.get("additions") is None and pr.get("deletions") is None:
        return None
    return int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)


# ─── DORA bands ──────────────────────────────────────────────────────
#
# The published DORA tiers, simplified to one threshold per step. They are
# shown beside the figure as orientation, not as a verdict.

def band_deploy_frequency(per_day: float | None) -> str | None:
    if per_day is None:
        return None
    if per_day >= 1:
        return "elite"
    if per_day >= 1 / 7:
        return "high"
    if per_day >= 1 / 30:
        return "medium"
    return "low"


def band_lead_time(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < _DAY:
        return "elite"
    if seconds < 7 * _DAY:
        return "high"
    if seconds < 30 * _DAY:
        return "medium"
    return "low"


def band_failure_rate(rate: float | None) -> str | None:
    if rate is None:
        return None
    if rate <= 0.05:
        return "elite"
    if rate <= 0.10:
        return "high"
    if rate <= 0.15:
        return "medium"
    return "low"


def band_recovery(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 3600:
        return "elite"
    if seconds < _DAY:
        return "high"
    if seconds < 7 * _DAY:
        return "medium"
    return "low"


# ─── one pull request ────────────────────────────────────────────────

def _enriched(pr: dict[str, Any]) -> bool:
    return pr.get("detail_state") in ("full", "partial")


def cycle_parts(pr: dict[str, Any]) -> dict[str, Any]:
    """The cycle-time breakdown of ONE merged pull request, in seconds.

    `coding` needs the first commit, `pickup` and `review` need a human review
    that happened before the merge, `cycle` needs a merge and a read detail. A key whose
    inputs are unknown is None, never zero.
    """
    created = _parse_ts(pr.get("created_at"))
    first_commit = _parse_ts(pr.get("first_commit_at"))
    merged = _parse_ts(pr.get("merged_at"))
    review = _parse_ts(pr.get("first_review_at"))
    if review is not None and merged is not None and review > merged:
        review = None  # a comment after the merge reviewed nothing
    known = _enriched(pr)
    return {
        "coding": _secs(created, first_commit),
        "pickup": _secs(review, created) if known else None,
        "review": _secs(merged, review) if known else None,
        # An unread PR's merge time may be the list's last-update time, and its
        # first commit is unknown: its cycle would be another definition.
        "cycle": _secs(merged, first_commit or created) if known else None,
        "reviewed": bool(known and review is not None),
        "known": known,
    }


# ─── cohorts ─────────────────────────────────────────────────────────

def _in(value: Any, start: datetime, end: datetime) -> bool:
    ts = _parse_ts(value)
    return ts is not None and start <= ts < end


def merged_in(prs: Iterable[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [p for p in prs if p.get("state") == "merged" and _in(p.get("merged_at"), start, end)]


def deployments_in(
    deployments: Iterable[dict[str, Any]], start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    return [d for d in deployments if _in(d.get("deployed_at"), start, end)]


def _settled(deployment: dict[str, Any], now: datetime) -> bool:
    at = _parse_ts(deployment.get("deployed_at"))
    if at is None:
        return False
    return at <= now - timedelta(days=int(deployment.get("settle_days") or 7))


def window_figures(
    prs: Sequence[dict[str, Any]], deployments: Sequence[dict[str, Any]],
    start: datetime, end: datetime, *, now: datetime,
) -> dict[str, Any]:
    """Every scalar of the overview for ONE period, so the previous period is the same call."""
    merged = merged_in(prs, start, end)
    parts = [cycle_parts(p) for p in merged]
    cycle = [x["cycle"] for x in parts if x["cycle"] is not None]
    coding = [x["coding"] for x in parts if x["coding"] is not None]
    pickup = [x["pickup"] for x in parts if x["pickup"] is not None and x["reviewed"]]
    review = [x["review"] for x in parts if x["review"] is not None and x["reviewed"]]

    sizes = [n for n in (lines_changed(p) for p in merged if _enriched(p)) if n is not None]

    # Lead time: PRs that reached production in the window.
    deployed = [p for p in prs if p.get("state") == "merged" and _in(p.get("prod_deployed_at"), start, end)]
    lead = []
    for p in deployed:
        began = _parse_ts(p.get("first_commit_at")) or _parse_ts(p.get("created_at"))
        secs = _secs(_parse_ts(p.get("prod_deployed_at")), began)
        if secs is not None:
            lead.append(secs)
    undeployed = [p for p in merged if _parse_ts(p.get("prod_deployed_at")) is None]

    deps = deployments_in(deployments, start, end)
    settled = [d for d in deps if _settled(d, now)]
    failed = [d for d in settled if d.get("is_failure")]
    recovery = []
    for d in deps:
        if d.get("is_failure"):
            secs = _secs(_parse_ts(d.get("recovered_at")), _parse_ts(d.get("deployed_at")))
            if secs is not None:
                recovery.append(secs)

    days = max(1.0, (end - start).total_seconds() / _DAY)
    bugs = sum(1 for p in merged if p.get("kind") in _BUGFIX_KINDS)
    return {
        "merged": len(merged),
        "opened": sum(1 for p in prs if _in(p.get("created_at"), start, end)),
        "declined": sum(1 for p in prs if p.get("state") == "declined"
                        and _in(p.get("closed_at"), start, end)),
        "cycle": _stats(cycle), "coding": _stats(coding),
        "pickup": _stats(pickup), "review": _stats(review),
        "merged_without_review": sum(1 for x in parts if x["known"] and not x["reviewed"]),
        "not_enriched": sum(1 for x in parts if not x["known"]),
        "size": {**_stats(sizes)},
        "lead_time": _stats(lead),
        "undeployed_merged": len(undeployed),
        "deploys": len(deps),
        "deploys_per_day": len(deps) / days,
        "deploys_per_week": len(deps) / days * 7,
        "failed_deploys": len(failed),
        "settled_deploys": len(settled),
        "unsettled_deploys": len(deps) - len(settled),
        "change_failure_rate": _ratio(len(failed), len(settled)),
        "recovery": _stats(recovery),
        "bug_ratio": _ratio(bugs, len(merged)),
    }


# ─── series ──────────────────────────────────────────────────────────

def bucket_start(value: datetime, bucket: str) -> date:
    """The day a bucket starts on: the day itself, or the Monday of its ISO week."""
    d = value.astimezone(UTC).date()
    return d - timedelta(days=d.weekday()) if bucket == "week" else d


def bucket_starts(start: datetime, end: datetime, bucket: str) -> list[date]:
    """Every bucket from `start` to `end`, empty ones included."""
    step = timedelta(days=7 if bucket == "week" else 1)
    out: list[date] = []
    cur = bucket_start(start, bucket)
    last = bucket_start(end, bucket)
    while cur <= last:
        out.append(cur)
        cur += step
    return out


def _group(rows: Iterable[dict[str, Any]], field: str, bucket: str) -> dict[date, list[dict[str, Any]]]:
    out: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        ts = _parse_ts(r.get(field))
        if ts is not None:
            out[bucket_start(ts, bucket)].append(r)
    return out


def cycle_series(
    merged: Sequence[dict[str, Any]], start: datetime, end: datetime, bucket: str,
) -> list[dict[str, Any]]:
    """Median coding, pickup and review per bucket (by merge time), for the stacked chart."""
    by = _group(merged, "merged_at", bucket)
    out = []
    for day in bucket_starts(start, end, bucket):
        parts = [cycle_parts(p) for p in by.get(day, [])]
        reviewed = [x for x in parts if x["reviewed"]]
        out.append({
            "date": day.isoformat(), "n": len(parts),
            "coding": percentile([x["coding"] for x in parts if x["coding"] is not None], 50),
            "pickup": percentile([x["pickup"] for x in reviewed if x["pickup"] is not None], 50),
            "review": percentile([x["review"] for x in reviewed if x["review"] is not None], 50),
        })
    return out


def throughput_series(
    prs: Sequence[dict[str, Any]], start: datetime, end: datetime, bucket: str,
) -> list[dict[str, Any]]:
    created = _group([p for p in prs if _in(p.get("created_at"), start, end)], "created_at", bucket)
    merged = _group(merged_in(prs, start, end), "merged_at", bucket)
    declined = _group(
        [p for p in prs if p.get("state") == "declined" and _in(p.get("closed_at"), start, end)],
        "closed_at", bucket)
    return [
        {"date": day.isoformat(), "opened": len(created.get(day, [])),
         "merged": len(merged.get(day, [])), "declined": len(declined.get(day, []))}
        for day in bucket_starts(start, end, bucket)
    ]


def size_histogram(merged: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merged PRs by size bucket, with the median cycle time of each."""
    by: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SIZE_BUCKETS}
    for p in merged:
        n = lines_changed(p) if _enriched(p) else None
        if n is not None:
            by[size_bucket(n)].append(p)
    out = []
    for name, limit in SIZE_BUCKETS:
        cycles = [c for c in (cycle_parts(p)["cycle"] for p in by[name]) if c is not None]
        out.append({"bucket": name, "max_lines": limit, "count": len(by[name]),
                    "cycle_p50": percentile(cycles, 50)})
    return out


def deployment_timeline(
    deployments: Sequence[dict[str, Any]], *, now: datetime,
) -> list[dict[str, Any]]:
    """The newest `MAX_TIMELINE` deployments, oldest first, failures flagged."""
    ordered = sorted(deployments, key=lambda d: _parse_ts(d.get("deployed_at")) or now)
    return [
        {"id": d.get("id"), "repo": d.get("repo"),
         "at": _parse_ts(d.get("deployed_at")).isoformat() if _parse_ts(d.get("deployed_at")) else None,
         "failed": bool(d.get("is_failure")), "settled": _settled(d, now),
         "source": d.get("source")}
        for d in ordered[-MAX_TIMELINE:]
    ]


# ─── implementation rate, from the issues ledger ─────────────────────

def implementation_stats_or_none() -> Callable[[Iterable[Any]], dict[str, Any]] | None:
    """The ledger's own implementation rate (`src.review.issues.implementation_stats`).

    That function belongs to the issues backlog; this reads it through one
    door so a build without it reports "not available" instead of failing.
    """
    try:
        from src.review.issues import implementation_stats
    except ImportError:
        return None
    return implementation_stats


def implementation_figures(
    rows: Sequence[dict[str, Any]], start: datetime, end: datetime, bucket: str,
    *, previous_start: datetime,
) -> dict[str, Any]:
    """The share of review suggestions the team took, with the previous period and a series.

    `rows` carry `close_outcome` and `first_seen_at`. The arithmetic is the
    ledger's, not repeated here: an issue counts once its PR has merged or
    closed, so recent issues are not in it yet.
    """
    stats = implementation_stats_or_none()
    if stats is None:
        return {"available": False}
    cur = [r for r in rows if _in(r.get("first_seen_at"), start, end)]
    prev = [r for r in rows if _in(r.get("first_seen_at"), previous_start, start)]
    now_stats, before = stats(cur), stats(prev)
    by = _group(cur, "first_seen_at", bucket)
    series = []
    for day in bucket_starts(start, end, bucket):
        s = stats(by.get(day, []))
        series.append({"date": day.isoformat(), "rate": s.get("implementation_rate"),
                       "implemented": s.get("implemented", 0),
                       "unimplemented": s.get("unimplemented", 0)})
    return {
        "available": True,
        **_figure(now_stats.get("implementation_rate"), before.get("implementation_rate")),
        "implemented": now_stats.get("implemented", 0),
        "unimplemented": now_stats.get("unimplemented", 0),
        "dismissed": now_stats.get("dismissed", 0),
        "abandoned": now_stats.get("abandoned", 0),
        "series": series,
    }


# ─── the overview ────────────────────────────────────────────────────

def overview(
    prs: Sequence[dict[str, Any]], deployments: Sequence[dict[str, Any]], *,
    days: int, bucket: str = "week", now: datetime | None = None,
    implementation: dict[str, Any] | None = None,
    covered_from: datetime | None = None,
) -> dict[str, Any]:
    """Everything /productivity draws, for the window ending now.

    `prs` and `deployments` span this period AND the previous one (the router
    reads both); the cohorts below pick their own rows. `covered_from` is the
    date the synced history starts at; when the previous period begins before
    it, `previous_incomplete` says its figures (and every delta against them)
    rest on partial history.
    """
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    cur = window_figures(prs, deployments, start, now, now=now)
    prev = window_figures(prs, deployments, prev_start, start, now=now)
    merged = merged_in(prs, start, now)

    def stat(key: str, field: str = "p50") -> dict[str, Any]:
        return _figure(cur[key][field], prev[key][field], n=cur[key]["n"],
                       p75=cur[key]["p75"], p90=cur[key]["p90"])

    freq = _figure(cur["deploys_per_week"], prev["deploys_per_week"], total=cur["deploys"],
                   per_day=cur["deploys_per_day"],
                   band=band_deploy_frequency(cur["deploys_per_day"]))
    lead = {**stat("lead_time"), "band": band_lead_time(cur["lead_time"]["p50"]),
            "undeployed_merged": cur["undeployed_merged"]}
    cfr = _figure(cur["change_failure_rate"], prev["change_failure_rate"],
                  failed=cur["failed_deploys"], settled=cur["settled_deploys"],
                  unsettled=cur["unsettled_deploys"],
                  band=band_failure_rate(cur["change_failure_rate"]))
    mttr = {**stat("recovery"), "band": band_recovery(cur["recovery"]["p50"])}
    return {
        "days": days, "bucket": bucket,
        "from": start.isoformat(), "to": now.isoformat(),
        "previous_incomplete": covered_from is not None and covered_from > prev_start,
        "covered_from": covered_from.isoformat() if covered_from is not None else None,
        "kpis": {
            "cycle_time": stat("cycle"),
            "lead_time": lead,
            "deploy_frequency": freq,
            "change_failure_rate": cfr,
            "time_to_recover": mttr,
            "merged_prs": _figure(cur["merged"], prev["merged"]),
            "opened_prs": _figure(cur["opened"], prev["opened"]),
            "pr_size": stat("size"),
            "bug_ratio": _figure(cur["bug_ratio"], prev["bug_ratio"]),
            "implementation_rate": implementation or {"available": False},
        },
        "breakdown": {
            "coding": stat("coding"), "pickup": stat("pickup"), "review": stat("review"),
            "merged_without_review": cur["merged_without_review"],
            "not_enriched": cur["not_enriched"],
        },
        "series": {
            "cycle": cycle_series(merged, start, now, bucket),
            "throughput": throughput_series(prs, start, now, bucket),
        },
        "size_histogram": size_histogram(merged),
        "deployments": deployment_timeline(deployments_in(deployments, start, now), now=now),
    }


# ─── drill-down and people ───────────────────────────────────────────

def pr_rows(
    prs: Sequence[dict[str, Any]], *, days: int, sort: str = "cycle", limit: int = 50,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The merged PRs of the window, slowest (or largest) first, with the breakdown."""
    now = now or datetime.now(UTC)
    rows = []
    for p in merged_in(prs, now - timedelta(days=days), now):
        parts = cycle_parts(p)
        rows.append({
            "provider": p.get("provider"), "repo": p.get("repo"), "number": p.get("number"),
            "title": p.get("title") or "", "url": p.get("url"),
            "author": p.get("author_name") or p.get("author_key"),
            "target_branch": p.get("target_branch"),
            "created_at": _parse_ts(p.get("created_at")).isoformat() if p.get("created_at") else None,
            "merged_at": _parse_ts(p.get("merged_at")).isoformat() if p.get("merged_at") else None,
            "coding": parts["coding"], "pickup": parts["pickup"], "review": parts["review"],
            "cycle": parts["cycle"], "reviewed": parts["reviewed"],
            "lines": lines_changed(p), "files": p.get("files_changed"),
            "kind": p.get("kind"), "deploy_link": p.get("deploy_link"),
        })
    key = {"cycle": "cycle", "size": "lines", "pickup": "pickup", "review": "review"}[sort]
    rows.sort(key=lambda r: (r[key] is None, -(r[key] or 0)))
    return rows[: max(1, limit)]


def developers(
    prs: Sequence[dict[str, Any]], events: Sequence[dict[str, Any]], *, days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per person: what they merged and what they reviewed in the window.

    `events` are the human, non-author acts of the window (`actor_key`,
    `actor_name`, `kind`, `pr_id`). A median cycle time needs `MIN_SAMPLE`
    merged PRs; below that it is None and `small_sample` says why.
    """
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    people: dict[str, dict[str, Any]] = {}

    def person(key: str, name: str | None) -> dict[str, Any]:
        row = people.setdefault(key, {
            "key": key, "name": name or key, "prs_merged": 0, "lines": 0,
            "_cycles": [], "_reviewed": set(), "comments_given": 0,
        })
        if name and row["name"] == key:
            row["name"] = name
        return row

    for p in merged_in(prs, start, now):
        key = p.get("author_key")
        if not key:
            continue
        row = person(key, p.get("author_name"))
        row["prs_merged"] += 1
        row["lines"] += lines_changed(p) or 0
        cycle = cycle_parts(p)["cycle"]
        if cycle is not None:
            row["_cycles"].append(cycle)
    for e in events:
        key = e.get("actor_key")
        if not key or e.get("is_bot") or e.get("is_author") or not _in(e.get("at"), start, now):
            continue
        row = person(key, e.get("actor_name"))
        row["_reviewed"].add(e.get("pr_id"))
        if e.get("kind") == "comment":
            row["comments_given"] += 1

    out = []
    for row in people.values():
        cycles = row.pop("_cycles")
        row["reviews_given"] = len(row.pop("_reviewed"))
        row["small_sample"] = row["prs_merged"] < MIN_SAMPLE
        row["cycle_p50"] = None if row["small_sample"] else percentile(cycles, 50)
        out.append(row)
    out.sort(key=lambda r: (-r["prs_merged"], -r["reviews_given"], r["name"].lower()))
    return out
