"""Every GitLab operation goes to the instance the workspace configured.

The defect this guards against is quiet: one call site left on the
gitlab.com constant sends a self-hosted workspace's token to gitlab.com (a
401 at best, a token disclosure at worst) while every other surface works.
So each operation is driven against a mocked self-hosted instance under a
sub-path, and the test asserts three things about every request it makes:

  * it went to https://git.example.com/gitlab/api/v4/... and nowhere else;
  * the client that sent it was allowed exactly that one extra host, pinned
    to the address the instance resolved to just now;
  * the token went in the PRIVATE-TOKEN header — never in a URL.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import httpx
import pytest

from src.config import get_settings
from src.sync import gitlab_instance as gi
from src.sync.gitlab_instance import METADATA_KEY

BASE = "https://git.example.com/gitlab"
API = f"{BASE}/api/v4"
HOST = "git.example.com"
PUBLIC = "93.184.216.34"
TOKEN = "glpat-SELFHOSTED-secret"
INSTANCE = gi.instance_of(BASE)
CRED = SimpleNamespace(secret=TOKEN, metadata={METADATA_KEY: BASE, "username": "bot"},
                       user_id="ws:ws-a")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GITLAB_ALLOWED_HOSTS", "GITLAB_HTTP_ALLOWED_HOSTS", "GITLAB_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(gi, "_resolve", lambda host, port: [PUBLIC])
    yield
    get_settings.cache_clear()


class Server:
    """A mocked GitLab. `routes` maps (METHOD, path-suffix) → response maker."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        for (method, suffix), make in self.routes.items():
            if request.method == method and path.endswith(suffix):
                return make(request)
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    def build_client(self, **kw):
        self.client_kwargs.append(kw)
        return httpx.Client(transport=httpx.MockTransport(self.handle),
                            headers=kw.get("headers"),
                            follow_redirects=kw.get("follow_redirects", False))

    def assert_all_on_the_instance(self):
        assert self.requests, "no request reached the mocked instance"
        for r in self.requests:
            assert str(r.url).startswith(API + "/"), str(r.url)
            assert TOKEN not in str(r.url)
        for kw in self.client_kwargs:
            assert kw.get("extra_allowed_hosts") == (HOST,), kw
            assert kw.get("pinned_addresses") == {HOST: PUBLIC}, kw


def _json(body, status=200, headers=None):
    return lambda req: httpx.Response(status, json=body, headers=headers or {})


# ─── merge requests: every page, on the instance ─────────────────────


def test_the_mr_list_walks_every_page_of_the_instance(monkeypatch):
    from src.repos import open_pulls

    def page(req):
        n = int(req.url.params.get("page", "1"))
        items = [{"iid": n * 10 + i, "title": f"mr {n}.{i}", "author": {"username": "a"},
                  "web_url": f"{BASE}/g/p/-/merge_requests/{n * 10 + i}",
                  "source_branch": "f", "target_branch": "main"} for i in range(2)]
        nxt = str(n + 1) if n < 3 else ""
        assert req.headers["PRIVATE-TOKEN"] == TOKEN
        return httpx.Response(200, json=items, headers={"X-Next-Page": nxt})

    srv = Server({("GET", "/projects/g%2Fsub%2Fp/merge_requests"): page})
    monkeypatch.setattr(open_pulls, "build_client", srv.build_client)
    open_pulls.clear_pull_cache()
    listing = open_pulls.fetch_open_pulls("gitlab", "g/sub/p", TOKEN, gitlab=INSTANCE)
    assert [p.number for p in listing.items] == [10, 11, 20, 21, 30, 31]
    assert not listing.truncated
    srv.assert_all_on_the_instance()


def test_a_next_link_off_the_instance_is_not_followed(monkeypatch):
    """A page link is data from the network: it must not carry the token to
    another host, nor out of the instance's sub-path on the same host."""
    from src.repos import open_pulls

    for bad in ("https://evil.example.com/api/v4/projects/x/merge_requests?page=2",
                "https://git.example.com/other/api/v4/projects/x/merge_requests?page=2"):
        srv = Server({("GET", "/merge_requests"): _json(
            [{"iid": 1, "title": "t"}], headers={"Link": f'<{bad}>; rel="next"'})})
        monkeypatch.setattr(open_pulls, "build_client", srv.build_client)
        open_pulls.clear_pull_cache()
        open_pulls.fetch_open_pulls("gitlab", "g/p", TOKEN, gitlab=INSTANCE)
        assert len(srv.requests) == 1, bad


def test_the_cache_does_not_mix_instances(monkeypatch):
    from src.repos import open_pulls

    srv = Server({("GET", "/merge_requests"): _json([{"iid": 1, "title": "t"}])})
    monkeypatch.setattr(open_pulls, "build_client", srv.build_client)
    open_pulls.clear_pull_cache()
    open_pulls.cached_open_pulls("gitlab", "g/p", TOKEN, gitlab=INSTANCE)
    other = gi.instance_of("https://git2.example.com")
    open_pulls.cached_open_pulls("gitlab", "g/p", TOKEN, gitlab=other)
    assert len(srv.requests) == 2


# ─── branches ────────────────────────────────────────────────────────


def test_branches_are_read_from_the_instance(monkeypatch):
    from src.repos import branches

    def page(req):
        n = int(req.url.params.get("page", "1"))
        names = [{"name": f"b{n}-{i}", "default": n == 1 and i == 0,
                  "commit": {"committed_date": "2026-01-01T00:00:00Z"}} for i in range(3)]
        return httpx.Response(200, json=names, headers={"X-Next-Page": "2" if n == 1 else ""})

    srv = Server({("GET", "/projects/g%2Fp/repository/branches"): page})
    monkeypatch.setattr(branches, "build_client", srv.build_client)
    branches.clear_branch_cache()
    listing = branches.fetch_branches("gitlab", "g/p", TOKEN, gitlab=INSTANCE)
    assert listing.default_branch == "b1-0"
    assert len(listing.names) == 6
    srv.assert_all_on_the_instance()


# ─── the review provider ─────────────────────────────────────────────


def _provider(monkeypatch, srv):
    from src.review.providers import gitlab as prov

    monkeypatch.setattr(prov, "build_client", srv.build_client)
    monkeypatch.setattr(prov, "resolve_git_credential", lambda *a, **k: CRED)
    return prov.GitLabPRProvider(workspace_id="ws-a")


MR = {"iid": 5, "title": "Add", "description": "body", "author": {"username": "bob"},
      "target_branch": "main", "source_branch": "f", "state": "opened",
      "diff_refs": {"base_sha": "b1", "head_sha": "h1", "start_sha": "b1"},
      "sha": "h1", "web_url": f"{BASE}/g/p/-/merge_requests/5"}
DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_the_provider_takes_its_instance_from_the_workspace_row(monkeypatch):
    calls: list = []
    srv = Server({
        ("GET", "/merge_requests/5"): _json(MR),
        ("GET", "/merge_requests/5/raw_diffs"): lambda r: httpx.Response(200, text=DIFF),
        ("PUT", "/merge_requests/5"): lambda r: calls.append(r) or httpx.Response(200, json=MR),
        ("GET", "/merge_requests/5/approvals"): _json({"approved_by": []}),
        ("POST", "/merge_requests/5/approve"): _json({}, 201),
        ("GET", "/user"): _json({"username": "bot"}),
    })
    provider = _provider(monkeypatch, srv)
    assert provider.api_base == API

    pr = provider.fetch_pull_request("g/p", 5)
    assert pr.head_sha == "h1" and pr.url.startswith(BASE)

    out = provider.update_description(pr, lambda d: d + "\n\nsummary")
    assert out.get("written") is not False or out.get("unchanged")
    assert calls, "the description was not written"

    res = provider._apply_approval("g%2Fp", pr, approve=True)
    assert res == {"approval": "approved"}
    provider.close()
    srv.assert_all_on_the_instance()
    assert all(r.headers.get("PRIVATE-TOKEN") == TOKEN for r in srv.requests)


def test_review_comments_are_posted_to_the_instance(monkeypatch):
    posted: list[httpx.Request] = []

    def note(req):
        posted.append(req)
        return httpx.Response(201, json={"id": 77})

    srv = Server({
        ("GET", "/merge_requests/5/notes"): _json([]),
        ("GET", "/merge_requests/5/discussions"): _json([]),
        ("POST", "/merge_requests/5/notes"): note,
        ("GET", "/user"): _json({"username": "bot"}),
    })
    provider = _provider(monkeypatch, srv)
    from src.review.models import PullRequest

    pr = PullRequest(provider="gitlab", repo="g/p", number=5, title="t", description="",
                     author="a", base_ref="main", base_sha="b", head_ref="f",
                     head_sha="h", state="open")
    provider.upsert_status_comment(pr, "Review in progress", create=True)
    provider.close()
    assert posted, "no note was posted"
    srv.assert_all_on_the_instance()


def test_a_broken_stored_url_fails_the_provider_instead_of_using_gitlab_com(monkeypatch):
    from src.review.providers import gitlab as prov
    from src.review.providers.base import PullRequestProviderError

    bad = SimpleNamespace(secret=TOKEN, metadata={METADATA_KEY: "http://git.example.com"})
    monkeypatch.setattr(prov, "resolve_git_credential", lambda *a, **k: bad)
    monkeypatch.setattr(prov, "build_client", lambda **kw: pytest.fail("client built"))
    with pytest.raises(PullRequestProviderError):
        prov.GitLabPRProvider(workspace_id="ws-a")


# ─── webhook install ─────────────────────────────────────────────────


def test_the_webhook_is_installed_on_the_instance(monkeypatch, tmp_path):
    from src.review import webhook_install as wi

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://celmis.example.com")
    get_settings.cache_clear()
    monkeypatch.setattr("src.review.webhook_secrets.resolve_webhook_secret",
                        lambda *a, **k: "whsec")
    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: CRED)
    hooks: list[dict] = []

    def create(req):
        import json
        hooks.append(json.loads(req.content))
        return httpx.Response(201, json={"id": 9, **hooks[-1]})

    srv = Server({
        ("GET", "/projects/g%2Fp"): _json({"path_with_namespace": "g/p"}),
        ("GET", "/projects/g%2Fp/hooks"): _json([]),
        ("POST", "/projects/g%2Fp/hooks"): create,
    })
    monkeypatch.setattr("src.http.build_client", srv.build_client)
    cfg = SimpleNamespace(provider="gitlab", full_name="g/p", workspace_id="ws-a")
    st = wi.install(cfg, user_id="u", base="https://celmis.example.com")
    assert st.status == "installed", st
    assert hooks and hooks[0]["url"].endswith("/webhook/gitlab/ws-a")
    assert hooks[0]["enable_ssl_verification"] is True
    srv.assert_all_on_the_instance()


# ─── reviewer assignment, polling, apply-fix ─────────────────────────


def test_reviewers_are_assigned_on_the_instance(monkeypatch):
    from src.review import reviewer_assignment as ra

    srv = Server({
        ("GET", "/users"): _json([{"id": 3, "username": "carol"}]),
        ("PUT", "/merge_requests/5"): _json({}),
    })
    monkeypatch.setattr(ra, "build_client", srv.build_client)
    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: CRED)
    out = ra._assign_gitlab(repo="g/p", mr_iid=5, candidates=[("carol@x", 1)],
                            author=None, user_id="u", workspace_id="ws-a")
    assert out["status"] == "ok", out
    srv.assert_all_on_the_instance()


def test_the_poller_asks_the_instance(monkeypatch):
    from src.review import poller

    srv = Server({("GET", "/projects/g%2Fp/merge_requests"): _json([])})
    monkeypatch.setattr(poller, "build_client", srv.build_client)
    store = SimpleNamespace(update_polling_state=lambda *a, **k: None)
    monkeypatch.setattr(poller, "get_auto_review_store", lambda: store)
    cfg = SimpleNamespace(full_name="g/p", last_poll_etag=None, last_polled_at=None,
                          last_seen_pr_id=0, user_id="u", repo_slug="gitlab_g-p",
                          workspace_id="ws-a")
    poller._poll_gitlab_project(TOKEN, cfg, INSTANCE)
    srv.assert_all_on_the_instance()


def test_apply_fix_commits_on_the_instance(monkeypatch):
    import base64

    from src.api.routers import apply_fix

    content = base64.b64encode(b"x = 1\n").decode()
    srv = Server({
        ("GET", "/projects/g%2Fp"): _json({"default_branch": "main"}),
        ("GET", "/repository/files/a.py"): _json({"content": content}),
        ("POST", "/repository/branches"): _json({}, 201),
        ("PUT", "/repository/files/a.py"): _json({}),
        ("POST", "/merge_requests"): _json({"web_url": f"{BASE}/g/p/-/merge_requests/9"}, 201),
    })
    monkeypatch.setattr(apply_fix, "build_client", srv.build_client)
    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: CRED)
    out = apply_fix.apply_replacement_on_default_branch_gitlab(
        repo_slug="g/p", file_path="a.py", line=1, old_text="1", new_text="2",
        user_id="u", workspace_id="ws-a", branch_name="celmis/fix", commit_message="m")
    assert out["status"] != "skipped", out
    assert out["pr_url"].startswith(BASE)
    srv.assert_all_on_the_instance()


# ─── clone: the URL carries no credential ────────────────────────────


def test_the_clone_url_carries_no_token_and_the_env_does(monkeypatch, tmp_path):
    from src.sync.clone import RepoSync
    from src.sync.git_providers import parse_repo_url

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    repo = parse_repo_url("gitlab:g/sub/p")
    url, label, env, base = RepoSync()._gitlab_plan(
        repo, BASE, username=None, password=None, api_token=TOKEN, user_id="u")
    assert url == f"{BASE}/g/sub/p.git"
    assert TOKEN not in url and "@" not in url
    assert base == BASE
    assert label == "gitlab:credential-helper"
    assert env["CELMIS_GIT_PW"] == TOKEN and env["CELMIS_GIT_USER"] == "oauth2"
    cfg = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
           for i in range(1, int(env["GIT_CONFIG_COUNT"]))}
    assert cfg["http.curloptResolve"] == f"{HOST}:443:{PUBLIC}"
    # The helper list is cleared first, then ours is the only one asked.
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert TOKEN not in "".join(v for k, v in env.items() if k.startswith("GIT_CONFIG"))


def test_a_clone_to_a_host_that_now_resolves_privately_is_refused(monkeypatch, tmp_path):
    from src.sync.clone import CloneError, RepoSync
    from src.sync.git_providers import parse_repo_url

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(gi, "_resolve", lambda host, port: ["169.254.169.254"])
    with pytest.raises(CloneError, match="non-public"):
        RepoSync()._gitlab_plan(parse_repo_url("gitlab:g/p"), BASE, username=None,
                                password=None, api_token=TOKEN, user_id="u")


def test_a_pull_scrubs_a_token_an_older_clone_kept_in_git_config(tmp_path):
    from git import Repo

    from src.sync.clone import _scrub_origin

    path = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    repo = Repo(str(path))
    repo.create_remote("origin", f"https://oauth2:{TOKEN}@git.example.com/gitlab/g/p.git")
    _scrub_origin(repo, f"{BASE}/g/p.git")
    assert repo.remotes.origin.url == f"{BASE}/g/p.git"
    assert TOKEN not in (path / ".git" / "config").read_text()


def test_a_scrub_leaves_an_origin_pointing_elsewhere_alone(tmp_path):
    from git import Repo

    from src.sync.clone import _scrub_origin

    path = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    repo = Repo(str(path))
    repo.create_remote("origin", "https://oauth2:x@other.example.com/g/p.git")
    _scrub_origin(repo, f"{BASE}/g/p.git")
    assert repo.remotes.origin.url == "https://oauth2:x@other.example.com/g/p.git"


# ─── parsing against the instance ────────────────────────────────────


@pytest.mark.parametrize("url, owner, name, branch", [
    (f"{BASE}/group/sub/proj", "group/sub", "proj", None),
    (f"{BASE}/group/sub/proj.git", "group/sub", "proj", None),
    (f"{BASE}/group/proj/-/merge_requests/12", "group", "proj", None),
    (f"{BASE}/group/proj/-/tree/dev/src", "group", "proj", "dev"),
    ("git@git.example.com:group/sub/proj.git", "group/sub", "proj", None),
])
def test_a_self_hosted_url_parses_as_gitlab_on_that_instance(url, owner, name, branch):
    from src.sync.git_providers import (
        GitProvider,
        build_authenticated_url,
        build_clone_url,
        parse_repo_url,
    )

    parsed = parse_repo_url(url, gitlab_base_url=BASE)
    assert parsed.provider == GitProvider.GITLAB
    assert (parsed.owner, parsed.name, parsed.branch_hint) == (owner, name, branch)
    assert parsed.base_url == BASE
    # The sub-path is the instance's, never a group.
    assert not parsed.owner.startswith("gitlab")
    assert parsed.slug == parse_repo_url(f"gitlab:{owner}/{name}").slug
    assert build_clone_url(parsed) == f"{BASE}/{owner}/{name}.git"
    assert build_authenticated_url(parsed, token="t") == \
        f"https://oauth2:t@git.example.com/gitlab/{owner}/{name}.git"


def test_without_the_instance_the_same_url_is_not_gitlab():
    from src.sync.git_providers import GitProvider, parse_repo_url

    assert parse_repo_url(f"{BASE}/group/proj").provider == GitProvider.GENERIC
    other = "https://other.example.com/gitlab"
    assert parse_repo_url(f"{BASE}/group/proj", gitlab_base_url=other).provider \
        == GitProvider.GENERIC


def test_gitlab_com_parsing_is_unchanged():
    from src.sync.git_providers import build_clone_url, parse_repo_url

    parsed = parse_repo_url("https://gitlab.com/a/b/c", gitlab_base_url=BASE)
    assert parsed.base_url is None
    assert build_clone_url(parsed) == "https://gitlab.com/a/b/c.git"
