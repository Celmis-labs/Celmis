"""What the INSTALLED LiteLLM knows about a model — ceilings and reasoning.

Written after the architect agent failed in 43% of runs against a hardcoded
4096-token output ceiling. The story is in `src/review/settings.py`: Gemini 3.x
counts reasoning tokens against the same output budget, so thinking ate the
budget, the findings array was truncated mid-JSON, and the agent reported "no
JSON array in the reply". Raising the number fixed that run. This module exists
so the NEXT number is not a guess either.

Two things are deliberately absent.

**A per-vendor table.** "Reasoning effort" is not one vocabulary: OpenAI takes
``reasoning_effort``, Anthropic a thinking budget in tokens, Gemini a
thinking_budget int where -1 means dynamic. Any table of that goes stale the
week after it is written — and it would be a SECOND table, because LiteLLM
already holds the mapping and does the translation. So everything below is read
out of the installed LiteLLM at runtime: the ceiling from ``get_model_info``,
the accepted parameter NAMES from ``get_supported_openai_params``, and the
effort vocabulary from the ``REASONING_EFFORT`` Literal itself. Measured on
litellm 1.97.0, ``reasoning_effort="low"`` becomes ``thinkingConfig
{"thinkingLevel": "low"}`` for Gemini 3 and ``thinking {"type": "enabled",
"budget_tokens": 1024}`` for Anthropic, from the same word — which is the whole
reason we normalise on the word rather than on either native shape.

**A guess for a model LiteLLM has never heard of.** A self-hosted
``openai/<name>``, or a release newer than the installed table (``gemini-3-pro``
is unmapped today while ``gemini-3-pro-preview`` is), reports ``known=False``,
every other field None. Callers must NOT clamp then: there is nothing to clamp
to, and inventing a ceiling would turn a working call into a truncated one.

The parameter probe is the same function LiteLLM itself calls before raising
``UnsupportedParamsError`` — asking it is how a model that cannot reason ends
up not receiving the parameter, instead of receiving it and answering 400.

But that probe answers a smaller question than it was being asked. It measures
what the ROUTER will send, not what the MODEL will accept, and the two differ:
`gemini-3.7-flash` takes "low"/"medium"/"high" from the live API and answers
400 to "none" and "minimal", both of which LiteLLM translates without
complaint. So the fields below are named for the authority that answered them —
``reasoning_values_router_accepts``, not "supported" — and the provider gets
the last word through ``_PROVIDER_REFUSED``, which is filled in from the calls
that actually went out. See that block for the whole story and for where the
memory lives.

:func:`resolve_litellm_model` lives here rather than next to either caller
because a capability answer is only worth anything when it is about the string
the request will actually carry. It used to be answered twice — once by
``src.llm.client`` for the call and once by ``src.api.routers.llm`` for the
settings page — and the two disagreed for exactly one input: a bare model id
belonging to a vendor other than the workspace's review profile. The screen
welded the profile's vendor on ("gemini/gpt-4o"), got ``known=False``, and
refused to save a model that the review path was meanwhile calling correctly
as "gpt-4o". One function, both callers, no second inference to drift.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

#: The two reasoning parameter names LiteLLM's `completion()` signature carries.
#: `reasoning_effort` is preferred wherever a model accepts it: it is the one
#: vocabulary that spans vendors, and LiteLLM translates it into each vendor's
#: native shape. `thinking` is the fallback for a model that takes only a token
#: budget — and note it is NOT a substitute on Gemini 3, where the installed
#: LiteLLM drops `budget_tokens` on the floor and sends a bare thinkingConfig.
_EFFORT_PARAM = "reasoning_effort"
_BUDGET_PARAM = "thinking"

REASONING_EFFORT = "effort"
REASONING_BUDGET = "budget"

# ─── What Celmis changed behind the operator's back, said out loud ───
#
# The runtime self-heals in four places — a ceiling above the model max is
# clamped, a reasoning word the provider refuses is dropped, a temperature the
# model refuses is dropped, a fallback model is called — and each of those used
# to be recorded somewhere different: two fields on LLMResult, one audit-log
# key, two fields on AgentRunResult. None of it reached the operator. The
# review page showed none of these, and on /settings/llm a refused reasoning
# word simply VANISHED from the dropdown with no reason, no date and no remedy.
# Invisible self-healing is how a review quietly gets worse and nobody knows
# which knob to turn, so every adjustment now travels as ONE shape, below, from
# the call that made it to the run row and the PR comment.

#: The parameters an adjustment can be about. Closed vocabulary: the API schema
#: and the reviews page switch on these words.
PARAM_MAX_OUTPUT_TOKENS = "max_output_tokens"
PARAM_REASONING = "reasoning"
PARAM_TEMPERATURE = "temperature"
PARAM_MODEL = "model"

#: What was done about it. `clamped` — the number was cut down to what the
#: model accepts; `dropped` — the parameter was not sent at all; `swapped` — a
#: different model answered.
ADJUST_CLAMPED = "clamped"
ADJUST_DROPPED = "dropped"
ADJUST_SWAPPED = "swapped"


@dataclass(frozen=True)
class ParameterAdjustment:
    """One parameter Celmis changed between what was asked and what was sent.

    `requested` is what the operator (or the agent on their behalf) asked for,
    `sent` is what actually went to the provider, `reason` is the provider's
    own sentence when there is one and the rule otherwise ("model ceiling is
    65535"). `agent` names the review stage the call belonged to, so a batch
    can carry the adjustments of every agent in one flat list; `model` names
    the model the parameter was fitted to, because "reasoning 'minimal' was
    refused" is only actionable together with WHO refused it.

    Built in exactly two places — `LLMClient.generate` for the three
    per-call kinds and `LLMReviewAgent._generate_and_parse` for the model swap
    — and merged, never re-derived, by everything downstream.
    """

    agent: str | None
    parameter: str        # one of PARAM_*
    requested: Any
    sent: Any
    action: str           # one of ADJUST_*
    reason: str
    model: str | None = None

    def as_dict(self) -> dict:
        """The wire shape — the same dict on the run row and on the API."""
        return {
            "agent": self.agent,
            "parameter": self.parameter,
            "requested": self.requested,
            "sent": self.sent,
            "action": self.action,
            "reason": self.reason,
            "model": self.model,
        }


def adjustment_as_dict(adjustment: ParameterAdjustment | dict) -> dict:
    """The wire dict of an adjustment, whichever shape a caller holds.

    A batch built by the orchestrator carries `ParameterAdjustment`s; a batch
    read back from a run row, or built by a test double, carries the dicts.
    The run-row writer and the PR-comment banner both read through this so
    neither has to know which one it was handed — the alternative is the
    same `isinstance` ladder in two files, which drift.
    """
    if isinstance(adjustment, ParameterAdjustment):
        return adjustment.as_dict()
    if isinstance(adjustment, dict):
        return {
            "agent": adjustment.get("agent"),
            "parameter": str(adjustment.get("parameter") or ""),
            "requested": adjustment.get("requested"),
            "sent": adjustment.get("sent"),
            "action": str(adjustment.get("action") or ""),
            "reason": str(adjustment.get("reason") or ""),
            "model": adjustment.get("model"),
        }
    raise TypeError(f"not an adjustment: {type(adjustment).__name__}")


@dataclass(frozen=True)
class ProviderRefusal:
    """One value the PROVIDER said no to, and when this process learned it.

    Learned from traffic, never from a table — see the `_PROVIDER_REFUSED`
    block below for why the memory lives where it does. `seen_at` is here so
    a screen can say "refused by the provider on <date>" instead of silently
    losing an option between two page loads: the memory is process-local and
    re-learned after every restart, and a fact with no date on it cannot be
    told apart from a fact that is stale.
    """

    parameter: str    # PARAM_REASONING | PARAM_TEMPERATURE
    value: str        # the refused value, as text ("minimal", "0.1")
    reason: str       # the provider's own sentence
    seen_at: str      # ISO-8601 UTC

    def as_dict(self) -> dict:
        return {
            "parameter": self.parameter,
            "value": self.value,
            "reason": self.reason,
            "seen_at": self.seen_at,
        }


@dataclass(frozen=True)
class ModelCapabilities:
    """One model's answer to "what may I ask of you?".

    `known` is the field that matters. False means LiteLLM has no entry, so
    every other field is None and the caller has to say "unknown" rather than
    substitute a default — the fail-closed rule this project runs on.
    """

    model: str
    known: bool
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    reasoning_kind: str | None = None                 # "effort" | "budget" | None
    #: The effort words this build may offer for this model: what the ROUTER
    #: accepts, minus anything the provider has since refused.
    #:
    #: Named for its source on purpose. It was called `reasoning_values` and
    #: rendered on /settings/llm as what the model supports, which is a claim
    #: nothing here had measured — the probe behind it asks LiteLLM whether it
    #: can translate the word, and LiteLLM translated two words gemini-3.7-flash
    #: answers 400 to. Until a call has gone out, "the router accepts it" is
    #: the whole of what is known, and the name now says so.
    reasoning_values_router_accepts: tuple[str, ...] | None = None
    #: Words the probe offered and the PROVIDER rejected, with the refusal
    #: measured rather than predicted. Separate from the list above so a screen
    #: can explain why a value it used to show is gone, instead of silently
    #: losing an option between two page loads.
    reasoning_values_provider_refused: tuple[str, ...] | None = None
    supports_function_calling: bool | None = None
    source: str = "unknown"                           # "litellm" | "unknown"
    #: Every value the provider has refused for this model, with the sentence
    #: and the date — the facts behind `reasoning_values_provider_refused`,
    #: plus the ones that list cannot hold (a temperature). Reported for an
    #: UNKNOWN model too: a self-hosted server's refusal is measured, not read
    #: from a table, and it is the one thing about that model we do know.
    provider_refusals: tuple[ProviderRefusal, ...] = ()

    @property
    def reasoning_values(self) -> tuple[str, ...] | None:
        """The old name for :attr:`reasoning_values_router_accepts`.

        Transitional, and it is here so the rename can cross the HTTP boundary
        in two commits instead of one big-bang: `src/api/routers/llm.py` and
        `web/` still read the old spelling, and this module does not own them.
        Delete both this property and the duplicate wire key in `as_dict` once
        the API and the settings page mirror the new name.
        """
        return self.reasoning_values_router_accepts

    def as_dict(self) -> dict:
        """The wire shape shared with GET /api/llm/model-capabilities.

        Lists, not tuples, because this is JSON — and the same dict either
        side of the HTTP boundary is what keeps the UI's idea of a model and
        the resolver's idea of it from drifting apart.
        """
        offered = (
            list(self.reasoning_values_router_accepts)
            if self.reasoning_values_router_accepts is not None else None
        )
        return {
            "model": self.model,
            "known": self.known,
            "max_output_tokens": self.max_output_tokens,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_kind": self.reasoning_kind,
            "reasoning_values_router_accepts": offered,
            "reasoning_values_provider_refused": (
                list(self.reasoning_values_provider_refused)
                if self.reasoning_values_provider_refused is not None else None
            ),
            # The old key, same value, on its way out — see the property above.
            "reasoning_values": offered,
            "supports_function_calling": self.supports_function_calling,
            "source": self.source,
            # Always a list, never null: "nothing learned" and "nothing
            # refused" are the same answer here, and a screen iterates it.
            "provider_refusals": [r.as_dict() for r in self.provider_refusals],
        }


# ─── Which LiteLLM string a configured model resolves to ─────────────

#: The one provider slug whose model ids carry no vendor of their own. A
#: self-hosted server is addressed in the OpenAI dialect at an address the
#: profile holds (``Profile.api_base``), and the operator names the model
#: whatever they like — "qwen3-coder" says nothing about who serves it, so the
#: prefix has to come from the profile. Every OTHER slug in
#: ``src.llm.profiles._LITELLM_PREFIX`` is deliberately NOT honoured here: a
#: model id that names its own vendor outranks the workspace's review profile,
#: which is the whole bug this function was extracted to end.
_SELF_HOSTED_PROVIDER = "openai_compatible"


def provider_of(model: str) -> str:
    """The provider slug LiteLLM will route `model` to, as this codebase reads it.

    Examples:
        "anthropic/claude-sonnet-5"         → "anthropic"
        "gemini/gemini-3-pro-preview"       → "gemini"
        "openrouter/anthropic/claude-…"     → "openrouter"
        "gpt-4o"                            → "openai"  (litellm's default)
        "claude-sonnet-5"                   → "anthropic"

    Deliberately name-shape heuristics rather than ``litellm.get_llm_provider``,
    and this is not an oversight: the answer here is what picks the API KEY, and
    LiteLLM reads a bare "gemini-3-flash-preview" as **vertex_ai** — which sends
    the call looking for Application Default Credentials a container has no
    reason to hold, while /settings/llm shows the Gemini key saved and its Test
    button passing. :func:`resolve_litellm_model` is what makes the two agree,
    by writing this answer into the model string before LiteLLM sees it.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    # Bare model name — best-effort mapping (LiteLLM's own routing does the same).
    m = model.lower()
    if m.startswith(("gpt-", "o1-", "o3-", "chatgpt")):
        return "openai"
    if m.startswith(("claude-",)):
        return "anthropic"
    if m.startswith(("gemini-",)):
        return "gemini"
    if m.startswith(("llama", "gemma")):
        return "groq"  # rough — could also be together_ai / bedrock
    if m.startswith(("mistral-", "mixtral-", "codestral", "open-mixtral", "open-mistral")):
        return "mistral"
    return "openai"  # LiteLLM's own fallback


def resolve_litellm_model(model: str, provider: str | None = None) -> str:
    """The exact string LiteLLM is handed for a configured `model`.

    THE one answer to that question. Ask it before every capability lookup and
    before every call, because a ceiling or a reasoning vocabulary read off a
    different string than the one that goes out is worse than no answer at all.

    `provider` is the surface's configured provider slug, and it is consulted
    for one case only — see :data:`_SELF_HOSTED_PROVIDER`. Per-agent overrides
    and the ``review_policies.<agent>_model`` columns have always stored BARE
    ids, and 529 of the keys /api/models/available offers are bare too, so a
    cross-vendor pick ("gpt-4o" under a Google review profile) is point-and-click
    reachable. Taking the vendor from the profile turned that pick into
    "gemini/gpt-4o": unknown to LiteLLM, so the settings page refused to save a
    model the review path was calling — correctly, as "gpt-4o" — all along.

    A value that already carries a prefix is returned untouched. That is how a
    self-hosted "openai/qwen3-coder" stays what the operator typed, and how it
    keeps reporting `known=False` instead of acquiring a plausible-looking
    vendor and a ceiling that belongs to somebody else's model.
    """
    name = (model or "").strip()
    if not name or "/" in name:
        return name
    if (provider or "").strip() == _SELF_HOSTED_PROVIDER:
        return f"openai/{name}"
    if provider_of(name) == "gemini":
        # The one rewrite LiteLLM needs from us: bare "gemini-*" is vertex_ai to
        # it and Gemini to us, and the key we hold is the Gemini one.
        return f"gemini/{name}"
    # Everything else goes to LiteLLM exactly as configured — including a name
    # LiteLLM has never heard of. Guessing a prefix for it would trade an honest
    # "unknown" for a confident answer about a different model.
    return name


# ─── LiteLLM probes — every one of them tolerant of an absent litellm ──


def _model_info(model: str) -> dict | None:
    """LiteLLM's entry for `model`, or None when it has never heard of it.

    `get_model_info` raises for an unmapped model rather than returning an
    empty dict, and that raise is the only reliable "unknown" signal there is:
    `supports_reasoning()` answers a flat False for an unmapped model, which
    is indistinguishable from a mapped model that genuinely does not reason.
    """
    try:
        import litellm
    except ImportError:  # pragma: no cover — litellm is a hard dependency
        return None
    try:
        info = litellm.get_model_info(model)
    except Exception:  # noqa: BLE001 — every failure means "not mapped"
        return None
    return info if isinstance(info, dict) else None


def _split_model(model: str) -> tuple[str, str] | None:
    """(bare_model, provider) as LiteLLM reads them, or None.

    Deliberately `get_llm_provider` rather than `model.split("/")`: a bare
    "gemini-3-flash-preview" resolves to **vertex_ai**, not gemini, which is
    exactly the mismatch documented in `client.py` that sent agents looking
    for Application Default Credentials a container has no reason to hold.
    """
    try:
        import litellm
    except ImportError:  # pragma: no cover
        return None
    try:
        bare, provider = litellm.get_llm_provider(model=model)[:2]
    except Exception:  # noqa: BLE001 — an unprefixed unknown name has no provider
        return None
    return str(bare), str(provider)


@lru_cache(maxsize=256)
def _supported_params(model: str) -> tuple[str, ...]:
    """The OpenAI-shaped parameter names this model accepts, per LiteLLM."""
    split = _split_model(model)
    if split is None:
        return ()
    bare, provider = split
    try:
        from litellm import get_supported_openai_params
        params = get_supported_openai_params(
            model=bare, custom_llm_provider=provider,
        )
    except Exception:  # noqa: BLE001
        return ()
    return tuple(str(p) for p in (params or ()))


@lru_cache(maxsize=256)
def _effort_vocabulary(model: str) -> tuple[str, ...]:
    """Which effort WORDS the ROUTER will translate for this model.

    Asked one word at a time, because there is no per-model list to read and
    the vendors genuinely differ: on litellm 1.97.0 `o3` and Claude take
    "xhigh" while `gpt-5` and Gemini 3 Flash refuse it. `get_optional_params`
    is the function that raises `UnsupportedParamsError` inside
    `litellm.completion`, so a word that survives it here is a word that
    survives the router.

    It is NOT a word the model accepts, and this docstring used to say it was.
    LiteLLM translates "minimal" for gemini-3.7-flash quite happily and the
    Gemini API answers 400 to the result. `_PROVIDER_REFUSED` is where that
    difference is kept; `model_capabilities` subtracts it from this list.
    """
    split = _split_model(model)
    if split is None:
        return ()
    bare, provider = split
    try:
        import typing

        import litellm
        from litellm.utils import get_optional_params
        candidates = typing.get_args(litellm.REASONING_EFFORT)
    except Exception:  # noqa: BLE001
        return ()
    accepted: list[str] = []
    for value in candidates:
        try:
            get_optional_params(
                model=bare, custom_llm_provider=provider, reasoning_effort=value,
            )
        except Exception:  # noqa: BLE001 — this vendor will not take that word
            continue
        accepted.append(str(value))
    return tuple(accepted)


# ─── What the PROVIDER refuses — measured, because the probe over-promises ──
#
# `_effort_vocabulary` asks `get_optional_params`, which is the function
# `litellm.completion` calls before it builds a request. A word that survives
# it is a word LiteLLM will TRANSLATE, and that was quietly read here as a word
# the model will ACCEPT. Measured against the live Gemini API with a real key:
#
#     model                     none     minimal  low      medium   high
#     gemini-3.7-flash          REFUSED  REFUSED  OK       OK       OK
#     gemini-3-flash-preview    OK       OK       OK       OK       OK
#     gemini-3.1-flash-lite     OK       OK       OK       OK       OK
#
# the refusal reading "Thinking level MINIMAL is not supported for this model.
# Please retry with other thinking level." So the screen whose entire purpose
# is "what does this model actually take" was offering two words that one of
# these three models answers 400 to. LiteLLM is not the authority on that
# question. The provider is, and the only way to ask the provider is to call
# it — so this is the one capability fact in the module that is learned from
# traffic instead of from a table.
#
# WHERE THE MEMORY LIVES, and why it is not a table.
#
# Process-local, and that is a decision rather than the easy path. The
# alternatives were a migration-backed table and a credential-store row, and
# both lose on the same two points:
#
#   * A refusal is a property of the MODEL, not of a tenant. Every workspace on
#     gemini-3.7-flash gets the same answer, so a credential-store row — which
#     is workspace-scoped — would re-learn one provider's behaviour once per
#     tenant and keep N copies of it.
#   * A persisted negative cache has no invalidation story. The day Google adds
#     MINIMAL to gemini-3.7-flash, a stored row hides it forever: no expiry, no
#     screen to clear it, and nobody would think to look. A process-local dict
#     is re-learned on every restart and every deploy, which is exactly the
#     refresh a persisted one would need and would not have.
#
# The price is stated so it can be argued with: ONE wasted call per
# (model, effort word) per worker process per restart. That call is a 400 the
# provider rejects before inference — no tokens billed — and
# `LLMClient.generate` immediately retries it without the parameter, so nothing
# above it fails. Persist this the day that price changes: if a refusal ever
# costs tokens, or ever reaches a user as a failed review, a table with a TTL
# becomes the cheaper side of the trade.
#
# Threading: review agents run in a ThreadPoolExecutor and a dict
# insert/lookup under the GIL needs no lock. The worst interleaving is two
# threads recording the same pair, which is idempotent.

#: (capability model, parameter, value as text) → what the provider said and
#: when. It held `(model, effort word) → sentence` until the temperature
#: refusal arrived (claude-sonnet-5 takes only its default) and needed the
#: same memory; the parameter joined the key rather than a second dict being
#: opened beside this one, so `provider_refusals` on the settings page reads
#: every learned fact about a model out of ONE place. `seen_at` joined the
#: value for the reason given on `ProviderRefusal`: a refusal the screen can
#: date is a refusal the operator can judge.
_PROVIDER_REFUSED: dict[tuple[str, str, str], ProviderRefusal] = {}


def _refusal_key(model: str, parameter: str, value: object) -> tuple[str, str, str]:
    return ((model or "").strip(), parameter, str(value))


def _temperature_text(temperature: float) -> str:
    """0.1, 0.10 and "0.1" are one refused value, not three."""
    try:
        return f"{float(temperature):g}"
    except (TypeError, ValueError):
        return str(temperature)


def provider_refusal(model: str, parameter: str, value: object) -> ProviderRefusal | None:
    """The remembered refusal of `value` for `parameter` on `model`, or None."""
    return _PROVIDER_REFUSED.get(_refusal_key(model, parameter, value))


def provider_refusals(model: str) -> tuple[ProviderRefusal, ...]:
    """Everything the provider has refused for `model`, oldest first."""
    name = (model or "").strip()
    return tuple(sorted(
        (r for (m, _p, _v), r in _PROVIDER_REFUSED.items() if m == name),
        key=lambda r: r.seen_at,
    ))


def _record_refusal(model: str, parameter: str, value: str, sentence: str) -> None:
    key = _refusal_key(model, parameter, value)
    if key not in _PROVIDER_REFUSED:
        _PROVIDER_REFUSED[key] = ProviderRefusal(
            parameter=parameter, value=str(value), reason=sentence,
            seen_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        logger.warning(
            "%s_value_refused_by_provider model=%s value=%s — this build "
            "stops sending it: %s", parameter, key[0], value, sentence,
        )
    # The composed answer is memoised and is now holding a value the provider
    # has just refused. Only that one cache is dropped: the raw probes stay,
    # because they remain an honest report of what LiteLLM would translate.
    model_capabilities.cache_clear()


def _forget_refusal(model: str, parameter: str, value: str) -> bool:
    if _PROVIDER_REFUSED.pop(_refusal_key(model, parameter, value), None) is None:
        return False
    logger.warning(
        "%s_refusal_withdrawn model=%s value=%s — the same refusal came back "
        "on a request that did not carry the value, so the value was not what "
        "the provider was objecting to", parameter, (model or "").strip(), value,
    )
    model_capabilities.cache_clear()
    return True

#: The subject half of a reasoning refusal: the provider has to NAME the
#: parameter for its complaint to be evidence about ours.
_REASONING_SUBJECT = re.compile(
    r"thinking[ _-]?(?:level|budget|config)|reasoning[ _-]?effort",
    re.IGNORECASE,
)
#: The refusal half. Deliberately not "error", "failed" or "invalid request":
#: a sentence that does not say the VALUE is unacceptable says nothing about
#: the value.
_REASONING_REFUSED = re.compile(
    r"not supported|unsupported|not valid|invalid value|not allowed|not available",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
#: Where a JSON string ENDS: a closing quote followed by structure. A provider
#: body reaches us as LiteLLM's prefix glued to Google's JSON, so the refusal
#: sentence arrives with `{"error": {"code": 400, "message": "` in front of it
#: and `", "status": "INVALID_ARGUMENT"}}` behind. This cuts the envelope off
#: without cutting an ordinary quoted word — `Thinking level "minimal" is …`
#: continues with a letter, not a brace.
_JSON_TAIL = re.compile(r'"\s*[,}\]]')
#: Ceiling on the provider prose carried forward, matching `errors.MAX_HINT`
#: and there for the same reason: nothing out of a provider body reaches a
#: person unbounded.
_MAX_REFUSAL_SENTENCE = 200


def _untangle(text: str) -> str:
    """One sentence of provider English, with the JSON envelope trimmed off."""
    return _JSON_TAIL.split(text, maxsplit=1)[0].strip()


def _clip(text: str) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= _MAX_REFUSAL_SENTENCE:
        return flat
    return f"{flat[:_MAX_REFUSAL_SENTENCE - 1]}…"


_TEMP_WORDS = ("temperature",)
_TEMP_BAD = ("only temperature", "does not support temperature",
             "unsupported", "not supported", "must be", "only supports",
             "invalid value")


def temperature_refusal(exc: BaseException, sent_temperature: float | None) -> str | None:
    """The provider's sentence, when `exc` is it refusing the temperature we sent.

    The sibling of :func:`reasoning_refusal`, and here for the same reason with
    one difference that matters: `get_supported_openai_params` answers TRUE for
    temperature on every model tried, because the parameter IS supported — it is
    the VALUE that is refused. `claude-sonnet-5` accepts only `temperature=1`
    and 400s on anything else, so a capability probe cannot see this coming and
    only the call can.

    The same three conditions, for the same reasons:
      1. we actually sent a temperature — a call that sent none cannot have been
         refused for one, whatever the body says;
      2. the status is 400 — a 429 or a 5xx is not evidence about a value;
      3. one sentence names temperature AND says it is unacceptable, so a
         request echo cannot pair with an unrelated complaint later on.

    Errs toward "not a refusal": a provider that refuses with a 422, or whose
    body never spells the word, keeps costing one rejected call. That is no
    worse than today. The opposite error — dropping temperature because of an
    unrelated 400 — would silently change what every review asks for, so it is
    the one this matcher is written to avoid.
    """
    if sent_temperature is None:
        return None
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    body = str(exc)
    # UnsupportedParamsError carries no status; its name is the evidence.
    if status not in (400, None) or (status is None
                                     and "unsupportedparams" not in type(exc).__name__.lower()):
        return None
    sentences = re.split(r"(?<=[.!?])\s+|\n", body)
    for i, sentence in enumerate(sentences):
        low = sentence.lower()
        if any(w in low for w in _TEMP_WORDS) and any(b in low for b in _TEMP_BAD):
            # Quote the English, leave the envelope — the same cut
            # `reasoning_refusal` makes, and now for the same reason: this
            # sentence travels to the run record and the PR comment as the
            # adjustment's `reason`, and LiteLLM's prefix glued to the
            # vendor's JSON is exactly what src/llm/errors.py exists to keep
            # off a screen. The cut starts at the word that names the
            # parameter (whole token, so Google's 'generation_config.temperature'
            # keeps its prefix) and ends where the JSON string does.
            #
            # And it carries the NEXT sentence(s) while they still talk about
            # temperature. The real message is two sentences — "claude-sonnet-5
            # does not support temperature=0.1. Only temperature=1 is
            # supported." — and cutting at the first full stop kept
            # "temperature=0.1." and threw away the only part an operator can
            # act on. The remedy is in sentence two; it rides along.
            tail = [sentence]
            for nxt in sentences[i + 1:]:
                if "temperature" not in nxt.lower():
                    break
                tail.append(nxt)
            joined = " ".join(t.strip() for t in tail)
            subject = re.search(r"[\w.'\-]*temperature", joined, re.IGNORECASE)
            start = subject.start() if subject else 0
            return _clip(_untangle(joined[start:].strip()))
    return None


def reasoning_refusal(exc: BaseException, sent: dict) -> str | None:
    """The provider's own sentence, when `exc` is it refusing what `sent` carried.

    None means "not a reasoning refusal", and a caller that gets None must
    re-raise. Three conditions have to hold together, and each is here to keep
    a DIFFERENT failure out of the vocabulary:

    1. `sent` actually carried a reasoning parameter. A call that asked for no
       thinking cannot have been refused for thinking, whatever the body says.
       No amount of message-matching substitutes for this one.
    2. The status is 400. A quota error is 429, a rejected key 401/403, an
       outage 5xx — none of them are evidence about whether a word is valid,
       and a 429 allowed to poison the vocabulary would delete a working level
       from every workspace's dropdown because one card expired.
    3. One sentence of the message names a reasoning parameter AND says it is
       unacceptable. Both in the same sentence, so "thinking level: high" in a
       request echo cannot pair up with an unrelated "invalid value" later on.

    WHAT WOULD MAKE THIS MATCHER WRONG. It is a text match on somebody else's
    prose, so it errs in a chosen direction. A provider that refuses a thinking
    level with a 422, or with a 400 whose body never spells the parameter
    (OpenAI's "Invalid value: 'minimal'" names the value but not the field),
    reads here as "not a refusal": the word stays advertised and every attempt
    keeps costing one rejected call. That is today's behaviour and no worse.
    The dangerous direction is the opposite one — a 400 that mentions a
    thinking parameter while complaining about something else, a malformed
    request quoting our own thinkingConfig back at us, say — because that
    strikes a good word off the list until the process restarts. Condition 1 is
    what makes that require us to have asked for thinking in the first place,
    and the restart is what bounds the damage.
    """
    if not sent:
        return None
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if status != 400:
        return None
    sentences = _SENTENCES.split(" ".join(str(exc).split()))
    for index, sentence in enumerate(sentences):
        subject = _REASONING_SUBJECT.search(sentence)
        refused = _REASONING_REFUSED.search(sentence)
        if not (subject and refused):
            continue
        # Quote the English, leave the envelope. This string travels to a run
        # record, and the envelope is exactly what `src/llm/errors.py` exists
        # to keep off a screen. The cut starts at whichever half of the match
        # comes first, so a provider that words it the other way round —
        # "Invalid value for thinking level X" — keeps its verdict.
        quoted = [_untangle(sentence[min(subject.start(), refused.start()):])]
        # Keep the advice when it gave any. "Please retry with other thinking
        # level" is the actionable half of the message observed, and dropping
        # it would leave the operator the diagnosis without the cure.
        following = sentences[index + 1] if index + 1 < len(sentences) else ""
        if following and _REASONING_SUBJECT.search(following):
            quoted.append(_untangle(following))
        return _clip(" ".join(part for part in quoted if part))
    return None


def record_reasoning_refusal(model: str, sent: dict, sentence: str) -> bool:
    """Strike the effort word `sent` carried off `model`'s offered vocabulary.

    True when something was recorded. False for a model whose reasoning is a
    token BUDGET rather than a word: there is no vocabulary to prune, the
    caller still drops the parameter and retries, but a number is not a list
    entry and putting "4096" in a dropdown would be a second bug.
    """
    name = (model or "").strip()
    word = sent.get(_EFFORT_PARAM)
    if not name or not isinstance(word, str):
        return False
    _record_refusal(name, PARAM_REASONING, word, sentence)
    return True


def forget_reasoning_refusal(model: str, sent: dict) -> bool:
    """Withdraw a refusal the retry disproved. True when one was withdrawn.

    The retry is a controlled experiment nobody had to pay extra for: the same
    request went out once with the level and once without, and if BOTH come
    back refused for a thinking level then the level was never the cause — the
    matcher fired on a 400 about something else. Recording is what strikes a
    word off every workspace's dropdown for the life of the process, which
    `reasoning_refusal` calls the dangerous direction, so the one moment the
    evidence is disproved is the one moment to undo it.

    Not reached in the ordinary case. When the provider really does refuse the
    level, the retry succeeds and there is nothing to withdraw.
    """
    name = (model or "").strip()
    word = sent.get(_EFFORT_PARAM)
    if not name or not isinstance(word, str):
        return False
    return _forget_refusal(name, PARAM_REASONING, word)


def refused_reasoning(model: str, reasoning: str | int | None) -> ProviderRefusal | None:
    """The remembered refusal of this reasoning level on `model`, or None.

    The fact itself, for a caller that wants the provider's sentence and the
    date rather than the composed note below — the run record's
    `parameter_adjustments` carries the sentence as `reason`, and quoting the
    note there would wrap the provider's words in ours twice over.
    """
    if not isinstance(reasoning, str):
        return None
    return provider_refusal(model, PARAM_REASONING, reasoning.strip().lower())


def refused_reasoning_note(model: str, reasoning: str | int | None) -> str | None:
    """The run-record sentence for a level this provider refuses, or None.

    One wording for two moments: the call that discovered the refusal and every
    later call that never sends the level because of it. The second half is the
    point — a setting that is silently ignored from the second run onward is
    the same bug this module exists to end, and `gemini_thinking_budget` spent
    a release reaching nothing while saying nothing about it.
    """
    refusal = refused_reasoning(model, reasoning)
    if refusal is None:
        return None
    return (
        f"reasoning level {refusal.value!r} was dropped — the provider refuses it for "
        f"this model: {refusal.reason}"
    )


def record_temperature_refusal(model: str, temperature: float | None, sentence: str) -> bool:
    """Remember that `model` refused `temperature`. True when recorded.

    The temperature sibling of :func:`record_reasoning_refusal`, kept in the
    same memory for the same reasons. There is no vocabulary to prune here —
    temperature has no dropdown — but the fact still has two readers: the next
    call in this process, which stops paying a 400 to re-learn it, and the
    settings page, which can now say "this model takes only its default
    temperature, refused on <date>" instead of nothing at all.
    """
    name = (model or "").strip()
    if not name or temperature is None:
        return False
    _record_refusal(name, PARAM_TEMPERATURE, _temperature_text(temperature), sentence)
    return True


def forget_temperature_refusal(model: str, temperature: float | None) -> bool:
    """Withdraw a temperature refusal the retry disproved — see
    :func:`forget_reasoning_refusal`, whose argument holds here unchanged."""
    name = (model or "").strip()
    if not name or temperature is None:
        return False
    return _forget_refusal(name, PARAM_TEMPERATURE, _temperature_text(temperature))


def refused_temperature(model: str, temperature: float | None) -> ProviderRefusal | None:
    """The remembered refusal of this temperature on `model`, or None."""
    if temperature is None:
        return None
    return provider_refusal(model, PARAM_TEMPERATURE, _temperature_text(temperature))


def refused_temperature_note(model: str, temperature: float | None) -> str | None:
    """The run-record sentence for a temperature this provider refuses, or None.

    Same one-wording-for-two-moments rule as :func:`refused_reasoning_note`:
    the first call paid for the refusal, every later call withholds the value
    because of it, and the second kind of call is the one that would otherwise
    be silent.
    """
    refusal = refused_temperature(model, temperature)
    if refusal is None:
        return None
    return (
        f"temperature {refusal.value} was dropped — the provider refuses it for "
        f"this model: {refusal.reason}"
    )


class ReasoningValueRefused(Exception):
    """A reasoning value the provider will not take, said in the provider's words.

    Raised instead of letting the original 400 travel, because that 400
    classifies as `generation_failed` and reaches a run record as "the provider
    call failed" — true, useless, and the least informative sentence available
    about a failure whose cause the provider spelled out. `str()` of this is
    that spelled-out sentence, and `errors.classify` keeps it verbatim: an
    UNRECOGNISED failure is reported as `str(exc)` precisely so a failure we
    cannot name does not get dressed up as one we can.

    It should be rare to see: `LLMClient.generate` retries without the value
    first, and this is what remains when even that is refused.
    """


@lru_cache(maxsize=256)
def model_capabilities(model: str) -> ModelCapabilities:
    """What LiteLLM knows about `model`. Never raises, never guesses."""
    name = (model or "").strip()
    if not name:
        return ModelCapabilities(model=model or "", known=False)

    info = _model_info(name)
    if info is None:
        # Unknown to the table, but not necessarily unknown to us: what the
        # provider refused is measured from calls, and a self-hosted model's
        # refusals are the one capability fact this build holds about it.
        return ModelCapabilities(
            model=name, known=False, provider_refusals=provider_refusals(name),
        )

    params = _supported_params(name)
    kind: str | None = None
    offered: tuple[str, ...] | None = None
    refused: tuple[str, ...] | None = None
    if _EFFORT_PARAM in params:
        kind = REASONING_EFFORT
        probed = _effort_vocabulary(name)
        # The probe is what the router will translate; `_PROVIDER_REFUSED` is
        # what the model answered 400 to. The second outranks the first — a
        # model is the authority on its own vocabulary and a router only ever
        # had an opinion — so anything measured as refused comes straight back
        # out of what this build offers. The two tuples partition the probe.
        refused = tuple(
            v for v in probed if provider_refusal(name, PARAM_REASONING, v)
        ) or None
        offered = tuple(
            v for v in probed if not provider_refusal(name, PARAM_REASONING, v)
        ) or None
    elif _BUDGET_PARAM in params:
        kind = REASONING_BUDGET

    ceiling = info.get("max_output_tokens") or info.get("max_tokens")
    return ModelCapabilities(
        model=name,
        known=True,
        max_output_tokens=int(ceiling) if ceiling else None,
        # LiteLLM leaves the flag absent rather than False for a model that
        # does not reason, so `bool()` — not the raw value — is the answer to
        # a bool-or-null field.
        supports_reasoning=bool(info.get("supports_reasoning")) or kind is not None,
        reasoning_kind=kind,
        reasoning_values_router_accepts=offered,
        reasoning_values_provider_refused=refused,
        supports_function_calling=bool(info.get("supports_function_calling")),
        source="litellm",
        provider_refusals=provider_refusals(name),
    )


# ─── Clamping — a configuration mistake must not surface as inference ──

#: (model, requested, ceiling) triples already reported. A clamp is a standing
#: configuration mistake, not an event: without this the same line lands on
#: every agent of every review, and a WARNING nobody can finish reading is a
#: WARNING nobody reads.
_CLAMPS_LOGGED: set[tuple[str, int, int]] = set()
_DROPPED_LOGGED: set[tuple[str, str]] = set()


def clamp_output_tokens(
    model: str, requested: int | None,
) -> tuple[int | None, int | None]:
    """Fit `requested` inside what `model` accepts.

    Returns ``(effective, ceiling_applied)``; `ceiling_applied` is None when
    nothing was clamped, and is the model's own ceiling when something was.

    A request above the model's ceiling is a 400 from the provider — a
    configuration mistake that surfaces hours later as an inference failure,
    with the number that caused it nowhere in the message. Clamping turns it
    into a shorter answer plus one line naming both numbers.

    An UNKNOWN model is returned untouched. There is nothing to clamp to, and
    a made-up ceiling would truncate a call that would have worked.
    """
    if requested is None:
        return None, None
    caps = model_capabilities(model)
    ceiling = caps.max_output_tokens
    if not caps.known or not ceiling or int(requested) <= ceiling:
        return requested, None
    key = (model, int(requested), int(ceiling))
    if key not in _CLAMPS_LOGGED:
        _CLAMPS_LOGGED.add(key)
        logger.warning(
            "max_output_tokens_clamped model=%s configured=%d model_ceiling=%d "
            "— the configured value would have been a 400 from the provider",
            model, int(requested), int(ceiling),
        )
    return ceiling, ceiling


# ─── Reasoning — translated to whatever the installed LiteLLM accepts ──


def _log_dropped(model: str, why: str) -> None:
    key = (model, why)
    if key in _DROPPED_LOGGED:
        return
    _DROPPED_LOGGED.add(key)
    logger.warning("reasoning_not_sent model=%s reason=%s", model, why)


def reasoning_kwargs(model: str, reasoning: str | int | None) -> dict:
    """Translate one configured reasoning value into LiteLLM kwargs.

    Returns ``{}`` — the parameter simply absent — whenever the model cannot
    take it. That is the point: `Settings.gemini_thinking_budget` was wired
    only into the native `gemini_client.py`, so for every LiteLLM call the
    setting existed in the UI and reached nothing. Threading it through
    naively would trade that silence for a 400, because `gpt-4o` answers
    `UnsupportedParamsError` to a `reasoning_effort` it never advertised.

    The dispatch is on the MODEL's kind, not on the Python type of the value:
    a token budget cannot be honestly translated into an effort word, and on
    Gemini 3 the installed LiteLLM drops `budget_tokens` silently — sending it
    anyway would be a third way to have a setting that reaches nothing.
    """
    if reasoning is None:
        return {}
    if isinstance(reasoning, bool):
        # `bool` is an `int` in Python, and True would otherwise be sent as a
        # one-token thinking budget.
        _log_dropped(model, "value is a bool, not an effort word or a budget")
        return {}

    value: str | int = reasoning
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        # A UI that renders a number input still posts a string.
        value = int(value) if value.lstrip("-").isdigit() else value.lower()

    name = (model or "").strip()
    caps = model_capabilities(model)
    params = _supported_params(model)
    kind = caps.reasoning_kind

    if kind is None:
        _log_dropped(
            model,
            "unknown to litellm" if not caps.known else "model takes no reasoning parameter",
        )
        return {}

    if kind == REASONING_EFFORT:
        if not isinstance(value, str):
            _log_dropped(model, "model takes an effort level, not a token budget")
            return {}
        if _EFFORT_PARAM not in params:  # pragma: no cover — kind implies the param
            return {}
        if provider_refusal(name, PARAM_REASONING, value):
            # Already absent from the vocabulary below — checked separately so
            # the log names the provider's 400 as the cause instead of the
            # vaguer "not one of […]", which reads like a typo in the setting.
            _log_dropped(model, f"the provider refused effort {value!r} for this model")
            return {}
        vocabulary = caps.reasoning_values_router_accepts
        if vocabulary and value not in vocabulary:
            _log_dropped(model, f"effort {value!r} is not one of {list(vocabulary)}")
            return {}
        return {_EFFORT_PARAM: value}

    # kind == REASONING_BUDGET — the model takes a token budget and nothing else.
    if not isinstance(value, int):
        _log_dropped(model, "model takes a token budget, not an effort level")
        return {}
    if _BUDGET_PARAM not in params:  # pragma: no cover — kind implies the param
        return {}
    return {_BUDGET_PARAM: {"type": "enabled", "budget_tokens": int(value)}}


def reset_capability_caches() -> None:
    """Drop every memoised answer AND the "already said that" log sets.

    Tests need it (a clamp warning is emitted once per process, so a second
    test asserting the same warning would see nothing), and so does anything
    that swaps the installed LiteLLM underneath a running process.
    """
    model_capabilities.cache_clear()
    _supported_params.cache_clear()
    _effort_vocabulary.cache_clear()
    _CLAMPS_LOGGED.clear()
    _DROPPED_LOGGED.clear()
    # Measured, not probed — but a test that watches a refusal narrow the
    # vocabulary must not narrow it for the next test in the same process.
    _PROVIDER_REFUSED.clear()


__all__ = [
    "ADJUST_CLAMPED",
    "ADJUST_DROPPED",
    "ADJUST_SWAPPED",
    "PARAM_MAX_OUTPUT_TOKENS",
    "PARAM_MODEL",
    "PARAM_REASONING",
    "PARAM_TEMPERATURE",
    "REASONING_BUDGET",
    "REASONING_EFFORT",
    "ModelCapabilities",
    "ParameterAdjustment",
    "ProviderRefusal",
    "ReasoningValueRefused",
    "adjustment_as_dict",
    "clamp_output_tokens",
    "forget_reasoning_refusal",
    "forget_temperature_refusal",
    "model_capabilities",
    "provider_of",
    "provider_refusal",
    "provider_refusals",
    "reasoning_kwargs",
    "reasoning_refusal",
    "record_reasoning_refusal",
    "record_temperature_refusal",
    "refused_reasoning",
    "refused_reasoning_note",
    "refused_temperature",
    "refused_temperature_note",
    "reset_capability_caches",
    "resolve_litellm_model",
    "temperature_refusal",
]
