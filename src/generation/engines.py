"""Who writes the documentation: a model given a prompt, or an agent given tools.

Documentation generation was the one large surface with no engine choice and no
profile. Chat, review and embeddings each resolve a profile; review picks
between `api` and `claude_code`; the dependency report picks between them too.
Generation went straight to `get_gemini_client()` three files deep — so it
could not use a workspace's own provider, and its calls did not leave through
the LiteLLM gateway the way every other surface's do.

The two engines answer different questions, and the difference matters more
here than it does for review:

  api          One prompt, one answer. The code and metadata are assembled by
               us and handed over in full. Fast, predictable, and the cost
               scales linearly with the number of documents.

  claude_code  An agent with the Celmis MCP tools and NOTHING else — no Bash,
               no Read, no Grep. It cannot open a file directly; it can only
               ask the index questions: which symbols exist, who calls this,
               what is the public surface, what is deprecated. So it writes a
               module PRD the way a person would — follow the imports, look at
               who calls it, then write — and every claim it makes came from a
               query against the graph rather than from a filename.

That last point is the argument for the agent, not the flat-rate billing.
Documentation is the surface where a model is most free to invent, because a
PRD is interpretation by nature and no evidence_kind can separate a proven
claim from a plausible one. The defence is not a better prompt; it is denying
the model any way to answer except by looking.

REDACTION APPLIES TO BOTH. GeminiClient ran `redact()` over the code before
sending it, and that is a security control, not a formatting step. Any engine
that skips it leaks secrets from the repository into a provider's logs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Engine identifiers. Deliberately the same two words `review_engine` uses —
#: one vocabulary across the product, so "claude_code" means the same thing on
#: every settings page.
ENGINE_API = "api"
ENGINE_CLAUDE_CODE = "claude_code"
ENGINES = (ENGINE_API, ENGINE_CLAUDE_CODE)

DEFAULT_ENGINE = ENGINE_API


@dataclass
class DocResult:
    """What an engine produced, in the shape the generators already expect."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: Which engine actually ran. Not always what was asked for: a workspace
    #: with no Claude subscription falls back, and the document should be able
    #: to say so rather than leaving the reader to guess.
    engine: str = ENGINE_API
    #: The model that actually answered. "api" alone is not provenance — it
    #: covers gemini-3-pro and gpt-4o alike, and two documents generated a
    #: month apart under the same engine can differ entirely because of this.
    model: str | None = None
    #: MCP tools the agent called, in order. Empty for the api engine. This is
    #: the evidence that the agent looked things up instead of guessing, and it
    #: is worth keeping for exactly that reason.
    tools_used: list[str] | None = None


class DocEngine(Protocol):
    """One document, one call."""

    name: str

    def generate(
        self,
        *,
        prompt: str,
        system_instruction: str,
        code_context: str | None,
        metadata_context: dict[str, Any] | None,
        operation: str,
        repo: str | None,
        module: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> DocResult:
        ...


def compose_prompt(
    prompt: str,
    code_context: str | None,
    metadata_context: dict[str, Any] | None,
    *,
    operation: str,
) -> tuple[str, dict]:
    """Assemble the payload exactly as GeminiClient did, redaction included.

    Shared so the two engines cannot drift into sending different things, and
    so `redact()` cannot be forgotten by whichever one is written next. Returns
    the prompt and the redaction stats for the log.
    """
    from src.security.redactor import redact

    redacted_code, stats = redact(code_context or "", source_hint=operation)

    parts: list[str] = [prompt]
    if metadata_context:
        parts.append("\n\n## Context (metadata)\n```json\n")
        parts.append(json.dumps(metadata_context, ensure_ascii=False, indent=2))
        parts.append("\n```")
    if redacted_code:
        parts.append("\n\n## Source code (redacted)\n")
        parts.append(redacted_code)
    return "".join(parts), stats


class ApiEngine:
    """One prompt, one answer — through the same transport as everything else.

    `LLMClient` is what the review agents use: it resolves the workspace's
    profile, and when the LiteLLM gateway is on it sends the call as
    `litellm_proxy/celmis-{ws}-{surface}` under that tenant's virtual key.
    Generation used the google-genai SDK directly and therefore appeared in
    none of that.
    """

    name = ENGINE_API

    def __init__(self, workspace_id: str = "default",
                 user_id: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.user_id = user_id

    @staticmethod
    def _settings():
        from src.config import get_settings

        return get_settings()

    def _model(self, _agent: str | None = None) -> str | None:
        """The chat profile's model, for the case where no gateway is routing.

        Generation has always run on the chat profile — `get_gemini_client()`
        resolves it — so the engine keeps that rather than quietly moving these
        documents onto the review model.
        """
        try:
            from src.llm.profiles import resolve_profile

            return resolve_profile("chat", self.workspace_id).model
        except Exception as exc:  # noqa: BLE001
            logger.debug("docs_model_resolve_failed err=%s", exc)
            return None

    def generate(
        self, *, prompt: str, system_instruction: str,
        code_context: str | None, metadata_context: dict[str, Any] | None,
        operation: str, repo: str | None, module: str | None = None,
        temperature: float | None = None, max_output_tokens: int | None = None,
    ) -> DocResult:
        from src.llm.client import build_llm_client

        t0 = time.monotonic()
        full_prompt, _stats = compose_prompt(
            prompt, code_context, metadata_context, operation=operation)

        # `build_llm_client`, not a bare LLMClient: it binds the key resolver to
        # this tenant and — when the workspace is routed through the LiteLLM
        # gateway, which in production is every workspace — swaps the bare model
        # name for the gateway deployment and takes the key from the same
        # profile. Constructing LLMClient directly is what made every review
        # agent die with LLMCredentialError before sending a token; there is a
        # long note about it in src/llm/client.py.
        client = build_llm_client(
            self.user_id or "system",
            self.workspace_id,
            surface="chat",
            # The chat PROFILE (which model to use) but the vault LEDGER: a
            # budget set on chat should not be spent by a batch vault build,
            # and the Usage view should show documentation as documentation.
            spend_surface="vault",
            resolve_model=self._model,
        )
        response = client.generate(
            prompt=full_prompt,
            agent="docs",
            system_instruction=system_instruction,
            mode="batch",
            operation=operation,
            repo=repo,
            # The generators pass neither, and None meant "the provider's
            # default" rather than the installation's: batch documentation was
            # generated at whatever temperature the vendor picked that month,
            # and with no ceiling, so a truncated document arrived with no
            # signal that it had been cut.
            temperature=(temperature if temperature is not None
                         else self._settings().gemini_temperature_generation),
            max_output_tokens=(max_output_tokens if max_output_tokens is not None
                               else self._settings().gemini_max_output_tokens),
            # The clock and the resend budget, stated for the same reason as
            # the two above it — and missed when they were written. Both were
            # `generate`'s own defaults: 120 seconds and THREE retries, so one
            # document had a real ceiling of 480 seconds that no setting named
            # and no operator could reach. A model asked for 8192 tokens over
            # a whole module's code context is the same size class as the
            # review calls that were measurably being cut at 120.
            timeout=float(self._settings().generation_timeout_seconds),
            num_retries=self._settings().generation_num_retries,
        )
        elapsed = time.monotonic() - t0
        # Said out loud, because it could not be looked up. This installation
        # has 2436 review calls in `llm_spend` and zero documentation ones, so
        # the deadline above had to be argued from the review path instead of
        # measured against this one. Whoever revisits it should not have to.
        logger.info(
            "generation_call_finished operation=%s repo=%s module=%s "
            "elapsed=%.1fs deadline=%ds tokens_out=%s",
            operation, repo or "-", module or "-", elapsed,
            self._settings().generation_timeout_seconds,
            getattr(response, "output_tokens", None) or "?",
        )
        text = getattr(response, "text", "") or ""
        if not text.strip():
            # Same reason as the agent engine: an empty note is stored as a
            # finished one and resume-mode keeps it forever.
            raise RuntimeError(
                f"{operation}: the model returned no text")
        return DocResult(
            text=text,
            input_tokens=int(getattr(response, "input_tokens", 0) or 0),
            output_tokens=int(getattr(response, "output_tokens", 0) or 0),
            engine=ENGINE_API,
            model=getattr(response, "model", None) or self._model(),
        )


def build_engine(engine: str, workspace_id: str = "default",
                 user_id: str | None = None) -> DocEngine:
    """The engine for this run, or the api engine if the named one cannot run.

    Falling back rather than raising is deliberate: a vault build is a
    long-running background job over many modules, and a workspace whose Claude
    subscription lapsed should get documentation from the API rather than a
    failed job and no documents at all. The fallback is logged and travels back
    on the result, so nothing claims to be agent-written when it is not.
    """
    if engine == ENGINE_CLAUDE_CODE:
        from src.generation.claude_docs import ClaudeDocsEngine, claude_docs_available

        ok, reason = claude_docs_available(workspace_id, user_id)
        if ok:
            return ClaudeDocsEngine(workspace_id=workspace_id,
                                    user_id=user_id)
        logger.warning(
            "docs_engine_unavailable requested=claude_code ws=%s reason=%s "
            "— falling back to api", workspace_id, reason)
    return ApiEngine(workspace_id=workspace_id, user_id=user_id)


__all__ = [
    "DEFAULT_ENGINE",
    "ENGINES",
    "ENGINE_API",
    "ENGINE_CLAUDE_CODE",
    "ApiEngine",
    "DocEngine",
    "DocResult",
    "build_engine",
    "compose_prompt",
]
