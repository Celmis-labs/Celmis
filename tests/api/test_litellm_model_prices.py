"""Manual prices for workspace-proxy aliases on /api/llm/litellm/prices.

What must hold:

    1. GET lists every alias on the CURRENT workspace's proxy with its
       effective price and source (manual / proxy / litellm / unknown), plus
       manual entries for aliases the proxy no longer lists (stale). No URL,
       host or key in the answer. No proxy → empty, not an error.
    2. Any member may GET; only a workspace admin may PUT (403 otherwise).
    3. PUT validates everything before writing: finite, 0..1000 USD per 1M
       tokens, alias on the proxy's list (null — delete — always allowed),
       ≤ 200 aliases. Any failure → 422 (409 with no proxy) and nothing
       written.
    4. null deletes; the change is audited (who, alias, old → new), never a
       secret.
    5. Saving the main LLM form keeps the prices.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.llm import litellm_proxy

HOST = "litellm.example.com"
BASE = f"https://{HOST}"
KEY = "sk-virtual-Qx7v9KpL2mZ8wR4tY6uB"
_ADMIN = SimpleNamespace(id="u-ops", email="ops@test", is_admin=True)
_MEMBER = SimpleNamespace(id="u-m", email="member@test", is_admin=False)


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(litellm_proxy, "_resolve",
                        lambda host, port: ["93.184.216.34"])
    litellm_proxy.reset_cache()
    yield
    litellm_proxy.reset_cache()


@pytest.fixture
def store(tmp_path):
    from cryptography.fernet import Fernet

    from src.credentials.store import CredentialStore

    real = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    with patch("src.credentials.get_credential_store", return_value=real):
        yield real


class _Proxy:
    def __init__(self):
        # alias → (underlying, mode, input_cost_per_token, output_cost_per_token)
        self.info = {
            "custom-ft": ("acme/finetune-x", "chat", None, None),
            "declared": ("acme/other", "chat", 1e-6, 2e-6),
            "table": ("gpt-4o", "chat", None, None),
        }

    def handle(self, _transport, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": k, "litellm_params": {"model": v[0]},
                 "model_info": {"mode": v[1], "input_cost_per_token": v[2],
                                "output_cost_per_token": v[3]}}
                for k, v in self.info.items()]})
        return httpx.Response(200, json={"data": [{"id": m} for m in self.info]})


@pytest.fixture
def proxy():
    p = _Proxy()
    with patch.object(httpx.HTTPTransport, "handle_request",
                      lambda transport, request: p.handle(transport, request)):
        yield p


def _connect(ws: str = "ws-a") -> None:
    litellm_proxy.save_endpoint(ws, litellm_proxy.Endpoint(base_url=BASE, api_key=KEY))


def _request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/llm/litellm/prices",
        "headers": [], "client": ("203.0.113.9", 51234), "query_string": b"",
    })


def _put(prices: dict, ws: str = "ws-a"):
    from src.api.routers.llm import ModelPricesIn, put_litellm_prices

    return put_litellm_prices(ModelPricesIn(prices=prices), _request(),
                              user=_ADMIN, workspace_id=ws)


def _get(ws: str = "ws-a", user=_ADMIN):
    from src.api.routers.llm import get_litellm_prices

    with patch("src.api.deps.is_workspace_admin", return_value=user is _ADMIN):
        return get_litellm_prices(user=user, workspace_id=ws)


def _stored(ws: str = "ws-a") -> dict:
    from src.api.routers.llm import _load_workspace_config

    return _load_workspace_config(ws).get("model_prices") or {}


def _rows(out) -> dict:
    return {r.alias: r for r in out.prices}


# ─── 1. GET ───────────────────────────────────────────────────────────


def test_every_alias_is_listed_with_its_source(store, proxy):
    _connect()
    rows = _rows(_get())
    assert rows["custom-ft"].price_source == "unknown"
    assert rows["custom-ft"].input_per_mtok is None
    assert rows["declared"].price_source == "proxy"
    assert (rows["declared"].input_per_mtok, rows["declared"].output_per_mtok) == (1.0, 2.0)
    assert rows["table"].price_source == "litellm"
    assert rows["table"].underlying == "gpt-4o"
    assert rows["table"].input_per_mtok and rows["table"].input_per_mtok > 0


def test_a_manual_price_wins_and_is_shown_as_such(store, proxy):
    _connect()
    _put({"declared": {"input_per_mtok": 0.25, "output_per_mtok": 1.25}})
    row = _rows(_get())["declared"]
    assert row.price_source == "manual"
    assert (row.input_per_mtok, row.output_per_mtok) == (0.25, 1.25)
    assert row.manual is not None and row.manual.updated_at


def test_no_proxy_is_an_empty_list(store):
    out = _get()
    assert out.connected is False and out.prices == []


def test_the_answer_carries_no_host_or_key(store, proxy):
    _connect()
    dumped = _get(user=_MEMBER).model_dump_json()
    assert HOST not in dumped and KEY not in dumped


def test_a_stale_manual_price_is_listed(store, proxy):
    _connect()
    _put({"custom-ft": {"input_per_mtok": 3, "output_per_mtok": 4}})
    del proxy.info["custom-ft"]
    litellm_proxy.reset_cache()
    row = _rows(_get())["custom-ft"]
    assert row.stale is True
    assert row.price_source == "manual"
    assert not _rows(_get())["declared"].stale


# ─── 2. RBAC ──────────────────────────────────────────────────────────


def test_a_member_reads_but_cannot_write(store, proxy):
    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import llm as llm_router

    _connect()
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _MEMBER
    app.dependency_overrides[current_workspace_id] = lambda: "ws-a"
    client = TestClient(app)
    with patch("src.api.deps.is_workspace_admin", return_value=False):
        r_get = client.get("/api/llm/litellm/prices")
        r_put = client.put("/api/llm/litellm/prices", json={
            "prices": {"custom-ft": {"input_per_mtok": 1, "output_per_mtok": 1}}})
    assert r_get.status_code == 200
    assert r_get.json()["can_edit"] is False
    assert r_put.status_code == 403
    assert _stored() == {}


def test_an_admin_writes_over_http(store, proxy):
    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import llm as llm_router

    _connect()
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _ADMIN
    app.dependency_overrides[current_workspace_id] = lambda: "ws-a"
    client = TestClient(app)
    with patch("src.api.deps.is_workspace_admin", return_value=True):
        r = client.put("/api/llm/litellm/prices", json={
            "prices": {"custom-ft": {"input_per_mtok": 1.5, "output_per_mtok": 6}}})
    assert r.status_code == 200, r.text
    assert r.json()["can_edit"] is True
    assert _stored()["custom-ft"]["input_per_mtok"] == 1.5
    assert _stored()["custom-ft"]["updated_by"] == "ops@test"


# ─── 3. Validation — nothing written ──────────────────────────────────


@pytest.mark.parametrize("body", [
    {"prices": {"custom-ft": {"input_per_mtok": -1, "output_per_mtok": 1}}},
    {"prices": {"custom-ft": {"input_per_mtok": 1001, "output_per_mtok": 1}}},
    {"prices": {"custom-ft": {"input_per_mtok": "1", "output_per_mtok": 1}}},
    {"prices": {"custom-ft": {"input_per_mtok": 1}}},
    {"prices": {"custom-ft": {"input_per_mtok": 1, "output_per_mtok": 1, "x": 1}}},
    {"prices": {"custom-ft": {"input_per_mtok": 1, "output_per_mtok": 1},
                "not-on-the-proxy": {"input_per_mtok": 1, "output_per_mtok": 1}}},
    {"prices": {f"a{i}": None for i in range(201)}},
])
def test_bad_input_is_422_and_nothing_is_written(store, proxy, body):
    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import llm as llm_router

    _connect()
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _ADMIN
    app.dependency_overrides[current_workspace_id] = lambda: "ws-a"
    client = TestClient(app)
    with patch("src.api.deps.is_workspace_admin", return_value=True), \
         patch("src.api.routers.llm.record_action") as audit:
        r = client.put("/api/llm/litellm/prices", json=body)
    assert r.status_code == 422, r.text
    assert _stored() == {}
    audit.assert_not_called()


def test_a_non_finite_number_is_refused():
    from pydantic import ValidationError

    from src.api.routers.llm import ModelPriceIn

    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            ModelPriceIn(input_per_mtok=bad, output_per_mtok=1)


def test_no_proxy_is_409_and_nothing_written(store):
    with pytest.raises(HTTPException) as exc:
        _put({"custom-ft": {"input_per_mtok": 1, "output_per_mtok": 1}})
    assert exc.value.status_code == 409
    assert _stored() == {}


# ─── 4. Delete, audit ─────────────────────────────────────────────────


def test_null_deletes_and_a_stale_entry_can_be_cleared(store, proxy):
    _connect()
    _put({"custom-ft": {"input_per_mtok": 3, "output_per_mtok": 4},
          "declared": {"input_per_mtok": 1, "output_per_mtok": 1}})
    del proxy.info["custom-ft"]
    litellm_proxy.reset_cache()
    _put({"custom-ft": None})                 # stale, no longer on the proxy
    assert set(_stored()) == {"declared"}
    _put({"declared": None})
    assert _stored() == {}
    assert _rows(_get())["declared"].price_source == "proxy"


def test_the_change_is_audited_without_secrets(store, proxy):
    _connect()
    with patch("src.api.routers.llm.record_action") as audit:
        _put({"custom-ft": {"input_per_mtok": 2, "output_per_mtok": 3}})
        _put({"custom-ft": {"input_per_mtok": 5, "output_per_mtok": 3}})
    first, second = (c.kwargs for c in audit.call_args_list)
    assert first["action"] == "llm_model_price.updated"
    assert first["actor"] == "ops@test" and first["workspace_id"] == "ws-a"
    assert first["detail"]["changes"] == [{
        "alias": "custom-ft", "old": None,
        "new": {"input_per_mtok": 2.0, "output_per_mtok": 3.0}}]
    assert second["detail"]["changes"][0]["old"] == {
        "input_per_mtok": 2.0, "output_per_mtok": 3.0}
    dumped = json.dumps([first, second], default=str)
    assert KEY not in dumped and HOST not in dumped and BASE not in dumped


def test_prices_are_per_workspace(store, proxy):
    _connect("ws-a")
    _connect("ws-b")
    _put({"custom-ft": {"input_per_mtok": 3, "output_per_mtok": 4}}, ws="ws-a")
    assert _rows(_get("ws-b"))["custom-ft"].price_source == "unknown"


# ─── 5. The main form keeps them ──────────────────────────────────────


def test_saving_the_llm_form_keeps_the_prices(store, proxy):
    from src.api.routers.llm import LLMConfigIn, put_config

    _connect()
    _put({"custom-ft": {"input_per_mtok": 3, "output_per_mtok": 4}})
    with patch("src.api.deps.is_workspace_admin", return_value=True):
        put_config(LLMConfigIn(docs_language="de"), _request(), user=_ADMIN,
                   workspace_id="ws-a")
    assert _stored()["custom-ft"]["input_per_mtok"] == 3
