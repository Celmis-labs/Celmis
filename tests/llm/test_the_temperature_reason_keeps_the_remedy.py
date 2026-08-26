"""The reason shown to an operator must contain the part they can act on.

The real message for claude-sonnet-5 is two sentences: "claude-sonnet-5 does
not support temperature=0.1. Only temperature=1 is supported." The first
sentence matches the refusal shape, and cutting the quote at its full stop
kept "temperature=0.1." — a reason that explains nothing — and threw away
the second sentence, which is the only part that says what would work. It
now rides along. Seen live before this was written.
"""

from __future__ import annotations

from src.llm.capabilities import temperature_refusal


class _Unsupported(Exception):
    status_code = 400


def test_the_remedy_sentence_survives_the_cut():
    exc = _Unsupported(
        "litellm.UnsupportedParamsError: claude-sonnet-5 does not support "
        "temperature=0.1. Only temperature=1 is supported. To drop unsupported "
        "params, set `litellm.drop_params = True`."
    )
    reason = temperature_refusal(exc, 0.1)
    assert reason is not None
    assert "Only temperature=1 is supported" in reason, reason
    assert "drop_params" not in reason, "litellm's own advice is not the provider's"


def test_a_single_sentence_message_is_unchanged():
    exc = _Unsupported(
        "GeminiException BadRequestError - generation_config.temperature must be "
        "between 0 and 2."
    )
    reason = temperature_refusal(exc, 3.5)
    assert reason is not None
    assert reason.startswith("generation_config.temperature")
    assert "between 0 and 2" in reason


def test_an_unrelated_following_sentence_is_not_dragged_in():
    exc = _Unsupported(
        "model-x does not support temperature=0.1. Please check your request "
        "id abc123 for details."
    )
    reason = temperature_refusal(exc, 0.1)
    assert reason is not None
    assert "request id" not in reason
