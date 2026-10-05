"""A self-hosted GitLab is reachable for the workspace that configured it, only.

The egress allowlist is process-wide; a workspace's GitLab URL is not. So the
instance host is never added to the shared list — it is the one extra host of
the clients built FOR THAT WORKSPACE, derived from its own credential row.
Workspace B cannot make Celmis call workspace A's instance: not through a
client, not by registering a repository with A's URL, not through the
process-wide door.

And the save path that puts a URL into that row is the place an attacker
would aim at: it validates the URL before the token goes anywhere, and no
answer it gives — success, refusal, provider error — carries the token.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from src.config import get_settings
from src.credentials.store import CredentialStore
from src.security.egress import EgressBlockedError
from src.sync import gitlab_instance as gi
from src.sync.gitlab_instance import METADATA_KEY, build_gitlab_client, instance_for_workspace

A_BASE = "https://gitlab.a.test:1"
A_HOST = "gitlab.a.test"
TOKEN_A = "glpat-AAAA-secret-token"
TOKEN_B = "glpat-BBBB-secret-token"


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in ("GITLAB_HTTP_ALLOWED_HOSTS", "GITLAB_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    # A's instance is on the LAN: the operator listed it, and it resolves to
    # loopback. Port 1 on loopback: nothing listens, so a ConnectError is
    # proof the egress check let the request through.
    monkeypatch.setenv("GITLAB_ALLOWED_HOSTS", f'["{A_HOST}"]')
    get_settings.cache_clear()
    monkeypatch.setattr(gi, "_resolve", lambda host, port: ["127.0.0.1"])
    s = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    s.save(provider="gitlab", secret=TOKEN_A, user_id="ws:ws-a",
           metadata={"username": "a-bot", METADATA_KEY: A_BASE})
    s.save(provider="gitlab", secret=TOKEN_B, user_id="ws:ws-b",
           metadata={"username": "b-bot"})
    yield s
    get_settings.cache_clear()


def test_each_workspace_resolves_its_own_instance(store):
    assert instance_for_workspace("ws-a", store=store).base_url == A_BASE
    assert instance_for_workspace("ws-b", store=store).is_default
    assert instance_for_workspace("ws-nobody", store=store).is_default


def test_workspace_a_can_reach_its_instance(store):
    inst = instance_for_workspace("ws-a", store=store)
    with build_gitlab_client(inst, token=TOKEN_A, timeout=2.0) as client, \
            pytest.raises(httpx.ConnectError):
        client.get(f"{inst.api_base}/user")


def test_workspace_b_cannot_reach_workspace_a_instance(store):
    inst_b = instance_for_workspace("ws-b", store=store)
    with build_gitlab_client(inst_b, token=TOKEN_B, timeout=2.0) as client, \
            pytest.raises(EgressBlockedError):
        client.get(f"{A_BASE}/api/v4/user")


def test_the_process_wide_door_does_not_know_the_instance(store):
    from src.http import allowed_hosts, build_client

    assert A_HOST not in allowed_hosts()
    with build_client(timeout=2.0) as client, pytest.raises(EgressBlockedError):
        client.get(f"{A_BASE}/api/v4/user")


def test_a_clone_for_workspace_b_cannot_target_a_host(store, tmp_path, monkeypatch):
    """B's repository URL on A's host is not GitLab for B — and a generic URL
    has no clone path at all."""
    from src.sync.git_providers import GitProvider, build_clone_url, parse_repo_url

    b_base = instance_for_workspace("ws-b", store=store)
    parsed = parse_repo_url(f"{A_BASE}/group/proj",
                            gitlab_base_url=None if b_base.is_default else b_base.base_url)
    assert parsed.provider == GitProvider.GENERIC
    with pytest.raises(ValueError):
        build_clone_url(parsed)


# ─── registering a repository ────────────────────────────────────────


class _Stop(Exception):
    pass


def _add(monkeypatch, workspace_base: str | None, url: str, branch: str | None = None):
    from src.api.routers import repos
    from src.api.schemas import RepoAddRequest

    stored: list = []

    def upsert(cfg):
        stored.append(cfg)
        raise _Stop

    fake = SimpleNamespace(existing_workspace_binding=lambda *a: None,
                           existing_slug_binding=lambda *a: None, upsert=upsert)
    monkeypatch.setattr(repos, "get_auto_review_store", lambda: fake)
    monkeypatch.setattr(repos, "_qualify_with_connected_provider", lambda v, w, u: v)
    monkeypatch.setattr(repos, "_workspace_gitlab_base", lambda w, u: workspace_base)
    user = SimpleNamespace(id="u1", email="a@x")
    req = RepoAddRequest(url=url, branch=branch)
    with pytest.raises((_Stop, HTTPException)) as err:
        repos.add_repo(request=None, req=req, user=user, workspace_id="ws-x")
    return err.value, stored


def test_a_self_hosted_url_registers_in_the_workspace_that_configured_it(monkeypatch):
    _, stored = _add(monkeypatch, "https://example.com/gitlab",
                     "https://example.com/gitlab/team/sub/app/-/tree/develop")
    cfg = stored[0]
    assert (cfg.provider, cfg.full_name) == ("gitlab", "team/sub/app")
    # Stored provider-prefixed: every later re-parse says gitlab and the same
    # slug, and the host always comes from the workspace's own connection.
    assert cfg.url == "gitlab:team/sub/app"
    assert cfg.repo_slug == "gitlab_team-sub-app"
    assert cfg.branch == "develop"


def test_another_workspace_cannot_register_it(monkeypatch):
    exc, stored = _add(monkeypatch, None, "https://example.com/gitlab/team/app")
    assert isinstance(exc, HTTPException) and exc.status_code == 422
    assert "Connections" in exc.detail
    assert not stored


# ─── the save path ───────────────────────────────────────────────────


@pytest.fixture
def connections(monkeypatch, tmp_path):
    from src.api.routers import connections as conn

    for name in ("GITLAB_ALLOWED_HOSTS", "GITLAB_HTTP_ALLOWED_HOSTS", "GITLAB_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    s = CredentialStore(tmp_path / "c.db", Fernet.generate_key())
    audit: list[dict] = []
    monkeypatch.setattr(conn, "get_credential_store", lambda: s)
    monkeypatch.setattr(conn, "record_action", lambda **kw: audit.append(kw))
    yield SimpleNamespace(mod=conn, store=s, audit=audit)
    get_settings.cache_clear()


class _Instance:
    """A mocked GitLab answering /api/v4/user, recording what reached it."""

    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[httpx.Request] = []
        self.kwargs: list[dict] = []

    def build_client(self, **kw):
        self.kwargs.append(kw)

        def handle(req):
            self.requests.append(req)
            return httpx.Response(self.status, json=self.body, headers=self.headers)

        return httpx.Client(transport=httpx.MockTransport(handle), headers=kw.get("headers"))


def _put(c, monkeypatch, *, base_url, token=TOKEN_A, provider="gitlab", server=None,
         resolves=("93.184.216.34",)):
    from src.api.schemas import ConnectionUpsert

    monkeypatch.setattr(gi, "_resolve", lambda host, port: list(resolves))
    if server is not None:
        monkeypatch.setattr("src.http.build_client", server.build_client)
    user = SimpleNamespace(id="admin", email="admin@x")
    return c.mod.upsert_connection(
        provider=provider, request=None,
        req=ConnectionUpsert(provider=provider, token=token, base_url=base_url),
        user=user, workspace_id="ws-a")


def test_a_valid_instance_is_verified_then_stored_beside_the_token(connections, monkeypatch):
    srv = _Instance(body={"username": "a-bot"})
    res = _put(connections, monkeypatch, base_url="https://Git.Example.com/gitlab/api/v4/",
               server=srv)
    assert res.ok and res.username == "a-bot"
    assert res.base_url == "https://git.example.com/gitlab"
    assert [str(r.url) for r in srv.requests] == ["https://git.example.com/gitlab/api/v4/user"]
    assert srv.requests[0].headers["PRIVATE-TOKEN"] == TOKEN_A
    assert srv.kwargs[0]["extra_allowed_hosts"] == ("git.example.com",)
    row = connections.store.load(provider="gitlab", user_id="ws:ws-a")
    assert row.secret == TOKEN_A
    assert row.metadata[METADATA_KEY] == "https://git.example.com/gitlab"
    assert TOKEN_A not in repr(connections.audit)


@pytest.mark.parametrize("url, resolves", [
    ("https://gitlab.internal.example.com", ("10.0.0.7",)),
    ("https://metadata.example.com", ("169.254.169.254",)),
    ("http://git.example.com", ("93.184.216.34",)),
    ("https://user:pw@git.example.com", ("93.184.216.34",)),
    ("https://git.example.com/?next=https://evil", ("93.184.216.34",)),
])
def test_an_unsafe_instance_is_refused_before_the_token_leaves(connections, monkeypatch,
                                                               url, resolves):
    srv = _Instance(body={"username": "x"})
    res = _put(connections, monkeypatch, base_url=url, server=srv, resolves=resolves)
    assert not res.ok
    assert not srv.requests, "the token was sent to an address that failed the rules"
    assert TOKEN_A not in (res.error or "")
    assert connections.store.load(provider="gitlab", user_id="ws:ws-a") is None


def test_a_private_instance_names_the_operator_setting(connections, monkeypatch):
    res = _put(connections, monkeypatch, base_url="https://gitlab.internal.example.com",
               server=_Instance(), resolves=("10.0.0.7",))
    assert "GITLAB_ALLOWED_HOSTS" in res.error


@pytest.mark.parametrize("status, headers, needle", [
    (401, {}, "rejected the token"),
    (403, {}, "rejected the token"),
    (302, {"Location": "https://evil.example.com/"}, "redirect"),
    (500, {}, "500"),
])
def test_a_provider_refusal_is_reported_without_the_token(connections, monkeypatch,
                                                          status, headers, needle):
    srv = _Instance(status=status, body={"message": TOKEN_A}, headers=headers)
    res = _put(connections, monkeypatch, base_url="https://git.example.com", server=srv)
    assert not res.ok and needle in res.error
    assert TOKEN_A not in res.error
    assert connections.store.load(provider="gitlab", user_id="ws:ws-a") is None


def test_a_non_gitlab_answer_is_not_a_success(connections, monkeypatch):
    srv = _Instance(body={"hello": "world"})
    res = _put(connections, monkeypatch, base_url="https://git.example.com", server=srv)
    assert not res.ok


def test_gitlab_com_rows_carry_no_url(connections, monkeypatch):
    srv = _Instance(body={"username": "bot"})
    res = _put(connections, monkeypatch, base_url="", server=srv)
    assert res.ok and res.base_url == "https://gitlab.com"
    assert str(srv.requests[0].url) == "https://gitlab.com/api/v4/user"
    row = connections.store.load(provider="gitlab", user_id="ws:ws-a")
    assert METADATA_KEY not in row.metadata


def test_a_url_is_refused_for_other_providers(connections, monkeypatch):
    with pytest.raises(HTTPException) as err:
        _put(connections, monkeypatch, base_url="https://git.example.com",
             provider="github")
    assert err.value.status_code == 400


# ─── a private CA, never "verify off" ────────────────────────────────


def test_a_missing_ca_bundle_fails_loudly(tmp_path):
    from src.http import build_client

    with pytest.raises(FileNotFoundError):
        build_client(timeout=2.0, ca_bundle=str(tmp_path / "nope.pem"))


def test_a_ca_bundle_is_added_to_the_public_roots_with_verification_on(tmp_path):
    import ssl

    import certifi

    from src.http import build_client
    from src.security.egress import ca_bundle_context

    ctx = ca_bundle_context(certifi.where())
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with build_client(timeout=2.0, ca_bundle=certifi.where()) as client:
        assert client is not None
