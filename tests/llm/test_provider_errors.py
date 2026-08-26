"""A provider failure must not arrive at the browser as a provider payload.

A Vertex 429 came back as ~1.6KB of nested JSON — quota metric names, internal
model ids, documentation URLs — and the chat pasted it whole into a toast,
which has no height cap. On a phone that is most of the screen, in text nobody
can act on.

`classify` is the boundary: a stable code the UI translates, and a hint built
from the exception's own attributes rather than its message. These tests pin
the two properties that matter — the right code, and a hint that is short and
free of the provider's body.

The litellm exception classes are not constructed here: the isinstance branches
need the SDK, which is not a test dependency. The status-code fallback below is
the path every non-litellm provider error takes anyway, and it is the one that
regressed during development (it read `code` but not `status_code`).
"""

from __future__ import annotations

import pytest

from src.llm.errors import MAX_HINT, classify

#: The shape that actually shipped, abridged but structurally faithful.
VERTEX_429 = (
    'litellm.RateLimitError: vertex_ai_betaException - {"error": {"code": 429, '
    '"message": "You exceeded your current quota, please check your plan and '
    "billing details. Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, "
    'limit: 0, model: gemini-3.1-pro"}, "status": "RESOURCE_EXHAUSTED", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure"}]}'
) * 4


def err(message: str, **attrs: object) -> Exception:
    exc = Exception(message)
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


def test_quota_exhausted_is_told_apart_from_being_rate_limited():
    """One status code, two different problems.

    "Slow down" clears on its own; "out of quota" never does, and sending the
    user back to retry is the wrong advice. Only the message separates them —
    which is why it is matched against, and still not shown.
    """
    assert classify(err(VERTEX_429, status_code=429)).code == "quota_exhausted"
    assert classify(err("Too many requests", status_code=429)).code == "rate_limited"


def test_the_provider_body_never_reaches_the_hint():
    """The actual regression: 1.6KB of provider JSON on the wire."""
    hint = classify(err(VERTEX_429, llm_provider="vertex_ai",
                        model="gemini-3.1-pro", status_code=429)).hint
    assert len(hint) <= MAX_HINT
    for leak in ("QuotaFailure", "generativelanguage", "billing details", "{"):
        assert leak not in hint, leak
    # What survives is the part that helps: who failed, and how.
    assert "vertex_ai" in hint and "429" in hint


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "invalid_key"),
        (403, "invalid_key"),
        (404, "model_not_found"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
def test_status_codes_map_to_actionable_outcomes(status: int, code: str):
    assert classify(err("upstream said no", status_code=status)).code == code


def test_google_genai_names_the_status_code_differently():
    """google-genai puts the HTTP status on `code`, most others on
    `status_code`. Reading only one of them drops the classification and puts
    the message back in the payload — which is how this was first written."""
    assert classify(err("RESOURCE_EXHAUSTED quota", code=429)).code == "quota_exhausted"
    assert classify(err("nope", code=404)).code == "model_not_found"


def test_a_missing_key_is_recognised_by_type_and_by_text():
    from src.llm.keys import LLMCredentialError
    assert classify(LLMCredentialError("no key")).code == "no_api_key"
    assert classify(err("No API key was provided")).code == "no_api_key"


def test_an_unrecognised_failure_still_says_something_bounded():
    """Falling back to silence would leave a user with nothing to report, so
    a clipped message is kept — clipped being the operative word."""
    failure = classify(err("x" * 5_000))
    assert failure.code == "generation_failed"
    assert len(failure.hint) <= MAX_HINT


# ─── Retry-After: read where litellm actually puts it, or not at all ──
#
# Measured on litellm 1.97.0 before this was written: its exceptions carry NO
# `retry_after` attribute. The header survives as `exc.headers` (a plain dict
# the proxy attaches, casing preserved) or `exc.response.headers` (the
# vendor's own 429 response). The review backoff tests exercise the real
# litellm exceptions; these pin the reader itself — including the refusals,
# because a misread here becomes a real sleep inside a review.


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def test_retry_after_is_read_from_the_attached_headers_dict():
    from src.llm.errors import retry_after_seconds

    exc = err("429", headers={"Retry-After": "7"})
    assert retry_after_seconds(exc) == 7.0


def test_retry_after_is_read_from_the_response_headers():
    from src.llm.errors import retry_after_seconds

    exc = err("429", headers=None, response=_Resp({"retry-after": "12"}))
    assert retry_after_seconds(exc) == 12.0


def test_the_deliberately_attached_dict_outranks_the_vendor_response():
    from src.llm.errors import retry_after_seconds

    exc = err("429", headers={"retry-after": "3"},
              response=_Resp({"retry-after": "60"}))
    assert retry_after_seconds(exc) == 3.0


@pytest.mark.parametrize(
    "value",
    ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "", "-5", "0"],
)
def test_anything_but_positive_seconds_is_no_answer(value):
    """The HTTP-date form is legal but unseen from LLM providers; a misparsed
    date, a negative or a zero would each become a wrong real sleep. None
    hands the decision back to the caller's own default."""
    from src.llm.errors import retry_after_seconds

    assert retry_after_seconds(err("429", headers={"Retry-After": value})) is None


def test_an_exception_with_no_headers_anywhere_is_no_answer():
    from src.llm.errors import retry_after_seconds

    assert retry_after_seconds(err("429")) is None
    assert retry_after_seconds(err("429", headers=None, response=object())) is None


# ─── REFUSED: a disposition for a 200 that says no ───────────────────
#
# Measured: the security agent's commonest failure was not a provider error
# at all but a polite refusal — prose, no JSON, status 200, nothing raised.
# `classify` never sees one; the reply reader in src.review.agents.base mints
# `model_refused` by hand and routes it through this vocabulary. Pinned here
# so the vocabulary stays one axis, and so nobody teaches `classify` to read
# refusals out of exception text.


def test_a_refusal_is_its_own_disposition_not_a_relabelled_one():
    from src.llm.errors import (
        REFUSED,
        TERMINAL,
        THROTTLED,
        TRANSIENT,
        UNRECOGNISED,
        ProviderFailure,
    )

    failure = ProviderFailure("model_refused")
    assert failure.disposition == REFUSED
    assert REFUSED not in {TERMINAL, THROTTLED, TRANSIENT, UNRECOGNISED}, (
        "TERMINAL would keep it off the fallback, which is the one thing that "
        "helps; TRANSIENT would buy it the identical resend that was measured "
        "to get the identical refusal"
    )
    assert failure.reason == "the model refused to review this change"
    assert "{" not in failure.reason and "(" not in failure.reason


def test_classify_never_reads_a_refusal_out_of_an_exception_message():
    """A refusal is a reply, not an exception. The words of a provider error
    body decide nothing here but the quota/rate-limit split — a refusal
    minted from message text would be the body-reading this module exists to
    end, one taxonomy over."""
    from src.llm.errors import REFUSED

    failure = classify(err("Sorry, I cannot fulfill your request to analyze this code."))
    assert failure.disposition != REFUSED
    assert failure.code == "generation_failed"
