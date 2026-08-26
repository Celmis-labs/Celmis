"""Self-hosted OpenAI-compatible provider ("openai_compatible") — direct path.

What must hold, end to end:

    1. Key resolution — a keyless local server resolves to the "local-no-key"
       sentinel instead of failing; a stored token or the env var still wins.
    2. Profile — `resolve_profile` carries the server address (`api_base`)
       from the per-surface "base_url" field, and the litellm model string is
       "openai/<model>".
    3. Dispatch — the litellm call receives BOTH the address and the sentinel;
       a profile with no address refuses rather than defaulting to
       api.openai.com (that default would be a working call to the one vendor
       a self-hosted profile exists to avoid).
    4. Spend — a local call writes NO ledger row (there is no invoice to
       reconcile against; see completion._seam_embed for the accounting rule).
    5. The generate-vault gate accepts a workspace whose chat profile is a
       self-hosted server with an address, and only then.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm.keys import LOCAL_NO_KEY, resolve_api_key
from src.llm.profiles import Profile

BASE_URL = "http://vllm.internal:8000/v1"
MODEL = "qwen3-32b"


def _local_profile(surface: str = "chat", api_base: str | None = BASE_URL) -> Profile:
    return Profile(
        surface=surface, provider="openai_compatible", model=MODEL,
        api_key=LOCAL_NO_KEY, api_base=api_base, raw_api_key=LOCAL_NO_KEY,
    )


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No provider env keys, no gateway — each test states what it adds."""
    for var in (
        "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def empty_store():
    """Credential store with no rows — the keyless-local state."""
    with patch("src.credentials.get_credential_store") as get_store:
        store = MagicMock()
        store.load.return_value = None
        get_store.return_value = store
        yield store


# ─── 1. Sentinel resolution ──────────────────────────────────────────


def test_a_keyless_local_server_resolves_to_the_sentinel(empty_store):
    """No stored key, no env var — openai_compatible must still resolve,
    because keyless is the NORMAL state for a local server."""
    assert resolve_api_key("openai_compatible", workspace_id="ws-a") == LOCAL_NO_KEY


def test_a_stored_local_token_beats_the_sentinel(empty_store):
    """A vLLM started with --api-key stores a real token; it must win."""
    stored = MagicMock()
    stored.secret = "sk-vllm-real-token-42"
    empty_store.load.return_value = stored
    assert resolve_api_key("openai_compatible", workspace_id="ws-a") == "sk-vllm-real-token-42"


def test_the_env_var_beats_the_sentinel(empty_store, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-env-local-token-99")
    assert resolve_api_key("openai_compatible") == "sk-env-local-token-99"


def test_hosted_providers_still_fail_without_a_key(empty_store):
    """The sentinel is scoped to openai_compatible only — a hosted provider
    with no key must keep refusing, not silently 'resolve'."""
    from src.llm.keys import LLMCredentialError

    with pytest.raises(LLMCredentialError):
        resolve_api_key("anthropic", workspace_id="ws-a")


# ─── 2. Profile carries the address ──────────────────────────────────


def test_the_resolved_profile_carries_the_servers_address(empty_store, monkeypatch):
    from src.llm import profiles

    blob = {"profiles": {"chat": {
        "provider": "openai_compatible", "model": MODEL, "base_url": BASE_URL,
    }}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)

    p = profiles.resolve_profile("chat")
    assert p.provider == "openai_compatible"
    assert p.api_base == BASE_URL
    assert p.api_key == LOCAL_NO_KEY
    assert p.litellm_model == f"openai/{MODEL}"
    assert p.via_gateway is False


def test_the_agent_surface_carries_the_servers_address(empty_store, monkeypatch):
    """The Celmis agent planner is chat-shaped and goes through the same
    LiteLLM path (build_llm_client → resolve_profile → api_base) — a
    self-hosted agent profile must carry the address exactly like chat's."""
    from src.llm import profiles

    blob = {"profiles": {"agent": {
        "provider": "openai_compatible", "model": MODEL, "base_url": BASE_URL,
    }}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)

    p = profiles.resolve_profile("agent")
    assert p.provider == "openai_compatible"
    assert p.api_base == BASE_URL
    assert p.api_key == LOCAL_NO_KEY
    assert p.litellm_model == f"openai/{MODEL}"
    assert p.via_gateway is False


def test_a_blank_base_url_resolves_to_none_not_empty_string(empty_store, monkeypatch):
    """Whitespace in the UI field must not become a truthy 'address'."""
    from src.llm import profiles

    blob = {"profiles": {"chat": {
        "provider": "openai_compatible", "model": MODEL, "base_url": "   ",
    }}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)
    assert profiles.resolve_profile("chat").api_base is None


# ─── 3. Dispatch — the litellm call gets the address and the sentinel ─


def _fake_stream_chunks():
    async def _gen():
        yield SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(content="local answer"))],
        )
    return _gen()


def _consume(agen):
    async def _run():
        return [chunk async for chunk in agen]
    return asyncio.run(_run())


def test_litellm_receives_the_address_and_the_sentinel():
    import litellm

    from src.llm.completion import _litellm_stream

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _fake_stream_chunks()

    with patch.object(litellm, "acompletion", side_effect=fake_acompletion), \
         patch("src.llm.budget.record_spend") as spend:
        chunks = _consume(_litellm_stream(
            _local_profile(), prompt="hi", system_instruction=None,
            temperature=None, max_output_tokens=None,
        ))

    assert chunks == ["local answer"]
    assert captured["model"] == f"openai/{MODEL}"
    assert captured["api_key"] == LOCAL_NO_KEY
    assert captured["api_base"] == BASE_URL
    # 4. Spend: no invoice exists for a local call → no ledger row at all,
    #    not a $0.00 one.
    spend.assert_not_called()


def test_a_local_profile_without_an_address_refuses_rather_than_calling_openai():
    """Fail-closed: "openai/<model>" with no api_base is a WORKING call to
    api.openai.com. The stream must refuse before litellm is ever invoked."""
    import litellm

    from src.llm.completion import _litellm_stream

    with patch.object(litellm, "acompletion") as acompletion, \
            pytest.raises(RuntimeError, match="api.openai.com"):
        _consume(_litellm_stream(
            _local_profile(api_base=None), prompt="hi",
            system_instruction=None, temperature=None, max_output_tokens=None,
        ))
    acompletion.assert_not_called()


# ─── 3b. Same guarantees through LLMClient (review/vault path) ───────


def _make_response(text: str) -> MagicMock:
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.total_cost = None
    return resp


def test_the_client_sends_a_bare_model_to_the_local_server():
    """`build_llm_client` on a local profile: a bare model name from the
    resolver goes out as "openai/<model>" to the profile's api_base with the
    sentinel key — and books nothing to the spend ledger."""
    from src.llm.client import build_llm_client

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _make_response("ok")

    with patch("src.llm.completion._routed", return_value=_local_profile()), \
         patch("litellm.completion", side_effect=fake_completion), \
         patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client(
            "u1", "ws-a", surface="chat", resolve_model=lambda _agent: MODEL,
        )
        result = client.generate(agent="qa", prompt="hi", mode="qa", operation="t")

    assert result.text == "ok"
    assert captured["model"] == f"openai/{MODEL}"
    assert captured["api_key"] == LOCAL_NO_KEY
    assert captured["api_base"] == BASE_URL
    spend.assert_not_called()


def test_the_client_refuses_a_local_profile_without_an_address():
    from src.llm.client import build_llm_client

    with patch("src.llm.completion._routed",
               return_value=_local_profile(api_base=None)), \
         patch("litellm.completion") as completion:
        client = build_llm_client(
            "u1", "ws-a", surface="chat", resolve_model=lambda _agent: MODEL,
        )
        with pytest.raises(RuntimeError, match="api.openai.com"):
            client.generate(agent="qa", prompt="hi", mode="qa", operation="t")
    completion.assert_not_called()


def test_an_explicit_hosted_override_is_not_hijacked_by_a_local_profile():
    """A per-agent policy override naming another vendor outright
    ("anthropic/claude-…") keeps its own key path and gets NO local api_base —
    the local profile only claims openai-dialect calls."""
    from src.llm.client import build_llm_client

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _make_response("ok")

    with patch("src.llm.completion._routed", return_value=_local_profile()), \
         patch("litellm.completion", side_effect=fake_completion), \
         patch("src.llm.keys.resolve_api_key", return_value="sk-ant-real") as rk:
        client = build_llm_client(
            "u1", "ws-a", surface="review",
            resolve_model=lambda _agent: "anthropic/claude-sonnet-5",
        )
        client.generate(agent="architect", prompt="hi", mode="review", operation="t")

    assert captured["model"] == "anthropic/claude-sonnet-5"
    assert captured["api_key"] == "sk-ant-real"
    assert "api_base" not in captured
    rk.assert_called_once()
    assert rk.call_args.args[0] == "anthropic"


# ─── 5. The generate-vault gate ──────────────────────────────────────


class _GateMarker(Exception):
    """Raised past the key gate — proves the request was not 400'd."""


def _call_generate_vault(monkeypatch, chat_profile: Profile | None):
    from src.api.routers import repos as repos_router

    # No hosted key anywhere — the old gate would 400 unconditionally.
    monkeypatch.setattr("src.llm.keys.has_key", lambda *a, **kw: False)
    if chat_profile is not None:
        monkeypatch.setattr(
            "src.llm.profiles.resolve_profile", lambda *a, **kw: chat_profile,
        )
    else:
        def _boom(*_a, **_kw):
            raise RuntimeError("no profile blob")
        monkeypatch.setattr("src.llm.profiles.resolve_profile", _boom)

    fake_store = MagicMock()
    fake_store.get_in_workspace.return_value = SimpleNamespace(repo_slug="r")
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: fake_store)
    # First thing after the gate — raising here proves the gate let us through.
    def _marker(*_a, **_kw):
        raise _GateMarker
    monkeypatch.setattr("src.generation.doc_language.resolve_doc_language", _marker)

    return repos_router.trigger_generate_vault(
        "some-repo", None, user=SimpleNamespace(id="u1"), workspace_id="ws-a",
    )


def test_the_vault_gate_passes_on_a_local_chat_profile(monkeypatch):
    with pytest.raises(_GateMarker):
        _call_generate_vault(monkeypatch, _local_profile())


def test_the_vault_gate_still_blocks_a_local_profile_without_an_address(monkeypatch):
    """Without a base URL every call refuses at dispatch time (fail-closed),
    so the gate must 400 now instead of queuing a job that dies later."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call_generate_vault(monkeypatch, _local_profile(api_base=None))
    assert exc.value.status_code == 400


def test_the_vault_gate_still_blocks_when_nothing_is_configured(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call_generate_vault(monkeypatch, None)
    assert exc.value.status_code == 400
