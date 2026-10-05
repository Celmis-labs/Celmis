"""Every open pull request can be found and reviewed by hand — on every provider.

What the manual list did before this change:

  * it asked each provider for ONE page (50 PRs) and showed it as the whole
    list — no pagination anywhere, and nothing said the list was cut;
  * it listed every target branch only by accident of the default (no
    `branch`), with no search at all;
  * on Bitbucket it always sent Basic auth with the saved Atlassian e-mail,
    so a workspace/repository access token — Bearer, no e-mail, which the
    review provider itself supports — could trigger a review it could not
    list;
  * "Review" ran in the API process's background tasks, outside the queue.

Pinned here, with the provider HTTP mocked per provider: every page is
walked (and only on the provider's own host), the target branch is an
optional filter, search covers title/number/author/branch, each row carries
its last review, "Review" and "Review all open PRs" go through the queue
with a run row per PR, the bulk request needs confirmation and stops at
`BULK_LIMIT`, and the permissions hold.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps as deps_module
from src.api.auto_review import AutoReviewStore, RepoConfig
from src.api.deps import current_workspace_id, get_current_user, slug_from_pr_ref
from src.api.review_runs import ReviewRun, ReviewRunStore
from src.api.routers import repos as repos_router
from src.cli import _parse_pr_ref
from src.repos import open_pulls
from src.review.dispatch import BULK_LIMIT
from src.users import User

WS = "ws-1"


# ─── mocked provider HTTP ────────────────────────────────────────────


def _pulls(n: int, *, start: int = 1, target: str = "main") -> list[dict]:
    return [{"n": i, "target": target if i % 2 else "develop"}
            for i in range(start, start + n)]


class _Server:
    """One fake API per provider: pages of open PRs, every request recorded."""

    def __init__(self, provider: str, total: int = 130, page: int = 50) -> None:
        self.provider = provider
        self.items = _pulls(total)
        self.page = page
        self.requests: list[httpx.Request] = []
        self.evil_next = False

    def _chunk(self, page_no: int, target: str | None) -> tuple[list[dict], bool]:
        items = [i for i in self.items if not target or i["target"] == target]
        lo = (page_no - 1) * self.page
        return items[lo:lo + self.page], lo + self.page < len(items)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = request.url.params
        if self.provider == "github":
            page_no = int(q.get("page", "1"))
            chunk, more = self._chunk(page_no, q.get("base"))
            headers = {}
            if more:
                host = "evil.example.com" if self.evil_next else "api.github.com"
                headers["Link"] = (f'<https://{host}/repos/acme/api/pulls?state=open'
                                   f'&per_page=100&page={page_no + 1}'
                                   f'{"&base=" + q["base"] if q.get("base") else ""}>; '
                                   f'rel="next"')
            return httpx.Response(200, headers=headers, json=[{
                "number": i["n"], "title": f"PR {i['n']}", "user": {"login": f"dev{i['n'] % 3}"},
                "html_url": f"https://github.com/acme/api/pull/{i['n']}",
                "head": {"ref": f"feat/{i['n']}"}, "base": {"ref": i["target"]},
                "draft": i["n"] == 2, "created_at": f"2026-09-{(i['n'] % 28) + 1:02d}T00:00:00Z",
                "updated_at": "2026-10-01T00:00:00Z",
            } for i in chunk])
        if self.provider == "gitlab":
            page_no = int(q.get("page", "1"))
            chunk, more = self._chunk(page_no, q.get("target_branch"))
            headers = {"X-Next-Page": str(page_no + 1) if more else ""}
            return httpx.Response(200, headers=headers, json=[{
                "iid": i["n"], "title": f"MR {i['n']}", "author": {"username": f"dev{i['n'] % 3}"},
                "web_url": f"https://gitlab.com/acme/api/-/merge_requests/{i['n']}",
                "source_branch": f"feat/{i['n']}", "target_branch": i["target"],
                "draft": False, "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-10-01T00:00:00Z",
            } for i in chunk])
        # bitbucket
        page_no = int(q.get("page", "1"))
        bbql = q.get("q") or ""
        target = bbql.split('destination.branch.name="', 1)[1].rstrip('"') if bbql else None
        chunk, more = self._chunk(page_no, target)
        body: dict = {"values": [{
            "id": i["n"], "title": f"PR {i['n']}",
            "author": {"display_name": f"Dev {i['n'] % 3}", "nickname": f"dev{i['n'] % 3}"},
            "links": {"html": {"href": f"https://bitbucket.org/acme/api/pull-requests/{i['n']}"}},
            "source": {"branch": {"name": f"feat/{i['n']}"}},
            "destination": {"branch": {"name": i["target"]}},
            "created_on": "2026-09-01T00:00:00+00:00",
            "updated_on": "2026-10-01T00:00:00+00:00",
        } for i in chunk]}
        if more:
            host = "evil.example.com" if self.evil_next else "api.bitbucket.org"
            body["next"] = (f"https://{host}/2.0/repositories/acme/api/pullrequests"
                            f"?page={page_no + 1}&pagelen=50"
                            + (f"&q={bbql}" if bbql else ""))
        return httpx.Response(200, json=body)


@pytest.fixture
def server(monkeypatch):
    holder: dict = {}

    def _make(provider: str, **kw) -> _Server:
        srv = _Server(provider, **kw)
        holder["srv"] = srv
        monkeypatch.setattr(
            open_pulls, "build_client",
            lambda **_: httpx.Client(transport=httpx.MockTransport(srv)),
        )
        open_pulls.clear_pull_cache()
        return srv

    yield _make
    open_pulls.clear_pull_cache()


# ─── the listing, per provider ───────────────────────────────────────


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_every_page_is_walked(server, provider):
    srv = server(provider)
    listing = open_pulls.fetch_open_pulls(provider, "acme/api", "tok", "")
    assert len(listing.items) == 130 and not listing.truncated
    assert sorted(p.number for p in listing.items) == list(range(1, 131))
    assert len(srv.requests) == 3, "one request per page, no more"
    pr = next(p for p in listing.items if p.number == 3)
    assert pr.target_branch == "main" and pr.source_branch == "feat/3"
    assert pr.author


@pytest.mark.parametrize("provider,param", [
    ("github", "base"), ("gitlab", "target_branch"), ("bitbucket", "q"),
])
def test_the_target_branch_is_an_optional_server_side_filter(server, provider, param):
    srv = server(provider)
    every = open_pulls.fetch_open_pulls(provider, "acme/api", "tok", "")
    assert {p.target_branch for p in every.items} == {"main", "develop"}
    assert all(param not in r.url.params for r in srv.requests)

    srv.requests.clear()
    only = open_pulls.fetch_open_pulls(provider, "acme/api", "tok", "", target="develop")
    assert {p.target_branch for p in only.items} == {"develop"}
    assert len(only.items) == 65
    assert param in srv.requests[0].url.params


@pytest.mark.parametrize("provider", ["github", "bitbucket"])
def test_a_next_link_to_another_host_is_not_followed(server, provider):
    srv = server(provider)
    srv.evil_next = True
    listing = open_pulls.fetch_open_pulls(provider, "acme/api", "tok", "")
    assert len(listing.items) == 50
    assert all(r.url.host != "evil.example.com" for r in srv.requests)


def test_the_cap_says_it_cut_the_list(server):
    server("gitlab")
    listing = open_pulls.fetch_open_pulls("gitlab", "acme/api", "tok", cap=60)
    assert listing.truncated and len(listing.items) == 60


def test_bitbucket_uses_bearer_for_an_access_token_and_basic_with_an_email(server):
    srv = server("bitbucket", total=3)
    open_pulls.fetch_open_pulls("bitbucket", "acme/api", "ATCTT-token", "")
    assert srv.requests[-1].headers["authorization"] == "Bearer ATCTT-token"

    open_pulls.fetch_open_pulls("bitbucket", "acme/api", "ATATT-token", "me@x.io")
    want = base64.b64encode(b"me@x.io:ATATT-token").decode()
    assert srv.requests[-1].headers["authorization"] == f"Basic {want}"


def test_a_quote_in_a_branch_cannot_break_out_of_bbql(server):
    srv = server("bitbucket", total=1)
    open_pulls.fetch_open_pulls("bitbucket", "acme/api", "t", "", target='x" OR "1')
    assert srv.requests[0].url.params["q"] == (
        'state="OPEN" AND destination.branch.name="x\\" OR \\"1"')


def test_search_covers_title_number_author_and_branches():
    pulls = [
        open_pulls.OpenPull(7, "Add cache", "dana", "u", "feat/cache", "main",
                            "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z"),
        open_pulls.OpenPull(12, "Fix login", "lee", "u", "fix/login", "release/2",
                            "2026-09-03T00:00:00Z", "2026-09-04T00:00:00Z"),
    ]
    num = lambda **kw: [p.number for p in open_pulls.select(pulls, **kw)]  # noqa: E731
    assert num(q="CACHE") == [7]
    assert num(q="#12") == [12] and num(q="12") == [12]
    assert num(q="lee") == [12]
    assert num(q="release/") == [12] and num(q="feat/") == [7]
    assert num(target="main") == [7]
    assert num() == [12, 7]
    assert num(sort="oldest") == [7, 12]
    assert num(sort="recently_updated") == [7, 12]


# ─── the API ─────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch) -> ReviewRunStore:
    s = ReviewRunStore(tmp_path / "runs.db")
    monkeypatch.setattr("src.api.review_runs._default_store", s)
    return s


@pytest.fixture
def jobs(monkeypatch):
    import src.sync.queue as q

    queued: list[dict] = []

    def _enqueue(**kw):
        if any(j["dedup_key"] == kw.get("dedup_key") for j in queued):
            return None
        queued.append(kw)
        return f"job-{len(queued)}"

    monkeypatch.setattr(q, "enqueue", _enqueue)
    return queued


def _app(monkeypatch, tmp_path, *, provider: str, perm: str | None = "review",
         grants: bool = True) -> TestClient:
    registry = AutoReviewStore(tmp_path / "auto_review.db")
    registry.upsert(RepoConfig(
        user_id="owner@x.io", repo_slug="acme-api", provider=provider,
        full_name="acme/api", url=f"https://{provider}.com/acme/api",
        workspace_id=WS, enabled=False, mode="manual",
    ))
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: registry)
    monkeypatch.setattr(
        repos_router, "resolve_git_credential",
        lambda *a, **kw: SimpleNamespace(secret="tok", metadata={}),
    )

    async def _perm(slug, user, workspace_id=None):
        return perm, grants

    monkeypatch.setattr(deps_module, "_effective_repo_permission", _perm)
    app = FastAPI()
    app.include_router(repos_router.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        id="u-1", email="reviewer@x.io")
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_the_list_is_every_open_pr_with_its_last_review(
    server, store, monkeypatch, tmp_path, provider,
):
    server(provider)
    store.insert(ReviewRun(
        id="r-old", user_id="u-1", pr_ref=f"{provider}:acme/api#4", workspace_id=WS,
        status="skipped", pr_provider=provider, pr_repo="acme/api", pr_number=4,
        stages=[{"key": "finished", "name": "Finished", "status": "skipped",
                 "reason": "Skipped — Draft: the pull request is a draft"}],
    ))
    c = _app(monkeypatch, tmp_path, provider=provider)

    body = c.get("/api/repos/acme-api/pulls?limit=200").json()
    assert body["open_total"] == 130 and body["total"] == 130
    assert len(body["items"]) == 130 and body["truncated"] is False
    assert body["target_branches"] == ["develop", "main"]
    assert body["bulk_limit"] == BULK_LIMIT
    four = next(i for i in body["items"] if i["number"] == 4)
    assert four["target_branch"] == "develop" and four["source_branch"] == "feat/4"
    assert four["last_review_status"] == "skipped"
    assert four["last_review_reason"].startswith("Skipped — Draft")
    assert next(i for i in body["items"] if i["number"] == 5)["last_review_status"] is None

    page = c.get("/api/repos/acme-api/pulls?limit=20&offset=20&branch=main").json()
    assert page["total"] == 65 and len(page["items"]) == 20
    assert {i["target_branch"] for i in page["items"]} == {"main"}

    hit = c.get("/api/repos/acme-api/pulls?q=%23101").json()
    assert [i["number"] for i in hit["items"]] == [101]


def test_a_provider_error_is_a_502_naming_the_provider(monkeypatch, tmp_path, store):
    def _fail(request):
        return httpx.Response(401, json={"type": "error"})

    monkeypatch.setattr(open_pulls, "build_client",
                        lambda **_: httpx.Client(transport=httpx.MockTransport(_fail)))
    open_pulls.clear_pull_cache()
    c = _app(monkeypatch, tmp_path, provider="bitbucket")
    r = c.get("/api/repos/acme-api/pulls")
    assert r.status_code == 502
    assert "Bitbucket answered HTTP 401" in r.json()["detail"]


def test_review_goes_through_the_queue_with_a_run_row(
    server, store, jobs, monkeypatch, tmp_path,
):
    server("bitbucket", total=3)
    c = _app(monkeypatch, tmp_path, provider="bitbucket")
    r = c.post("/api/repos/acme-api/pulls/2/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued" and body["run_id"]
    payload = jobs[0]["payload"]
    assert payload == {**payload, "provider": "bitbucket", "repo": "acme/api",
                       "pr_number": 2, "run_id": body["run_id"], "source": "manual",
                       "user_id": "u-1", "workspace_id": WS}
    row = store.get(body["run_id"])
    assert row.status == "queued" and row.pr_provider == "bitbucket"
    assert [s["key"] for s in row.stages] == ["received", "queued"]

    again = c.post("/api/repos/acme-api/pulls/2/review").json()
    assert again["status"] == "duplicate"
    assert "already queued" in again["reason"]


def test_bulk_review_needs_confirmation(server, store, jobs, monkeypatch, tmp_path):
    server("github", total=3)
    c = _app(monkeypatch, tmp_path, provider="github")
    r = c.post("/api/repos/acme-api/pulls/review-all", json={})
    assert r.status_code == 400
    assert jobs == []


def test_bulk_review_stops_at_the_limit(server, store, jobs, monkeypatch, tmp_path):
    server("gitlab", total=BULK_LIMIT * 2 + 2)  # 26 target main, 26 develop
    c = _app(monkeypatch, tmp_path, provider="gitlab")
    r = c.post("/api/repos/acme-api/pulls/review-all", json={"confirm": True})
    assert r.status_code == 422
    assert f"at most {BULK_LIMIT}" in r.json()["detail"]
    assert jobs == []

    r = c.post("/api/repos/acme-api/pulls/review-all",
               json={"confirm": True, "branch": "main", "q": "MR 1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == body["queued"] == len(jobs) > 0
    assert all(j["payload"]["source"] == "bulk" for j in jobs)
    assert all(store.get(i["run_id"]).status == "queued" for i in body["items"])


def test_bulk_review_of_listed_numbers_skips_closed_ones(
    server, store, jobs, monkeypatch, tmp_path,
):
    server("github", total=5)
    c = _app(monkeypatch, tmp_path, provider="github")
    r = c.post("/api/repos/acme-api/pulls/review-all",
               json={"confirm": True, "numbers": [1, 3, 999]})
    assert r.status_code == 200, r.text
    assert sorted(i["number"] for i in r.json()["items"]) == [1, 3]


def test_a_reader_can_list_but_not_review(server, store, jobs, monkeypatch, tmp_path):
    server("github", total=3)
    c = _app(monkeypatch, tmp_path, provider="github", perm="read")
    assert c.get("/api/repos/acme-api/pulls").status_code == 200
    assert c.post("/api/repos/acme-api/pulls/1/review").status_code == 403
    assert c.post("/api/repos/acme-api/pulls/review-all",
                  json={"confirm": True}).status_code == 403
    assert jobs == []


def test_no_grant_on_a_granted_repo_cannot_even_list(server, store, monkeypatch, tmp_path):
    server("github", total=3)
    c = _app(monkeypatch, tmp_path, provider="github", perm=None, grants=True)
    assert c.get("/api/repos/acme-api/pulls").status_code == 403


def test_an_unregistered_repo_is_a_404(server, store, monkeypatch, tmp_path):
    server("github", total=3)
    c = _app(monkeypatch, tmp_path, provider="github")
    assert c.get("/api/repos/nope/pulls").status_code == 404
    assert c.post("/api/repos/nope/pulls/1/review").status_code == 404


# ─── Bitbucket references and the trigger ────────────────────────────


@pytest.mark.parametrize("ref,want", [
    ("bitbucket:acme/api#12", ("bitbucket", "acme/api", 12)),
    ("bitbucket:my-team/my.repo_2#7", ("bitbucket", "my-team/my.repo_2", 7)),
    ("https://bitbucket.org/acme/api/pull-requests/12",
     ("bitbucket", "acme/api", 12)),
    ("https://bitbucket.org/acme/api/pull-requests/12/overview",
     ("bitbucket", "acme/api", 12)),
    ("  bitbucket:acme/api#3  ", ("bitbucket", "acme/api", 3)),
])
def test_bitbucket_references_parse(ref, want):
    assert _parse_pr_ref(ref) == want
    assert slug_from_pr_ref(ref) == want[1]


def test_a_bitbucket_review_triggered_by_hand_runs_end_to_end(monkeypatch, tmp_path, store):
    """The real Bitbucket provider over a mocked API: the PR and its diff are
    fetched with the access token as Bearer, the review runs, the row has the
    PR's coordinates and its stages."""
    import src.review.issues as issues_mod
    import src.review.providers as providers_mod
    from src.api.routers import reviews as reviews_router
    from src.review.providers.bitbucket import BitbucketPRProvider
    from tests.review.test_a_review_says_how_it_got_there import (
        POLICY,
        _Agent,
        _finding,
        _orch,
        _wire,
    )
    from tests.review.test_the_pr_hears_the_review_begin_and_end import (
        _install_settings,
    )

    _install_settings(monkeypatch)
    import src.api.routers.llm as llm_router
    import src.notifications as notifications
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    from src.review.agents.base import AgentRunResult

    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})
    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))
    monkeypatch.setattr(notifications, "notify", lambda **kw: None)
    monkeypatch.setattr(issues_mod, "record_review_run", lambda *a, **kw: True)

    seen: list[httpx.Request] = []
    diff = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1,1 +1,2 @@\n line\n+x = 1\n")

    def _bb(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/diff"):
            return httpx.Response(200, text=diff)
        return httpx.Response(200, json={
            "id": 12, "title": "Add x", "state": "OPEN", "description": "",
            "author": {"nickname": "dana"},
            "source": {"branch": {"name": "feat/x"}, "commit": {"hash": "abc123"}},
            "destination": {"branch": {"name": "develop"}, "commit": {"hash": "def456"}},
            "links": {"html": {"href": "https://bitbucket.org/acme/api/pull-requests/12"}},
        })

    provider = BitbucketPRProvider(token="ATCTT-access-token")
    provider._http = httpx.Client(
        transport=httpx.MockTransport(_bb),
        headers={"Authorization": "Bearer ATCTT-access-token"})
    called: list[tuple] = []
    _wire(monkeypatch, _orch(monkeypatch, [_Agent("defect", [_finding()])],
                             policy=POLICY), provider)
    monkeypatch.setattr(providers_mod, "get_provider_for",
                        lambda name, **kw: (called.append((name, kw)), provider)[1])

    async def _perm(slug, user, workspace_id=None):
        return "review", True

    monkeypatch.setattr(deps_module, "_effective_repo_permission", _perm)
    app = FastAPI()
    monkeypatch.setattr("src.api.deps.is_workspace_admin", lambda _u, _ws: False)
    app.include_router(reviews_router.router)
    app.dependency_overrides[get_current_user] = lambda: User(id="u-1", email="r@x.io")
    app.dependency_overrides[current_workspace_id] = lambda: WS
    c = TestClient(app)

    r = c.post("/api/reviews/trigger",
               json={"pr_ref": "bitbucket:acme/api#12", "post_comments": False})
    assert r.status_code == 200, r.text
    row = store.get(r.json()["id"])
    assert called and called[0][0] == "bitbucket"
    assert row.status == "complete", row.summary
    assert (row.pr_provider, row.pr_repo, row.pr_number) == ("bitbucket", "acme/api", 12)
    assert {"/2.0/repositories/acme/api/pullrequests/12",
            "/2.0/repositories/acme/api/pullrequests/12/diff"} <= {q.url.path for q in seen}
    assert all(q.headers["authorization"] == "Bearer ATCTT-access-token" for q in seen)
    keys = [s["key"] for s in row.stages]
    assert keys[0] == "received" and keys[-1] == "finished"
    fetch = next(s for s in row.stages if s["key"] == "fetch_pr")
    assert "develop ← feat/x" in fetch["reason"]
    detail = c.get(f"/api/reviews/{row.id}").json()
    assert [s["key"] for s in detail["stages"]] == keys
    assert c.get("/api/reviews/history").json()[0]["stages"] is None
