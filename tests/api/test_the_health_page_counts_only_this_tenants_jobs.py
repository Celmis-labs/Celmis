"""One workspace's health page showed the whole installation's queue.

    from src.sync.queue import stats
    s = stats()

`stats()` with no argument counts every row in `sync_jobs`, and this card is
rendered on a tenant's own health page. So an operator read another tenant's
backlog, and a workspace with an empty queue saw its queue card go "degraded"
because somebody else had a dead job.

`stats` has taken a `workspace_id` since it was written, and its docstring
gives the reason: "the tiles above the list must not count rows the list
refuses to show, or the page lies." This was the caller that did not pass it.

The Prometheus collector in api/metrics.py calls it unscoped and stays that
way — that surface is scraped by whoever runs the box, and installation-wide
is what it is for.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _Session:
    """Enough of an AsyncSession to reach the queue card.

    The MCP and notification blocks above it are wrapped in try/except and
    degrade to no cards; raising here exercises that rather than pulling in
    Postgres for a test about an argument.
    """

    async def scalars(self, *_a, **_kw):
        raise RuntimeError("no database in this test")


def _cards(monkeypatch, *, seen: list):
    import src.sync.queue as queue_mod
    from src.api.routers import search as search_router
    monkeypatch.setattr(queue_mod, "stats",
                        lambda **kw: (seen.append(kw), {"pending": 2})[1])

    user = SimpleNamespace(id="u-1", email="lead@acme.example")
    out = asyncio.run(search_router.integrations_health(
        session=_Session(), user=user, ws_id="ws-tenant-a"))
    return out["cards"]


def test_the_queue_card_asks_for_this_workspace(monkeypatch):
    seen: list = []
    _cards(monkeypatch, seen=seen)
    assert seen, "the queue card never called stats()"
    assert seen[0].get("workspace_id") == "ws-tenant-a", (
        f"stats called with {seen[0]} — an unscoped call counts every tenant"
    )


def test_the_card_still_renders(monkeypatch):
    """Scoping it must not turn the card off."""
    cards = _cards(monkeypatch, seen=[])
    queue = [c for c in cards if c.get("name") == "sync_jobs"]
    assert queue, f"no queue card among {[c.get('name') for c in cards]}"
    assert "pending=2" in queue[0]["detail"]


def test_another_tenants_dead_job_does_not_degrade_this_one(monkeypatch):
    """The visible symptom: `dead` came from the installation, so a workspace
    with nothing wrong displayed "degraded"."""
    import src.sync.queue as queue_mod
    from src.api.routers import search as search_router

    def _stats(**kw):
        # Tenant A is clean; the installation is not.
        return {"pending": 1} if kw.get("workspace_id") else {"dead": 9}

    monkeypatch.setattr(queue_mod, "stats", _stats)
    user = SimpleNamespace(id="u-1", email="lead@acme.example")
    out = asyncio.run(search_router.integrations_health(
        session=_Session(), user=user, ws_id="ws-tenant-a"))
    queue = [c for c in out["cards"] if c.get("name") == "sync_jobs"][0]
    assert queue["status"] == "healthy", queue


def test_the_prometheus_collector_is_deliberately_installation_wide():
    """Not an oversight, and the difference is the point: /metrics is scraped
    by whoever runs the box."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/api/metrics.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "stats"]
    assert calls, "metrics.py no longer collects queue stats"
    assert all(not c.keywords for c in calls)
