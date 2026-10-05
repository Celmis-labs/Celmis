"""The three cards above the pull-requests list: reviewed today, awaiting
review, needs attention — and the `bucket` filter a click on a card applies.

Each counter's definition is pinned here (see `_classify` in
src/api/routers/pull_requests.py), with the scoping of the list: the active
workspace, the optional repo filter, and only repos the caller may read.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api import deps as deps_module
from src.api.auto_review import AutoReviewStore, RepoConfig
from src.api.deps import current_workspace_id, get_current_user
from src.api.review_runs import ReviewRun, ReviewRunStore
from src.api.routers import pull_requests as prs_router
from src.db.models import (
    RepoReviewPolicy,
    ReviewIssue,
    ReviewPullRequest,
    WorkspaceReviewDefaults,
)
from src.db.session import get_async_session
from src.repos.open_pulls import OpenPull
from tests.api.rbac_world import _sqlite_booleans
from tests.api.test_issues_and_pull_requests_are_scoped import (
    _jsonb_as_json_on_sqlite,  # noqa: F401 — registers the JSONB→JSON compile
)

WS = "ws-1"
NOW = datetime.now(UTC)
TODAY = prs_router._utc_day_start() + timedelta(seconds=1)
YESTERDAY = prs_router._utc_day_start() - timedelta(hours=1)


def _pr(pid, number, *, repo="acme/api", slug="acme-api", state="open",
        status="complete", run=None, base="main", ws=WS, count=1) -> ReviewPullRequest:
    return ReviewPullRequest(
        id=pid, workspace_id=ws, provider="github", repo=repo, repo_slug=slug,
        number=number, title=f"PR {number}", state=state, reviews_count=count,
        last_review_status=status, last_run_id=run, base_ref=base,
        opened_at=NOW, updated_at=NOW)


def _run(store, rid, number, *, when=TODAY, status="complete", verdict="comment",
         repo="acme/api", ws=WS):
    store.insert(ReviewRun(
        id=rid, user_id="u-1", pr_ref=f"github:{repo}#{number}", workspace_id=ws,
        status=status, verdict=verdict, started_at=when.isoformat(),
        pr_provider="github", pr_repo=repo, pr_number=number))


def _issue(i, number, *, severity="error", status="open", repo="acme/api"):
    return ReviewIssue(
        id=f"i{i}", workspace_id=WS, repo_slug="github_acme-api",
        fingerprint=f"fp{i}", file_path="a.py", severity=severity, title="t",
        status=status, pr_provider="github", pr_repo=repo, pr_number=number,
        first_seen_at=NOW, last_seen_at=NOW)


@asynccontextmanager
async def api(tmp_path, monkeypatch, *, rows, runs=(), issues=(), policies=(),
              ws_default=None, enabled=True, denied=(), listings=None,
              ws_drafts=None):
    store = ReviewRunStore(tmp_path / "runs.db")
    monkeypatch.setattr("src.api.review_runs._default_store", store)
    for r in runs:
        _run(store, **r)

    registry = AutoReviewStore(tmp_path / "auto.db")
    for slug, full in (("acme-api", "acme/api"), ("acme-web", "acme/web"),
                       ("secret", "acme/secret")):
        registry.upsert(RepoConfig(
            user_id="o@x.io", repo_slug=slug, provider="github", full_name=full,
            url=f"https://github.com/{full}", workspace_id=WS, enabled=enabled,
            mode="webhook"))
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: registry)

    # Provider listings: full_name -> list[OpenPull] | Exception | "hang".
    from src.api.routers import repos as repos_router

    monkeypatch.setattr(repos_router, "_repo_credential",
                        lambda cfg, user: ("tok", "", None))

    def _listing(cfg, secret, email, *, branch, refresh=False, gitlab=None):
        got = (listings or {}).get(cfg.full_name, [])
        if got == "hang":
            import time
            time.sleep(1.0)
            got = []
        if isinstance(got, Exception):
            raise got
        return SimpleNamespace(items=tuple(got), truncated=False)

    monkeypatch.setattr(repos_router, "_open_listing", _listing)
    monkeypatch.setattr(prs_router, "AWAITING_BUDGET_S", 0.2)

    async def _enforce(slug, user, min_perm="read", workspace_id=None):
        if slug in denied:
            raise HTTPException(403, "no")

    monkeypatch.setattr(deps_module, "enforce_repo_permission", _enforce)

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    async with engine.begin() as conn:
        for t in (ReviewIssue, ReviewPullRequest, RepoReviewPolicy,
                  WorkspaceReviewDefaults):
            await conn.run_sync(t.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(rows)
        s.add_all(issues)
        s.add_all([RepoReviewPolicy(repo_slug=slug, target_branches=tb, run_on_drafts=rd)
                   for slug, tb, rd in policies])
        if ws_default is not None or ws_drafts is not None:
            s.add(WorkspaceReviewDefaults(workspace_id=WS, target_branches=ws_default,
                                          run_on_drafts=ws_drafts))
        await s.commit()

    app = FastAPI()
    app.include_router(prs_router.router)

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


async def _stats(c, repo=None):
    r = await c.get("/api/pull-requests/stats", params={"repo": repo} if repo else {})
    assert r.status_code == 200, r.text
    b = r.json()
    return b["reviewed_today"], b["awaiting"], b["attention"]


def _op(number, *, base="main", sha="s1", draft=False, title=None):
    return OpenPull(number=number, title=title or f"Open {number}", author="dana",
                    url=f"https://x/{number}", source_branch="feat", target_branch=base,
                    created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
                    draft=draft, head_sha=sha)


async def test_reviewed_today_counts_prs_whose_latest_review_completed_today(
    tmp_path, monkeypatch,
):
    rows = [
        _pr("a", 1, run="ra"),                        # complete today
        _pr("b", 2, run="rb", status="partial"),      # partial today counts
        _pr("c", 3, run="rc"),                        # complete yesterday
        _pr("d", 4, run="rd", status="skipped"),      # skipped today: not reviewed
        _pr("e", 5, run="re", state="merged"),        # merged after review: counts
        _pr("f", 6, run="rf", ws="ws-2"),             # another workspace
    ]
    runs = [dict(rid="ra", number=1), dict(rid="rb", number=2, status="partial"),
            dict(rid="rc", number=3, when=YESTERDAY),
            dict(rid="rd", number=4, status="skipped"), dict(rid="re", number=5)]
    async with api(tmp_path, monkeypatch, rows=rows, runs=runs) as c:
        assert await _stats(c) == (3, 0, 0)
        listed = (await c.get("/api/pull-requests?bucket=reviewed_today")).json()
        assert sorted(i["number"] for i in listed["items"]) == [1, 2, 5]


async def test_a_close_webhook_today_is_not_a_review_today(tmp_path, monkeypatch):
    # updated_at is bumped by a close webhook; the run says the review was old.
    rows = [_pr("a", 1, run="ra", state="merged")]
    runs = [dict(rid="ra", number=1, when=YESTERDAY)]
    async with api(tmp_path, monkeypatch, rows=rows, runs=runs) as c:
        assert (await _stats(c))[0] == 0


def _nums(body):
    return sorted(i["number"] for i in body["items"])


async def test_awaiting_counts_never_reviewed_open_prs_even_in_manual_mode(
    tmp_path, monkeypatch,
):
    # No DB rows at all, auto-review off: the provider listing is the source.
    listings = {"acme/api": [_op(1), _op(2)]}
    async with api(tmp_path, monkeypatch, rows=[], enabled=False,
                   listings=listings) as c:
        assert (await _stats(c)) == (0, 2, 0)
        body = (await c.get("/api/pull-requests?bucket=awaiting")).json()
        assert _nums(body) == [1, 2] and body["total"] == 2
        first = body["items"][0]
        assert first["last_review_status"] == "awaiting" and first["state"] == "open"
        assert first["id"].startswith("awaiting:github:acme/api#")
        assert first["base_ref"] == "main" and first["repo"] == "acme/api"


async def test_awaiting_compares_the_head_sha_of_the_last_completed_review(
    tmp_path, monkeypatch,
):
    def row(pid, n, sha, status="complete"):
        r = _pr(pid, n, status=status, run=None)
        r.head_sha = sha
        return r

    rows = [row("a", 1, "s1"),              # reviewed at the current head
            row("b", 2, "old"),             # pushed since: awaiting again
            row("c", 3, None, "failed"),    # never completed
            row("d", 4, "s1", "skipped")]   # skipped at that head: not a review
    listings = {"acme/api": [_op(1), _op(2), _op(3), _op(4)]}
    async with api(tmp_path, monkeypatch, rows=rows, listings=listings) as c:
        assert (await _stats(c))[1] == 3
        body = (await c.get("/api/pull-requests?bucket=awaiting")).json()
        assert _nums(body) == [2, 3, 4]
        # a PR with a row keeps its id, so its reviews stay reachable
        assert {i["number"]: i["id"] for i in body["items"]}[2] == "b"


async def test_awaiting_without_a_provider_sha_falls_back_to_any_completed_review(
    tmp_path, monkeypatch,
):
    rows = [_pr("a", 1)]
    listings = {"acme/api": [_op(1, sha=None), _op(2, sha=None)]}
    async with api(tmp_path, monkeypatch, rows=rows, listings=listings) as c:
        assert (await _stats(c))[1] == 1


async def test_awaiting_skips_drafts_unless_the_repo_reviews_them(tmp_path, monkeypatch):
    listings = {"acme/api": [_op(1, draft=True), _op(2)]}
    async with api(tmp_path, monkeypatch, rows=[], listings=listings) as c:
        assert (await _stats(c))[1] == 1
    async with api(tmp_path, monkeypatch, rows=[], listings=listings,
                   ws_drafts=True) as c:
        assert (await _stats(c))[1] == 2
    async with api(tmp_path, monkeypatch, rows=[], listings=listings, ws_drafts=True,
                   policies=[("acme-api", None, False)]) as c:
        assert (await _stats(c))[1] == 1  # the repo policy beats the workspace


async def test_awaiting_leaves_out_prs_on_untargeted_base_branches(tmp_path, monkeypatch):
    listings = {"acme/api": [_op(1, base="main"), _op(2, base="develop"),
                             _op(3, base="release/2")]}
    async with api(tmp_path, monkeypatch, rows=[], listings=listings,
                   ws_default=["main", "release/*"]) as c:
        assert _nums((await c.get("/api/pull-requests?bucket=awaiting")).json()) == [1, 3]
    async with api(tmp_path, monkeypatch, rows=[], listings=listings,
                   ws_default=["main"], policies=[("acme-api", ["develop"], None)]) as c:
        assert _nums((await c.get("/api/pull-requests?bucket=awaiting")).json()) == [2]
    async with api(tmp_path, monkeypatch, rows=[], listings=listings) as c:
        assert (await _stats(c))[1] == 3  # no patterns: every branch


async def test_a_failing_or_slow_listing_makes_the_count_partial(tmp_path, monkeypatch):
    listings = {"acme/api": [_op(1)], "acme/web": RuntimeError("boom"),
                "acme/secret": "hang"}
    async with api(tmp_path, monkeypatch, rows=[], listings=listings) as c:
        b = (await c.get("/api/pull-requests/stats")).json()
        assert b["awaiting"] == 1 and b["partial"] is True
        assert b["missing_repos"] == ["acme/secret", "acme/web"]
    async with api(tmp_path, monkeypatch, rows=[], listings={"acme/api": [_op(1)]}) as c:
        b = (await c.get("/api/pull-requests/stats")).json()
        assert b["partial"] is False and b["missing_repos"] == []


async def test_awaiting_list_filters_and_pages(tmp_path, monkeypatch):
    listings = {"acme/api": [_op(n, title=f"Thing {n}") for n in range(1, 6)]}
    async with api(tmp_path, monkeypatch, rows=[], listings=listings) as c:
        body = (await c.get("/api/pull-requests?bucket=awaiting&limit=2&offset=2")).json()
        assert body["total"] == 5 and len(body["items"]) == 2
        assert (await c.get("/api/pull-requests?bucket=awaiting&q=%234")).json()["total"] == 1
        assert (await c.get("/api/pull-requests?bucket=awaiting&state=merged")).json()["total"] == 0
        assert (await c.get("/api/pull-requests?bucket=awaiting&review_status=failed")).json()["total"] == 0


async def test_needs_attention_failed_critical_or_changes_requested(
    tmp_path, monkeypatch,
):
    rows = [
        _pr("a", 1, status="failed"),                         # failed
        _pr("b", 2, run="rb"),                                # open error finding
        _pr("c", 3, run="rc"),                                # open critical
        _pr("d", 4, run="rd"),                                # verdict changes
        _pr("e", 5, run="re"),                                # only a warning
        _pr("f", 6, run="rf"),                                # error already resolved
        _pr("g", 7, status="failed", state="closed"),         # closed: ignored
    ]
    runs = [dict(rid=f"r{k}", number=n, when=YESTERDAY,
                 verdict="changes" if k == "d" else "comment")
            for k, n in zip("bcdef", (2, 3, 4, 5, 6), strict=True)]
    issues = [_issue(1, 2), _issue(2, 3, severity="critical"),
              _issue(3, 5, severity="warning"), _issue(4, 6, status="resolved"),
              _issue(5, 2, repo="elsewhere/x")]
    async with api(tmp_path, monkeypatch, rows=rows, runs=runs, issues=issues) as c:
        assert (await _stats(c))[2] == 4
        listed = (await c.get("/api/pull-requests?bucket=attention")).json()
        assert sorted(i["number"] for i in listed["items"]) == [1, 2, 3, 4]


async def test_scoping_repo_filter_workspace_and_read_permission(
    tmp_path, monkeypatch,
):
    rows = [
        _pr("a", 1, status="failed"),
        _pr("b", 2, status="failed", repo="acme/web", slug="acme-web"),
        _pr("c", 3, status="failed", repo="acme/secret", slug="secret"),
        _pr("d", 4, status="failed", ws="ws-2"),
    ]
    listings = {"acme/api": [_op(10)], "acme/web": [_op(20), _op(21)],
                "acme/secret": [_op(30)]}
    async with api(tmp_path, monkeypatch, rows=rows, denied=("secret",),
                   listings=listings) as c:
        # the unreadable repo and the other workspace are not counted
        assert (await _stats(c)) == (0, 3, 2)
        assert (await _stats(c, repo="acme/web")) == (0, 2, 1)
        assert (await _stats(c, repo="acme-api")) == (0, 1, 1)
        assert (await _stats(c, repo="acme/secret")) == (0, 0, 0)
        # the list under a bucket honours the same repo filter
        listed = (await c.get("/api/pull-requests?bucket=attention&repo=acme/web")).json()
        assert [i["number"] for i in listed["items"]] == [2]
        aw = (await c.get("/api/pull-requests?bucket=awaiting&repo=acme/web")).json()
        assert _nums(aw) == [20, 21]


async def test_an_empty_bucket_lists_nothing(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch, rows=[_pr("a", 1, run="ra")],
                   runs=[dict(rid="ra", number=1, when=YESTERDAY)]) as c:
        assert (await _stats(c)) == (0, 0, 0)
        assert (await c.get("/api/pull-requests?bucket=attention")).json()["total"] == 0
        assert (await c.get("/api/pull-requests")).json()["total"] == 1
