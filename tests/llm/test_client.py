"""Tests for `src.llm.client.LLMClient`.

The critical one is `test_llm_client_preserves_redaction` — without it, any
future refactor could silently drop redaction and no test would notice.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.llm.client import LLMClient, _provider_of

# ─── Fake `litellm.completion` response ──────────────────────────────


def _make_response(text: str, in_tok: int = 100, out_tok: int = 50) -> MagicMock:
    """Simulate a `litellm.ModelResponse` — attribute shape mirrors LiteLLM's."""
    resp = MagicMock()
    # response.choices[0].message.content
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = "stop"
    resp.choices = [choice]
    # response.usage.prompt_tokens / completion_tokens
    resp.usage.prompt_tokens = in_tok
    resp.usage.completion_tokens = out_tok
    # No total_cost — force fallback to litellm.completion_cost().
    resp.usage.total_cost = None
    return resp


# ─── The important one ──────────────────────────────────────────────


@pytest.fixture
def _redaction_on(monkeypatch):
    """Force `settings.redaction_enabled = True` for tests that need real
    pattern matching — otherwise the redactor is a no-op if the env doesn't
    set REDACTION_ENABLED (production default is off in this repo)."""
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("REDACTION_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_llm_client_preserves_redaction(_redaction_on):
    """Regression guard: even after refactoring to litellm, code_context must
    still go through redact() before reaching the provider. If someone drops
    the redact() call, this test fails."""
    aws_secret = "AKIAIOSFODNN7EXAMPLE"
    code = f"config = {{'AWS_ACCESS_KEY_ID': '{aws_secret}'}}\n"

    client = LLMClient(resolve_key=lambda p: "sk-fake-key-for-tests")

    captured_messages: list = []

    def fake_completion(**kwargs):
        captured_messages.append(kwargs["messages"])
        return _make_response("architect finding")

    with patch("litellm.completion", side_effect=fake_completion):
        result = client.generate(
            model="anthropic/claude-sonnet-5",
            prompt="Review this diff",
            code_context=code,
            mode="review",
            operation="review_architect",
        )

    # 1. Text came through
    assert result.text == "architect finding"

    # 2. Full messages block sent to litellm.completion — the secret must not
    #    appear anywhere in it. Turn the messages list into a big string and
    #    check exact substring absence.
    payload_str = str(captured_messages)
    assert aws_secret not in payload_str, (
        "REGRESSION: raw AWS secret leaked to litellm — redaction was skipped"
    )
    # And the redacted marker should be present so we know redact() ran.
    assert "REDACTED" in payload_str or "[REDACTED_" in payload_str


# ─── Basic wiring ────────────────────────────────────────────────────


def test_llm_client_calls_key_resolver_with_correct_provider():
    """`resolve_key` must be called with the provider extracted from the model,
    not the full model string."""
    calls: list[str] = []

    def resolver(provider: str) -> str:
        calls.append(provider)
        return "sk-fake"

    client = LLMClient(resolve_key=resolver)
    with patch("litellm.completion", return_value=_make_response("ok")):
        client.generate(
            model="anthropic/claude-sonnet-5",
            prompt="hi",
            mode="review",
            operation="test",
        )
    assert calls == ["anthropic"]


def test_llm_client_uses_model_resolver_when_no_explicit_model():
    """If `.generate()` is called with `agent=` but no `model=`, the model
    resolver kicks in."""
    client = LLMClient(
        resolve_key=lambda p: "sk-fake",
        resolve_model=lambda agent: (
            "anthropic/claude-sonnet-5" if agent == "architect" else "openai/gpt-4o-mini"
        ),
    )
    seen = []
    with patch("litellm.completion",
               side_effect=lambda **kw: (seen.append(kw["model"]), _make_response("ok"))[1]):
        client.generate(agent="architect", prompt="hi", mode="review", operation="t")
        client.generate(agent="quality", prompt="hi", mode="review", operation="t")

    assert seen == ["anthropic/claude-sonnet-5", "openai/gpt-4o-mini"]


def test_llm_client_no_model_no_agent_raises():
    client = LLMClient(resolve_key=lambda p: "sk-fake")
    with pytest.raises(RuntimeError, match="no model"):
        client.generate(prompt="x", mode="review", operation="t")


# ─── Cost extraction ────────────────────────────────────────────────


def test_openrouter_actual_cost_wins():
    """When usage.total_cost is present (OpenRouter path), the client returns
    that value with source='openrouter_actual', not the litellm estimate."""
    resp = _make_response("ok", in_tok=1000, out_tok=200)
    resp.usage.total_cost = 0.0123

    client = LLMClient(resolve_key=lambda p: "sk-fake")
    with patch("litellm.completion", return_value=resp):
        result = client.generate(
            model="openrouter/anthropic/claude-sonnet-5",
            prompt="hi",
            mode="review",
            operation="t",
        )
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.cost_source == "openrouter_actual"


def test_missing_model_reports_unknown_cost():
    """When the model is not in either pricing table, cost_usd is None and
    cost_source='unknown'. Callers must handle — no silent $1 default."""
    client = LLMClient(resolve_key=lambda p: "sk-fake")
    with patch("litellm.completion", return_value=_make_response("ok")), patch(
        "src.llm.pricing.extract_actual_cost_usd",
        return_value=(None, "unknown"),
    ):
        r = client.generate(
            model="anthropic/claude-fictional-9000",
            prompt="hi",
            mode="review",
            operation="t",
        )
    assert r.cost_usd is None
    assert r.cost_source == "unknown"


# ─── Provider extraction ────────────────────────────────────────────


@pytest.mark.parametrize("model,expected", [
    ("anthropic/claude-sonnet-5", "anthropic"),
    ("openai/gpt-4o", "openai"),
    ("gemini/gemini-3-pro-preview", "gemini"),
    ("openrouter/anthropic/claude-opus-4-8", "openrouter"),
    ("gpt-4o", "openai"),
    ("claude-3-7-sonnet-20250219", "anthropic"),
    ("gemini-2.5-flash", "gemini"),
])
def test_provider_extraction(model, expected):
    assert _provider_of(model) == expected


# ─── Audit envelope preservation ────────────────────────────────────


def test_llm_client_writes_audit_record():
    """The client must write an audit record with the model + operation + provider
    tag. Regression guard for the same class of miss as redaction."""
    seen = {}

    class FakeAudit:
        def track(self, **kwargs):
            seen.update(kwargs)
            return _FakeCtx()

        def hash_response(self, text: str) -> str:
            return f"hash({len(text)})"

    class _FakeCtx:
        def __enter__(self):
            self.record = MagicMock()
            return self.record

        def __exit__(self, *args):
            return False

    client = LLMClient(
        resolve_key=lambda p: "sk-fake",
        audit=FakeAudit(),
    )
    with patch("litellm.completion", return_value=_make_response("ok")):
        client.generate(
            model="anthropic/claude-sonnet-5",
            prompt="hi",
            mode="review",
            operation="review_architect",
            repo="test-repo",
        )

    assert seen["mode"] == "review"
    assert seen["model"] == "anthropic/claude-sonnet-5"
    assert seen["operation"] == "review_architect"
    assert seen["repo"] == "test-repo"
    assert seen["extra"]["provider"] == "anthropic"
    assert seen["extra"]["redaction"] is not None
