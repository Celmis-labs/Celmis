"""Turn a provider exception into something a person can act on.

`str(exc)` on a LiteLLM or google-genai error is the provider's whole response
body: nested JSON, quota metric names, documentation URLs, retry hints. That
string was going onto the wire and into a toast, where a rate-limit error
covered half a phone screen with text nobody can act on — and which nobody
should be shown, since it names internal model and quota identifiers.

So the exception is classified here into a stable slug the UI translates, plus
a short technical hint built from the exception's OWN ATTRIBUTES — provider,
model, status — never from its message body. The full text stays in the server
log, which is where a diagnosis actually happens.

Pure and import-light: the provider SDKs are imported inside the function, so a
workspace on one vendor does not pay for the other's import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Belt to the hint's brace. Nothing built here should come close, but a future
#: SDK that puts JSON in an attribute must not reopen the hole.
MAX_HINT = 200

_QUOTA = re.compile(
    r"quota|resource_exhausted|billing|exceeded your current", re.IGNORECASE,
)

# ─── Is calling again worth anything? ───────────────────────────────
#
# The code says WHAT went wrong; the disposition says whether saying it again
# would help. Neither derives from the other — "invalid_key" and
# "provider_unavailable" are both failures and only one of them clears on a
# resend — so the second axis lives here, beside the first, rather than as a
# second taxonomy in whichever caller happened to need it.

#: A resend fails identically. Stop, and tell the user the thing they can fix.
TERMINAL = "terminal"
#: Clears with time, not with another call now: a retry inside the same window
#: spends a request to be refused again, and counts against the next window.
THROTTLED = "throttled"
#: Network drop, 5xx, timeout — the one failure class a plain resend fixes.
TRANSIENT = "transient"
#: The model answered, and the answer was a no. Not a transport failure at
#: all — a 200 carrying polite prose where the findings array should be — so
#: `classify` never mints it: nothing was raised. The reader of the reply does
#: (`looks_like_refusal` in src.review.agents.base) and routes it through THIS
#: vocabulary, so that "is asking again worth anything" stays one axis rather
#: than growing a second taxonomy inside the one caller that met a refusal.
#: Measured before it was written: the security agent's commonest failure was
#: this, ~11% of runs, and its corrective resend — the same model, the same
#: question — got the same refusal back word for word. Which is what sets it
#: apart from its neighbours: not TERMINAL, because a DIFFERENT model or a
#: differently framed question may well answer (the same PR did, re-run by
#: hand); not a parse failure, because more room and a reminder to emit JSON
#: are answers to a truncated array, not to a model that declined.
REFUSED = "refused"
#: Not recognised. Treated as un-retryable on purpose: an unknown failure is
#: not something to hammer, and fail-closed is the house rule.
UNRECOGNISED = "unrecognised"

#: Every code `classify` can return has a row. A code that does not is
#: UNRECOGNISED — so adding a code without thinking about retries costs a
#: retry, never an unwanted one.
_DISPOSITION: dict[str, str] = {
    "no_api_key": TERMINAL,
    "invalid_key": TERMINAL,
    "model_not_found": TERMINAL,
    "context_too_long": TERMINAL,
    "budget_exceeded": TERMINAL,
    # A 429 whose body says "quota": the window does not reopen by itself,
    # somebody has to pay or wait out a billing period. Not a "slow down".
    "quota_exhausted": TERMINAL,
    "rate_limited": THROTTLED,
    "provider_unavailable": TRANSIENT,
    # Transient because a slow call is often a call that would have
    # answered: the retry is worth its price. What it must NOT do is
    # blame the provider on the way past — see `classify`.
    "local_timeout": TRANSIENT,
    "generation_failed": UNRECOGNISED,
    # The one code `classify` does NOT return. A refusal is a reply, not an
    # exception; the reply reader builds `ProviderFailure("model_refused")`
    # by hand and the retry ladder reads the row here. It lives in this table
    # all the same, because a disposition decided anywhere else is the second
    # taxonomy this module exists not to have.
    "model_refused": REFUSED,
    # The other code `classify` does not return. The reply arrived, was paid
    # for, and could not be read as findings; the agent layer builds it by
    # hand at both of its parse-failure exits. It is in this table for the
    # same reason `model_refused` is — a disposition decided anywhere else is
    # the second taxonomy this module exists not to have — and it is
    # UNRECOGNISED because the agent has already spent its own corrective
    # retry by the time it says this, so a further resend is a second bill
    # for a question already asked twice.
    "unreadable_reply": UNRECOGNISED,
}

#: One sentence per code, for a person. These travel into a review summary and
#: a run record, where "security agent: provider quota exhausted" is the
#: difference between a user who fixes their billing and a user who files a
#: bug. Written here and not at each call site so there is one wording to fix.
_REASON: dict[str, str] = {
    "no_api_key": "no API key is configured for this workspace",
    "invalid_key": "the provider rejected the API key",
    "model_not_found": "the configured model does not exist at the provider",
    "context_too_long": "the request was larger than the model's context window",
    "budget_exceeded": "the workspace spend budget is exhausted",
    "quota_exhausted": "provider quota exhausted",
    "rate_limited": "the provider is rate-limiting this workspace",
    "provider_unavailable": "the provider is unavailable",
    "local_timeout": (
        "the request passed this installation's own timeout before the "
        "provider answered — raise REVIEW_LLM_TIMEOUT_SECONDS if the "
        "configured model is a slow one"
    ),
    # The reply reader appends the model's own sentence after this — see
    # `_agent_error_text` in src.review.agents.base. The sentence is model
    # OUTPUT, redacted and clipped where it was read, not a provider body;
    # what this row says is the part the UI can key on.
    "model_refused": "the model refused to review this change",
    "unreadable_reply": (
        "the model's reply could not be read as findings, twice"
    ),
    # `classify_vector_store`'s codes. They belong in the SAME table for the
    # same reason that function lives in this file: "no provider body ever
    # reaches a user" is one rule. Without a row here they fell through to the
    # generic sentence below, which calls the vault a provider — and the vault
    # is an accelerator whose absence is not an error at all.
    "vault_not_generated": "no documentation has been generated for this repository yet",
    "vault_unauthorized": "the documentation index rejected our credentials",
    "vault_unavailable": "the documentation index is unavailable",
}

#: Said instead of the hint when a code has no row above.
#:
#: `reason` used to fall back to `self.hint or self.code`, and the one code
#: that actually reaches that fallback is `generation_failed` — the
#: unclassified case, whose hint `classify` builds as
#: ``f"{type(exc).__name__}: {message}"``. That is the provider's response
#: body: clipped to MAX_HINT, but the body. So the property whose docstring
#: promised "a sentence safe to show an end user" returned exactly the payload
#: this module exists to keep off a screen, and it did so for the failure most
#: likely to be carrying one.
#:
#: The code slug is ours and stable, so it is what travels; the hint stays on
#: `hint`, where the callers that knowingly want a clipped message (the Q&A
#: error event) already read it, and in the log, where a diagnosis happens.
_UNMAPPED_REASON = "the provider call failed"


@dataclass(frozen=True)
class ProviderFailure:
    """`code` is translated by the UI; `hint` is optional technical detail."""

    code: str
    hint: str = ""

    @property
    def disposition(self) -> str:
        """TERMINAL / THROTTLED / TRANSIENT / REFUSED / UNRECOGNISED — see above."""
        return _DISPOSITION.get(self.code, UNRECOGNISED)

    @property
    def reason(self) -> str:
        """A sentence safe to show an end user, hint appended when there is one.

        Both halves are free of the provider's body, and now actually are: the
        sentence comes from `_REASON`, written here, and `hint` is assembled by
        `_hint_from` out of the exception's OWN ATTRIBUTES. An unmapped code
        does NOT fall back to the hint — see `_UNMAPPED_REASON` for the leak
        that fallback was. Bounded is not the same as safe.
        """
        text = _REASON.get(self.code)
        if text is None:
            return f"{_UNMAPPED_REASON} ({self.code})"
        return f"{text} ({self.hint})" if self.hint else text


def _trim(text: str, limit: int = MAX_HINT) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"{flat[:limit - 1]}…"


def _hint_from(exc: BaseException) -> str:
    """Provider/model/status only — assembled from attributes, not the body."""
    bits: list[str] = []
    for attr in ("llm_provider", "model"):
        value = getattr(exc, attr, None)
        if value:
            bits.append(str(value))
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        bits.append(f"HTTP {status}")
    return _trim(" · ".join(bits)) if bits else ""


def classify(exc: BaseException) -> ProviderFailure:
    """Map a generation failure to a stable code plus a short hint."""
    from src.llm.keys import LLMCredentialError

    if isinstance(exc, LLMCredentialError):
        return ProviderFailure("no_api_key")

    try:
        from src.llm.budget import BudgetExceeded
        if isinstance(exc, BudgetExceeded):
            return ProviderFailure("budget_exceeded")
    except Exception:  # noqa: BLE001 - budget module optional at import time
        pass

    message = str(exc)
    hint = _hint_from(exc)

    try:
        import litellm.exceptions as le
    except Exception:  # noqa: BLE001
        le = None  # type: ignore[assignment]

    if le is not None:
        # ContextWindowExceededError subclasses BadRequestError, so it is
        # tested first or it would never be reached.
        if isinstance(exc, le.ContextWindowExceededError):
            return ProviderFailure("context_too_long", hint)
        if isinstance(exc, (le.AuthenticationError, le.PermissionDeniedError)):
            return ProviderFailure("invalid_key", hint)
        if isinstance(exc, le.NotFoundError):
            return ProviderFailure("model_not_found", hint)
        if isinstance(exc, le.RateLimitError):
            # A 429 is two different problems wearing one status code: "slow
            # down" clears by itself, "you are out of quota" never does. Only
            # the message distinguishes them, so it is matched but not shown.
            return ProviderFailure(
                "quota_exhausted" if _QUOTA.search(message) else "rate_limited", hint,
            )
        if isinstance(exc, le.Timeout):
            # NOT `provider_unavailable`. A timeout is OUR deadline elapsing,
            # and the only thing we actually observed is that no answer had
            # arrived yet — the provider may be working on it perfectly well,
            # merely slower than the number we chose. Reporting it as an
            # outage sends the reader to the provider's status page for a
            # problem that lives in this repository's settings.
            #
            # It is not academic. Sixteen agent failures in eight hours on the
            # benchmark install were every one of them this, at a 120-second
            # default nothing could change; the harness read the run of
            # failures as provider quota and stopped three runs to protect a
            # dataset that was never in danger.
            return ProviderFailure("local_timeout", hint)
        if isinstance(exc, (le.ServiceUnavailableError, le.InternalServerError,
                            le.APIConnectionError)):
            return ProviderFailure("provider_unavailable", hint)

    # Reached when the SDK is absent, or when the exception is not one of its
    # classes but still carries an HTTP status. google-genai names it `code`,
    # most others `status_code`; both are read, because falling through to
    # "unknown" would put the provider's message back in the payload.
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return ProviderFailure("invalid_key", hint)
        if status == 404:
            return ProviderFailure("model_not_found", hint)
        if status == 429:
            return ProviderFailure(
                "quota_exhausted" if _QUOTA.search(message) else "rate_limited", hint,
            )
        if status >= 500:
            return ProviderFailure("provider_unavailable", hint)

    if "No API key was provided" in message:
        return ProviderFailure("no_api_key")

    # Unrecognised: the type name plus a clipped message, because an
    # unclassified failure with no detail at all cannot be reported by a user.
    return ProviderFailure(
        "generation_failed", _trim(f"{type(exc).__name__}: {message}"),
    )


def curated_reason(code: str | None) -> str | None:
    """The table's sentence for a code, or None when there is none.

    The gate between a run record and a public pull-request comment.
    `_agent_error_text` keeps `str(exc)` verbatim for an UNRECOGNISED failure
    — right for a record an authenticated operator reads, and not something to
    paste where anyone with access to the repository can see it, since a
    provider's own message is the one thing this module exists to keep out of
    a user's face.

    So a caller that publishes asks HERE rather than reusing the record's
    prose: a code with a row gets the sentence written for it, and a code
    without one gets nothing, which the caller renders as its own generic
    wording. Never a fallback sentence invented here — a failure we cannot
    name must not be dressed up as one we can.
    """
    if not code:
        return None
    return _REASON.get(code)


def _header_value(headers: object, name: str) -> str | None:
    """One header out of whatever header-shaped thing the exception carries.

    Compared lowercase by hand because only one of the two sources is
    case-insensitive: `httpx.Headers` is, but the plain dict LiteLLM's proxy
    attaches keeps whatever casing the proxy wrote.
    """
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — not header-shaped, so no header
        return None
    for key, value in items:
        if str(key).lower() == name:
            return str(value)
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own "come back in N seconds", when `exc` carries one.

    Measured on the installed litellm (1.97.0), not assumed: its exceptions
    have NO `retry_after` attribute. A Retry-After header survives in exactly
    two places — `exc.headers`, the plain dict LiteLLM's proxy attaches
    deliberately, and `exc.response.headers`, where the vendor's own 429
    response headers are preserved (LiteLLM keeps them off `exc.headers` on
    purpose, so both must be read; the dict wins because attaching it was an
    explicit act).

    Numeric seconds only. The HTTP-date form of Retry-After is legal, but no
    LLM provider has been seen sending it, and a misparsed date becomes a real
    sleep in a review thread — so a date, like any other unparseable value, is
    None and the caller falls back to its own default pause. The caller also
    owns the ceiling: this function reports what the provider said, however
    large, because "what was said" and "what we will honour" are two answers.
    """
    value = _header_value(getattr(exc, "headers", None), "retry-after")
    if value is None:
        response = getattr(exc, "response", None)
        value = _header_value(getattr(response, "headers", None), "retry-after")
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


#: Qdrant says this when the collection was never created. It is not an error
#: condition for the product — it means nobody has generated documentation yet,
#: and answers come from source code instead.
_NO_COLLECTION = re.compile(r"doesn'?t exist|not found: collection", re.IGNORECASE)


def classify_vector_store(exc: BaseException) -> ProviderFailure:
    """Same contract as `classify`, for the vector store rather than a model.

    The retriever stored `str(exc)[:200]` and passed it to the browser, so a
    missing collection reached users as

        Unexpected Response: 404 (Not Found) Raw response content:
        b'{"status":{"error":"Not found: Collection code_analysis_vault
        doesn\\'t exist!"}…

    which names an internal collection and reads as a crash. It is neither: the
    vault is an ACCELERATOR, the retrieval already degrades to grep + graph +
    source, and the only thing the user needs to know is that documentation
    search was not part of this answer.

    Lives beside `classify` on purpose — "no provider body ever reaches a user"
    is one rule, and a second file would be the one that forgets it.
    """
    # By TYPE first. `CollectionMissing` is raised by our own probe before any
    # request is made, and its message is ours to word — so matching it on
    # "doesn't exist" would mean the wording of an internal exception had to
    # keep agreeing with a regex written for Qdrant's response body. The regex
    # stays for the vendor errors it was written for.
    from src.retrieval.tier1_vault import CollectionMissing
    if isinstance(exc, CollectionMissing):
        return ProviderFailure("vault_not_generated")

    message = str(exc)
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)

    if _NO_COLLECTION.search(message) or status == 404:
        return ProviderFailure("vault_not_generated")
    if isinstance(status, int) and status in (401, 403):
        return ProviderFailure("vault_unauthorized")
    return ProviderFailure("vault_unavailable", _trim(type(exc).__name__, 60))


__all__ = [
    "curated_reason",
    "MAX_HINT", "REFUSED", "TERMINAL", "THROTTLED", "TRANSIENT", "UNRECOGNISED",
    "ProviderFailure", "classify", "classify_vector_store",
    "retry_after_seconds",
]
