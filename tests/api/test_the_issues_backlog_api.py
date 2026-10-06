"""The issues API after the backlog layer: filters, repeats, summary, recheck.

  * `scope=backlog` is issues of merged PRs, `scope=pr` the others;
    `resolution` and `outcome` filter on how and what became of an issue;
  * a repeat of a backlog issue on another PR is hidden unless asked for and
    shows as a count on the issue it repeats;
  * `/summary` is the one place the implementation rate is read from, over the
    PRs merged in the window, never another workspace's rows;
  * `/recheck` needs member or above and queues one recheck per branch with
    something to watch, inside the caller's workspace.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import issues as issues_router
from src.db.models import ReviewIssue, ReviewIssueRecheckState, ReviewPullRequest
from src.db.session import get_async_session


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


WS = "ws-1"
NOW = datetime.now(UTC)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def _issue(i: str, *, pr=7, status="open", source=None, merged=None, outcome=None,
           dup_of=None, base="main", repo="acme/api", ws=WS, **kw) -> dict:
    return {
        "id": i, "workspace_id": ws, "repo_slug": repo.replace("/", "-"),
        "fingerprint": f"fp{i}", "file_path": f"src/{i}.py", "line": 1,
        "agent": "defect", "rule_id": "defect.x", "category": "bug",
        "severity": "error", "title": f"Issue {i}", "body": "", "suggestion": None,
        "status": status, "resolution_source": source, "pr_provider": "github",
        "pr_repo": repo, "pr_number": pr, "occurrences": 1,
        "first_seen_at": _ago(10), "last_seen_at": _ago(10),
        "closed_at": _ago(1) if status != "open" else None,
        "merged_at": _ago(merged) if merged is not None else None,
        "base_ref": base if merged is not None else None,
        "close_outcome": outcome, "dup_of": dup_of, **kw,
    }


ISSUES = [
    _issue("a", pr=1, merged=5, outcome="unimplemented"),                  # backlog, open
    _issue("b", pr=1, merged=5, outcome="implemented", status="fixed",
           source="auto_at_merge"),
    _issue("c", pr=1, merged=5, outcome="unimplemented", status="fixed",
           source="auto_head_check", fixed_by_pr_number=12,
           fixed_by_pr_url="https://github.com/acme/api/pull/12", resolution_note="removed"),
    _issue("d", pr=1, merged=40, outcome="implemented", status="fixed",
           source="auto_next_commit"),                                     # outside 30 days
    _issue("e", pr=2),                                                     # an open PR's
    _issue("f", pr=3, merged=2, outcome="dismissed", status="dismissed", source="manual"),
    _issue("g", pr=4, dup_of="a"),                                         # a repeat of "a"
    _issue("h", pr=1, merged=5, outcome="unimplemented", repo="acme/web", base="dev"),
    _issue("z", pr=1, merged=5, outcome="implemented", ws="ws-2"),         # another tenant's
]
PRS = [
    {"id": "p12", "workspace_id": WS, "provider": "github", "repo": "acme/api", "number": 12,
     "title": "Fix totals", "state": "merged", "reviews_count": 1,
     "opened_at": _ago(3), "updated_at": _ago(1)},
]


@asynccontextmanager
async def api(*, role="member", monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        for t in (ReviewIssue, ReviewPullRequest, ReviewIssueRecheckState):
            await conn.run_sync(t.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for row in ISSUES:
            s.add(ReviewIssue(**row))
        for row in PRS:
            s.add(ReviewPullRequest(**row))
        s.add(ReviewIssueRecheckState(
            workspace_id=WS, pr_provider="github", pr_repo="acme/api", base_ref="main",
            last_result={"reopened": 2, "resolved": 1}, llm_calls_total=3))
        await s.commit()
    monkeypatch.setattr(deps_module, "workspace_role", lambda uid, ws: role)
    app = FastAPI()
    app.include_router(issues_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u-1", email="u@test", is_admin=False)
    app.dependency_overrides[current_workspace_id] = lambda: WS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await engine.dispose()


def _ids(body) -> set[str]:
    return {i["id"] for i in body["items"]}


async def test_scope_separates_the_backlog_from_prs_still_under_review(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        backlog = (await c.get("/api/issues?scope=backlog")).json()
        assert _ids(backlog) == {"a", "b", "c", "d", "f", "h"}
        under_review = (await c.get("/api/issues?scope=pr")).json()
        assert _ids(under_review) == {"e"}
        assert "z" not in _ids((await c.get("/api/issues")).json())   # another tenant


async def test_resolution_and_outcome_filters_say_how_and_what(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        auto = (await c.get("/api/issues?resolution=auto")).json()
        assert _ids(auto) == {"b", "c", "d"}
        assert _ids((await c.get("/api/issues?resolution=manual")).json()) == {"f"}
        assert _ids((await c.get("/api/issues?resolution=nonsense")).json()) == set()
        assert _ids((await c.get("/api/issues?outcome=implemented")).json()) == {"b", "d"}
        both = (await c.get("/api/issues?outcome=unimplemented,dismissed")).json()
        assert _ids(both) == {"a", "c", "f", "h"}


async def test_a_repeat_is_hidden_until_asked_for_and_counted_on_its_original(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        shown = (await c.get("/api/issues")).json()
        assert "g" not in _ids(shown)
        a = next(i for i in shown["items"] if i["id"] == "a")
        assert a["duplicates_count"] == 1
        assert "g" in _ids((await c.get("/api/issues?include_duplicates=true")).json())
        assert shown["status_counts"]["open"] == 3      # a, e, h — the repeat is not counted twice


async def test_asking_for_one_pr_shows_its_repeats_too(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        one_pr = (await c.get("/api/issues?pr=4")).json()
    assert _ids(one_pr) == {"g"}
    assert one_pr["status_counts"]["open"] == 1


async def test_an_auto_resolved_issue_names_the_pr_that_fixed_it(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        items = {i["id"]: i for i in (await c.get("/api/issues?scope=backlog")).json()["items"]}
    c_ = items["c"]
    assert c_["resolution_kind"] == "auto" and c_["fixed_by_pr_number"] == 12
    assert c_["fixed_by_pr_title"] == "Fix totals" and c_["resolution_note"] == "removed"
    assert c_["close_outcome"] == "unimplemented"       # the fate frozen at the merge
    assert items["f"]["resolution_kind"] == "manual"
    assert items["a"]["resolution_kind"] is None and items["a"]["base_ref"] == "main"


async def test_the_summary_is_the_rate_over_prs_merged_in_the_window(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        s = (await c.get("/api/issues/summary?days=30")).json()
        # b implemented; a, c, h unimplemented; f dismissed; d merged 40 days ago.
        assert (s["implemented"], s["unimplemented"], s["dismissed"]) == (1, 3, 1)
        assert s["implementation_rate"] == 0.25
        assert s["backlog_open"] == 2                    # a and h
        assert s["auto_resolved"] == 3                   # b, c, d: any automatic source
        assert s["reopened"] == 2
        wide = (await c.get("/api/issues/summary?days=90")).json()
        assert wide["implemented"] == 2
        one = (await c.get("/api/issues/summary?repo=acme/web")).json()
        assert (one["unimplemented"], one["implemented"], one["reopened"]) == (1, 0, 0)
        assert one["implementation_rate"] == 0.0


async def test_a_summary_with_nothing_decided_has_no_rate(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        s = (await c.get("/api/issues/summary?repo=nothing/here")).json()
    assert s["implementation_rate"] is None and s["backlog_open"] == 0


async def test_a_viewer_cannot_start_a_recheck_and_a_member_queues_one_per_branch(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr("src.review.issue_resolver.recheck_backlog",
                        lambda *a, **kw: calls.append((a, kw)))
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        assert (await c.post("/api/issues/recheck", json={})).status_code == 403
    async with api(role="member", monkeypatch=monkeypatch) as c:
        r = await c.post("/api/issues/recheck", json={})
        assert r.status_code == 202 and r.json() == {"queued": 2}
        await asyncio.sleep(0.2)
    branches = {(a[0], a[2], a[3]) for a, _kw in calls}
    assert branches == {(WS, "acme/api", "main"), (WS, "acme/web", "dev")}
    assert all(kw["reason"] == "manual" for _a, kw in calls)


async def test_a_manual_recheck_runs_one_branch_at_a_time_with_the_repositorys_owner(monkeypatch) -> None:
    import threading
    import time

    live = {"now": 0, "max": 0}
    lock = threading.Lock()
    seen: list[dict] = []

    def fake(*a, **kw):
        seen.append(kw)
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1

    monkeypatch.setattr("src.review.issue_resolver.recheck_backlog", fake)
    async with api(role="member", monkeypatch=monkeypatch) as c:
        assert (await c.post("/api/issues/recheck", json={})).json() == {"queued": 2}
        await asyncio.sleep(0.4)
    assert len(seen) == 2 and live["max"] == 1
    # no token of the asker's: the provider is the repository's owner's
    assert all("user_id" not in kw for kw in seen)


async def test_a_recheck_can_be_narrowed_to_one_repository(monkeypatch) -> None:
    monkeypatch.setattr("src.review.issue_resolver.recheck_backlog", lambda *a, **kw: None)
    async with api(monkeypatch=monkeypatch) as c:
        assert (await c.post("/api/issues/recheck", json={"repo": "acme/web"})).json() == {"queued": 1}
        assert (await c.post("/api/issues/recheck", json={"repo": "none/x"})).json() == {"queued": 0}
        assert (await c.post("/api/issues/recheck")).status_code == 202


async def test_reopening_an_auto_resolved_issue_clears_what_the_resolver_recorded(monkeypatch) -> None:
    async with api(monkeypatch=monkeypatch) as c:
        r = await c.patch("/api/issues/c", json={"status": "open"})
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["status"] == "open" and body["resolution_kind"] is None
    assert (body["fixed_by_pr_number"], body["fixed_by_pr_url"], body["resolution_note"]) == (
        None, None, None)
    assert body["close_outcome"] == "unimplemented"
