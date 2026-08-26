"""Local-model setup rules on PUT /api/llm/config.

Two decisions this file pins down:

    1. The Celmis agent surface may point at a self-hosted server. It is
       chat-shaped and goes through the same LiteLLM path as chat and review
       (build_llm_client → resolve_profile → api_base), so refusing it the
       base_url was an arbitrary gap, not a safety property. Embeddings stay
       env-first — the refusal there is on purpose and must survive.

    2. The embeddings profile only accepts providers LiteLLM can actually
       route embeddings to. litellm.embedding() has no anthropic or groq
       branch, and OpenRouter serves no embedding models — saving one of
       those used to succeed here and then fail at index time, inside a
       queued job with nobody watching.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

BASE_URL = "http://vllm.internal:8000/v1"
MODEL = "qwen3-32b"

_ADMIN = SimpleNamespace(id="u-ops", email="ops@test", is_admin=True)


class _FakeStore:
    """In-memory credentials store — the only persistence put_config touches."""

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
    """Hermetic config storage: no DB, no gateway, no leaked provider env."""
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    fake = _FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


def _request() -> object:
    """The minimum a `Request` has to be for this handler.

    The handler audits who re-pointed the workspace's LLM routing and from
    where, so it takes a Request. Calling it directly means supplying one —
    `client_ip` reads the forwarding headers and the peer address, and a
    starlette Request built from a scope gives both without a server.
    """
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/llm/config",
        "headers": [], "client": ("203.0.113.9", 51234), "query_string": b"",
    })


def _put(profiles: dict) -> object:
    from src.api.routers.llm import LLMConfigIn, put_config

    return put_config(LLMConfigIn(profiles=profiles), _request(),
                      user=_ADMIN, workspace_id="default")


# ─── 1. The agent surface takes a self-hosted profile ────────────────


def test_the_agent_surface_accepts_a_self_hosted_profile(store):
    """Saving openai_compatible + base_url for the agent must succeed and the
    address must come back on the agent profile — the same contract chat and
    review already have."""
    out = _put({"agent": {
        "provider": "openai_compatible", "model": MODEL, "base_url": BASE_URL,
    }})
    agent = out.profiles["agent"]
    assert agent.provider == "openai_compatible"
    assert agent.model == MODEL
    assert agent.base_url == BASE_URL


def test_the_agent_profile_still_fails_closed_without_an_address(store):
    """A self-hosted agent profile with no base_url would send the workspace's
    prompts to api.openai.com on the first call — refuse at save time."""
    with pytest.raises(HTTPException) as exc:
        _put({"agent": {"provider": "openai_compatible", "model": MODEL}})
    assert exc.value.status_code == 422
    assert "base_url" in exc.value.detail


def test_a_saved_agent_profile_resolves_with_the_address(store):
    """End to end: what put_config stored, resolve_profile carries — this is
    the value build_llm_client passes to litellm as api_base."""
    from src.llm.profiles import resolve_profile

    _put({"agent": {
        "provider": "openai_compatible", "model": MODEL, "base_url": BASE_URL,
    }})
    p = resolve_profile("agent", "default")
    assert p.api_base == BASE_URL
    assert p.litellm_model == f"openai/{MODEL}"


# ─── 2. Embeddings stay env-first ─────────────────────────────────────


def test_embeddings_still_refuse_a_base_url(store):
    """Indexing ships source code to the embedder, so where it goes stays an
    installation-level (env) decision — the agent extension must not have
    widened this door."""
    with pytest.raises(HTTPException) as exc:
        _put({"embeddings": {
            "provider": "openai_compatible", "model": "nomic-embed-text",
            "base_url": BASE_URL,
        }})
    assert exc.value.status_code == 422
    assert "EMBEDDING_" in exc.value.detail


# ─── 3. Embeddings providers must be routable ────────────────────────


@pytest.mark.parametrize("provider", ["anthropic", "groq", "openrouter"])
def test_embeddings_refuse_a_vendor_litellm_cannot_route(store, provider):
    """litellm.embedding() has no branch for these vendors (or, for
    OpenRouter, no embedding model exists to point at) — a profile saved with
    one only fails later, at index time. Refuse now, while the person who
    chose it is still on the page."""
    with pytest.raises(HTTPException) as exc:
        _put({"embeddings": {"provider": provider, "model": "some-model"}})
    assert exc.value.status_code == 422
    assert provider in exc.value.detail


@pytest.mark.parametrize("provider,model", [
    ("google", "gemini-embedding-001"),
    ("openai", "text-embedding-3-small"),
    ("mistral", "mistral-embed"),
])
def test_embeddings_accept_the_vendors_that_serve_them(store, provider, model):
    out = _put({"embeddings": {"provider": provider, "model": model}})
    emb = out.profiles["embeddings"]
    assert emb.provider == provider
    assert emb.model == model


def test_chat_and_review_keep_the_full_vendor_list(store):
    """The embeddings restriction is about embeddings — anthropic stays a
    perfectly good chat/review provider."""
    out = _put({
        "chat": {"provider": "anthropic", "model": "claude-sonnet-5"},
        "review": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
    })
    assert out.profiles["chat"].provider == "anthropic"
    assert out.profiles["review"].provider == "groq"
