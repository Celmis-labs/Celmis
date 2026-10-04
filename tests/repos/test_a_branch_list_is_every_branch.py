"""A branch dropdown shows every branch, and can find one.

Production: a Bitbucket repository with hundreds of branches, and the branch
pickers on /repositories, /dependencies and the review-policy page each showed
the first page of 100 — the API's first page presented as the whole list, no
search, nothing saying it was cut. These tests drive `src/repos/branches.py`
against a mocked provider that pages exactly the way each real API does, and
the two routes on top of it.
"""

from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest
from fastapi import HTTPException

from src.repos import branches as br
from src.users import User


@pytest.fixture(autouse=True)
def _fresh_cache():
    br.clear_branch_cache()
    yield
    br.clear_branch_cache()


class Provider:
    """A mocked provider: records every request, answers from `handler`."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def install(self, monkeypatch) -> Provider:
        transport = httpx.MockTransport(self)
        monkeypatch.setattr(br, "build_client",
                            lambda **kw: httpx.Client(transport=transport, **kw))
        return self

    def calls(self, path_part: str) -> list[httpx.Request]:
        return [r for r in self.requests if path_part in r.url.path]


# ─── GitHub: per_page=100 and Link rel="next" ────────────────────────


def _github(names: list[str], default: str = "main", *, next_host: str | None = None):
    pages = [names[i:i + 100] for i in range(0, len(names), 100)] or [[]]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/big":
            return httpx.Response(200, json={"default_branch": default})
        page = int(request.url.params.get("page", "1"))
        assert request.url.params.get("per_page") == "100"
        headers = {}
        if page < len(pages):
            host = next_host or "api.github.com"
            headers["Link"] = (f'<https://{host}/repositories/1/branches'
                               f'?per_page=100&page={page + 1}>; rel="next"')
        body = [{"name": n} for n in pages[page - 1]]
        return httpx.Response(200, json=body, headers=headers)
    return handler


def test_github_reads_every_page(monkeypatch):
    names = [f"feature/{i:04d}" for i in range(250)] + ["main"]
    p = Provider(_github(names)).install(monkeypatch)

    listing = br.fetch_branches("github", "acme/big", "tok")

    assert len(listing.names) == 251
    assert listing.names[0] == "main"          # default first
    assert list(listing.names[1:]) == sorted(names[:-1])  # then A→Z
    assert listing.truncated is False
    assert len(p.calls("/branches")) == 3
    assert all(r.headers["Authorization"] == "Bearer tok" for r in p.requests)


def test_github_stops_at_the_cap_and_says_so(monkeypatch):
    names = [f"b{i:04d}" for i in range(450)]
    p = Provider(_github(names, default="b0001")).install(monkeypatch)
    monkeypatch.setattr(br, "BRANCH_CAP", 200)

    listing = br.fetch_branches("github", "acme/big", "tok")

    assert len(listing.names) == 200
    assert listing.truncated is True
    assert len(p.calls("/branches")) == 2      # never read page 3


def test_a_next_link_to_another_host_is_not_followed(monkeypatch):
    """The page link is network data; following it would send the token."""
    names = [f"b{i:03d}" for i in range(150)]
    p = Provider(_github(names, next_host="evil.example.com")).install(monkeypatch)

    listing = br.fetch_branches("github", "acme/big", "tok")

    assert len(listing.names) == 100
    assert {r.url.host for r in p.requests} == {"api.github.com"}


def test_github_search_filters_the_cached_list(monkeypatch):
    names = ["main", "Release/2026-09", "release/2026-10", "hotfix/release-x", "dev"]
    p = Provider(_github(names)).install(monkeypatch)

    page = br.branch_page("github", "acme/big", "tok", q="RELEASE", limit=2)

    # prefix matches before infix ones; case-insensitive
    assert page.branches == ["Release/2026-09", "release/2026-10"]
    assert page.total == 3
    before = len(p.requests)
    again = br.branch_page("github", "acme/big", "tok", q="dev")
    assert again.branches == ["dev"]
    assert len(p.requests) == before           # served from the cache


# ─── GitLab: X-Next-Page, `default`, committed_date, `search=` ───────


def _gitlab(branches: list[tuple[str, str]], default: str = "main"):
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params.get("per_page") == "100"
        rows = branches
        if params.get("search"):
            rows = [b for b in rows if params["search"].lower() in b[0].lower()]
        page = int(params.get("page", "1"))
        chunk = rows[(page - 1) * 100: page * 100]
        more = page * 100 < len(rows)
        body = [{"name": n, "default": n == default,
                 "commit": {"committed_date": d}} for n, d in chunk]
        return httpx.Response(200, json=body,
                              headers={"X-Next-Page": str(page + 1) if more else ""})
    return handler


def test_gitlab_follows_x_next_page_and_orders_by_recency(monkeypatch):
    rows = [(f"topic-{i:03d}", f"2026-01-01T00:00:{i % 60:02d}Z") for i in range(230)]
    rows.append(("main", "2020-01-01T00:00:00Z"))
    rows.append(("fresh", "2026-09-30T10:00:00+02:00"))
    p = Provider(_gitlab(rows)).install(monkeypatch)

    listing = br.fetch_branches("gitlab", "grp/sub/proj", "glpat")

    assert len(listing.names) == 232
    assert listing.default_branch == "main"
    assert listing.names[:2] == ("main", "fresh")
    assert len(p.requests) == 3
    assert p.requests[0].headers["PRIVATE-TOKEN"] == "glpat"
    assert "grp%2Fsub%2Fproj" in str(p.requests[0].url)


def test_gitlab_searches_server_side_past_the_cap(monkeypatch):
    rows = [(f"b{i:04d}", "2026-01-01T00:00:00Z") for i in range(300)]
    rows.append(("needle-far-away", "2025-01-01T00:00:00Z"))
    p = Provider(_gitlab(rows, default="b0000")).install(monkeypatch)
    monkeypatch.setattr(br, "BRANCH_CAP", 100)

    page = br.branch_page("gitlab", "grp/proj", "glpat", q="needle")

    assert page.branches == ["needle-far-away"]
    assert any(r.url.params.get("search") == "needle" for r in p.requests)


# ─── Bitbucket: pagelen=100, `next` URL, BBQL search ─────────────────


def _bitbucket(names: list[str], default: str = "develop"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2.0/repositories/team/repo":
            return httpx.Response(200, json={"mainbranch": {"name": default}})
        params = request.url.params
        rows = names
        q = params.get("q", "")
        if q:
            term = q.split('"')[1]
            rows = [n for n in rows if term.lower() in n.lower()]
        page = int(params.get("page", "1"))
        if page == 1:
            assert params.get("pagelen") == "100"
            assert params.get("sort") == "-target.date"
        chunk = rows[(page - 1) * 100: page * 100]
        body: dict = {"values": [{"name": n, "target": {"date": f"2026-0{1 + i % 9}-01T00:00:00+00:00"}}
                                 for i, n in enumerate(chunk)]}
        if page * 100 < len(rows):
            query = {"pagelen": "100", "sort": "-target.date", "page": str(page + 1)}
            if q:
                query["q"] = q
            body["next"] = ("https://api.bitbucket.org/2.0/repositories/team/repo/refs/branches?"
                            + urllib.parse.urlencode(query))
        return httpx.Response(200, json=body)
    return handler


def test_bitbucket_follows_next_until_the_end(monkeypatch):
    names = [f"feature/AIR-{i}" for i in range(347)] + ["develop"]
    p = Provider(_bitbucket(names)).install(monkeypatch)

    listing = br.fetch_branches("bitbucket", "team/repo", "app-pw", "me@x")

    assert len(listing.names) == 348
    assert listing.names[0] == "develop"
    assert listing.truncated is False
    assert len(p.calls("/refs/branches")) == 4
    assert all(r.headers["Authorization"].startswith("Basic ") for r in p.requests)


def test_bitbucket_cap_then_bbql_search_finds_the_rest(monkeypatch):
    names = [f"feature/x{i:04d}" for i in range(400)] + ['odd"one\\x']
    p = Provider(_bitbucket(names)).install(monkeypatch)
    monkeypatch.setattr(br, "BRANCH_CAP", 200)

    # An empty query reads only the head (one page), never the whole repo.
    head = br.branch_page("bitbucket", "team/repo", "pw", "me@x", limit=50)
    assert head.truncated is True and len(head.branches) == 50 and head.total == 100
    assert len(p.calls("/refs/branches")) == 1

    found = br.branch_page("bitbucket", "team/repo", "pw", "me@x", q='odd"one')
    assert found.branches == ['odd"one\\x']
    searched = [r for r in p.requests if r.url.params.get("q")]
    assert searched and searched[0].url.params["q"] == 'name ~ "odd"'


# ─── cache ───────────────────────────────────────────────────────────


def test_cache_is_per_credential_expires_and_never_keeps_a_failure(monkeypatch):
    ok = {"fail": False}
    base = _github(["main", "dev"])

    def handler(request):
        if ok["fail"]:
            return httpx.Response(500)
        return base(request)
    p = Provider(handler).install(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(br, "_now", lambda: clock["t"])

    br.cached_branches("github", "acme/big", "tok-a")
    n = len(p.requests)
    br.cached_branches("github", "acme/big", "tok-a")
    assert len(p.requests) == n                    # hit
    br.cached_branches("github", "acme/big", "tok-b")
    assert len(p.requests) > n                     # another credential, own entry

    clock["t"] += br.CACHE_TTL + 1
    ok["fail"] = True
    with pytest.raises(httpx.HTTPStatusError):
        br.cached_branches("github", "acme/big", "tok-a")
    ok["fail"] = False
    assert br.cached_branches("github", "acme/big", "tok-a").names == ("main", "dev")


def test_the_cache_key_does_not_hold_the_token(monkeypatch):
    Provider(_github(["main"])).install(monkeypatch)
    br.cached_branches("github", "acme/big", "super-secret-token")
    assert "super-secret-token" not in json.dumps([list(k) for k in br._cache])


# ─── the routes ──────────────────────────────────────────────────────


class _Creds:
    def __init__(self, secret="tok", metadata=None):
        self.secret = secret
        self.metadata = metadata or {}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from src.api.auto_review import AutoReviewStore, RepoConfig
    from src.config import get_settings

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    store = AutoReviewStore(tmp_path / "secrets" / "auto_review.db")
    for ws, slug, full in (("ws-a", "github_acme-big", "acme/big"),
                           ("ws-b", "github_beta-secret", "beta/secret")):
        store.upsert(RepoConfig(
            user_id=f"user-{ws}", repo_slug=slug, provider="github",
            full_name=full, url=f"https://github.com/{full}", workspace_id=ws,
        ))
    monkeypatch.setattr("src.api.auto_review._default_store", store)
    yield store
    get_settings.cache_clear()


def test_repos_route_answers_search_limit_and_truncation(registry, monkeypatch):
    from src.api.routers import repos

    names = [f"feature/{i:03d}" for i in range(260)] + ["main"]
    Provider(_github(names)).install(monkeypatch)
    seen = {}

    def creds(provider, *, user_id, workspace_id, **kw):
        seen["ws"] = workspace_id
        return _Creds()
    monkeypatch.setattr(repos, "resolve_git_credential", creds)
    user = User(id="user-ws-a", email="a@x")

    out = repos.list_branches("github_acme-big", q="", limit=10,
                              user=user, workspace_id="ws-a")
    assert out.branches[0] == "main" and len(out.branches) == 10
    assert out.total == 261 and out.truncated is False and out.source == "provider"
    assert seen["ws"] == "ws-a"

    hit = repos.list_branches("github_acme-big", q="FEATURE/25", limit=100,
                              user=user, workspace_id="ws-a")
    assert hit.branches == [f"feature/{i}" for i in range(250, 260)]

    with pytest.raises(HTTPException) as foreign:
        repos.list_branches("github_beta-secret", q="", limit=10,
                            user=user, workspace_id="ws-a")
    assert foreign.value.status_code == 404


def test_repos_route_says_why_the_list_is_empty(registry, monkeypatch):
    from src.api.routers import repos

    user = User(id="user-ws-a", email="a@x")
    monkeypatch.setattr(repos, "resolve_git_credential", lambda *a, **k: None)
    none = repos.list_branches("github_acme-big", q="", limit=10,
                               user=user, workspace_id="ws-a")
    assert none.branches == [] and none.error == "no_credential"

    monkeypatch.setattr(repos, "resolve_git_credential", lambda *a, **k: _Creds())
    Provider(lambda r: httpx.Response(401)).install(monkeypatch)
    failed = repos.list_branches("github_acme-big", q="", limit=10,
                                 user=user, workspace_id="ws-a")
    assert failed.branches == [] and failed.error == "provider_error"


async def test_policy_route_prefers_the_provider_to_the_single_branch_clone(
        registry, monkeypatch):
    from src.api.routers.review_policies import list_branches

    names = [f"rel/{i}" for i in range(150)] + ["main"]
    Provider(_github(names)).install(monkeypatch)
    monkeypatch.setattr("src.credentials.resolve_git_credential",
                        lambda *a, **k: _Creds())

    out = await list_branches("github_acme-big", user=User(id="user-ws-a", email="a@x"),
                              ws_id="ws-a", q="rel/14", limit=5)

    assert out.source == "provider"
    assert out.default_branch == "main"
    assert out.branches == ["rel/14", *[f"rel/{i}" for i in range(140, 144)]]
    assert out.total == 11


async def test_policy_route_without_a_token_reads_the_clone(registry, monkeypatch):
    import subprocess

    from src.api.routers.review_policies import list_branches
    from src.config import get_settings

    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: None)
    path = get_settings().repo_path("github_acme-big")
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    subprocess.run(["git", "-C", str(path), "branch", "Topic-A"], check=True)

    out = await list_branches("github_acme-big", user=User(id="user-ws-a", email="a@x"),
                              ws_id="ws-a", q="topic", limit=100)

    assert out.source == "clone" and out.branches == ["Topic-A"] and out.total == 1


# ─── the full walk stays off the request path where the provider searches ──


def test_a_bitbucket_search_never_walks_every_page(monkeypatch):
    """2,092 branches took ~19 s to walk on a real repository; a search must
    not wait for that. It is one BBQL request, and it still finds a branch
    that sits far past the first page."""
    names = [f"feature/x{i:04d}" for i in range(950)] + ["VP-1231-far-away"]
    p = Provider(_bitbucket(names)).install(monkeypatch)

    found = br.branch_page("bitbucket", "team/repo", "pw", "me@x", q="vp-1231")

    assert found.branches == ["VP-1231-far-away"]
    unfiltered = [r for r in p.calls("/refs/branches") if not r.url.params.get("q")]
    assert unfiltered == []


def test_an_empty_query_reads_the_head_and_says_there_is_more(monkeypatch):
    names = [f"feature/x{i:04d}" for i in range(950)]
    p = Provider(_bitbucket(names, default="develop")).install(monkeypatch)

    head = br.branch_page("bitbucket", "team/repo", "pw", "me@x", limit=100)

    assert head.truncated is True
    assert len(p.calls("/refs/branches")) == 1
    again = br.branch_page("bitbucket", "team/repo", "pw", "me@x", limit=100)
    assert again.branches == head.branches
    assert len(p.calls("/refs/branches")) == 1          # served from the cache


def test_a_cached_full_listing_answers_without_asking_again(monkeypatch):
    names = [f"feature/x{i:04d}" for i in range(250)] + ["needle"]
    p = Provider(_bitbucket(names)).install(monkeypatch)
    br.cached_branches("bitbucket", "team/repo", "pw", "me@x")      # warm the full walk
    before = len(p.requests)

    found = br.branch_page("bitbucket", "team/repo", "pw", "me@x", q="needle")
    everything = br.branch_page("bitbucket", "team/repo", "pw", "me@x", limit=1000)

    assert found.branches == ["needle"] and everything.total == 251
    assert everything.truncated is False
    assert len(p.requests) == before


def test_github_still_walks_every_page_to_search(monkeypatch):
    """No branch-search API on GitHub: the capped, cached full walk stays."""
    names = [f"feature/{i:03d}" for i in range(260)] + ["main"]
    Provider(_github(names)).install(monkeypatch)

    hit = br.branch_page("github", "acme/big", "ghp", q="feature/25")

    assert hit.branches == [f"feature/{i}" for i in range(250, 260)]
