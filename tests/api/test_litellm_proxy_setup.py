"""A workspace's own LiteLLM proxy on /api/llm — validate-then-save, models, safety.

What must hold:

    1. URL rules: https only; no userinfo/query/fragment; "/" and "/v1"
       normalised; every resolved address public (private, loopback,
       link-local, CGNAT, ULA, mapped, … refused); operator allowlist only via
       LITELLM_PROXY_ALLOWED_HOSTS.
    2. Transport: pinned to the validated IP (SNI/Host = hostname), no
       redirects, 2 MB cap.
    3. Validate-then-save: 401 / redirect / oversize / bad URL → 422 and
       NOTHING written; success → one encrypted row (key AND URL encrypted,
       fingerprint in metadata); audit with fingerprint + host, never the key.
    4. GET /config never carries the key or URL; host only for admins.
    5. Only workspace admins may save / test / delete (403 otherwise).
    6. A "litellm" profile/agent/fallback model must be in the proxy's list.
    7. Workspace isolation; no secret in the logs.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.llm import litellm_proxy

HOST = "litellm.example.com"
BASE = f"https://{HOST}"
PUBLIC_IP = "93.184.216.34"
KEY = "sk-virtual-Qx7v9KpL2mZ8wR4tY6uB"
OTHER_KEY = "sk-other-virtual-key-55555"
_ADMIN = SimpleNamespace(id="u-ops", email="ops@test", is_admin=True)
_MEMBER = SimpleNamespace(id="u-m", email="member@test", is_admin=False)

DNS: dict[str, list[str]] = {}


def _fake_resolve(host: str, port: int) -> list[str]:
    if host in DNS:
        return list(DNS[host])
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        raise OSError("NXDOMAIN") from None


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY",
                "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    DNS.clear()
    DNS.update({HOST: [PUBLIC_IP], "other.example.com": ["93.184.216.99"],
                "localhost": ["127.0.0.1", "::1"]})
    monkeypatch.setattr(litellm_proxy, "_resolve", _fake_resolve)
    litellm_proxy.reset_cache()
    yield
    litellm_proxy.reset_cache()


@pytest.fixture
def store(tmp_path):
    from cryptography.fernet import Fernet

    from src.credentials.store import CredentialStore

    real = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    real.db_file = tmp_path / "creds.db"
    with patch("src.credentials.get_credential_store", return_value=real):
        yield real


def _row(store, ws: str):
    return store.load(provider="litellm", user_id=f"ws:{ws}", account_label="default")


class _Proxy:
    """Stands in for the network UNDER the guarded transport: the allowlist
    and the IP pinning run for real; only the socket is fake."""

    def __init__(self):
        self.status = 200
        self.models = ["chat-a", "embedding-2-test"]
        self.body: bytes | None = None          # raw override for /v1/models
        self.chunked: list[bytes] | None = None
        self.redirect: str | None = None
        self.info: dict | None = None
        self.embed_status = 200
        self.embed_width: int | None = None
        self.requests: list[httpx.Request] = []

    def handle(self, _transport, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.redirect:
            return httpx.Response(302, headers={"location": self.redirect})
        if path == "/v1/embeddings":
            if self.embed_status != 200:
                return httpx.Response(self.embed_status, json={"error": "x"})
            body = json.loads(request.content)
            width = self.embed_width or body.get("dimensions") or 3072
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * width}]})
        if path == "/model/info":
            if self.info is None:
                return httpx.Response(403, json={})
            return httpx.Response(200, json={"data": [
                {"model_name": k, "litellm_params": {"model": v[0]},
                 "model_info": {"mode": v[1]}} for k, v in self.info.items()]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        if self.chunked is not None:
            return httpx.Response(200, content=iter(self.chunked))
        if self.body is not None:
            return httpx.Response(200, content=self.body)
        return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})


@pytest.fixture
def proxy():
    p = _Proxy()

    def handle(transport, request):
        return p.handle(transport, request)

    with patch.object(httpx.HTTPTransport, "handle_request", handle):
        yield p


def _request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/llm/litellm",
        "headers": [], "client": ("203.0.113.9", 51234), "query_string": b"",
    })


def _save(ws: str = "ws-a", base: str = BASE, key: str = KEY, user=_ADMIN):
    from src.api.routers.llm import LiteLLMProxyIn, put_litellm_proxy

    return put_litellm_proxy(LiteLLMProxyIn(base_url=base, api_key=key), _request(),
                             user=user, workspace_id=ws)


def _put(ws: str = "default", **body):
    from src.api.routers.llm import LLMConfigIn, put_config

    return put_config(LLMConfigIn(**body), _request(), user=_ADMIN, workspace_id=ws)


def _test(ws: str = "default", **body):
    from src.api.routers.llm import TestConnectionIn, test_connection

    return test_connection(TestConnectionIn(provider="litellm", **body),
                           user=_ADMIN, workspace_id=ws)


def _refused(ws: str = "ws-a", base: str = BASE, key: str = KEY) -> str:
    with pytest.raises(HTTPException) as exc:
        _save(ws, base, key)
    assert exc.value.status_code == 422
    return str(exc.value.detail)


# ─── 1. URL rules ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    f"http://{HOST}",
    f"ftp://{HOST}",
    HOST,
    f"https://user:pass@{HOST}",
    f"https://user@{HOST}",
    f"{BASE}/?x=1",
    f"{BASE}/v1?key=1",
    f"{BASE}/#frag",
    "https://",
])
def test_a_malformed_or_non_https_url_is_refused_before_any_request(store, proxy, url):
    _refused(base=url)
    assert proxy.requests == []
    assert _row(store, "ws-a") is None


@pytest.mark.parametrize("url", [
    "https://localhost",
    "https://127.0.0.1",
    "https://10.1.2.3",
    "https://172.16.0.1",
    "https://192.168.1.10:4000",
    "https://169.254.169.254",
    "https://[::1]",
    "https://[fc00::1]",
    "https://[fd12:3456::1]",
    "https://[fe80::1]",
    "https://100.64.1.1",
    "https://0.0.0.0",
    "https://224.0.0.1",
    "https://[::ffff:127.0.0.1]",
    "https://[::ffff:169.254.169.254]",
    "https://[64:ff9b::a00:1]",
    "https://[2002:a00:1::1]",
])
def test_a_non_public_address_is_refused(store, proxy, url):
    detail = _refused(base=url)
    assert "non-public" in detail
    assert proxy.requests == []
    assert _row(store, "ws-a") is None


def test_a_hostname_resolving_to_a_private_ip_is_refused(store, proxy):
    DNS["sneaky.example.com"] = ["10.0.0.5"]
    assert "non-public" in _refused(base="https://sneaky.example.com")
    # ONE private record among public ones is enough.
    DNS["mixed.example.com"] = [PUBLIC_IP, "169.254.169.254"]
    assert "non-public" in _refused(base="https://mixed.example.com")
    assert proxy.requests == []


def test_an_unresolvable_host_is_refused(store, proxy):
    assert "does not resolve" in _refused(base="https://nowhere.example.com")


@pytest.mark.parametrize("raw, want", [
    (BASE + "/", BASE),
    (BASE + "/v1", BASE),
    (BASE + "/v1/", BASE),
    ("HTTPS://LiteLLM.Example.com:443/", BASE),
    (BASE + ":8443/gw/v1", BASE + ":8443/gw"),
])
def test_the_url_is_normalised(raw, want):
    assert litellm_proxy.normalise_base_url(raw) == want


def test_the_operator_allowlist_admits_a_lan_proxy_but_never_link_local(store, proxy):
    from src.config import get_settings

    DNS["litellm.corp.internal"] = ["10.20.0.5"]
    DNS["meta.corp.internal"] = ["169.254.169.254"]
    assert "LITELLM_PROXY_ALLOWED_HOSTS" in _refused(base="https://litellm.corp.internal")

    s = get_settings().model_copy(update={"litellm_proxy_allowed_hosts": ["corp.internal"]})
    with patch("src.config.get_settings", return_value=s):
        _save("ws-a", "https://litellm.corp.internal")
        assert _row(store, "ws-a") is not None
        _refused("ws-b", "https://meta.corp.internal")
    assert _row(store, "ws-b") is None


def test_the_private_network_flag_does_not_open_this_path(store, proxy):
    from src.config import get_settings

    s = get_settings().model_copy(update={"egress_allow_private_network": True})
    with patch("src.config.get_settings", return_value=s):
        _refused(base="https://10.0.0.5")


# ─── 2. Transport ─────────────────────────────────────────────────────


def test_the_request_is_pinned_to_the_validated_ip(store, proxy):
    _save()
    req = proxy.requests[0]
    assert req.url.host == PUBLIC_IP                     # connected to the IP
    assert req.url.path == "/v1/models"
    assert req.headers["host"] == HOST                   # Host header kept
    assert req.extensions["sni_hostname"] == HOST        # TLS name kept
    assert req.headers["authorization"] == f"Bearer {KEY}"


def test_a_redirect_is_not_followed(store, proxy):
    proxy.redirect = "https://169.254.169.254/latest/meta-data/"
    assert "redirect" in _refused()
    assert len(proxy.requests) == 1
    assert _row(store, "ws-a") is None


def test_an_oversized_model_list_is_refused(store, proxy):
    proxy.body = b'{"data": [' + b" " * (litellm_proxy.MAX_RESPONSE_BYTES + 10) + b"]}"
    assert "too large" in _refused()
    assert _row(store, "ws-a") is None


def test_an_oversized_chunked_model_list_is_refused(store, proxy):
    chunk = b" " * (512 * 1024)
    proxy.chunked = [b'{"data": ['] + [chunk] * 5 + [b"]}"]
    assert "too large" in _refused()
    assert _row(store, "ws-a") is None


# ─── 3. Validate-then-save ────────────────────────────────────────────


def test_a_rejected_key_saves_nothing(store, proxy):
    proxy.status = 401
    assert "rejected the virtual key" in _refused()
    assert _row(store, "ws-a") is None


def test_a_rejected_key_keeps_the_previous_row(store, proxy):
    _save()
    proxy.status = 401
    _refused(key=OTHER_KEY)
    assert json.loads(_row(store, "ws-a").secret)["key"] == KEY


@pytest.mark.parametrize("body", [b"not json", b'{"object": "list"}', b'{"data": []}'])
def test_a_proxy_without_a_model_list_saves_nothing(store, proxy, body):
    proxy.body = body
    _refused()
    assert _row(store, "ws-a") is None


def test_success_is_encrypted_at_rest(store, proxy):
    out = _save(base=BASE + "/v1/")
    assert out.models == ["chat-a", "embedding-2-test"]
    row = _row(store, "ws-a")
    assert json.loads(row.secret) == {"url": BASE, "key": KEY}
    assert row.metadata["fingerprint"] == litellm_proxy.fingerprint(KEY)
    assert len(row.metadata["fingerprint"]) == 12
    assert "base_url" not in row.metadata
    raw = b"".join(p.read_bytes() for p in store.db_file.parent.glob("creds.db*"))
    assert KEY.encode() not in raw
    assert HOST.encode() not in raw
    assert BASE.encode() not in raw


def test_the_save_is_audited_without_the_key(store, proxy):
    with patch("src.api.routers.llm.record_action") as audit:
        _save()
    kw = audit.call_args.kwargs
    assert kw["action"] == "llm_key.saved" and kw["workspace_id"] == "ws-a"
    assert kw["actor"] == _ADMIN.email
    assert kw["detail"]["fingerprint"] == litellm_proxy.fingerprint(KEY)
    assert kw["detail"]["host"] == HOST
    assert KEY not in json.dumps(kw, default=str)


def test_the_delete_is_audited_and_scoped(store, proxy):
    from src.api.routers.llm import delete_litellm_proxy

    _save("ws-a")
    _save("ws-b", key=OTHER_KEY)
    with patch("src.api.routers.llm.record_action") as audit:
        out = delete_litellm_proxy(_request(), user=_ADMIN, workspace_id="ws-a")
    assert out.litellm.connected is False
    assert _row(store, "ws-a") is None and _row(store, "ws-b") is not None
    kw = audit.call_args.kwargs
    assert kw["action"] == "llm_key.deleted"
    assert kw["detail"]["fingerprint"] == litellm_proxy.fingerprint(KEY)
    assert kw["detail"]["host"] == HOST
    assert KEY not in json.dumps(kw, default=str)


def test_a_bare_key_through_put_config_is_refused(store):
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", provider_keys={"litellm": KEY})
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        _put("ws-a", provider="litellm", api_key=KEY)
    assert _row(store, "ws-a") is None


# ─── 4. What GET /config shows ────────────────────────────────────────


def test_get_config_never_carries_the_key_or_url(store, proxy):
    from src.api.routers.llm import get_config

    _save()
    with patch("src.api.deps.is_workspace_admin", return_value=False):
        member = get_config(user=_MEMBER, workspace_id="ws-a")
    admin = get_config(user=_ADMIN, workspace_id="ws-a")
    for cfg in (member, admin):
        dumped = cfg.model_dump_json()
        assert KEY not in dumped and BASE not in dumped
        assert cfg.litellm.connected is True
        assert cfg.litellm.masked == "…" + KEY[-4:]
        assert cfg.litellm.fingerprint == litellm_proxy.fingerprint(KEY)
    assert member.litellm.host is None
    assert admin.litellm.host == HOST


# ─── 5. Only workspace admins ─────────────────────────────────────────


def test_a_non_admin_gets_403_and_nothing_changes(store, proxy):
    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import llm as llm_router

    _save()
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _MEMBER
    app.dependency_overrides[current_workspace_id] = lambda: "ws-a"
    client = TestClient(app)
    with patch("src.api.deps.is_workspace_admin", return_value=False):
        r1 = client.put("/api/llm/litellm",
                        json={"base_url": "https://other.example.com", "api_key": OTHER_KEY})
        r2 = client.post("/api/llm/test-connection",
                         json={"provider": "litellm", "api_key": "use-saved"})
        r3 = client.delete("/api/llm/litellm")
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)
    assert json.loads(_row(store, "ws-a").secret) == {"url": BASE, "key": KEY}
    assert len(proxy.requests) == 1          # only the admin's original save
    with patch("src.api.deps.is_workspace_admin", return_value=True):
        ok = client.put("/api/llm/litellm",
                        json={"base_url": "https://other.example.com", "api_key": OTHER_KEY})
    assert ok.status_code == 200, ok.text
    assert ok.json()["litellm"]["host"] == "other.example.com"
    assert KEY not in ok.text and OTHER_KEY not in ok.text


# ─── 6. Models come from the proxy's list ────────────────────────────


def test_a_profile_model_must_be_in_the_proxy_list(store, proxy):
    _save()
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "gpt-made-up"}})
    assert exc.value.status_code == 422 and "not offered" in exc.value.detail
    out = _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})
    assert out.profiles["chat"].provider == "litellm"
    assert out.profiles["chat"].model == "chat-a"


def test_a_profile_needs_a_saved_proxy(store, proxy):
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"review": {"provider": "litellm", "model": "chat-a"}})
    assert exc.value.status_code == 422


def test_agent_and_fallback_models_must_be_in_the_list(store, proxy):
    _save()
    _put("ws-a", profiles={"review": {"provider": "litellm", "model": "chat-a"}})
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", review_fallback_model="free-text-model")
    assert "not offered" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", agents={"architect": {"model": "free-text-model"}})
    assert "not offered" in exc.value.detail


def test_an_unreachable_proxy_refuses_the_profile(store, proxy):
    _save()
    proxy.status = 503
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})
    assert "cannot verify" in exc.value.detail


def test_embeddings_via_the_proxy_only_in_the_default_workspace(store, proxy):
    _save("default")
    out = _put("default", profiles={"embeddings": {"provider": "litellm",
                                                   "model": "embedding-2-test",
                                                   "dimensions": 768}})
    assert out.profiles["embeddings"].provider == "litellm"
    _save("ws-a")
    with pytest.raises(HTTPException) as exc:
        _put("ws-a", profiles={"embeddings": {"provider": "litellm",
                                              "model": "embedding-2-test"}})
    assert "default workspace" in exc.value.detail


def test_models_split_by_mode_or_name(store, proxy):
    from src.api.routers.llm import provider_models

    _save()
    out = provider_models("litellm", user=_ADMIN, workspace_id="ws-a")
    assert (out.generation, out.embedding) == (["chat-a"], ["embedding-2-test"])
    litellm_proxy.reset_cache()
    proxy.models = ["vectors", "chat-a"]
    proxy.info = {"vectors": ("gemini/gemini-embedding-001", "embedding"),
                  "chat-a": ("gemini/gemini-3-flash", "chat")}
    out = provider_models("litellm", user=_ADMIN, workspace_id="ws-a")
    assert (out.generation, out.embedding) == (["chat-a"], ["vectors"])


# ─── 7. Isolation, Test, logs ─────────────────────────────────────────


def test_workspace_b_sees_nothing_of_workspace_a(store, proxy):
    from src.api.routers.llm import get_config, provider_models
    from src.llm.profiles import resolve_profile

    _save("ws-a")
    _put("ws-a", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})
    cfg_b = get_config(user=_ADMIN, workspace_id="ws-b")
    assert cfg_b.litellm.connected is False and cfg_b.litellm.host is None
    assert KEY[-4:] not in cfg_b.model_dump_json()
    assert provider_models("litellm", user=_ADMIN, workspace_id="ws-b").generation == []
    assert _test("ws-b", api_key="use-saved").ok is False
    a = resolve_profile("chat", "ws-a")
    assert (a.api_base, a.api_key) == (BASE, KEY)
    with pytest.raises(HTTPException):
        _put("ws-b", profiles={"chat": {"provider": "litellm", "model": "chat-a"}})


def test_test_connection_saves_nothing_and_never_resends_a_saved_key(store, proxy):
    r = _test("ws-a", api_key=KEY, base_url=BASE)
    assert r.ok is True and r.models == ["chat-a", "embedding-2-test"]
    assert _row(store, "ws-a") is None
    _save("ws-a")
    n = len(proxy.requests)
    r = _test("ws-a", api_key="use-saved", base_url="https://other.example.com")
    assert r.ok is False and len(proxy.requests) == n
    assert _test("ws-a", api_key="use-saved").ok is True


def test_an_embeddings_test_reports_the_width(store, proxy):
    _save("default")
    r = _test(api_key="use-saved", surface="embeddings",
              model="embedding-2-test", dimensions=768)
    assert r.ok is True and r.vector_width == 768
    emb = [q for q in proxy.requests if q.url.path == "/v1/embeddings"]
    assert len(emb) == 1 and emb[0].url.host == PUBLIC_IP
    proxy.embed_width = 3072
    r = _test(api_key="use-saved", surface="embeddings",
              model="embedding-2-test", dimensions=768)
    assert r.ok is True and r.warning and "768" in r.warning


def test_no_key_or_url_reaches_the_log(store, proxy, caplog):
    from src.api.routers.llm import delete_litellm_proxy, get_config, provider_models

    caplog.set_level(logging.DEBUG)
    _save("ws-a")
    get_config(user=_ADMIN, workspace_id="ws-a")
    provider_models("litellm", user=_ADMIN, workspace_id="ws-a")
    _test("ws-a", api_key="use-saved")
    _test("ws-a", api_key=OTHER_KEY, base_url="https://10.0.0.1")
    proxy.status = 401
    _refused("ws-a", key=OTHER_KEY)
    proxy.status = 200
    DNS["sneaky.example.com"] = ["10.0.0.5"]
    _refused("ws-a", base="https://sneaky.example.com/v1", key=OTHER_KEY)
    delete_litellm_proxy(_request(), user=_ADMIN, workspace_id="ws-a")
    text = caplog.text
    assert caplog.records, "the paths above are expected to log something"
    for secret in (KEY, OTHER_KEY, BASE, "https://sneaky.example.com"):
        assert secret not in text
