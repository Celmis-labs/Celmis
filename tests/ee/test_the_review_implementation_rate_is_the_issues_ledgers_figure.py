# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The review implementation rate comes from the issues ledger, never a tally of its own.

`src.review.issues.implementation_stats` (the issues backlog) owns the rule; the
productivity page reads it through one door. A build that does not have it
yet reports "not available" and the rest of the page is unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.db.models import ReviewIssue
from src.ee.analytics import productivity as m
from src.ee.analytics import productivity_router
from tests.ee.productivity_support import api, merged_pr

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OUTCOMES = ("implemented", "unimplemented", "dismissed", "abandoned")


def ledger_stats(rows) -> dict:
    """The contract of the ledger's function, small enough to restate."""
    counts = dict.fromkeys(OUTCOMES, 0)
    for r in rows:
        o = r["close_outcome"] if isinstance(r, dict) else r.close_outcome
        if o in counts:
            counts[o] += 1
    decided = counts["implemented"] + counts["unimplemented"]
    return {**counts, "implementation_rate": counts["implemented"] / decided if decided else None}


def issue(outcome: str | None, days: float) -> dict:
    return {"close_outcome": outcome, "first_seen_at": NOW - timedelta(days=days)}


def test_the_rate_is_the_ledgers_over_issues_first_seen_in_the_window(monkeypatch) -> None:
    monkeypatch.setattr(m, "implementation_stats_or_none", lambda: ledger_stats)
    rows = [issue("implemented", 1), issue("implemented", 2), issue("unimplemented", 3),
            issue("dismissed", 3), issue(None, 1),
            issue("implemented", 10), issue("unimplemented", 11)]
    out = m.implementation_figures(rows, NOW - timedelta(days=7), NOW, "week",
                                   previous_start=NOW - timedelta(days=14))
    assert out["available"] is True
    assert out["value"] == pytest.approx(2 / 3)
    assert out["previous"] == pytest.approx(0.5)
    assert (out["implemented"], out["unimplemented"], out["dismissed"]) == (2, 1, 1)
    assert out["series"] and all(set(p) == {"date", "rate", "implemented", "unimplemented"} for p in out["series"])


def test_nothing_decided_yet_is_no_rate_not_zero_percent(monkeypatch) -> None:
    monkeypatch.setattr(m, "implementation_stats_or_none", lambda: ledger_stats)
    out = m.implementation_figures([issue(None, 1)], NOW - timedelta(days=7), NOW, "week",
                                   previous_start=NOW - timedelta(days=14))
    assert out["available"] is True and out["value"] is None and out["delta"] is None


def test_without_the_ledgers_function_the_figure_is_not_available(monkeypatch) -> None:
    monkeypatch.setattr(m, "implementation_stats_or_none", lambda: None)
    assert m.implementation_figures([issue("implemented", 1)], NOW - timedelta(days=7), NOW, "week",
                                    previous_start=NOW - timedelta(days=14)) == {"available": False}


def test_the_door_opens_on_the_ledgers_own_function() -> None:
    from src.review.issues import implementation_stats
    assert m.implementation_stats_or_none() is implementation_stats
    assert productivity_router._outcome_column() is ReviewIssue.close_outcome


async def test_a_build_without_the_ledger_column_still_serves_the_page(monkeypatch, tmp_path) -> None:
    """The issues backlog adds `close_outcome`; until it is merged this must not raise."""
    monkeypatch.setattr(productivity_router, "_outcome_column", lambda: None)
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=[merged_pr(1)]) as c:
        r = await c.get("/api/analytics/productivity/overview?days=30")
    assert r.status_code == 200
    assert r.json()["kpis"]["implementation_rate"] == {"available": False}


def _issue_row(i: int, outcome: str, *, repo: str = "acme/shop", days: float = 2, workspace: str = "ws-1") -> dict:
    return {
        "id": f"i{i}", "workspace_id": workspace, "repo_slug": "github_acme-shop", "fingerprint": f"fp{i}",
        "file_path": "a.py", "line": 1, "agent": "defect", "rule_id": "defect.x", "category": "bug",
        "severity": "error", "title": f"Issue {i}", "body": "", "suggestion": None, "status": "open",
        "close_outcome": outcome,
        "resolution_source": None, "pr_provider": "github", "pr_repo": repo, "pr_number": 7, "pr_url": None,
        "first_run_id": "r1", "last_run_id": "r1", "occurrences": 1,
        "first_seen_at": datetime.now(UTC) - timedelta(days=days),
        "last_seen_at": datetime.now(UTC) - timedelta(days=days), "closed_at": None,
    }


async def test_with_the_ledger_the_page_reports_the_rate_for_the_filtered_repositories(monkeypatch, tmp_path) -> None:
    """The real ledger function and the real `close_outcome` column, no stand-ins."""
    issues = [_issue_row(1, "implemented"), _issue_row(2, "implemented"), _issue_row(3, "unimplemented"),
              _issue_row(4, "unimplemented", repo="acme/api"),
              _issue_row(5, "implemented", workspace="ws-2")]
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=[merged_pr(1)], issues=issues) as c:
        everything = (await c.get("/api/analytics/productivity/overview?days=30")).json()
        shop = (await c.get("/api/analytics/productivity/overview?days=30&repo=acme/shop")).json()
    rate = everything["kpis"]["implementation_rate"]
    assert rate["available"] is True and rate["value"] == pytest.approx(0.5), "another workspace's issue leaked in"
    assert shop["kpis"]["implementation_rate"]["value"] == pytest.approx(2 / 3)
