"""Provider-agnostic LLM client (Stage 11).

Wraps LiteLLM's ``completion()`` inside the existing redaction + audit envelope
so agents can call multiple providers (Anthropic, OpenAI, Google, OpenRouter,
Groq, …) with the same interface AND without silently bypassing our security
layer.

Design invariant (see the previous PR-discussion for the full argument):

    Abstract the TRANSPORT (litellm), not the envelope.
    Redaction and audit-logging stay in this file — replacing
    ``get_gemini_client().generate()`` with ``litellm.completion()`` directly
    would drop both, and no test would catch it.

Key resolution + model selection are injected as callables so this client
never touches user_id / policy row internals.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.llm.capabilities import ParameterAdjustment, resolve_litellm_model
from src.llm.capabilities import provider_of as _provider_of
from src.security.audit import AuditLogger, get_audit_logger
from src.security.redactor import redact

# `_provider_of` moved to src/llm/capabilities.py, where the capability lookups
# live, so the settings page and this call path answer "which vendor, which
# litellm string" out of ONE function instead of two that agreed until somebody
# picked a model from another vendor. Re-exported under the old private name:
# it is what this module's comments and tests/llm/test_client.py call it.

logger = logging.getLogger(__name__)


# ─── Result type ─────────────────────────────────────────────────────


@dataclass
class LLMResult:
    """Provider-agnostic result. Superset of the legacy ``GenerationResult``.

    ``cost_usd`` is None when the model is not in the pricing tables — callers
    must tolerate that and record ``cost_source="unknown"``.
    """

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    finish_reason: str
    cost_usd: float | None
    cost_source: str        # "openrouter_actual" | "litellm_estimate" | "unknown"
    provider: str           # extracted from model prefix ("anthropic/..." → "anthropic")
    cached_input_tokens: int = 0  # provider-native prompt cache reads (Anthropic)
    #: The model's own output ceiling, when the requested one was above it and
    #: the call was cut down. None when nothing was clamped. Carried on the
    #: result so a caller can put it on its run record: a configured value the
    #: provider would have rejected is a configuration mistake, and the only
    #: other way to learn about it is a 400 hours later that never names it.
    max_output_tokens_clamped_to: int | None = None
    #: Set when the configured reasoning level did not go out because the
    #: provider refuses it for this model. The sentence names the level and
    #: quotes the provider's own words, and it is here for the same reason as
    #: the field above: a review that quietly ran without the thinking level
    #: somebody configured looks exactly like one that ran with it. On the
    #: first call this is the refusal we just paid for; on every later call it
    #: is the remembered one, because a setting silently ignored from the
    #: second run onward is the bug, not the first run.
    reasoning_dropped: str | None = None
    #: Set when the temperature did not go out because the provider refuses
    #: it for this model — claude-sonnet-5 takes only its own default and
    #: answers 400 to 0.1. The third self-heal, and until it was put here it
    #: lived ONLY in the audit record's extra and a log line: the result the
    #: caller actually held said nothing, so no run record could either.
    temperature_dropped: str | None = None
    #: Every one of the three adjustments above as ONE shape — what was asked,
    #: what was sent, why — so a caller merges a list instead of reading three
    #: fields that each spell the same kind of fact differently. The fields
    #: stay for the readers that already have them; this is what travels.
    parameter_adjustments: list[ParameterAdjustment] = field(default_factory=list)


# ─── Type aliases for injected callables ─────────────────────────────

# resolve_key(provider) → API key string. Raises LLMCredentialError.
KeyResolver = Callable[[str], str]

# resolve_model(agent_name) → provider-qualified model string like
# "anthropic/claude-sonnet-5" or "gemini/gemini-3-pro-preview".
# Agent gets its own model override (from policy or workspace default).
ModelResolver = Callable[[str], str]

# on_delta(text_so_far) → False to stop consuming the stream.
#
# It receives the whole answer SO FAR rather than the newest fragment: every
# consumer of a partial answer has to reassemble it anyway, and doing that once
# here is one buffer instead of one per caller. Returning False closes the
# stream — that is how a person pressing Stop interrupts the model instead of
# waiting for it to finish talking.
DeltaSink = Callable[[str], "bool | None"]


class _StreamUnavailable(Exception):
    """The provider or gateway would not stream this call.

    Not an error the caller sees: streaming is a latency improvement, so this
    is caught inside `generate()` and the ordinary non-streaming call is made
    instead. A deployment that cannot stream must still answer.
    """


# ─── Client ──────────────────────────────────────────────────────────


class LLMClient:
    """One instance per review invocation. Immutable-ish — the resolvers
    close over the user context (user_id, repo_slug, active policy)."""

    def __init__(
        self,
        *,
        resolve_key: KeyResolver,
        resolve_model: ModelResolver | None = None,
        surface: str | None = None,
        audit: AuditLogger | None = None,
        workspace_id: str = "default",
        resolve_api_base: Callable[[str], str | None] | None = None,
        resolve_billing_model: Callable[[str], str] | None = None,
        user_id: str | None = None,
    ) -> None:
        self._resolve_key = resolve_key
        self._resolve_model = resolve_model  # optional — callers can pass model directly
        self._audit = audit or get_audit_logger()
        self._workspace_id = workspace_id or "default"
        # Which product surface this client serves. It reaches the spend ledger,
        # so a vault build is billed to "vault" rather than to "review".
        self._surface = surface
        # Injected like the other two, so this file still never reads workspace
        # internals — see the design invariant at the top.
        self._resolve_api_base = resolve_api_base
        # The model NAME for the ledger, which is not the name we call. Through
        # the gateway every request goes to a per-workspace deployment, so the
        # ledger recorded `litellm_proxy/celmis-<uuid>-chat` — and "usage by
        # model", the whole point of that breakdown, became a list of opaque
        # deployment names identical across every model the workspace ever
        # used. This maps back to what actually ran.
        self._resolve_billing_model = resolve_billing_model
        # Who asked. It was bound into the key resolver's closure and never
        # reached the ledger, so "by user" on the Usage page attributed every
        # call made through this client to nobody — 95% of the workspace's
        # spend in one row labelled "—".
        self._user_id = user_id

    def _capability_model(self, resolved: str) -> str:
        """The name to ask LiteLLM about the ceiling and the reasoning shape.

        Usually the model we are about to call. Not on the gateway: there,
        every request goes to a per-workspace deployment named
        `litellm_proxy/celmis-<uuid>-review`, which LiteLLM has no entry for —
        so a workspace behind the proxy would report "unknown model", never
        clamp, and hand a 200 000-token ceiling to a Gemini that stops at
        65 535. `_resolve_billing_model` already maps a deployment back to
        what actually runs behind it, for exactly the same reason on the
        Usage page. Reused here rather than mapped a second way.

        The logical name is taken only when LiteLLM actually knows it —
        otherwise we are back to the deployment name, which is honest about
        being unknown.
        """
        if not self._resolve_billing_model:
            return resolved
        try:
            logical = self._resolve_billing_model(resolved)
        except Exception:  # noqa: BLE001 — a ceiling lookup must not fail a call
            return resolved
        if not logical or logical == resolved:
            return resolved
        from src.llm.capabilities import model_capabilities
        return logical if model_capabilities(logical).known else resolved

    def _openrouter_fallback_available(self) -> bool:
        """True if the workspace enabled the OpenRouter fallback toggle AND
        there is a valid key. Falls back to False on any error — the client
        should never crash when checking for a fallback."""
        try:
            from src.api.routers.llm import _load_workspace_config
            if not _load_workspace_config(self._workspace_id).get("openrouter_enabled"):
                return False
            self._resolve_key("openrouter")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── Sync generate (used by review agents in ThreadPoolExecutor) ──

    def generate(
        self,
        *,
        prompt: str,
        model: str | None = None,
        agent: str | None = None,
        system_instruction: str | None = None,
        code_context: str | None = None,
        mode: str = "review",
        operation: str,
        repo: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: str | int | None = None,
        num_retries: int = 3,
        timeout: float = 120,
        on_delta: DeltaSink | None = None,
    ) -> LLMResult:
        from src.ops.telemetry import record_llm_call
        record_llm_call()
        """Provider-agnostic generation.

        Model resolution order:
            1. `model` arg (explicit override)
            2. `resolve_model(agent)` (per-agent from policy / defaults)
            3. RuntimeError if neither given

        Redaction runs on `code_context` before the prompt is assembled.
        Audit-log records tokens, redaction stats, model, cost.

        `reasoning` is one value — an effort word or a token budget — that the
        caller configured for this agent. It is translated to whatever the
        INSTALLED LiteLLM accepts for the resolved model, and dropped entirely
        when the model takes no reasoning parameter: `gpt-4o` answers
        `UnsupportedParamsError` to a `reasoning_effort` it never advertised,
        so "send it and hope" turns a configuration choice into a 400.

        LiteLLM accepting the word is not the model accepting it, though, and
        that gap is what the retry below is for: `gemini-3.7-flash` answers 400
        to a "minimal" LiteLLM translates without complaint. When the provider
        refuses the value, the pair is remembered (see `_PROVIDER_REFUSED` in
        `src/llm/capabilities.py`) and the SAME request goes out once more
        without it — a review that ran without the requested thinking level is
        worth far more than no review, and `LLMResult.reasoning_dropped` says
        it happened and why.

        HOW MANY PROVIDER CALLS THAT IS, because retries multiply and this one
        had to be counted against the retry `LLMReviewAgent._generate_and_parse`
        already owns.

            per `generate()`   1, or 2 the one time a reasoning refusal is
                               discovered. Never 3: the retry carries no
                               reasoning parameter, and `reasoning_refusal`
                               returns None when none was sent, so a second
                               refusal cannot start a third call — it raises.
                               A temperature refusal is the same shape with
                               the same ceiling: one retry without the value,
                               remembered so the next call never sends it.
            per review agent   2 before this change (two `generate()` calls,
                               each with num_retries=0), 3 after, and only in
                               one run. Attempt 1 spends 2 calls discovering
                               the refusal; if its reply is then unparseable,
                               attempt 2 spends 1, because by then the pair is
                               remembered and the level is never sent again.
                               Once the process has learned it, an agent is
                               back to 2 — this is a first-encounter cost, not
                               a standing one.

        `num_retries` sits UNDER all of that, inside `litellm.completion`.
        Review agents pass 0 and say why; a caller that leaves the default of 3
        multiplies both numbers by four and should not.

        `max_output_tokens` is clamped to the model's own ceiling when LiteLLM
        knows one, and left alone when it does not — there is nothing to clamp
        an unmapped model to, and a guessed ceiling truncates a working call.

        `on_delta` opts this call into streaming. Passing it changes NOTHING
        about what comes back — same LLMResult, same audit record, same spend
        ledger row — it only means the caller is handed the answer as it is
        written instead of after it is finished. A caller that passes nothing
        takes exactly the path this method has always taken, which is why the
        argument is a callback rather than a second method: one code path owns
        redaction, audit and billing, and there is no second one to forget to
        update.
        """
        resolved_model = model or (self._resolve_model(agent) if (agent and self._resolve_model) else None)
        if not resolved_model:
            raise RuntimeError(
                "LLMClient.generate: no model — pass `model=...` or wire a resolver"
            )

        provider = _provider_of(resolved_model)
        # `_provider_of` and LiteLLM read the same bare "gemini-3.7-flash"
        # differently: this module calls it Gemini and hands over the
        # workspace's API key, LiteLLM calls it Vertex AI and goes looking for
        # Application Default Credentials that a container has no reason to
        # hold. `resolve_litellm_model` — after the provider decision, before
        # the call — is what makes the two agree. It is the same function
        # /settings/llm asks for capabilities, so what the page says a model
        # accepts is what this line is about to send.
        resolved_model = resolve_litellm_model(resolved_model)
        # BYOK: try direct provider key first. If that fails (no key saved)
        # AND the user enabled OpenRouter fallback, reroute the same request
        # through OpenRouter — same model still, just prefixed.
        try:
            api_key = self._resolve_key(provider)
        except Exception:  # noqa: BLE001
            or_available = self._openrouter_fallback_available()
            if not or_available:
                raise
            logger.info(
                "llm_fallback_to_openrouter primary_provider=%s model=%s",
                provider, resolved_model,
            )
            # Prefix with "openrouter/" if not already there — LiteLLM will route
            # through OpenRouter with the model as a passthrough parameter.
            resolved_model = (
                resolved_model if resolved_model.startswith("openrouter/")
                else f"openrouter/{resolved_model}"
            )
            provider = "openrouter"
            api_key = self._resolve_key(provider)

        # ── Fit the request to the model, now that we know which model ──
        #
        # Both of these are here rather than at the caller on purpose. The
        # caller owns the NUMBER (the review agent even doubles it on its
        # corrective retry); this is the only place that knows what the number
        # will actually be sent to, after the gemini prefixing and the
        # OpenRouter fallback above have both had their say. A ceiling
        # computed in two places is a ceiling the two places stop agreeing on.
        from src.llm.capabilities import (
            ADJUST_CLAMPED,
            ADJUST_DROPPED,
            PARAM_MAX_OUTPUT_TOKENS,
            PARAM_REASONING,
            PARAM_TEMPERATURE,
            ReasoningValueRefused,
            clamp_output_tokens,
            forget_reasoning_refusal,
            forget_temperature_refusal,
            reasoning_kwargs,
            reasoning_refusal,
            record_reasoning_refusal,
            record_temperature_refusal,
            refused_reasoning,
            refused_reasoning_note,
            refused_temperature,
            refused_temperature_note,
            temperature_refusal,
        )

        capability_model = self._capability_model(resolved_model)
        # Everything this call changed between what was asked and what went
        # out, in the one shape the run record and the PR comment read. Each
        # self-heal below appends to it at the moment it happens; nothing
        # downstream re-derives an entry from the scalar fields.
        adjustments: list[ParameterAdjustment] = []
        requested_output_tokens = max_output_tokens
        max_output_tokens, clamped_to = clamp_output_tokens(
            capability_model, max_output_tokens,
        )
        if clamped_to is not None:
            adjustments.append(ParameterAdjustment(
                agent=agent, parameter=PARAM_MAX_OUTPUT_TOKENS,
                requested=requested_output_tokens, sent=clamped_to,
                action=ADJUST_CLAMPED, reason=f"model ceiling is {clamped_to}",
                model=capability_model,
            ))
        reasoning_params = reasoning_kwargs(capability_model, reasoning)
        # Empty kwargs and a configured level means the level was withheld, and
        # `refused_reasoning_note` answers the one question that matters about
        # that: was it withheld because THIS provider already refused it? Only
        # then is there something for a run record to say. Every other reason
        # for an empty dict — a model that does not reason, a word this router
        # never had — is a configuration answer the settings page gives at save
        # time, not news from a review that has just finished.
        reasoning_dropped = (
            refused_reasoning_note(capability_model, reasoning)
            if reasoning is not None and not reasoning_params else None
        )
        if reasoning_dropped:
            remembered = refused_reasoning(capability_model, reasoning)
            adjustments.append(ParameterAdjustment(
                agent=agent, parameter=PARAM_REASONING,
                requested=reasoning, sent=None, action=ADJUST_DROPPED,
                reason=remembered.reason if remembered else reasoning_dropped,
                model=capability_model,
            ))
        # Temperature, the same way: once this process has seen the model
        # refuse the value, the value is withheld up front rather than paid
        # for again — and SAID, on every call, because a call that silently
        # sends nothing is the run after the first one, where a dropped
        # setting hides.
        temperature_dropped = refused_temperature_note(capability_model, temperature)
        if temperature_dropped:
            remembered = refused_temperature(capability_model, temperature)
            adjustments.append(ParameterAdjustment(
                agent=agent, parameter=PARAM_TEMPERATURE,
                requested=temperature, sent=None, action=ADJUST_DROPPED,
                reason=remembered.reason if remembered else temperature_dropped,
                model=capability_model,
            ))
        # What the provider is actually handed. None once withheld, so that
        # `temperature_refusal` — whose first condition is "we sent one" —
        # cannot read a later, unrelated 400 as a refusal of a value that
        # never went out.
        sent_temperature = None if temperature_dropped else temperature

        redacted_code, red_stats = redact(code_context or "", source_hint=operation)
        messages = _build_messages(
            prompt=prompt,
            system_instruction=system_instruction,
            redacted_code=redacted_code,
        )

        with self._audit.track(
            mode=mode,
            model=resolved_model,
            operation=operation,
            # Same tenant the spend ledger is billed to a few lines below —
            # one source, so the audit trail and the invoice can never
            # disagree about whose call this was.
            workspace_id=self._workspace_id,
            repo=repo,
            extra={
                "redaction": red_stats.as_dict(),
                "provider": provider,
                "agent": agent,
                # What this call was actually allowed to spend, and the
                # ceiling that cut it down if one did. On the audit record
                # because "the architect keeps truncating" and "the architect
                # is configured above what the model accepts" look identical
                # from the outside, and this is the line that tells them apart.
                "max_output_tokens": max_output_tokens,
                "max_output_tokens_clamped_to": clamped_to,
                "reasoning": reasoning_params or None,
                # Null on the calls that sent what was asked for. Non-null is
                # the run record's only chance to say the level went missing —
                # see `LLMResult.reasoning_dropped`.
                "reasoning_dropped": reasoning_dropped,
                # Same contract for the temperature — see
                # `LLMResult.temperature_dropped`. Overwritten below the one
                # time the refusal is discovered on this very call.
                "temperature_dropped": temperature_dropped,
            },
        ) as record:
            import litellm

            call_kwargs: dict[str, Any] = dict(reasoning_params)
            # A gateway deployment needs the proxy's address. LiteLLM does NOT
            # read LITELLM_PROXY_API_BASE here — without this it posts to
            # api.openai.com and the call dies as an auth error that names the
            # wrong vendor.
            base = self._resolve_api_base(provider) if self._resolve_api_base else None
            if base:
                call_kwargs["api_base"] = base

            def _attempt(kwargs: dict[str, Any],
                         drop_temperature: bool = False) -> tuple[str, str, Any]:
                """One provider call, streamed when the caller asked for it.

                Extracted so the reasoning retry below re-runs exactly this
                minus the refused parameter, rather than a second copy of the
                call that would drift from this one.
                """
                # None both when the caller set none and when it was
                # withheld — up front from the memory, or by the retry below.
                temp = None if drop_temperature else sent_temperature
                streamed: tuple[str, str, Any] | None = None
                if on_delta is not None:
                    try:
                        streamed = self._stream(
                            model=resolved_model,
                            api_key=api_key,
                            messages=messages,
                            temperature=temp,
                            max_output_tokens=max_output_tokens,
                            timeout=timeout,
                            call_kwargs=kwargs,
                            on_delta=on_delta,
                        )
                    except _StreamUnavailable as exc:
                        # A provider, or a gateway deployment, that refuses
                        # `stream=True` still has to answer. Streaming is how
                        # fast the first word arrives, not whether there is one.
                        logger.info(
                            "llm_stream_unavailable model=%s falling_back err=%s",
                            resolved_model, exc,
                        )
                if streamed is not None:
                    # Deliberately a response-SHAPED object, not a second copy
                    # of the bookkeeping below: usage, cost, audit and the
                    # ledger read the same fields whichever way it arrived.
                    return streamed
                reply = litellm.completion(
                    model=resolved_model,
                    api_key=api_key,
                    messages=messages,
                    # Знімається повністю, а не виставляється в 1: модель, яка
                    # приймає лише свій дефолт, відмовляє і на явному значенні,
                    # що дорівнює дефолту. Так само й тоді, коли значення
                    # притримано наперед із пам'яті — інакше наступний виклик
                    # послав би `temperature=None`, а не нічого.
                    **({} if temp is None else {"temperature": temp}),
                    max_tokens=max_output_tokens,
                    num_retries=num_retries,
                    # Generous by default — an architect call with a big context
                    # takes 30-60s on Sonnet. But the default is a CEILING, and it
                    # multiplies: with num_retries=3 a caller inherits four of
                    # these, so a surface with a person watching a spinner must
                    # pass its own. See the automation planner.
                    timeout=timeout,
                    **kwargs,
                )
                choice = reply.choices[0] if reply.choices else None
                if choice is None:
                    return "", "", reply
                body = (
                    (choice.message.content or "") if hasattr(choice, "message") else ""
                )
                return body, str(getattr(choice, "finish_reason", "") or ""), reply

            try:
                text, finish_reason, response = _attempt(call_kwargs)
            except Exception as exc:  # noqa: BLE001 — re-raised unless it is THE one
                # Температура — та сама історія, що й reasoning, з однією
                # відмінністю: get_supported_openai_params відповідає TRUE, бо
                # параметр підтримується — відмовляє ЗНАЧЕННЯ. claude-sonnet-5
                # приймає лише temperature=1 і 400-ить на 0.1, тож проба
                # можливостей цього не бачить, а бачить лише сам виклик.
                # Рев'ю без заданої температури варте набагато більше за
                # відсутнє рев'ю, і це та сама угода, що вже укладена вище.
                temp_sentence = temperature_refusal(exc, sent_temperature)
                if temp_sentence is not None:
                    # Remember the pair first, same as the reasoning arm below
                    # and for the same reason: the next call in this process
                    # then withholds the value up front instead of paying a
                    # 400 to re-learn it, and the settings page can date the
                    # fact. Then buy the answer we came for.
                    record_temperature_refusal(capability_model, temperature, temp_sentence)
                    temperature_dropped = (
                        refused_temperature_note(capability_model, temperature)
                        or f"temperature {temperature} was dropped — {temp_sentence}"
                    )
                    record.extra["temperature_dropped"] = temperature_dropped
                    adjustments.append(ParameterAdjustment(
                        agent=agent, parameter=PARAM_TEMPERATURE,
                        requested=temperature, sent=None, action=ADJUST_DROPPED,
                        reason=temp_sentence, model=capability_model,
                    ))
                    logger.warning(
                        "temperature_dropped_retrying model=%s agent=%s temp=%s %s",
                        resolved_model, agent, temperature, temp_sentence,
                    )
                    try:
                        text, finish_reason, response = _attempt(
                            call_kwargs, drop_temperature=True,
                        )
                    except Exception as second:  # noqa: BLE001
                        if temperature_refusal(second, temperature) is None:
                            raise
                        # Refused for temperature again on a request that
                        # carried NONE — the retry just disproved the fact
                        # recorded a moment ago, so withdraw it before it
                        # makes every later call in this process withhold a
                        # value the provider never objected to. Same
                        # controlled experiment as the reasoning arm.
                        forget_temperature_refusal(capability_model, temperature)
                        raise
                else:
                    sentence = reasoning_refusal(exc, reasoning_params)
                    if sentence is None:
                        raise
                    # The provider has spoken and it outranks the router. Remember
                    # the pair first, so /settings/llm stops offering the word and
                    # the next call in this process never sends it — then buy the
                    # answer we came for. A review that ran without the requested
                    # thinking level is worth a great deal more than no review, and
                    # a thinking level is a preference, not a requirement.
                    record_reasoning_refusal(capability_model, reasoning_params, sentence)
                    reasoning_dropped = (
                        refused_reasoning_note(capability_model, reasoning)
                        or f"reasoning was dropped — {sentence}"
                    )
                    record.extra["reasoning_dropped"] = reasoning_dropped
                    adjustments.append(ParameterAdjustment(
                        agent=agent, parameter=PARAM_REASONING,
                        requested=reasoning, sent=None, action=ADJUST_DROPPED,
                        reason=sentence, model=capability_model,
                    ))
                    logger.warning(
                        "reasoning_dropped_retrying model=%s agent=%s %s",
                        resolved_model, agent, reasoning_dropped,
                    )
                    retry_kwargs = {
                        k: v for k, v in call_kwargs.items() if k not in reasoning_params
                    }
                    try:
                        text, finish_reason, response = _attempt(retry_kwargs)
                    except Exception as second:  # noqa: BLE001
                        if reasoning_refusal(second, reasoning_params) is None:
                            # A different failure on the second call is that
                            # failure's to report, not this one's.
                            raise
                        # Refused for reasoning again on a request that carries
                        # NO reasoning parameter. The retry was a free controlled
                        # experiment and it just disproved the evidence we recorded
                        # a moment ago, so put the word back before anything else —
                        # a value struck off every workspace's dropdown on a
                        # mismatch is the one failure mode `reasoning_refusal` calls
                        # dangerous. Then stop, rather than loop.
                        forget_reasoning_refusal(capability_model, reasoning_params)
                        # What travels is still the provider's own sentence: this
                        # caller is the one paying for the discovery, and
                        # `errors.classify` reports an UNRECOGNISED failure as
                        # `str(exc)` verbatim, so it is the clearest thing anyone
                        # downstream will be handed about a 400 nobody has decoded.
                        raise ReasoningValueRefused(sentence) from second

            input_tokens, output_tokens, cached_input_tokens = _usage_numbers(
                getattr(response, "usage", None)
            )

            from src.llm.pricing import extract_actual_cost_usd
            cost_usd, cost_source = extract_actual_cost_usd(response)

            record.input_tokens_estimated = input_tokens
            record.output_tokens_estimated = output_tokens
            record.response_hash = self._audit.hash_response(text)
            record.redaction = red_stats.as_dict()
            # The one list, on the audit record too — so the trail that
            # already says "max_output_tokens_clamped_to" and "reasoning
            # dropped" in three different keys also says it once, the way
            # the run row will.
            record.extra["parameter_adjustments"] = [a.as_dict() for a in adjustments]

            # Stage 23 — spend ledger (powers the admin Usage view + budgets).
            #
            # `local_call`: a direct call that had to be handed its address
            # went to a self-hosted server — `build_llm_client._base` is the
            # only source of an api_base outside the gateway ("litellm_proxy/…"
            # models). No invoice exists for tokens on the operator's own
            # hardware, so NO row is written rather than a $0.00 one; the
            # accounting rule and its WHY live at completion._seam_embed.
            local_call = bool(base) and provider != "litellm_proxy"
            try:
                from src.llm.budget import SURFACE_REVIEW, record_spend
                if not local_call:
                    record_spend(
                        workspace_id=self._workspace_id,
                        # The surface this client was built for, not a constant.
                        # Review was the only caller when this was written; then
                        # documentation and Q&A moved onto the same client and
                        # their spend started arriving labelled "review" — which
                        # is worse than unlabelled, because a budget set on review
                        # would then throttle a vault build.
                        surface=getattr(self, "_surface", None) or SURFACE_REVIEW,
                        agent=agent,
                        model=(self._resolve_billing_model(resolved_model)
                               if self._resolve_billing_model else resolved_model),
                        provider=provider,
                        cost_usd=cost_usd or 0.0,
                        cost_source=cost_source or "unknown",
                        tokens_in=input_tokens,
                        tokens_out=output_tokens,
                        cached_tokens_in=cached_input_tokens,
                        repo_slug=repo,
                        user_id=getattr(self, "_user_id", None),
                        # Already a parameter of this call; it just never reached
                        # the ledger, so "which job cost that" was unanswerable
                        # for every surface with more than one kind of job.
                        operation=operation,
                    )
            except Exception:  # noqa: BLE001 — ledger must never break a review
                pass

            return LLMResult(
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=resolved_model,
                finish_reason=finish_reason,
                cost_usd=cost_usd,
                cost_source=cost_source,
                provider=provider,
                cached_input_tokens=cached_input_tokens,
                max_output_tokens_clamped_to=clamped_to,
                reasoning_dropped=reasoning_dropped,
                temperature_dropped=temperature_dropped,
                parameter_adjustments=adjustments,
            )

    # ── Streaming ────────────────────────────────────────────────────

    def _stream(
        self,
        *,
        model: str,
        api_key: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_output_tokens: int | None,
        timeout: float,
        call_kwargs: dict[str, Any],
        on_delta: DeltaSink,
    ) -> tuple[str, str, Any]:
        """One streaming call. Returns (text, finish_reason, response).

        The third element is reassembled into the same shape a non-streaming
        response has, so everything after this — usage, cost, audit, the spend
        ledger — is the code that was already there rather than a parallel
        implementation that drifts.

        Raises `_StreamUnavailable` when the stream never produced anything,
        which the caller answers by making the ordinary call.
        """
        import litellm

        def _open(with_usage: bool):
            kwargs = dict(call_kwargs)
            if with_usage:
                # Without this the final chunk carries no usage and every
                # streamed call would book zero tokens and zero dollars — the
                # spend ledger would keep working in the sense that it kept
                # writing rows, which is the worst way for it to break.
                kwargs["stream_options"] = {"include_usage": True}
            return litellm.completion(
                model=model,
                api_key=api_key,
                messages=messages,
                temperature=temperature,
                max_tokens=max_output_tokens,
                stream=True,
                # Zero, and not the caller's budget, on purpose: the retry for
                # a stream that failed is the non-streaming call the caller
                # falls back to, which is more likely to work and carries the
                # caller's own num_retries. Two ladders would multiply into
                # the minutes-long wait the short timeout exists to prevent.
                num_retries=0,
                timeout=timeout,
                **kwargs,
            )

        try:
            stream = _open(True)
        except Exception as exc:  # noqa: BLE001
            if "stream_option" in str(exc).lower():
                # Some gateways accept `stream=True` and reject the usage
                # option. Losing token counts is not acceptable, losing
                # streaming for those deployments is — but try once without it
                # before giving the whole thing up.
                try:
                    stream = _open(False)
                except Exception as exc2:  # noqa: BLE001
                    raise _StreamUnavailable(str(exc2)) from exc2
            else:
                raise _StreamUnavailable(str(exc)) from exc

        chunks: list[Any] = []
        parts: list[str] = []
        finish_reason = ""
        usage: Any = None
        # A stream that stops arriving mid-answer holds the connection open
        # with no error to raise. The request timeout does not cover the gap
        # BETWEEN chunks, so the deadline is enforced here too.
        deadline = time.monotonic() + float(timeout or 120)
        try:
            for chunk in stream:
                chunks.append(chunk)
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                delta, reason = _chunk_delta(chunk)
                if reason:
                    finish_reason = reason
                if delta:
                    parts.append(delta)
                    # Between chunks — which is what makes Stop interrupt the
                    # model rather than wait for it.
                    if on_delta("".join(parts)) is False:
                        finish_reason = "cancelled"
                        break
                if time.monotonic() > deadline:
                    finish_reason = finish_reason or "timeout"
                    break
        except Exception as exc:  # noqa: BLE001
            if not parts:
                raise _StreamUnavailable(str(exc)) from exc
            # Half an answer is not an answer. Re-raised rather than salvaged:
            # the caller must not bill and cache a truncated plan as if the
            # model had finished it.
            raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                # Best-effort: we are already unwinding, and a provider that
                # fails to close must not replace the real exception.
                with contextlib.suppress(Exception):
                    close()

        text = "".join(parts)
        return text, finish_reason, _rebuild_response(
            chunks, messages=messages, usage=usage, model=model, text=text,
        )


# ─── Helpers ─────────────────────────────────────────────────────────


def _usage_numbers(usage: Any) -> tuple[int, int, int]:
    """(input, output, cached input) out of a usage object.

    `prompt_tokens_details` is a pydantic wrapper on some providers and a
    plain dict on others — LiteLLM returns whichever the upstream gave.
    Calling `.get` on the wrapper raised AttributeError AFTER the model had
    already answered, so every agent call through the gateway died on its own
    bookkeeping with the response in hand.
    """
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    if isinstance(details, dict):
        detail_cached = details.get("cached_tokens", 0)
    else:
        detail_cached = getattr(details, "cached_tokens", 0) or 0
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or detail_cached or 0)
    return input_tokens, output_tokens, cached


def _chunk_delta(chunk: Any) -> tuple[str, str]:
    """(new text, finish reason) out of one streaming chunk, tolerantly.

    The chunk carrying usage often has no choices at all, and a chunk that
    only opens a tool call has a delta whose content is None. Neither is an
    error, so neither may raise.
    """
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return "", ""
    first = choices[0]
    delta = getattr(first, "delta", None)
    text = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
    return str(text or ""), str(getattr(first, "finish_reason", "") or "")


def _rebuild_response(
    chunks: list[Any], *, messages: list[dict[str, Any]], usage: Any,
    model: str, text: str,
) -> Any:
    """Put the chunks back together into something response-shaped.

    LiteLLM's own `stream_chunk_builder` is the right tool — it fills in usage
    for providers that only report it at the end, and it produces a real
    ModelResponse, which is what `completion_cost()` wants. When it cannot
    (an unusual chunk shape, an older LiteLLM), the fallback carries the usage
    we collected ourselves, because losing the token counts loses the spend
    row and that is not a cosmetic loss.
    """
    rebuilt = None
    try:
        import litellm

        rebuilt = litellm.stream_chunk_builder(chunks, messages=messages)
    except Exception as exc:  # noqa: BLE001
        logger.debug("stream_chunk_builder_failed err=%s", exc)

    if rebuilt is not None:
        if usage is not None and not getattr(
                getattr(rebuilt, "usage", None), "completion_tokens", 0):
            # The usage chunk is the authority when the rebuild came back with
            # nothing in it. Some ModelResponse variants refuse the assignment;
            # then the rebuilt usage stands and we lose nothing we had before.
            with contextlib.suppress(Exception):
                rebuilt.usage = usage
        return rebuilt

    from types import SimpleNamespace
    return SimpleNamespace(
        model=model, usage=usage, choices=[SimpleNamespace(
            message=SimpleNamespace(content=text), finish_reason="stop")],
    )


def _build_messages(
    *,
    prompt: str,
    system_instruction: str | None,
    redacted_code: str,
) -> list[dict[str, Any]]:
    """OpenAI-style messages. LiteLLM normalises to each provider's format.

    `cache_control` breakpoints are added on the big blocks (system prompt +
    code context) so Anthropic's prompt cache kicks in when multiple agents
    share the same content. This is the free 75%-input-tokens win.
    """
    messages: list[dict[str, Any]] = []
    if system_instruction:
        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_instruction,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        })

    user_parts: list[dict[str, Any]] = []
    if redacted_code:
        user_parts.append({
            "type": "text",
            "text": f"## Source code (redacted)\n{redacted_code}",
            "cache_control": {"type": "ephemeral"},
        })
    user_parts.append({"type": "text", "text": prompt})

    messages.append({"role": "user", "content": user_parts})
    return messages


# ─── Convenience — build a client for one review invocation ──────────


def build_llm_client(
    user_id: str,
    workspace_id: str = "default",
    *,
    surface: str = "review",
    spend_surface: str | None = None,
    resolve_model: ModelResolver | None = None,
    audit: AuditLogger | None = None,
) -> LLMClient:
    """Standard construction path. Binds `user_id` into the key resolver
    closure so each `.generate()` call goes through the right BYOK path.

    When the workspace is routed through the LiteLLM gateway — which is every
    workspace in production — the agents' bare model names are replaced by the
    gateway deployment, and the key and address come from the same profile.

    Without this the review agents asked for "gemini-3-pro", the key resolver
    went looking for a raw `gemini` credential the workspace does not hold (it
    holds a LiteLLM virtual key), and every LLM agent died with
    `LLMCredentialError` before sending a token. That is what produced
    "PARTIAL REVIEW — architect, security agent(s) failed" with tokens 0/0
    and a six-minute wall clock: the only agent that finished was the one
    running ast-grep, which never calls a model at all.
    """
    from src.llm.keys import LLMCredentialError, resolve_api_key

    def _route():
        try:
            # `_routed`, not `resolve_profile`: a surface added AFTER a
            # workspace was provisioned has no deployment on the proxy, and
            # resolving it plainly returns a direct-key profile. The key
            # resolver then goes looking for a raw `gemini` credential that a
            # gateway tenant does not hold, and the call dies before sending a
            # token — which is exactly what adding the "agent" surface did to
            # every workspace that had been provisioned for three.
            from src.llm.completion import _routed
            return _routed(surface, workspace_id)
        except Exception:  # noqa: BLE001 — a workspace with no route is direct-key
            try:
                from src.llm.profiles import resolve_profile
                return resolve_profile(surface, workspace_id)
            except Exception:  # noqa: BLE001
                return None

    def _model(agent: str) -> str | None:
        p = _route()
        if p is not None and p.via_gateway:
            return p.litellm_model
        m = resolve_model(agent) if resolve_model else None
        # A bare model name on a self-hosted profile must not reach
        # `_provider_of`'s name-shape heuristics — "llama-3.3-70b" reads as
        # groq there, and the call would then go looking for a groq key this
        # workspace never had. That is the one case where the PROFILE's vendor
        # outranks the model id's own, which is why the profile is handed over
        # here and nowhere else.
        return resolve_litellm_model(m or "", p.provider if p is not None else None) or m

    def _key(provider: str) -> str:
        if provider == "litellm_proxy":
            p = _route()
            if p is not None and p.api_key:
                return p.api_key
        elif provider in ("openai", "openai_compatible"):
            # "openai" is what a self-hosted model string ("openai/<model>")
            # extracts to. When this workspace's profile is self-hosted, the
            # profile's key — the stored local token or the "local-no-key"
            # sentinel — is the right credential. `resolve_api_key("openai")`
            # would either fail (no OpenAI account, the normal local case) or,
            # worse, return a REAL OpenAI key and send it to the local server.
            p = _route()
            if p is not None and p.provider == "openai_compatible" and p.api_key:
                return p.api_key
        # "google" and "gemini" name one key. The Connections page stores it
        # as "google"; `_provider_of` derives "gemini" from a bare "gemini-*"
        # model name. Asking for only one of the two is how a workspace with a
        # working, tested key still fails every agent with "no API key is
        # configured" — resolve_provider_key() in profiles.py already tries
        # both, and this path was the one that did not.
        candidates = (
            ("google", "gemini") if provider in ("google", "gemini") else (provider,)
        )
        last: LLMCredentialError | None = None
        for prov in candidates:
            try:
                return resolve_api_key(prov, user_id=user_id, workspace_id=workspace_id)
            except LLMCredentialError as exc:
                last = exc
        raise last  # type: ignore[misc]

    def _base(provider: str) -> str | None:
        if provider == "litellm_proxy":
            p = _route()
            return p.gateway_url if p is not None else None
        if provider in ("openai", "openai_compatible"):
            p = _route()
            if p is not None and p.provider == "openai_compatible":
                if not p.api_base:
                    # Fail closed. "openai/<model>" with no api_base is not a
                    # broken local call — it is a WORKING call to
                    # api.openai.com, carrying this workspace's code to a
                    # vendor the operator explicitly did not choose.
                    raise RuntimeError(
                        "self-hosted LLM profile has no base URL — set it in "
                        "/settings/llm; refusing to default to api.openai.com"
                    )
                return p.api_base
        return None

    def _billing_model(resolved: str) -> str:
        """What to write in the ledger for a call made to `resolved`.

        Identical to `resolved` off the gateway. On it, the deployment name
        says which workspace and surface — and nothing about which model —
        so the breakdown people open Usage for was unreadable.
        """
        if not resolved or not resolved.startswith("litellm_proxy/"):
            return resolved
        p = _route()
        if p is None:
            return resolved
        return p.gateway_underlying or p.model or resolved

    return LLMClient(
        resolve_key=_key,
        resolve_model=_model if resolve_model else None,
        resolve_api_base=_base,
        resolve_billing_model=_billing_model,
        user_id=user_id,
        workspace_id=workspace_id,
        # Defaults to the profile surface, which is right for review and for
        # chat; a caller whose ledger surface differs from its profile surface
        # — documentation runs on the chat profile but bills to "vault" — says
        # so explicitly.
        surface=spend_surface or surface,
        audit=audit,
    )


__all__ = [
    "LLMResult",
    "LLMClient",
    "KeyResolver",
    "ModelResolver",
    "DeltaSink",
    "build_llm_client",
]
