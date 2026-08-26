"""Wrapper over the google-genai SDK — one surface left: the tool loop.

This module used to carry every Gemini call in the system (generation,
streaming, embeddings). Those left one by one for LiteLLM — chat through
`src.llm.completion.stream_chat`, documentation through `build_llm_client`,
and finally embeddings through `completion.embed`/`embed_batch`, retired only
after the outgoing wire request was proven byte-identical (same model, task
types and dimensionality; pinned in
tests/llm/test_embedding_requests_do_not_drift.py).

What stays is the QA exploration agent's native function-calling loop
(`generate_with_tools_turn`): it hands raw `types.Part` objects back so
thought signatures round-trip verbatim into the next turn — Gemini 3.x
answers 400 INVALID_ARGUMENT when they are lost, and LiteLLM's OpenAI shape
does not contract to carry them. Every call here still writes an audit record
and bills the spend ledger itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import tiktoken
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings, get_settings
from src.llm.budget import (
    SURFACE_DEPS,
    SURFACE_EMBEDDINGS,
    SURFACE_OTHER,
    SURFACE_QA,
    SURFACE_REVIEW,
    SURFACE_VAULT,
)
from src.security.audit import AuditLogger, get_audit_logger

# Approximation used when a provider does not return exact token counts
# (kept here for its importers: src.llm.completion and the QA router).
_TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        return len(_TOKEN_ENCODER.encode(text))
    except Exception:  # noqa: BLE001
        return len(text) // 4

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """A single tool-call emitted by the model within a turn."""

    name: str
    args: dict[str, Any]
    id: str | None = None  # id for function_response (Gemini fills it in itself if None)


@dataclass
class ToolTurnResult:
    """Result of a single turn in the tool-loop. Either text (final), or a list
    of tool-calls.

    `model_parts` — the original Part objects from the response (with
    thoughtSignature and so on). The caller has to append them directly into
    `contents` for the next turn — Gemini 3.x requires thoughtSignature to be
    preserved, otherwise it returns 400 INVALID_ARGUMENT.
    """

    text: str | None
    tool_calls: list[ToolCall]
    model_parts: list[Any]  # original types.Part from response — for history
    input_tokens: int
    output_tokens: int
    finish_reason: str
    raw_response: Any  # for passing on into history later


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ─── Spend-ledger surface mapping ────────────────────────────────────
#
# `llm_spend.surface` powers the Usage/Spend breakdown. Native-Gemini callers
# only tell us `operation` (+ a coarse `mode`), so map that to a surface here —
# one place, so a new operation name can't silently invent a new bucket.
#
# Order matters: "answer_overview" contains both "answer" (QA) and "overview"
# (vault-ish), and QA must win.
_SURFACE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("embed",), SURFACE_EMBEDDINGS),
    (("deps",), SURFACE_DEPS),
    (("review", "compliance", "verifier", "architecture"), SURFACE_REVIEW),
    (("qa", "chat", "answer", "subagent", "explor"), SURFACE_QA),
    (("vault", "module", "feature", "integration", "prd", "doc", "overview"), SURFACE_VAULT),
)


def _surface_for(operation: str | None = None, mode: str | None = None) -> str:
    """Map an (operation, mode) pair to a spend surface.

    `operation` wins over `mode`: the legacy review path and the deps report
    both call ``generate(mode="qa", operation="review_…"/"deps_report")``, so
    trusting `mode` would file them under Q&A.
    """
    op = (operation or "").strip().lower()
    if op == "ask":
        return SURFACE_QA
    for needles, surface in _SURFACE_RULES:
        if any(n in op for n in needles):
            return surface
    md = (mode or "").strip().lower()
    if md == "embedding":
        return SURFACE_EMBEDDINGS
    if md == "qa":
        return SURFACE_QA
    if md in {"batch", "generation"}:
        return SURFACE_VAULT
    return SURFACE_OTHER


def _is_retryable_error(exc: BaseException) -> bool:
    """Retry for: network errors, Gemini 5xx/429, transport disconnects."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        status = getattr(exc, "status_code", 0) or getattr(exc, "code", 0)
        return int(status or 0) in _RETRYABLE_STATUS_CODES
    # httpx transport errors — proxy/CDN drops, server timeout without a response, etc.
    # google-genai lets these through unwrapped.
    return isinstance(
        exc,
        (
            httpx.RemoteProtocolError,   # the server dropped the connection
            httpx.ReadError,             # transport read failure
            httpx.ConnectError,          # could not establish a connection
            httpx.WriteError,            # transport write failure
            httpx.ReadTimeout,           # timeout waiting for the response
            httpx.WriteTimeout,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
        ),
    )


class GeminiClient:
    """The main client for all interactions with Gemini."""

    def __init__(
        self,
        settings: Settings | None = None,
        audit: AuditLogger | None = None,
        workspace_id: str = "default",
    ) -> None:
        self.settings = settings or get_settings()
        self.audit = audit or get_audit_logger()
        # Tenant this client bills to. Instances are cached per (key, models,
        # workspace) in `src.llm.completion._gemini_for`, so this stays correct
        # even when two workspaces share the same Google key.
        self.workspace_id = workspace_id or "default"
        # The deadline, connected. `Settings.gemini_timeout_seconds` existed,
        # was documented as an operator knob, defaulted to 120 — and was read
        # by NO code path in the repository. So the exploration agent's Gemini
        # calls had no request timeout at all: a provider that accepted the
        # connection and then went quiet hung the turn, and the tool-use loop
        # around it, for as long as the socket stayed open.
        #
        # A knob wired to nothing is worse than a missing one. A missing knob
        # sends an operator looking; this one answered them, and the answer was
        # false — they set a number, watched nothing change, and had no reason
        # to suspect the setting rather than the provider.
        #
        # The SDK counts milliseconds; the setting is named seconds, and stays
        # named seconds because that is what an operator thinks in.
        self._client = genai.Client(
            api_key=self.settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=int(self.settings.gemini_timeout_seconds * 1000),
            ),
        )

    # ─── Spend ledger ────────────────────────────────────────────────
    def _record_spend(
        self,
        *,
        operation: str | None = None,
        mode: str | None = None,
        model: str,
        tokens_in: int,
        tokens_out: int = 0,
        cached_tokens_in: int = 0,
        repo: str | None = None,
        user_id: str | None = None,
        agent: str | None = None,
    ) -> None:
        """Append one row to the ``llm_spend`` ledger. Never raises.

        Without this, every workspace whose provider is Google writes nothing
        to the ledger (the native SDK path bypasses LiteLLM), so the Usage and
        Spend pages read zero no matter how much was actually spent.
        """
        try:
            from src.llm.budget import record_spend
            from src.llm.pricing import cost_for

            cost = cost_for(model, int(tokens_in or 0), int(tokens_out or 0))
            record_spend(
                workspace_id=self.workspace_id,
                surface=_surface_for(operation, mode),
                model=model,
                provider="google",
                cost_usd=cost or 0.0,
                cost_source="litellm_estimate" if cost is not None else "unknown",
                tokens_in=int(tokens_in or 0),
                tokens_out=int(tokens_out or 0),
                cached_tokens_in=int(cached_tokens_in or 0),
                agent=agent,
                operation=operation,
                user_id=user_id,
                repo_slug=repo,
            )
        except Exception as exc:  # noqa: BLE001 — a ledger write must never
            # be able to fail a generation.
            logger.warning("gemini_spend_record_failed op=%s err=%s", operation, exc)

    # generate() and generate_stream() used to live here. Both reached zero
    # callers once chat moved to completion.stream_chat and documentation to
    # build_llm_client, and dead completion paths into one vendor's SDK are
    # exactly what tests/llm/test_one_transport.py exists to keep out — so
    # they were deleted rather than kept "just in case".

    # ─── Tool-use (subagent loop) ────────────────────────────────────
    def generate_with_tools_turn(
        self,
        *,
        contents: list[Any],  # list of types.Content (history + new user/tool messages)
        tools: list[Any],     # list of types.Tool
        system_instruction: str,
        model: str | None = None,
        operation: str = "subagent_turn",
        repo: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 2048,
    ) -> ToolTurnResult:
        from src.ops.telemetry import record_llm_call
        record_llm_call()
        """One turn in the tool-use loop. Returns either text (final) or tool_calls.

        Does NOT do redaction (the subagent receives structured tool results, not
        raw code). Loop orchestration lives in the calling code (ExplorationAgent).
        """
        cfg = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            top_p=self.settings.gemini_top_p,
            system_instruction=system_instruction,
            tools=tools,
            # Auto function calling is disabled — we handle tool_calls ourselves
            # (that gives us control over the cap, parallelization, the side-store
            # for bodies).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # Explicit, not dynamic: see Settings.gemini_thinking_budget. -1 keeps
            # the provider default so this is a no-op until somebody sets it.
            **(
                {"thinking_config": types.ThinkingConfig(
                    thinking_budget=self.settings.gemini_thinking_budget)}
                if getattr(self.settings, "gemini_thinking_budget", -1) != -1
                else {}
            ),
        )

        model_name = model or self.settings.gemini_subagent_model

        with self.audit.track(
            workspace_id=self.workspace_id,
            mode="qa",
            model=model_name,
            operation=operation,
            repo=repo,
        ) as record:
            response = self._call_subagent_turn(model_name, contents, cfg)
            usage = response.usage_metadata
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            record.input_tokens_estimated = input_tokens
            record.output_tokens_estimated = output_tokens

            billed_in, billed_out, cached_in = _usage_tokens(usage)
            self._record_spend(
                operation=operation, mode="qa", model=model_name,
                tokens_in=billed_in, tokens_out=billed_out,
                cached_tokens_in=cached_in, repo=repo,
            )

        # We extract: model_parts (with thoughtSignature), text (for the log),
        # tool_calls (for dispatch). Parts are passed into history WITHOUT
        # modification, ToolCall — only for the dispatch handlers (the caller
        # does not reconstruct a Part).
        text: str | None = None
        tool_calls: list[ToolCall] = []
        model_parts: list[Any] = []
        try:
            cands = getattr(response, "candidates", []) or []
            if cands:
                content = cands[0].content
                model_parts = list(getattr(content, "parts", []) or [])
                for part in model_parts:
                    fc = getattr(part, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        tool_calls.append(ToolCall(
                            name=str(fc.name),
                            args=dict(fc.args or {}),
                            id=getattr(fc, "id", None),
                        ))
                    elif getattr(part, "text", None):
                        text = (text or "") + str(part.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("subagent_response_parse_failed err=%s", exc)

        return ToolTurnResult(
            text=text,
            tool_calls=tool_calls,
            model_parts=model_parts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=_extract_finish_reason(response),
            raw_response=response,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    def _call_subagent_turn(self, model: str, contents: list[Any], cfg) -> Any:
        return self._client.models.generate_content(
            model=model,
            contents=contents,
            config=cfg,
        )

    # embed() and embed_batch() used to live here too, behind the documented
    # "embedding exception" — the fear being that a changed transport could
    # drift from the vectors the Qdrant collection was built with and degrade
    # search silently. The exception was retired the day the fear was answered
    # with evidence instead of caution: LiteLLM's gemini route was captured at
    # the wire posting the same batchEmbedContents body (same model string,
    # same taskType, same outputDimensionality), and that request shape is now
    # pinned in tests/llm/test_embedding_requests_do_not_drift.py. The live
    # path is src.llm.completion.embed / embed_batch.


def _usage_tokens(usage: Any) -> tuple[int, int, int]:
    """``(prompt, output, cached)`` from a Gemini ``usage_metadata`` object.

    Thinking tokens are reported separately from ``candidates_token_count`` but
    are billed as output — fold them in so the ledger matches the invoice.
    """
    if usage is None:
        return 0, 0, 0
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    out = int(getattr(usage, "candidates_token_count", 0) or 0)
    out += int(getattr(usage, "thoughts_token_count", 0) or 0)
    cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
    return prompt, out, cached


def _extract_finish_reason(response: Any) -> str:
    try:
        candidates = getattr(response, "candidates", []) or []
        if candidates:
            fr = getattr(candidates[0], "finish_reason", None)
            if fr is not None:
                return str(fr)
    except Exception:  # noqa: BLE001
        pass
    return "UNKNOWN"


# Cache GeminiClient instances per (workspace, key) so we don't spin up a
# genai.Client per request. This cache lived in src.llm.completion while the
# embedding branches there still constructed clients; it moved here with the
# last of them. completion.reset_caches() clears it through sys.modules on
# purpose — an air-gapped install has no google-genai on disk, and importing
# this module just to clear a cache that was never filled broke exactly there.
_gemini_cache: dict[str, GeminiClient] = {}


def _gemini_for(*, key: str, workspace_id: str = "default") -> GeminiClient:
    """The cached client billing to `workspace_id`, calling with `key`.

    The model overrides this took while it also served generation and
    embeddings are gone with those methods: the one surviving call, the
    exploration agent's tool loop, reads `gemini_subagent_model` (or an
    explicit argument) and never the chat/embeddings profiles.
    """
    base = get_settings()
    # Fail with an actionable error instead of genai.Client's cryptic
    # "No API key was provided" when neither the workspace slot nor the
    # env has a Google key (BYOK deployment, key not configured).
    if not (key or (base.gemini_api_key.get_secret_value() or "").strip()):
        from src.llm.keys import LLMCredentialError
        raise LLMCredentialError(
            "no Google/Gemini API key for this workspace — add one on the "
            "LLM Setup page"
        )
    eff = base.model_copy(update={"gemini_api_key": SecretStr(key)}) if key else base
    # Cache key MUST be a collision-resistant digest of the FULL secret, not a
    # prefix: per-workspace Google keys all share the "AIzaSy" prefix, so a
    # prefix cache would hand one tenant another tenant's cached client (with
    # its full key) — a cross-tenant credential leak. Mirror Profile.signature().
    import hashlib
    kh = hashlib.sha256(key.encode()).hexdigest()[:16] if key else "env"
    # `workspace_id` is part of the cache key on purpose: the client carries the
    # tenant it bills to (spend ledger). Two workspaces configured with the SAME
    # Google key would otherwise share one instance and all of their spend would
    # land on whichever tenant warmed the cache first.
    ws = workspace_id or "default"
    sig = f"{ws}:{kh}"
    client = _gemini_cache.get(sig)
    if client is None:
        client = GeminiClient(settings=eff, workspace_id=ws)
        _gemini_cache[sig] = client
    return client


# Env-default fallbacks, one per workspace — the spend ledger bills to
# `client.workspace_id`, so a single shared instance would file every tenant's
# fallback calls under whichever workspace happened to warm the cache.
_default_clients: dict[str, GeminiClient] = {}


def get_gemini_client(workspace_id: str = "default") -> GeminiClient:
    """Return the client for the one native surface left — the QA exploration
    agent's tool loop. The main Q&A path dispatches via
    :func:`src.llm.completion.stream_chat`, embeddings via `completion.embed`.

    `workspace_id` selects the tenant whose Google key to call with (the same
    slot chain as every provider key: ws:{id} → env), so a BYOK tenant
    explores on its own key and bills to its own ledger. The model is
    `gemini_subagent_model` — env-configured, not a UI profile."""
    try:
        from src.llm.profiles import get_provider_key
        return _gemini_for(
            key=get_provider_key("google", workspace_id),
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gemini_key_resolve_failed err=%s — using env defaults", exc)
        ws = workspace_id or "default"
        client = _default_clients.get(ws)
        if client is None:
            client = GeminiClient(workspace_id=ws)
            _default_clients[ws] = client
        return client
