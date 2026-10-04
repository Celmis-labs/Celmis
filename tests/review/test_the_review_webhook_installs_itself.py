"""Registering a repository installs its review webhook.

On production a Bitbucket repository was registered with auto-review on and
nothing was ever reviewed: Celmis only DOCUMENTED the webhook, nobody pasted
it, and /pull-requests stayed empty with no reason anywhere. These tests pin
the installer (src/review/webhook_install.py) and the endpoints around it,
against a mocked provider API for each of GitHub, GitLab and Bitbucket:

  * create when absent, update in place when our URL is already there — never
    a duplicate;
  * the secret installed is the one the receiver resolves, and it never
    appears in a response or a log line;
  * a token without webhook permission comes back as a status with a hint,
    and registration still succeeds;
  * the binding is enabled and stored under the provider's own spelling, and
    the webhook lookup is case-insensitive (Bitbucket full_name);
  * an admin of ANOTHER workspace cannot install on this repository.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps as deps_module
from src.api.auto_review import AutoReviewStore, RepoConfig
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import repos as repos_router
from src.config import get_settings
from src.review import webhook_install as wi

WS = "ws-a"
OTHER_WS = "ws-b"
SECRET = "s3cr3t-value-never-to-be-shown-0123456789"
TOKEN = "provider-token-xyz"
BASE = "https://celmis.example.com"
ADMIN = SimpleNamespace(id="u-admin", email="admin@a.test", is_admin=False, name="A")


# ─── a fake provider ─────────────────────────────────────────────────


class FakeProvider:
    """Records every request; answers from a small in-memory hook list."""

    def __init__(self, provider: str, *, canonical: str, hooks=None,
                 fail: dict[tuple[str, str], tuple[int, dict]] | None = None):
        self.provider = provider
        self.canonical = canonical
        self.hooks = list(hooks or [])
        self.fail = fail or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        # raw path, so percent-encoding (GitLab ids, Bitbucket {uuid}) is seen
        # exactly as sent.
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.calls.append((request.method, path, body))
        for (method, frag), (code, payload) in self.fail.items():
            if request.method == method and frag in path:
                return httpx.Response(code, json=payload)
        is_hooks = "/hooks" in path
        if request.method == "GET" and not is_hooks:
            key = {"github": "full_name", "gitlab": "path_with_namespace",
                   "bitbucket": "full_name"}[self.provider]
            return httpx.Response(200, json={key: self.canonical, "id": 42})
        if request.method == "GET" and is_hooks:
            if self.provider == "bitbucket":
                return httpx.Response(200, json={"values": self.hooks})
            return httpx.Response(200, json=self.hooks)
        if request.method == "POST":
            hook = {**(body or {}), "id": 99, "uuid": "{new-uuid}"}
            self.hooks.append(hook)
            return httpx.Response(201, json=hook)
        if request.method in ("PATCH", "PUT"):
            return httpx.Response(200, json={**(body or {}), "id": 7, "uuid": "{old}"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    get_settings.cache_clear()
    saved: dict[tuple[str, str], str] = {}

    def resolve(provider, workspace_id, settings):
        return saved.get((provider, workspace_id))

    def save(provider, workspace_id, secret):
        saved[(provider, workspace_id)] = secret

    monkeypatch.setattr("src.review.webhook_secrets.resolve_webhook_secret", resolve)
    monkeypatch.setattr("src.review.webhook_secrets.save_webhook_secret", save)
    monkeypatch.setattr(
        "src.credentials.resolve_git_credential",
        lambda provider, **kw: SimpleNamespace(
            secret=TOKEN, metadata={"atlassian_email": "bot@a.test"}
            if provider == "bitbucket" else {}),
    )
    store = AutoReviewStore(tmp_path / "secrets" / "auto_review.db")
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: store)
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: store)
    monkeypatch.setattr(repos_router, "record_action", lambda **kw: None)
    yield SimpleNamespace(store=store, secrets=saved, tmp=tmp_path)
    get_settings.cache_clear()


def use(monkeypatch, fake: FakeProvider) -> None:
    monkeypatch.setattr(
        wi, "_client",
        lambda provider, cred: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )


def cfg(provider: str, full_name: str, *, ws: str = WS, enabled: bool = True) -> RepoConfig:
    return RepoConfig(
        user_id=ADMIN.id, repo_slug=f"{provider}_{full_name.replace('/', '-')}",
        provider=provider, full_name=full_name,
        url=f"{provider}:{full_name}", workspace_id=ws, enabled=enabled,
        mode="manual",
    )


URL = {p: f"{BASE}/backend/webhook/{p}/{WS}" for p in ("github", "gitlab", "bitbucket")}


# ─── create / update per provider ────────────────────────────────────


@pytest.mark.parametrize("provider,full", [
    ("github", "acme/billing"),
    ("gitlab", "acme/group/billing"),
    ("bitbucket", "acme/billing"),
])
def test_creates_the_hook_when_absent(env, monkeypatch, provider, full):
    fake = FakeProvider(provider, canonical=full)
    use(monkeypatch, fake)

    st = wi.install(cfg(provider, full), user_id=ADMIN.id, base=BASE)

    assert st.status == "installed", st
    assert st.action == "created"
    assert st.url == URL[provider]
    assert fake.methods().count("POST") == 1
    _, path, body = next(c for c in fake.calls if c[0] == "POST")
    secret = env.secrets[(provider, WS)]
    if provider == "github":
        assert path == "/repos/acme/billing/hooks"
        assert body["config"] == {"url": URL[provider], "content_type": "json",
                                  "secret": secret, "insecure_ssl": "0"}
        assert body["events"] == ["pull_request", "push"]
        assert body["active"] is True
    elif provider == "gitlab":
        assert path == "/api/v4/projects/acme%2Fgroup%2Fbilling/hooks"
        assert body["url"] == URL[provider]
        assert body["token"] == secret
        assert body["merge_requests_events"] is True
        assert body["push_events"] is False
    else:
        assert path == "/2.0/repositories/acme/billing/hooks"
        assert body["secret"] == secret
        assert body["events"] == ["pullrequest:created", "pullrequest:updated",
                                  "pullrequest:fulfilled", "pullrequest:rejected"]
        assert body["active"] is True
    # Never in the answer.
    assert secret not in json.dumps(st.as_dict())


@pytest.mark.parametrize("provider,existing,method,frag", [
    ("github", {"id": 7, "config": {"url": URL["github"]}, "events": ["push"]},
     "PATCH", "/hooks/7"),
    ("gitlab", {"id": 7, "url": URL["gitlab"]}, "PUT", "/hooks/7"),
    ("bitbucket", {"uuid": "{old}", "url": URL["bitbucket"] + "/"}, "PUT",
     "/hooks/%7Bold%7D"),
])
def test_updates_our_hook_in_place_never_duplicates(env, monkeypatch, provider,
                                                    existing, method, frag):
    other = ({"id": 1, "config": {"url": "https://ci.example/hook"}} if provider == "github"
             else {"id": 1, "uuid": "{x}", "url": "https://ci.example/hook"})
    fake = FakeProvider(provider, canonical="acme/billing", hooks=[other, existing])
    use(monkeypatch, fake)

    st = wi.install(cfg(provider, "acme/billing"), user_id=ADMIN.id, base=BASE)

    assert st.status == "installed" and st.action == "updated", st
    assert "POST" not in fake.methods()
    assert "DELETE" not in fake.methods()
    m, path, body = next(c for c in fake.calls if c[0] == method)
    assert path.endswith(frag), path
    assert body  # events + secret re-sent so a repair actually repairs


def test_repairing_twice_leaves_one_hook(env, monkeypatch):
    fake = FakeProvider("bitbucket", canonical="acme/billing")
    use(monkeypatch, fake)
    c = cfg("bitbucket", "acme/billing")
    first = wi.install(c, user_id=ADMIN.id, base=BASE)
    second = wi.install(c, user_id=ADMIN.id, base=BASE)
    assert (first.action, second.action) == ("created", "updated")
    assert len(fake.hooks) == 1


def test_an_existing_workspace_secret_is_reused_not_rotated(env, monkeypatch):
    env.secrets[("github", WS)] = SECRET
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    wi.install(cfg("github", "acme/billing"), user_id=ADMIN.id, base=BASE)
    body = next(c for c in fake.calls if c[0] == "POST")[2]
    assert body["config"]["secret"] == SECRET
    assert env.secrets[("github", WS)] == SECRET


# ─── refusals are statuses with hints, never secrets ─────────────────


@pytest.mark.parametrize("provider,code,reason,needle", [
    ("bitbucket", 403, "permission", "write:webhook:bitbucket"),
    ("github", 404, "not_found", "admin:repo_hook"),
    ("gitlab", 403, "permission", "Maintainer"),
    ("github", 401, "auth", "admin:repo_hook"),
])
def test_permission_errors_are_surfaced_with_what_the_token_needs(
        env, monkeypatch, provider, code, reason, needle):
    env.secrets[(provider, WS)] = SECRET
    fake = FakeProvider(provider, canonical="acme/billing", fail={
        ("GET", "/hooks"): (code, {"message": f"nope (you sent {SECRET})"}),
    })
    use(monkeypatch, fake)

    st = wi.install(cfg(provider, "acme/billing"), user_id=ADMIN.id, base=BASE)

    assert st.status == "failed"
    assert st.reason == reason
    assert needle in (st.hint or "")
    dumped = json.dumps(st.as_dict())
    assert SECRET not in dumped
    assert TOKEN not in dumped


def test_no_secret_or_token_reaches_the_logs(env, monkeypatch, caplog):
    env.secrets[("github", WS)] = SECRET
    fake = FakeProvider("github", canonical="acme/billing", fail={
        ("POST", "/hooks"): (422, {"message": f"Validation Failed {SECRET}"}),
    })
    use(monkeypatch, fake)
    with caplog.at_level(logging.DEBUG):
        st = wi.install(cfg("github", "acme/billing"), user_id=ADMIN.id, base=BASE)
        ok = FakeProvider("github", canonical="acme/billing")
        use(monkeypatch, ok)
        wi.install(cfg("github", "acme/billing"), user_id=ADMIN.id, base=BASE)
    assert st.reason == "rejected"
    assert SECRET not in (st.message or "")
    assert SECRET not in caplog.text
    assert TOKEN not in caplog.text


def test_missing_credentials_is_a_status(env, monkeypatch):
    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: None)
    st = wi.install(cfg("gitlab", "acme/billing"), user_id=ADMIN.id, base=BASE)
    assert st.status == "failed" and st.reason == "no_credentials"


def test_uninstall_removes_only_our_hook(env, monkeypatch):
    other = {"id": 1, "config": {"url": "https://ci.example/hook"}}
    ours = {"id": 7, "config": {"url": URL["github"]}}
    fake = FakeProvider("github", canonical="acme/billing", hooks=[other, ours])
    use(monkeypatch, fake)
    st = wi.uninstall(cfg("github", "acme/billing"), user_id=ADMIN.id, base=BASE)
    assert st.status == "not_installed" and st.action == "removed"
    deletes = [p for m, p, _ in fake.calls if m == "DELETE"]
    assert deletes == ["/repos/acme/billing/hooks/7"]


def test_status_reports_github_last_delivery(env, monkeypatch):
    ours = {"id": 7, "active": True, "events": ["pull_request", "push"],
            "config": {"url": URL["github"], "secret": "********"},
            "last_response": {"code": 401, "status": "invalid", "message": "Invalid signature"}}
    fake = FakeProvider("github", canonical="acme/billing", hooks=[ours])
    use(monkeypatch, fake)
    st = wi.status(cfg("github", "acme/billing"), user_id=ADMIN.id, base=BASE)
    assert st.status == "installed"
    assert st.last_delivery == {"code": 401, "status": "invalid",
                                "message": "Invalid signature"}


# ─── public URL ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value,ok", [
    ("", False), ("celmis.example.com", False), ("http://localhost:3000", False),
    ("https://celmis.example.com/", True),
])
def test_public_base_url_must_be_reachable(monkeypatch, value, ok):
    monkeypatch.setenv("PUBLIC_BASE_URL", value)
    get_settings.cache_clear()
    try:
        base, problem = wi.public_base_url()
    finally:
        get_settings.cache_clear()
    assert (base is not None) is ok
    if not ok:
        assert "PUBLIC_BASE_URL" in problem


def test_webhook_url_matches_the_manual_setup_shape():
    assert wi.webhook_url("https://h.example", "bitbucket", "ws-1") == \
        "https://h.example/backend/webhook/bitbucket/ws-1"
    assert wi.webhook_url("https://h.example/backend", "github", "ws-1") == \
        "https://h.example/backend/webhook/github/ws-1"


# ─── binding: case-insensitive, canonical, enabled ───────────────────


def test_bitbucket_payload_full_name_finds_a_differently_cased_binding(tmp_path):
    store = AutoReviewStore(tmp_path / "ar.db")
    store.upsert(cfg("bitbucket", "Acme/Billing-API"))
    found = store.config_for_repo("bitbucket", "acme/billing-api")
    assert found is not None and found.workspace_id == WS
    assert store.workspace_for_repo("bitbucket", "ACME/billing-api") == WS
    # Provider still matters.
    assert store.config_for_repo("github", "acme/billing-api") is None


def test_case_variants_in_two_workspaces_fail_closed(tmp_path):
    store = AutoReviewStore(tmp_path / "ar.db")
    a = cfg("bitbucket", "acme/billing")
    b = cfg("bitbucket", "Acme/Billing", ws=OTHER_WS)
    b.user_id = "u-other"
    store.upsert(a)
    store.upsert(b)
    assert store.config_for_repo("bitbucket", "acme/billing") is None
    assert store.workspace_for_repo("bitbucket", "acme/billing") is None


def test_registration_refuses_a_case_variant_owned_elsewhere(tmp_path):
    store = AutoReviewStore(tmp_path / "ar.db")
    store.upsert(cfg("bitbucket", "acme/billing", ws=OTHER_WS))
    assert store.existing_workspace_binding("bitbucket", "Acme/Billing") == OTHER_WS


# ─── the API ─────────────────────────────────────────────────────────


@pytest.fixture
def api(env, monkeypatch):
    admins = {(ADMIN.id, WS)}
    monkeypatch.setattr(deps_module, "is_workspace_admin",
                        lambda user, ws: (user.id, ws) in admins)
    monkeypatch.setattr(repos_router, "is_workspace_admin",
                        lambda user, ws: (user.id, ws) in admins)
    monkeypatch.setattr("src.sync.queue.enqueue", lambda **kw: "job-1")
    app = FastAPI()
    app.include_router(repos_router.router)
    state = SimpleNamespace(user=ADMIN, ws=WS, admins=admins)
    app.dependency_overrides[get_current_user] = lambda: state.user
    app.dependency_overrides[current_workspace_id] = lambda: state.ws
    return SimpleNamespace(client=TestClient(app), state=state, **vars(env))


def test_install_endpoint_binds_canonical_name_and_enables(api, monkeypatch):
    c = cfg("bitbucket", "Acme/Billing", enabled=False)
    api.store.upsert(c)
    fake = FakeProvider("bitbucket", canonical="acme/billing")
    use(monkeypatch, fake)

    r = api.client.post(f"/api/repos/{c.repo_slug}/webhook")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "installed"
    assert body["url"] == URL["bitbucket"]
    row = api.store.get_in_workspace(WS, c.repo_slug)
    assert row.full_name == "acme/billing"
    assert row.enabled is True and row.mode == "webhook"
    # The exact name Bitbucket will send now resolves to this tenant.
    assert api.store.config_for_repo("bitbucket", "acme/billing").workspace_id == WS
    assert api.secrets[("bitbucket", WS)] not in r.text
    # The list shows the last known state without calling the provider.
    n = len(fake.calls)
    listed = api.client.get("/api/repos").json()
    assert listed[0]["webhook"]["status"] == "installed"
    assert len(fake.calls) == n


def test_permission_failure_is_200_with_hint(api, monkeypatch):
    c = cfg("bitbucket", "acme/billing")
    api.store.upsert(c)
    use(monkeypatch, FakeProvider("bitbucket", canonical="acme/billing", fail={
        ("POST", "/hooks"): (403, {"error": {"message": "Your credentials lack scope"}}),
    }))
    body = api.client.post(f"/api/repos/{c.repo_slug}/webhook").json()
    assert body["status"] == "failed" and body["reason"] == "permission"
    assert "write:webhook:bitbucket" in body["hint"]


def test_no_public_base_url_is_409_with_instructions(api, monkeypatch):
    c = cfg("github", "acme/billing")
    api.store.upsert(c)
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    get_settings.cache_clear()
    r = api.client.post(f"/api/repos/{c.repo_slug}/webhook")
    assert r.status_code == 409
    assert "PUBLIC_BASE_URL" in r.json()["detail"]
    assert api.client.delete(f"/api/repos/{c.repo_slug}/webhook").status_code == 409
    got = api.client.get(f"/api/repos/{c.repo_slug}/webhook").json()
    assert got["reason"] == "no_public_url"


def test_a_member_who_is_not_admin_cannot_install(api, monkeypatch):
    c = cfg("github", "acme/billing")
    api.store.upsert(c)
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    api.state.user = SimpleNamespace(id="u-member", email="m@a.test", is_admin=False)
    assert api.client.post(f"/api/repos/{c.repo_slug}/webhook").status_code == 403
    assert api.client.delete(f"/api/repos/{c.repo_slug}/webhook").status_code == 403
    assert fake.calls == []


def test_an_admin_of_another_workspace_cannot_install_here(api, monkeypatch):
    c = cfg("github", "acme/billing")  # registered in WS
    api.store.upsert(c)
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    intruder = SimpleNamespace(id="u-b", email="b@b.test", is_admin=False)
    api.state.admins.add((intruder.id, OTHER_WS))
    api.state.user, api.state.ws = intruder, OTHER_WS

    r = api.client.post(f"/api/repos/{c.repo_slug}/webhook")

    assert r.status_code == 404
    assert fake.calls == []
    assert api.client.get(f"/api/repos/{c.repo_slug}/webhook").status_code == 404


def test_delete_endpoint_removes_and_falls_back_to_polling(api, monkeypatch):
    c = cfg("github", "acme/billing")
    c.mode = "webhook"
    api.store.upsert(c)
    fake = FakeProvider("github", canonical="acme/billing",
                        hooks=[{"id": 7, "config": {"url": URL["github"]}}])
    use(monkeypatch, fake)
    body = api.client.delete(f"/api/repos/{c.repo_slug}/webhook").json()
    assert body["status"] == "not_installed" and body["action"] == "removed"
    assert api.store.get_in_workspace(WS, c.repo_slug).mode == "polling"


# ─── registration ────────────────────────────────────────────────────


def test_registration_installs_the_webhook(api, monkeypatch):
    fake = FakeProvider("bitbucket", canonical="acme/billing")
    use(monkeypatch, fake)
    r = api.client.post("/api/repos", json={
        "url": "https://bitbucket.org/Acme/Billing", "auto_review": True, "index": False})
    assert r.status_code == 201, r.text
    hook = r.json()["webhook"]
    assert hook["status"] == "installed" and hook["action"] == "created"
    row = api.store.config_for_repo("bitbucket", "acme/billing")
    assert row is not None and row.enabled and row.workspace_id == WS


def test_registration_survives_a_failed_install(api, monkeypatch):
    use(monkeypatch, FakeProvider("github", canonical="acme/billing", fail={
        ("GET", "/hooks"): (404, {"message": "Not Found"}),
    }))
    r = api.client.post("/api/repos", json={
        "url": "https://github.com/acme/billing", "auto_review": True, "index": False})
    assert r.status_code == 201, r.text
    hook = r.json()["webhook"]
    assert hook["status"] == "failed" and hook["reason"] == "not_found"
    assert "admin:repo_hook" in hook["hint"]
    assert api.store.get_in_workspace(WS, r.json()["slug"]) is not None


def test_registration_survives_an_installer_crash(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(wi, "install", boom)
    r = api.client.post("/api/repos", json={
        "url": "https://github.com/acme/billing", "auto_review": True, "index": False})
    assert r.status_code == 201, r.text


def test_registration_without_auto_review_touches_no_provider(api, monkeypatch):
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    r = api.client.post("/api/repos", json={
        "url": "https://github.com/acme/billing", "auto_review": False, "index": False})
    assert r.status_code == 201
    assert r.json()["webhook"]["reason"] == "auto_review_disabled"
    assert fake.calls == []


def test_registration_by_a_non_admin_does_not_install(api, monkeypatch):
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    api.state.user = SimpleNamespace(id="u-member", email="m@a.test", is_admin=False)
    r = api.client.post("/api/repos", json={
        "url": "https://github.com/acme/billing", "auto_review": True, "index": False})
    assert r.status_code == 201
    assert r.json()["webhook"]["reason"] == "not_admin"
    assert fake.calls == []


# ─── what the provider will send is what the receiver accepts ────────


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_the_installed_secret_verifies_at_the_receiver(env, monkeypatch, provider):
    import hashlib
    import hmac

    from src.review import webhook as receiver

    fake = FakeProvider(provider, canonical="acme/billing")
    use(monkeypatch, fake)
    wi.install(cfg(provider, "acme/billing"), user_id=ADMIN.id, base=BASE)
    body = next(c for c in fake.calls if c[0] == "POST")[2]
    installed = {"github": lambda b: b["config"]["secret"],
                 "gitlab": lambda b: b["token"],
                 "bitbucket": lambda b: b["secret"]}[provider](body)
    # The receiver resolves the secret for the workspace named in the URL.
    assert installed == env.secrets[(provider, WS)]
    url = body.get("url") or body["config"]["url"]
    assert url.endswith(f"/webhook/{provider}/{WS}")

    payload = b'{"x": 1}'
    sig = "sha256=" + hmac.new(installed.encode(), payload, hashlib.sha256).hexdigest()
    if provider == "github":
        assert receiver._verify_github_signature(payload, sig, installed)
    elif provider == "bitbucket":
        assert receiver._verify_bitbucket_signature(payload, sig, installed)
    else:
        assert receiver._verify_gitlab_token(installed, installed)
