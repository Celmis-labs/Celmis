"""A workspace's own LiteLLM proxy on /api/llm — save, test, list models.

What must hold:

    1. The proxy is ONE credential (base URL + virtual key) in slot ws:{id}.
       A changed URL needs the key typed again, on save and on Test — a saved
       key is never sent to a new address.
    2. Test pings {base}/v1/models with the virtual key and reports the model
       ids; a 401 says the PROXY rejected the key.
    3. /models?provider=litellm lists the proxy's aliases, split by mode.
    4. Another workspace sees neither the key nor the URL.
    5. A "litellm" profile is refused while no proxy resolves; embeddings may
       use it only in the default workspace (the shared embeddings profile).
    6. The OpenAI 401 points at the proxy row; LLMConfigOut reports
       gateway_enabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

BASE = "https://litellm.example.com"
KEY = "sk-virtual-key-1234567890"
_ADMIN = SimpleNamespace(id="u-ops", email="ops@test", is_admin=True)


class _FakeStore:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], SimpleNamespace] = {}

    def save(self, *, provider, secret, metadata=None, user_id="", account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=metadata or {},
        )

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))


@pytest.fixture
def store(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "LITELLM_API_KEY", "LITELLM_API_BASE", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from src.llm import litellm_proxy
    litellm_proxy.reset_cache()
    fake = _FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


class _Proxy:
    """A fake proxy behind httpx.MockTransport; records every request."""

    def __init__(self, *, status: int = 200, models=("chat-a", "embedding-2-test"),
                 info: dict | None = None):
        self.status = status
        self.models = list(models)
        self.info = info
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/model/info":
            if self.info is None:
                return httpx.Response(403, json={})
            return httpx.Response(200, json={"data": [
                {"model_name": k, "litellm_params": {"model": v[0]},
                 "model_info": {"mode": v[1]}}
                for k, v in self.info.items()
            ]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})

    def client(self, base_url, *, timeout):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def proxy():
    p = _Proxy()
    with patch("src.llm.litellm_proxy.ping_client", side_effect=p.client):
        yield p


def _request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/llm/config",
        "headers": [], "client": ("203.0.113.9", 51234), "query_string": b"",
    })


def _put(ws: str = "default", **body):
    from src.api.routers.llm import LLMConfigIn, put_config

    return put_config(LLMConfigIn(**body), _request(), user=_ADMIN, workspace_id=ws)


def _test(ws: str = "default", **body):
    from src.api.routers.llm import TestConnectionIn, test_connection

    return test_connection(TestConnectionIn(provider="litellm", **body),
                           user=_ADMIN, workspace_id=ws)


# ─── 1. One credential ────────────────────────────────────────────────


def test_the_pair_is_saved_in_the_workspace_slot(store):
    out = _put("ws-a", litellm={"base_url": BASE + "/", "api_key": KEY})
    row = store.rows[("litellm", "ws:ws-a", "default")]
    assert row.secret == KEY
    assert row.metadata["base_url"] == BASE          # trailing slash stripped
    assert out.litellm.connected is True
    assert out.litellm.base_url == BASE
    assert KEY not in out.litellm.masked
    assert out.litellm.source == "ui"


def test_a_new_url_without_the_key_is_refused(store):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", litellm={"base_url": "https://evil.example.com"})
    assert exc.value.status_code == 422
    assert store.rows[("litellm", "ws:ws-a", "default")].metadata["base_url"] == BASE


def test_a_key_only_update_keeps_the_url(store):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    _put("ws-a", provider_keys={"litellm": "sk-rotated-key-0987654321"})
    row = store.rows[("litellm", "ws:ws-a", "default")]
    assert row.secret == "sk-rotated-key-0987654321"
    assert row.metadata["base_url"] == BASE


def test_a_key_with_no_url_anywhere_is_refused(store):
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", provider_keys={"litellm": KEY})
    assert exc.value.status_code == 422


def test_a_non_http_url_is_refused(store):
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", litellm={"base_url": "litellm:4000", "api_key": KEY})
    assert exc.value.status_code == 422


# ─── 2. Test connection ───────────────────────────────────────────────


def test_test_lists_the_proxys_models_with_the_bearer_key(store, proxy):
    r = _test(api_key=KEY, base_url=BASE)
    assert r.ok is True
    assert r.models == ["chat-a", "embedding-2-test"]
    assert r.models_available == 2
    req = proxy.requests[0]
    assert str(req.url) == f"{BASE}/v1/models"
    assert req.headers["authorization"] == f"Bearer {KEY}"


def test_a_v1_suffix_is_not_doubled(store, proxy):
    _test(api_key=KEY, base_url=BASE + "/v1")
    assert str(proxy.requests[0].url) == f"{BASE}/v1/models"


def test_test_uses_the_saved_pair(store, proxy):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    r = _test("ws-a", api_key="use-saved")
    assert r.ok is True
    assert proxy.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_the_saved_key_is_never_sent_to_a_new_url(store, proxy):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    r = _test("ws-a", api_key="use-saved", base_url="https://evil.example.com")
    assert r.ok is False
    assert proxy.requests == []


def test_a_rejected_key_says_the_proxy_rejected_it(store, proxy):
    proxy.status = 401
    r = _test(api_key=KEY, base_url=BASE)
    assert r.ok is False
    assert "proxy rejected" in r.detail


# ─── 3. Model listing ─────────────────────────────────────────────────


def test_models_split_by_name_when_model_info_is_refused(store, proxy):
    from src.api.routers.llm import provider_models

    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    out = provider_models("litellm", user=_ADMIN, workspace_id="ws-a")
    assert out.generation == ["chat-a"]
    assert out.embedding == ["embedding-2-test"]


def test_models_split_by_mode_when_model_info_answers(store, proxy):
    from src.api.routers.llm import provider_models

    proxy.models = ["vectors", "chat-a"]
    proxy.info = {"vectors": ("gemini/gemini-embedding-001", "embedding"),
                  "chat-a": ("gemini/gemini-3-flash", "chat")}
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    out = provider_models("litellm", user=_ADMIN, workspace_id="ws-a")
    assert out.embedding == ["vectors"]
    assert out.generation == ["chat-a"]


# ─── 4. Workspace isolation ───────────────────────────────────────────


def test_another_workspace_sees_neither_key_nor_url(store, proxy):
    from src.api.routers.llm import get_config, provider_models
    from src.llm.profiles import resolve_profile

    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})

    cfg_b = get_config(user=_ADMIN, workspace_id="ws-b")
    assert cfg_b.litellm.connected is False
    assert cfg_b.litellm.base_url is None
    assert provider_models("litellm", user=_ADMIN, workspace_id="ws-b").generation == []
    assert _test("ws-b", api_key="use-saved").ok is False
    # The profile resolution side: ws-a carries the pair, ws-b nothing.
    a = resolve_profile("chat", "ws-a")
    assert (a.api_base, a.api_key) == (BASE, KEY)
    with pytest.raises(HTTPException):
        _put("ws-b", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})


# ─── 5. Profiles ──────────────────────────────────────────────────────


def test_a_litellm_profile_needs_a_proxy(store):
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"review": {"provider": "litellm", "model": "chat-a"}})
    assert exc.value.status_code == 422


def test_proxy_and_profile_in_one_request(store):
    out = _put("ws-a", litellm={"base_url": BASE, "api_key": KEY},
               profiles={"review": {"provider": "litellm", "model": "chat-a"}})
    assert out.profiles["review"].provider == "litellm"
    assert out.profiles["review"].base_url == BASE


def test_a_litellm_profile_takes_no_per_surface_base_url(store):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    with pytest.raises(HTTPException):
        _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "x",
                                        "base_url": "https://other.example.com"}})


def test_embeddings_via_the_proxy_in_the_default_workspace(store):
    out = _put("default", litellm={"base_url": BASE, "api_key": KEY},
               profiles={"embeddings": {"provider": "litellm",
                                        "model": "embedding-2-test",
                                        "dimensions": 768}})
    assert out.litellm_embeddings_allowed is True
    assert out.profiles["embeddings"].provider == "litellm"


def test_embeddings_via_the_proxy_are_refused_elsewhere(store):
    _put("ws-a", litellm={"base_url": BASE, "api_key": KEY})
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"embeddings": {"provider": "litellm",
                                              "model": "embedding-2-test"}})
    assert "default workspace" in exc.value.detail


# ─── 6. Neighbouring fixes ────────────────────────────────────────────


def test_the_openai_401_points_at_the_proxy_row(store):
    from src.api.routers.llm import TestConnectionIn, test_connection

    def client(endpoint, *, timeout):
        return httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(401, json={})))

    with patch("src.api.routers.llm._provider_ping_client", side_effect=client), \
         patch("src.api.routers.llm._record_verified"):
        r = test_connection(TestConnectionIn(provider="openai", api_key=KEY),
                            user=_ADMIN, workspace_id="default")
    assert r.ok is False
    assert "invalid API key (401)" in r.detail
    assert "LiteLLM proxy row" in r.detail


def test_the_config_reports_gateway_mode(store):
    from src.api.routers.llm import get_config

    with patch("src.llm.gateway.is_enabled", return_value=True):
        assert get_config(user=_ADMIN, workspace_id="default").gateway_enabled is True
    with patch("src.llm.gateway.is_enabled", return_value=False):
        assert get_config(user=_ADMIN, workspace_id="default").gateway_enabled is False
