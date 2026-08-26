"""Wire-level contract for `src.llm.gateway` against LiteLLM's admin API.

`tests/llm/test_gateway.py` covers behaviour with a MagicMock standing in for
httpx. This module goes one level lower: every request is built by a REAL
`httpx.Client` over an `httpx.MockTransport`, so what the assertions see is the
actual method, path, headers and JSON body that would reach the proxy.

Why bother: the gateway had never made a single real request, and a payload
that is "obviously right" in a MagicMock world (a stray query string, a header
that never got attached, a GET carrying a body) is exactly what LiteLLM answers
with 401/404/422. The shapes below were checked against the pinned image
(`ghcr.io/berriai/litellm-database:v1.96.0`, i.e. litellm 1.96.0):

    POST /team/new       litellm/proxy/management_endpoints/team_endpoints.py
                         -> NewTeamRequest(team_id, team_alias, metadata, …);
                         a repeat team_id answers 400 "Team id = … already
                         exists"
    GET  /team/info      ?team_id=… — the only way to tell that 400 apart from
                         a rejection
    GET  /model/info     -> {"data": [{"model_name", "model_info": {"id"}}]}
    POST /model/new      -> Deployment(model_name, litellm_params, model_info);
                         APPENDS, never upserts
    POST /model/delete   -> ModelInfoDelete(id)
    POST /key/generate   -> GenerateKeyRequest(models, team_id, …) -> {"key"}
                         "models … (if empty, key is allowed to call all
                         models)"
    POST /key/delete     -> KeyRequest(keys=[...])

Auth is `Authorization: Bearer <master key>` on every admin route;
/health/liveliness is in LiteLLM's `public_routes` and needs none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

import httpx
import pytest

from src.llm import gateway

#: The gateway no longer builds its own client: it calls
#: :func:`src.http.build_client`, which returns an httpx.Client wearing the
#: egress whitelist transport. That factory is the seam these tests patch, and
#: the real class is kept here so the replacement can still build one.
_REAL_CLIENT = httpx.Client

BASE = "http://litellm:4000"
MASTER = "sk-master-contract-key"

MODELS = {
    "chat": "gemini-3-flash-preview",
    "review": "gemini-3.1-pro-preview",
    "embeddings": "gemini-embedding-2",
}


# ─── Recording transport ─────────────────────────────────────────────


@dataclass
class Sent:
    method: str
    path: str
    query: str
    headers: dict
    body: dict | None

    @property
    def target(self) -> str:
        return f"{self.path}?{self.query}" if self.query else self.path


class Proxy:
    """Scripted LiteLLM proxy behind a real httpx client."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.status_for: dict[str, int] = {}   # path (no query) -> status
        self.models: list[dict] = []
        self.minted = 0

    def paths(self) -> list[str]:
        return [s.path for s in self.sent]

    def count(self, path: str) -> int:
        return self.paths().count(path)

    def one(self, path: str) -> Sent:
        hits = [s for s in self.sent if s.path == path]
        assert hits, f"{path} was never called; saw {self.paths()}"
        return hits[0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content or b""
        body = json.loads(raw) if raw else None
        self.sent.append(Sent(
            method=request.method,
            path=request.url.path,
            query=request.url.query.decode(),
            headers=dict(request.headers),
            body=body,
        ))
        status = self.status_for.get(request.url.path, 200)
        if status >= 400:
            # LiteLLM error bodies echo the offending request back, api_key and
            # all — the reason nothing from the wire may be logged raw.
            return httpx.Response(status, json={"error": {"message": "boom", "request": body}})
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": self.models})
        if request.url.path == "/key/generate":
            self.minted += 1
            return httpx.Response(200, json={"key": f"sk-virtual-{self.minted}"})
        if request.url.path == "/health/liveliness":
            return httpx.Response(200, json="I'm alive!")
        return httpx.Response(status, json={})


@dataclass
class _Stored:
    secret: str
    metadata: dict


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], _Stored] = {}

    def save(self, provider, secret, *, metadata=None, user_id="default",
             account_label="default"):
        self.rows[(user_id, provider, account_label)] = _Stored(
            secret=secret, metadata=json.loads(json.dumps(metadata or {})),
        )

    def load(self, provider, *, user_id="default", account_label="default",
             update_last_used=True):
        return self.rows.get((user_id, provider, account_label))

    def delete(self, provider, *, user_id="default", account_label="default"):
        return self.rows.pop((user_id, provider, account_label), None) is not None


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_TIMEOUT",
                "LITELLM_PROXY_API_BASE"):
        monkeypatch.delenv(var, raising=False)
    gateway.reset_cache()
    yield
    gateway.reset_cache()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LITELLM_PROXY_URL", BASE)
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)


@pytest.fixture
def store():
    fake = FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


@pytest.fixture
def proxy():
    fake = Proxy()

    def build(*_args, **kwargs):
        return _REAL_CLIENT(
            transport=httpx.MockTransport(fake.handler),
            timeout=kwargs.get("timeout", 5.0),
        )

    with patch("src.http.build_client", new=build):
        yield fake


# ─── 1. Request shapes ───────────────────────────────────────────────


def test_every_admin_call_is_bearer_authenticated(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    assert proxy.sent, "no request was made at all"
    for s in proxy.sent:
        assert s.headers.get("authorization") == f"Bearer {MASTER}", s.path


def test_team_new_payload(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    call = proxy.one("/team/new")
    assert call.method == "POST"
    assert call.headers.get("content-type") == "application/json"
    assert call.body == {
        "team_id": "ws-acme",
        "team_alias": "celmis:acme",
        "metadata": {"celmis_workspace_id": "acme"},
    }


def test_model_new_payload_carries_the_real_provider_key(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    bodies = {s.body["model_name"]: s.body for s in proxy.sent if s.path == "/model/new"}
    assert sorted(bodies) == ["celmis-acme-chat", "celmis-acme-embed", "celmis-acme-review"]

    chat = bodies["celmis-acme-chat"]
    assert chat["litellm_params"] == {
        "model": "gemini/gemini-3-flash-preview", "api_key": "AIzaREALKEY123",
    }
    assert chat["model_info"]["celmis_workspace_id"] == "acme"
    assert chat["model_info"]["celmis_surface"] == "chat"
    assert "mode" not in chat["model_info"], "only the embeddings deployment is a mode=embedding one"

    # LiteLLM's ModelInfo.mode is Literal["embedding","chat","completion"] —
    # without it the proxy routes embeddings through the chat path.
    assert bodies["celmis-acme-embed"]["model_info"]["mode"] == "embedding"


def test_key_generate_payload_is_scoped_to_this_tenants_deployments(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    body = proxy.one("/key/generate").body
    assert sorted(body["models"]) == [
        "celmis-acme-chat", "celmis-acme-embed", "celmis-acme-review",
    ]
    assert body["team_id"] == "ws-acme"
    assert body["metadata"] == {"celmis_workspace_id": "acme"}
    assert body["key_alias"].startswith("celmis-acme-")


def test_read_only_calls_are_gets_without_a_body(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    info = proxy.one("/model/info")
    assert info.method == "GET"
    assert info.body is None, "a GET with a JSON body is what makes proxies answer 422"


def test_health_probes_the_public_liveliness_route(enabled, store, proxy):
    assert gateway.health() is True
    call = proxy.one("/health/liveliness")
    assert call.method == "GET"


# ─── 2. Idempotency ──────────────────────────────────────────────────


def test_reprovisioning_an_unchanged_workspace_writes_nothing(enabled, store, proxy):
    first = gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)
    proxy.sent.clear()
    second = gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    assert first == second == "sk-virtual-1"
    assert proxy.sent == [], "a no-op re-provision must not touch the proxy at all"


def test_existing_deployment_is_deleted_before_it_is_recreated(enabled, store, proxy):
    proxy.models = [{"model_name": "celmis-acme-chat", "model_info": {"id": "stale-1"}}]
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    order = [s.path for s in proxy.sent if s.path in ("/model/delete", "/model/new")]
    assert order[0] == "/model/delete", "/model/new appends — delete has to come first"
    assert proxy.one("/model/delete").body == {"id": "stale-1"}


def test_team_400_is_confirmed_against_team_info(enabled, store, proxy):
    """LiteLLM returns 400 for "already exists" AND for a rejected request."""
    proxy.status_for["/team/new"] = 400

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) == "sk-virtual-1"
    info = proxy.one("/team/info")
    assert info.method == "GET"
    assert info.query == "team_id=ws-acme"


def test_team_400_without_a_team_aborts_provisioning(enabled, store, proxy):
    proxy.status_for["/team/new"] = 400
    proxy.status_for["/team/info"] = 404

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert proxy.count("/model/new") == 0
    assert proxy.count("/key/generate") == 0


def test_unreadable_model_list_never_stacks_a_duplicate_deployment(enabled, store, proxy):
    """/model/new appends. Provisioning without knowing what is already
    registered leaves two deployments under one name, round-robining between
    the fresh provider key and the revoked one."""
    proxy.status_for["/model/info"] = 500

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert proxy.count("/model/new") == 0


def test_key_rotation_revokes_the_old_key_only_after_the_new_one_exists(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaOLD0001", MODELS)
    proxy.models = [
        {"model_name": gateway.deployment_name("acme", s), "model_info": {"id": f"m-{s}"}}
        for s in gateway.SURFACES
    ]
    proxy.sent.clear()

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaNEW0002", MODELS) == "sk-virtual-2"

    seq = [s.path for s in proxy.sent]
    assert seq.index("/key/generate") < seq.index("/key/delete"), (
        "revoking first would leave the tenant with no key if minting failed"
    )
    assert proxy.one("/key/delete").body == {"keys": ["sk-virtual-1"]}


# ─── 3. Degradation — never raise into the caller ────────────────────


@pytest.mark.parametrize("failing", ["/team/new", "/model/new", "/key/generate"])
def test_proxy_failure_degrades_to_direct_provider_calls(enabled, store, proxy, failing):
    proxy.status_for[failing] = 503
    if failing == "/team/new":
        proxy.status_for["/team/info"] = 503

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert gateway.route_for("chat", "acme") is None, (
        "no route == the caller keeps using the tenant's own provider key"
    )


def test_unreachable_proxy_degrades(enabled, store):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def build(*_a, **kw):
        return _REAL_CLIENT(transport=httpx.MockTransport(refuse), timeout=kw.get("timeout", 5.0))

    with patch("src.http.build_client", new=build):
        assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
        assert gateway.provision_workspace("acme") is False
        assert gateway.health() is False
        assert gateway.route_for("chat", "acme") is None


def test_a_half_finished_reprovision_drops_the_stale_route(enabled, store, proxy):
    """The outage this guards against: the upsert deletes the old deployment,
    the recreate fails, and the cached route now names a model the proxy does
    not have — so every call 400s instead of falling back."""
    gateway.ensure_workspace_keys("acme", "google", "AIzaOLD0001", MODELS)
    assert gateway.route_for("chat", "acme") is not None

    proxy.models = [
        {"model_name": gateway.deployment_name("acme", s), "model_info": {"id": f"m-{s}"}}
        for s in gateway.SURFACES
    ]
    proxy.status_for["/model/new"] = 500

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaNEW0002", MODELS) is None
    assert gateway.route_for("chat", "acme") is None
    assert store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme") is None


def test_a_malformed_key_response_is_not_treated_as_a_key(enabled, store, proxy):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"expires": "2030-01-01"})   # no "key"
        return proxy.handler(request)

    with patch("src.http.build_client",
               new=lambda *a, **kw: _REAL_CLIENT(
                   transport=httpx.MockTransport(handler), timeout=5.0)):
        assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme") is None


def test_disabled_gateway_makes_no_request_at_all(store, proxy):
    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert gateway.provision_workspace("acme") is False
    assert gateway.route_for("chat", "acme") is None
    assert gateway.health() is False
    assert proxy.sent == []


# ─── 4. Secrets stay off the wire we log ─────────────────────────────


def test_no_secret_survives_into_the_logs(enabled, store, proxy, caplog):
    import logging

    proxy.status_for["/model/new"] = 500
    with caplog.at_level(logging.DEBUG, logger="src.llm.gateway"):
        gateway.ensure_workspace_keys("acme", "google", "AIzaSUPERSECRET99", MODELS)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "AIzaSUPERSECRET99" not in blob
    assert MASTER not in blob
    assert "sk-virtual-" not in blob
