"""A reasoning setting that changes no request is not a setting.

`Settings.gemini_thinking_budget` was wired into `src/llm/gemini_client.py` and
nowhere else. That is the native path, which is being retired, so on every
LiteLLM call — which is every call — the number existed, was configurable, and
reached nothing. This project has shipped that bug twice under other names.

Threading it through naively buys the opposite failure. "Reasoning effort" is
not one vocabulary: OpenAI takes `reasoning_effort`, Anthropic a thinking
budget in tokens, Gemini a thinking_budget int where -1 means dynamic — and
`gpt-4o` answers `UnsupportedParamsError` to a `reasoning_effort` it never
advertised. Sending the parameter to everything turns a configuration choice
into a 400.

So the value is translated by asking the INSTALLED LiteLLM what this model
accepts, and dropped entirely when the answer is "nothing". Both halves are
measured here at the kwargs `litellm.completion` receives — and the drop is
measured twice over, because a test that only asserts absence would also pass
if reasoning had never been implemented at all: `test_the_parameter_this_model
_refuses_is_the_one_being_dropped` asserts that LiteLLM really does reject it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Reasoning, driven by an effort word. LiteLLM turns "low" into
#: thinkingConfig {"thinkingLevel": "low"} for this one.
THINKS = "gemini/gemini-3-flash-preview"
#: No reasoning parameter at all — and it does not merely ignore one.
THINKS_NOT = "openai/gpt-4o"

VALID = '[{"file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 100
    completion_tokens = 40
    prompt_tokens_details = None
    total_cost = 0.01


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeLiteLLM:
    def __init__(self) -> None:
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        return _Completion(VALID)


@pytest.fixture(autouse=True)
def _fresh_capability_state():
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


@pytest.fixture
def fake_litellm(monkeypatch):
    import litellm

    fake = _FakeLiteLLM()
    monkeypatch.setattr(litellm, "completion", fake)
    return fake


def _client(tmp_path: Path, model: str):
    from src.llm.client import LLMClient
    from src.security.audit import AuditLogger

    return LLMClient(
        resolve_key=lambda provider: "sk-test",
        resolve_model=lambda agent: model,
        surface="review",
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        workspace_id="ws-test",
    )


def _generate(client, **kwargs):
    return client.generate(
        prompt="p", agent="architect", operation="review_architect",
        max_output_tokens=1024, **kwargs,
    )


# ─── it arrives ──────────────────────────────────────────────────────


def test_a_model_that_reasons_receives_the_effort(tmp_path, fake_litellm):
    _generate(_client(tmp_path, THINKS), reasoning="high")

    kwargs = fake_litellm.kwargs_seen[0]
    assert kwargs.get("reasoning_effort") == "high", (
        f"litellm was handed {sorted(kwargs)} — the configured reasoning level "
        "stopped somewhere between the config and the call, which is exactly "
        "how gemini_thinking_budget spent a release changing nothing"
    )


def test_what_litellm_receives_is_something_litellm_accepts(tmp_path, fake_litellm):
    """Verified against LiteLLM, not against its documentation.

    The stub above swallows anything, so "the kwarg was passed" proves only
    that we passed it. This replays the captured kwargs through the very
    function `litellm.completion` calls before it builds a request — the one
    that raises `UnsupportedParamsError` — and asserts the vendor translation
    actually happens.
    """
    import litellm
    from litellm.utils import get_optional_params

    _generate(_client(tmp_path, THINKS), reasoning="low")
    captured = fake_litellm.kwargs_seen[0]

    bare, provider = litellm.get_llm_provider(model=captured["model"])[:2]
    translated = get_optional_params(
        model=bare, custom_llm_provider=provider,
        max_tokens=captured.get("max_tokens"),
        reasoning_effort=captured["reasoning_effort"],
    )
    assert "thinkingConfig" in translated, (
        f"the effort word did not become a Gemini thinking directive: {translated}"
    )


def test_no_reasoning_configured_sends_no_reasoning_parameter(tmp_path, fake_litellm):
    _generate(_client(tmp_path, THINKS))

    kwargs = fake_litellm.kwargs_seen[0]
    assert "reasoning_effort" not in kwargs and "thinking" not in kwargs, (
        "an unconfigured agent started paying for thinking it never asked for"
    )


# ─── it is withheld ──────────────────────────────────────────────────


def test_a_model_that_does_not_reason_receives_no_reasoning_parameter(
    tmp_path, fake_litellm,
):
    _generate(_client(tmp_path, THINKS_NOT), reasoning="high")

    kwargs = fake_litellm.kwargs_seen[0]
    assert "reasoning_effort" not in kwargs and "thinking" not in kwargs, (
        f"gpt-4o was sent {sorted(set(kwargs) & {'reasoning_effort', 'thinking'})} "
        "— which is a 400 from OpenAI, not an ignored hint"
    )


def test_the_parameter_this_model_refuses_is_the_one_being_dropped(tmp_path):
    """The other half: absence only means something if presence would break.

    Without this, deleting the whole reasoning feature would leave the test
    above green.
    """
    import litellm
    from litellm.utils import get_optional_params

    bare, provider = litellm.get_llm_provider(model=THINKS_NOT)[:2]
    with pytest.raises(litellm.exceptions.UnsupportedParamsError):
        get_optional_params(
            model=bare, custom_llm_provider=provider, reasoning_effort="high",
        )


def test_an_effort_word_this_model_does_not_take_is_dropped(tmp_path, fake_litellm):
    """The vocabulary is per model, and the differences are real: on litellm
    1.97.0 `o3` and Claude accept "xhigh" while Gemini 3 Flash does not. A
    hardcoded per-vendor list would have sent it and earned a 400."""
    _generate(_client(tmp_path, THINKS), reasoning="xhigh")

    assert "reasoning_effort" not in fake_litellm.kwargs_seen[0]


def test_a_token_budget_is_not_smuggled_in_as_an_effort(tmp_path, fake_litellm):
    """Gemini 3 takes both parameter names in this LiteLLM, and `thinking`
    silently loses its `budget_tokens` on the way — a third way to own a
    setting that reaches nothing. A model that wants a word gets a word or
    nothing."""
    _generate(_client(tmp_path, THINKS), reasoning=2048)

    kwargs = fake_litellm.kwargs_seen[0]
    assert "thinking" not in kwargs and "reasoning_effort" not in kwargs


def test_an_unmapped_model_is_not_guessed_at(tmp_path, fake_litellm):
    """Fail closed. LiteLLM has no entry for `gemini-3-pro`, so what it takes
    is unknown — and unknown does not get a guess sent to it."""
    _generate(_client(tmp_path, "gemini/gemini-3-pro"), reasoning="high")

    kwargs = fake_litellm.kwargs_seen[0]
    assert "reasoning_effort" not in kwargs and "thinking" not in kwargs
