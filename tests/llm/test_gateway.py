"""Tests for `src.llm.gateway` — the LiteLLM proxy provisioning layer.

Coverage:
    1. is_enabled — both env vars, one, neither, placeholder / bad-prefix key
    2. ensure_workspace_keys — happy path: team + one deployment per surface +
       ONE scoped virtual key, cached in the credential store
    3. idempotency — a second identical call makes NO second /key/generate
    4. re-provisioning — a rotated provider key mints a fresh key and revokes
       the previous one
    5. fallback — 5xx anywhere on the proxy returns None (caller keeps using
       the tenant's direct provider key)
    6. the virtual key is NEVER generated with an empty `models` list
       (on LiteLLM that means "all models" — i.e. every other tenant's)
    7. secrets never reach the logs, even when the proxy echoes the request
    8. profiles/completion stay untouched when the gateway is off

Every proxy call is a mocked httpx request; nothing hits the network.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from src.llm import gateway

# ─── Fakes ───────────────────────────────────────────────────────────


@dataclass
class _Stored:
    secret: str
    metadata: dict


class FakeStore:
    """In-memory stand-in for the encrypted credential store."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], _Stored] = {}

    def save(self, provider, secret, *, metadata=None, user_id="default",
             account_label="default"):
        # Round-trip through JSON like the real (SQLite) store does.
        self.rows[(user_id, provider, account_label)] = _Stored(
            secret=secret, metadata=json.loads(json.dumps(metadata or {})),
        )

    def load(self, provider, *, user_id="default", account_label="default",
             update_last_used=True):
        return self.rows.get((user_id, provider, account_label))

    def delete(self, provider, *, user_id="default", account_label="default"):
        return self.rows.pop((user_id, provider, account_label), None) is not None


class FakeProxy:
    """Scripted LiteLLM proxy. Records every request it is asked to serve."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.status_for: dict[str, int] = {}
        self.models: list[dict] = []
        self.key_counter = 0

    # -- helpers ------------------------------------------------------
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]

    def count(self, path: str) -> int:
        return sum(1 for p in self.paths() if p == path)

    def payload_for(self, path: str) -> dict:
        for _, p, body in self.calls:
            if p == path:
                return body
        raise AssertionError(f"{path} was never called")

    # -- the transport ------------------------------------------------
    def request(self, method, url, json=None, headers=None, **_kw):
        path = url.split("4000", 1)[-1] if "4000" in url else url
        for prefix in ("http://litellm:4000", "http://proxy.test"):
            if url.startswith(prefix):
                path = url[len(prefix):]
                break
        self.calls.append((method, path, json or {}))
        status = self.status_for.get(path, 200)
        body: dict = {}
        if path == "/model/info":
            body = {"data": self.models}
        elif path == "/key/generate" and status < 400:
            self.key_counter += 1
            body = {"key": f"sk-virtual-{self.key_counter}"}
        elif status >= 400:
            # LiteLLM error bodies echo the request back — api_key included.
            body = {"error": {"message": "boom", "request": json or {}}}
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = __import__("json").dumps(body)
        return resp


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_gateway_state(monkeypatch):
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_TIMEOUT", raising=False)
    # `is_enabled` mirrors the URL here for LiteLLM's SDK; registering the
    # delete makes monkeypatch undo that write when the test ends.
    monkeypatch.delenv("LITELLM_PROXY_API_BASE", raising=False)
    gateway.reset_cache()
    yield
    gateway.reset_cache()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")


@pytest.fixture
def store():
    fake = FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


@pytest.fixture
def proxy():
    fake = FakeProxy()
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.request.side_effect = fake.request
    with patch("src.http.build_client", return_value=client):
        yield fake


MODELS = {
    "chat": "gemini-3-flash-preview",
    "review": "gemini-3.1-pro-preview",
    "embeddings": "gemini-embedding-2",
}


# ─── 1. is_enabled ───────────────────────────────────────────────────


def test_is_enabled_requires_both_env_vars(monkeypatch):
    assert gateway.is_enabled() is False

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    assert gateway.is_enabled() is False, "url alone must not enable the gateway"

    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")
    assert gateway.is_enabled() is True

    monkeypatch.delenv("LITELLM_PROXY_URL")
    assert gateway.is_enabled() is False, "key alone must not enable the gateway"


@pytest.mark.parametrize("bad_key", ["", "replace-me", "master-no-prefix", "sk-x"])
def test_is_enabled_rejects_unusable_master_key(monkeypatch, bad_key):
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", bad_key)
    assert gateway.is_enabled() is False


def test_is_enabled_does_no_io(monkeypatch):
    """It runs on the hot path of every profile resolution."""
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")
    with patch("src.http.build_client") as client:
        assert gateway.is_enabled() is True
    client.assert_not_called()


def test_enabling_mirrors_the_url_for_the_litellm_sdk(monkeypatch):
    """`litellm_proxy/…` calls made outside completion.py (deps report) pass
    only model+api_key, so the SDK has to find the base URL in the env."""
    import os

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000/")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")
    assert gateway.is_enabled() is True
    assert os.environ["LITELLM_PROXY_API_BASE"] == "http://litellm:4000"


# ─── 2. Provisioning happy path ──────────────────────────────────────


def test_ensure_workspace_keys_provisions_team_models_and_key(enabled, store, proxy):
    key = gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    assert key == "sk-virtual-1"
    assert proxy.count("/team/new") == 1
    assert proxy.count("/model/new") == 3
    assert proxy.count("/key/generate") == 1

    assert proxy.payload_for("/team/new")["team_id"] == "ws-acme"

    names = sorted(
        body["model_name"] for _, path, body in proxy.calls if path == "/model/new"
    )
    assert names == ["celmis-acme-chat", "celmis-acme-embed", "celmis-acme-review"]

    # Deployments carry the REAL provider key, fully qualified for LiteLLM.
    chat = next(
        b for _, p, b in proxy.calls
        if p == "/model/new" and b["model_name"] == "celmis-acme-chat"
    )
    assert chat["litellm_params"]["model"] == "gemini/gemini-3-flash-preview"
    assert chat["litellm_params"]["api_key"] == "AIzaREALKEY123"


def test_virtual_key_is_scoped_and_cached(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    body = proxy.payload_for("/key/generate")
    assert sorted(body["models"]) == [
        "celmis-acme-chat", "celmis-acme-embed", "celmis-acme-review",
    ]
    assert body["team_id"] == "ws-acme"
    assert body["metadata"]["celmis_workspace_id"] == "acme"

    row = store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme")
    assert row is not None and row.secret == "sk-virtual-1"
    assert row.metadata["deployments"]["chat"] == "celmis-acme-chat"
    # The tenant's real provider key is NOT what we cached.
    assert "AIzaREALKEY123" not in json.dumps(row.metadata)


def test_route_for_reads_the_cached_provisioning(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)
    gateway.reset_cache()

    route = gateway.route_for("chat", "acme")
    assert route is not None
    assert route.deployment == "celmis-acme-chat"
    assert route.virtual_key == "sk-virtual-1"
    assert route.base_url == "http://litellm:4000"
    assert route.underlying_model == "gemini/gemini-3-flash-preview"

    assert gateway.route_for("chat", "other-ws") is None


def test_workspace_ids_never_collide_on_a_deployment_name():
    a = gateway.deployment_name("Acme Corp", "chat")
    b = gateway.deployment_name("acme-corp", "chat")
    assert a != b, "two ids must never share one deployment (= shared provider key)"


# ─── 3. Idempotency ──────────────────────────────────────────────────


def test_second_identical_call_does_not_regenerate_the_key(enabled, store, proxy):
    first = gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)
    second = gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    assert first == second == "sk-virtual-1"
    assert proxy.count("/key/generate") == 1, "a repeat call must reuse the cached key"
    assert proxy.count("/model/new") == 3
    assert proxy.count("/team/new") == 1


def test_rotated_provider_key_reprovisions_and_revokes_the_old_key(
    enabled, store, proxy,
):
    first = gateway.ensure_workspace_keys("acme", "google", "AIzaOLDKEY0001", MODELS)
    second = gateway.ensure_workspace_keys("acme", "google", "AIzaNEWKEY0002", MODELS)

    assert first == "sk-virtual-1"
    assert second == "sk-virtual-2"
    assert proxy.count("/key/generate") == 2
    assert proxy.payload_for("/key/delete")["keys"] == ["sk-virtual-1"]
    assert store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme").secret == second


def test_existing_deployments_are_replaced_not_stacked(enabled, store, proxy):
    proxy.models = [
        {"model_name": "celmis-acme-chat", "model_info": {"id": "stale-id"}},
    ]
    gateway.ensure_workspace_keys("acme", "google", "AIzaREALKEY123", MODELS)

    deleted = [b for _, p, b in proxy.calls if p == "/model/delete"]
    assert {"id": "stale-id"} in deleted


# ─── 5. Fallback on proxy failure ────────────────────────────────────


@pytest.mark.parametrize(
    "failing_path", ["/team/new", "/model/new", "/key/generate"],
)
def test_5xx_anywhere_falls_back_to_direct_keys(enabled, store, proxy, failing_path):
    proxy.status_for[failing_path] = 503

    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    # Nothing half-provisioned is cached — the next attempt starts clean.
    assert store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme") is None


def test_unreachable_proxy_falls_back(enabled, store):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.request.side_effect = OSError("connection refused")
    with patch("src.http.build_client", return_value=client):
        assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None


def test_disabled_gateway_never_calls_the_proxy(store, proxy):
    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) is None
    assert proxy.calls == []


def test_team_already_exists_is_not_a_failure(enabled, store, proxy):
    proxy.status_for["/team/new"] = 400  # LiteLLM's "team already exists"
    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS) == "sk-virtual-1"


# ─── 6. Never an unrestricted virtual key ────────────────────────────


@pytest.mark.parametrize("models", [{}, None, {"chat": ""}, {"bogus": "m"}])
def test_never_generates_a_key_without_models(enabled, store, proxy, models):
    assert gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", models) is None
    assert proxy.count("/key/generate") == 0, (
        "an empty `models` list means ALL models on LiteLLM — never mint one"
    )


def test_generate_key_refuses_empty_models_directly(enabled, store, proxy):
    assert gateway._generate_key("acme", []) is None
    assert gateway._generate_key("acme", ["", None]) is None  # type: ignore[list-item]
    assert proxy.count("/key/generate") == 0


def test_every_generated_key_carries_a_non_empty_models_list(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS)
    for _, path, body in proxy.calls:
        if path == "/key/generate":
            assert body.get("models"), "virtual key generated without model scope"


def test_missing_provider_key_is_not_provisioned(enabled, store, proxy):
    assert gateway.ensure_workspace_keys("acme", "google", "", MODELS) is None
    assert proxy.calls == []


# ─── 7. Secrets never logged ─────────────────────────────────────────


def test_proxy_error_body_is_scrubbed_before_logging(enabled, store, proxy, caplog):
    proxy.status_for["/model/new"] = 500
    with caplog.at_level(logging.DEBUG, logger="src.llm.gateway"):
        gateway.ensure_workspace_keys("acme", "google", "AIzaSUPERSECRET99", MODELS)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "AIzaSUPERSECRET99" not in blob
    assert "sk-master-test-key" not in blob


def test_safe_scrubber_masks_key_shapes():
    dirty = '{"api_key": "AIzaSECRETVALUE", "k": "sk-abcdef123456"}'
    clean = gateway._safe(dirty)
    assert "AIzaSECRETVALUE" not in clean
    assert "sk-abcdef123456" not in clean


# ─── 4. Revocation ───────────────────────────────────────────────────


def test_revoke_deletes_key_deployments_and_cache(enabled, store, proxy):
    gateway.ensure_workspace_keys("acme", "google", "AIzaKEY", MODELS)
    proxy.models = [
        {"model_name": "celmis-acme-chat", "model_info": {"id": "m1"}},
        {"model_name": "celmis-acme-embed", "model_info": {"id": "m2"}},
    ]

    assert gateway.revoke_workspace_key("acme") is True
    assert proxy.payload_for("/key/delete")["keys"] == ["sk-virtual-1"]
    assert store.load(gateway.VIRTUAL_KEY_PROVIDER, user_id="ws:acme") is None
    assert gateway.route_for("chat", "acme") is None


# ─── 8. Off by default — the direct path is untouched ────────────────


def test_profile_is_not_routed_when_gateway_is_off():
    from src.llm.profiles import Profile

    p = Profile(surface="chat", provider="google", model="gemini-3-flash-preview",
                api_key="AIzaKEY", raw_api_key="AIzaKEY")
    assert p.via_gateway is False
    assert p.is_google is True
    assert p.litellm_model == "gemini/gemini-3-flash-preview"


def test_routed_profile_points_at_the_proxy_even_for_google():
    from src.llm.profiles import Profile

    p = Profile(
        surface="chat", provider="google", model="gemini-3-flash-preview",
        api_key="sk-virtual-1", raw_api_key="AIzaKEY",
        gateway_model="celmis-acme-chat", gateway_url="http://litellm:4000",
        gateway_underlying="gemini/gemini-3-flash-preview",
    )
    assert p.via_gateway is True
    assert p.is_google is False, "google must leave through the proxy like everyone else"
    assert p.google_family is True
    assert p.litellm_model == "litellm_proxy/celmis-acme-chat"


def test_embedding_kwargs_forward_task_type_and_dimensions_only_via_gateway():
    from src.llm.completion import _embedding_kwargs
    from src.llm.profiles import Profile

    direct = Profile(surface="embeddings", provider="openai", model="text-embedding-3-large",
                     api_key="sk-openai", dimensions=1536)
    kwargs = _embedding_kwargs(direct, ["hi"], "RETRIEVAL_DOCUMENT")
    assert "task_type" not in kwargs and "api_base" not in kwargs
    assert kwargs["dimensions"] == 1536

    routed = Profile(
        surface="embeddings", provider="google", model="gemini-embedding-2",
        api_key="sk-virtual-1", dimensions=3072,
        gateway_model="celmis-acme-embed", gateway_url="http://litellm:4000",
        gateway_underlying="gemini/gemini-embedding-2",
    )
    kwargs = _embedding_kwargs(routed, ["hi"], "RETRIEVAL_DOCUMENT")
    assert kwargs["api_base"] == "http://litellm:4000"
    assert kwargs["task_type"] == "RETRIEVAL_DOCUMENT"
    assert kwargs["dimensions"] == 3072
    assert kwargs["model"] == "litellm_proxy/celmis-acme-embed"


# ─── Embeddings drift guard ──────────────────────────────────────────


def _embed_profile(model="gemini-embedding-2", dims=3072, underlying="gemini/gemini-embedding-2"):
    from src.llm.profiles import Profile

    return Profile(
        surface="embeddings", provider="google", model=model, api_key="sk-virtual-1",
        dimensions=dims, gateway_model="celmis-acme-embed",
        gateway_url="http://litellm:4000", gateway_underlying=underlying,
    )


def test_embeddings_guard_passes_when_nothing_drifted(enabled):
    route = gateway.GatewayRoute(
        workspace_id="acme", surface="embeddings", deployment="celmis-acme-embed",
        virtual_key="sk-virtual-1", base_url="http://litellm:4000",
        underlying_model="gemini/gemini-embedding-2", provider="google",
    )
    with patch.object(gateway, "_indexed_signature",
                      return_value="google:gemini-embedding-2:3072"):
        gateway.assert_embeddings_compatible(_embed_profile(), route)


def test_embeddings_guard_raises_when_deployment_drifted(enabled):
    route = gateway.GatewayRoute(
        workspace_id="acme", surface="embeddings", deployment="celmis-acme-embed",
        virtual_key="sk-virtual-1", base_url="http://litellm:4000",
        underlying_model="openai/text-embedding-3-large", provider="openai",
    )
    with pytest.raises(gateway.EmbeddingConfigMismatch) as exc:
        gateway.assert_embeddings_compatible(_embed_profile(), route)
    assert "celmis-acme-embed" in str(exc.value)


def test_embeddings_guard_raises_on_dimension_drift(enabled):
    with (
        patch.object(gateway, "_indexed_signature",
                     return_value="google:gemini-embedding-2:3072"),
        pytest.raises(gateway.EmbeddingConfigMismatch) as exc,
    ):
        gateway.assert_embeddings_compatible(_embed_profile(dims=768), None)
    assert "3072" in str(exc.value)


def test_embeddings_guard_is_silent_before_the_first_index(enabled):
    with patch.object(gateway, "_indexed_signature", return_value=None):
        gateway.assert_embeddings_compatible(_embed_profile(), None)


# ─── Streaming through the proxy + single spend row ──────────────────


class _Chunk:
    def __init__(self, text: str, usage=None) -> None:
        delta = MagicMock()
        delta.content = text
        choice = MagicMock()
        choice.delta = delta
        self.choices = [choice]
        self.usage = usage


class _Stream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


def _run(agen_factory) -> tuple[list[str], dict]:
    import asyncio

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        usage = MagicMock()
        usage.prompt_tokens = 11
        usage.completion_tokens = 7
        return _Stream([_Chunk("Hel"), _Chunk("lo", usage=usage)])

    async def drive():
        out = []
        async for piece in agen_factory():
            out.append(piece)
        return out

    with patch("litellm.acompletion", new=fake_acompletion):
        chunks = asyncio.run(drive())
    return chunks, captured


def test_stream_chat_goes_through_the_proxy_and_bills_exactly_once(enabled):
    from src.llm import completion
    from src.llm.profiles import Profile

    routed = Profile(
        surface="chat", provider="google", model="gemini-3-flash-preview",
        api_key="sk-virtual-1", raw_api_key="AIzaKEY",
        gateway_model="celmis-acme-chat", gateway_url="http://litellm:4000",
        gateway_underlying="gemini/gemini-3-flash-preview",
    )
    spend = MagicMock()
    with patch("src.llm.completion.resolve_profile", return_value=routed), \
         patch("src.llm.budget.record_spend", spend), \
         patch("src.ops.telemetry.record_llm_call"), \
         patch("src.security.audit.get_audit_logger", return_value=MagicMock()):
        chunks, kwargs = _run(lambda: completion.stream_chat(
            prompt="hi", system_instruction="be brief", workspace_id="acme",
        ))

    assert "".join(chunks) == "Hello"
    # Google routed through the proxy — NOT through the native Gemini client.
    assert kwargs["model"] == "litellm_proxy/celmis-acme-chat"
    assert kwargs["api_key"] == "sk-virtual-1"
    assert kwargs["api_base"] == "http://litellm:4000"
    assert kwargs["stream"] is True

    # The proxy keeps its own spend table; our ledger must get exactly one row
    # (the native GeminiClient, which bills itself, is not involved).
    assert spend.call_count == 1
    row = spend.call_args.kwargs
    assert row["workspace_id"] == "acme"
    assert row["provider"] == "google"
    assert row["model"] == "gemini-3-flash-preview", "ledger keeps the real model"
    assert row["tokens_in"] == 11 and row["tokens_out"] == 7


def test_stream_chat_uses_litellm_even_with_the_gateway_off():
    """One transport, both routes.

    This used to assert the opposite — that switching the gateway off restored
    the google-genai path byte for byte. That was the migration guarantee when
    the gateway arrived; it has been retired deliberately. A second code path
    to one vendor's SDK is exactly what the gateway exists to remove, and it
    was unreachable in production anyway (every workspace resolves to
    via_gateway=True), so it was dead code that nonetheless decided what
    happens when that vendor changes its API.

    `p.litellm_model` yields `gemini/<model>` without a gateway, so a
    direct-key workspace still works — through LiteLLM.
    """
    import asyncio

    from src.llm import completion
    from src.llm.profiles import Profile

    direct = Profile(
        surface="chat", provider="google", model="gemini-3-flash-preview",
        api_key="AIzaKEY", raw_api_key="AIzaKEY",
    )

    async def fake_stream(_p, **_kw):
        for piece in ("a", "b"):
            yield piece

    async def drive():
        return [c async for c in completion.stream_chat(
            prompt="hi", system_instruction=None, workspace_id="acme",
        )]

    with patch("src.llm.completion.resolve_profile", return_value=direct), \
         patch("src.llm.gemini_client._gemini_for") as gem, \
         patch("src.llm.completion._litellm_stream", fake_stream):
        assert asyncio.run(drive()) == ["a", "b"]

    # `_gemini_for` moved to gemini_client.py when the embedding exception was
    # retired — completion.py no longer even imports it, so this assertion is
    # structural now. It stays because it is the sentence the test exists to
    # say: no gateway does not mean back to the vendor SDK.
    gem.assert_not_called()
