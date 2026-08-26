"""Provider-agnostic completion + embedding dispatch (Stage 22.1).

Routes each surface to its configured provider (see :mod:`src.llm.profiles`),
and every route is LiteLLM:
  * gateway on → everything leaves through the proxy as
    ``litellm_proxy/celmis-{ws}-{surface}`` with a per-tenant virtual key.
  * gateway off → LiteLLM calls the vendor directly (``gemini/<model>`` for a
    Google profile — same API, same key, one transport).

Embeddings answer to one more question before any of that: `EMBEDDING_PROVIDER`.
Set to anything but "gemini" it sends every embedding through the Embedder in
:mod:`src.indexing.vectors.embedder` — a server on the operator's own network —
and no provider profile, gateway route or key is consulted at all. Unset, which
is the default, nothing below changes.

Keeps embeddings working against the existing Gemini-built Qdrant collection
by default, while letting Chat/Review/Embeddings each pick their own model.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import nullcontext

from src.llm.profiles import Profile, resolve_profile

logger = logging.getLogger(__name__)


# ─── Gateway routing ─────────────────────────────────────────────────


def _routed(surface: str, workspace_id: str = "default") -> Profile:
    """Resolve a profile, provisioning the LiteLLM gateway on first use.

    When the gateway is off this is exactly ``resolve_profile`` — one env read
    more. When it is on, the first call for a workspace creates its team,
    deployments and virtual key on the proxy; every later call reads the cached
    route. A proxy that refuses to provision leaves the profile untouched, so
    the tenant keeps calling providers directly instead of losing service.
    """
    p = resolve_profile(surface, workspace_id)
    if p.via_gateway:
        return p
    try:
        from src.llm import gateway

        if not gateway.is_enabled() or not gateway.provision_workspace(workspace_id):
            return p
    except Exception as exc:  # noqa: BLE001 — never break a call over routing
        logger.warning("gateway_provision_failed surface=%s err=%s", surface, exc)
        return p
    return resolve_profile(surface, workspace_id)


# ─── Spend ledger (non-Google branches) ──────────────────────────────
#
# One row per call, written here — including gateway calls. The proxy keeps its
# own spend table, but the Usage/Spend pages read OUR ledger, and the gateway
# branches below go through `_record_litellm_spend` exactly once (the native
# GeminiClient, which bills itself, is not involved when routing).
#
# The Google branches bill themselves inside `GeminiClient._record_spend`, so
# only the LiteLLM branches below need an explicit write. Recording at THIS
# level (rather than in the callers) is what keeps every entry point — SSE
# chat, the non-streaming variant, indexing — on the ledger.


def _record_litellm_spend(
    p: Profile, *, operation: str, workspace_id: str,
    tokens_in: int, tokens_out: int = 0, cached_tokens_in: int = 0,
    user_id: str | None = None,
    cost_usd: float | None = None, cost_source: str | None = None,
) -> None:
    """Append one ledger row for a LiteLLM call. Never raises."""
    if p.provider == "openai_compatible":
        # A self-hosted server has no invoice to reconcile against — same
        # accounting rule as `_seam_embed` below: NO row rather than a $0.00
        # row. And NOT a table estimate either: a local model named after a
        # hosted one would price itself off that vendor's card ("openai/…"
        # matches OpenAI's table) for tokens the vendor never saw.
        return
    try:
        from src.llm.budget import record_spend
        from src.llm.gemini_client import _surface_for
        from src.llm.pricing import cost_for

        if cost_usd is None:
            cost_usd = cost_for(p.litellm_model, tokens_in, tokens_out)
            if cost_usd is None:
                cost_usd = cost_for(p.model, tokens_in, tokens_out)
            cost_source = "litellm_estimate" if cost_usd is not None else "unknown"
        record_spend(
            workspace_id=workspace_id,
            surface=_surface_for(operation),
            model=p.model,
            provider=p.provider,
            cost_usd=cost_usd or 0.0,
            cost_source=cost_source or "unknown",
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            cached_tokens_in=int(cached_tokens_in or 0),
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 — ledger must never break a call
        logger.warning("litellm_spend_record_failed op=%s err=%s", operation, exc)


def record_completion_spend(
    p: Profile, resp, *, operation: str, workspace_id: str,
    user_id: str | None = None,
) -> None:
    """Ledger row for a non-streaming ``litellm.completion()`` issued outside
    :mod:`src.llm.client` (the deps report, …). Never raises."""
    try:
        from src.llm.pricing import extract_actual_cost_usd

        usage = getattr(resp, "usage", None)
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        cost_usd, cost_source = extract_actual_cost_usd(resp)
        _record_litellm_spend(
            p, operation=operation, workspace_id=workspace_id,
            tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
            tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_tokens_in=cached,
            # None → `_record_litellm_spend` falls back to a table estimate.
            cost_usd=cost_usd, cost_source=cost_source, user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("completion_spend_record_failed op=%s err=%s", operation, exc)


def _embedding_tokens(resp, texts: list[str]) -> int:
    """Prompt tokens of a LiteLLM embedding response, tiktoken-estimated when
    the provider doesn't report usage."""
    usage = getattr(resp, "usage", None)
    for attr in ("prompt_tokens", "total_tokens"):
        try:
            value = int(getattr(usage, attr, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    from src.llm.gemini_client import _estimate_tokens
    return sum(_estimate_tokens(t) for t in texts)


# ─── Chat generation (streaming) ─────────────────────────────────────


async def stream_chat(
    *,
    prompt: str,
    system_instruction: str | None,
    question: str | None = None,
    files_sent: list[str] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    workspace_id: str = "default",
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """Stream a chat answer using the workspace's configured `chat` profile.

    Both branches write the spend ledger themselves — callers must NOT record
    again (that was the old shape, and it double-counted once the provider
    layer started billing).
    """
    p = _routed("chat", workspace_id)
    # Every provider, Google included, gateway or direct → LiteLLM.
    #
    # There used to be a `if p.is_google:` branch here that called google-genai
    # directly. It was unreachable in production (every workspace is routed
    # through the gateway) and it was the only thing in the request path tied
    # to one vendor's SDK and one vendor's endpoint — which is the opposite of
    # why the gateway exists. `p.litellm_model` already yields `gemini/<model>`
    # for a direct Google profile, so LiteLLM calls the same API with the same
    # key; the difference is that swapping a provider, or absorbing a vendor's
    # API change, is now a LiteLLM concern rather than ours.
    async for chunk in _litellm_stream(
        p, prompt=prompt, system_instruction=system_instruction,
        temperature=temperature, max_output_tokens=max_output_tokens,
        workspace_id=workspace_id, user_id=user_id,
        question=question, files_sent=files_sent,
    ):
        yield chunk


def _stream_defaults(
    p: Profile, temperature: float | None, max_output_tokens: int | None,
) -> tuple[float, int | None]:
    """Temperature/max-tokens the way the *direct* path for this vendor would
    have picked them — so turning the gateway on doesn't quietly change the
    answers a Google workspace gets."""
    if temperature is not None and max_output_tokens is not None:
        return temperature, max_output_tokens
    if p.via_gateway and p.google_family:
        from src.config import get_settings
        s = get_settings()
        return (
            temperature if temperature is not None else s.gemini_temperature_qa,
            max_output_tokens or s.gemini_max_output_tokens,
        )
    return (temperature if temperature is not None else 0.3), max_output_tokens


async def _litellm_stream(
    p: Profile, *, prompt: str, system_instruction: str | None,
    temperature: float | None, max_output_tokens: int | None,
    workspace_id: str = "default", user_id: str | None = None,
    question: str | None = None, files_sent: list[str] | None = None,
) -> AsyncIterator[str]:
    import litellm

    if not p.api_key:
        raise RuntimeError(
            f"no API key configured for provider {p.provider!r} — set it in /settings/llm"
        )
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})
    temp, max_tokens = _stream_defaults(p, temperature, max_output_tokens)
    kwargs: dict = {}
    if p.via_gateway:
        # Explicit, even though the SDK would also read LITELLM_PROXY_API_BASE
        # for `litellm_proxy/…` chat calls — the embedding path does NOT read it
        # (it falls back to OPENAI_API_BASE), so both call shapes pass it.
        kwargs["api_base"] = p.gateway_url
    elif p.provider == "openai_compatible":
        # Self-hosted profile: the model string is "openai/<model>", so litellm
        # WITHOUT an explicit api_base would not fail — it would post this
        # workspace's prompt to api.openai.com, authenticated with the
        # "local-no-key" sentinel (or a real local token, leaking that too).
        # A missing address therefore refuses instead of defaulting, same
        # fail-closed direction as the gateway refusal in _attach_gateway.
        if not p.api_base:
            raise RuntimeError(
                "self-hosted LLM profile has no base URL — set it in "
                "/settings/llm; refusing to default to api.openai.com"
            )
        kwargs["api_base"] = p.api_base
    from src.ops.telemetry import record_llm_call
    record_llm_call()
    # Audit: the native Gemini stream writes one record per call, so the gateway
    # path (which replaces it) writes one too. The direct non-gateway path is
    # left exactly as it was.
    if p.via_gateway:
        from src.security.audit import get_audit_logger
        tracker = get_audit_logger().track(
            mode="qa", model=p.model, operation="answer_streaming",
            # The tenant is a parameter of this function; without it the one
            # record type a workspace owner actually asks about — "who asked
            # my Celmis what, and when" — would stay unattributed forever.
            workspace_id=workspace_id,
            question=question, files_sent=files_sent,
            # Never the key, never the prompt — provenance only.
            extra={"transport": "litellm_gateway", "deployment": p.gateway_model,
                   "provider": p.provider},
        )
    else:
        tracker = nullcontext(None)
    with tracker as record:
        resp = await litellm.acompletion(
            model=p.litellm_model,
            api_key=p.api_key,
            messages=messages,
            temperature=temp,
            max_tokens=max_tokens,
            stream=True,
            timeout=120,
            **kwargs,
        )
        collected: list[str] = []
        usage = None
        try:
            async for chunk in resp:
                # Some providers attach cumulative usage to the chunks; most only do
                # so with stream_options={"include_usage": true}, which not every
                # provider accepts — hence the estimate fallback below.
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = u
                try:
                    delta = chunk.choices[0].delta
                    text = getattr(delta, "content", None)
                except (AttributeError, IndexError):
                    text = None
                if text:
                    collected.append(text)
                    yield text
        finally:
            # NB: no `return` in here — that would swallow a propagating exception
            # (and GeneratorExit) out of an async generator.
            if usage is not None or collected:
                from src.llm.gemini_client import _estimate_tokens
                t_in = int(getattr(usage, "prompt_tokens", 0) or 0) or _estimate_tokens(
                    (system_instruction or "") + prompt
                )
                t_out = int(getattr(usage, "completion_tokens", 0) or 0) or _estimate_tokens(
                    "".join(collected)
                )
                if record is not None:
                    from src.security.audit import get_audit_logger
                    full = "".join(collected)
                    record.input_tokens_estimated = t_in
                    record.output_tokens_estimated = t_out
                    record.response_hash = get_audit_logger().hash_response(full)
                _record_litellm_spend(
                    p, operation="answer_streaming", workspace_id=workspace_id,
                    tokens_in=t_in, tokens_out=t_out, user_id=user_id,
                )


# ─── Embeddings ──────────────────────────────────────────────────────
#
# Two questions that look like one, answered in this order:
#
#   `settings.embedding_provider` — WHOSE SERVER embeds. Installation-level,
#     env-only, and the air-gap switch: "gemini" (the default) means every
#     branch below behaves exactly as it always has; anything else means the
#     customer's source code never leaves the box.
#     src/indexing/vectors/embedder.py holds those implementations.
#
#   the `embeddings` PROFILE — which hosted model and whose key, for the
#     installs that do send it out. Workspace-shared, chosen in the UI.
#
# The env answers first. A seam a UI selection could override is a seam a
# regulated install cannot rely on — and the profile page has no way to
# express "a server on my own network" at all.

#: Logged once per process, not per call: `embed_batch` runs per batch during
#: indexing and would otherwise print this thousands of times.
_seam_override_logged = False


def _configured_embedder():
    """The configured :class:`Embedder`, or None when this install runs Gemini.

    None on purpose, and not the seam's own Gemini implementation. The LiteLLM
    path below carries four things the seam does not know about — the
    workspace profile, the per-workspace key, the gateway route and the spend
    ledger — and swapping it for an equivalent-looking implementation would
    drop all four without changing a single vector.

    The seam HAD such an implementation (`GeminiEmbedder`), and this line is
    why it was deleted rather than kept as a fallback: returning None here is
    the only call there is, so nothing could ever reach it. An unreachable
    second way to post the customer's source code to a vendor is not a
    fallback, it is a bypass waiting for a caller.
    """
    global _seam_override_logged
    from src.config import get_settings

    s = get_settings()
    if s.embedding_provider == "gemini":
        return None
    if not _seam_override_logged:
        _seam_override_logged = True
        logger.warning(
            "embeddings_provider_override provider=%s model=%s — EMBEDDING_PROVIDER "
            "is set, so the workspace's embeddings profile is NOT what runs; the "
            "LLM Setup page still shows the profile and cannot show this",
            s.embedding_provider, s.embedding_model or "(unset)",
        )
    from src.indexing.vectors.embedder import get_embedder

    return get_embedder(s)


def _effective_task_type(requested: str | None, *, query: bool) -> str:
    """The task type this call actually means, resolved in ONE place.

    Gemini embeds asymmetrically — a question and the chunk that answers it go
    into different corners of the same space — and the value saying which side
    a call is on was, at every call site, a string literal that happened to
    agree with the settings. `None` now means "the configured value for this
    side", so the intent is one value end to end instead of five copies.

    `embedding_task_type_enabled=False` collapses both sides onto the document
    task type. That is SYMMETRIC, not absent: Gemini has no "no task type"
    value and an empty string is a 400. It applies to an explicitly passed
    task type too — a kill switch a caller can route around is not one.
    """
    from src.config import get_settings

    s = get_settings()
    if not s.embedding_task_type_enabled:
        return s.gemini_embedding_task_doc
    if requested:
        return requested
    return s.gemini_embedding_task_query if query else s.gemini_embedding_task_doc


#: Markers that name the QUERY half of an asymmetric pair, for the providers
#: that take a side rather than a vendor enum.
_QUERY_SIDE_MARKERS = ("QUERY", "QUESTION_ANSWERING", "FACT_VERIFICATION")


def _is_query_side(task_type: str) -> bool:
    """Which half of the pair a task type names.

    Anything unrecognised is treated as a document. That is the safe direction:
    a query embedded as a document costs recall on one search, a document
    embedded as a query is written into the index and costs every search until
    somebody re-indexes.
    """
    tt = (task_type or "").upper()
    return any(marker in tt for marker in _QUERY_SIDE_MARKERS)


def _seam_embed(embedder, texts: list[str], *, task_type: str, operation: str,
                workspace_id: str | None) -> list[list[float]]:
    """Embed through the configured Embedder — the air-gapped path.

    ACCOUNTING, checked rather than assumed: the seam writes one audit record
    per text (mode="embedding", now carrying the tenant and the operation) and
    no spend row, and that is left exactly as it is. The LiteLLM path below
    bills itself because there is an invoice to reconcile against; a model
    server on the operator's own hardware has none, and a ledger of $0.00 rows
    attributed to an invented provider would make the Spend page describe a
    cost that does not exist.
    """
    from src.ops.telemetry import record_llm_call

    record_llm_call()
    if _is_query_side(task_type):
        return [
            embedder.embed_query(text, workspace_id=workspace_id, operation=operation)
            for text in texts
        ]
    results = list(embedder.embed_documents(
        [(f"{operation}:{i}", text) for i, text in enumerate(texts)],
        workspace_id=workspace_id,
        operation=operation,
    ))
    failed = [r for r in results if r.error or not r.vector]
    if failed:
        # The seam isolates per-chunk failures so one bad file cannot end an
        # index run. This function's contract is different: a vector per text,
        # in order, for a caller that zips them into Qdrant points. An empty
        # vector written as a point is a point nothing will ever retrieve.
        raise RuntimeError(
            f"embedding failed for {len(failed)}/{len(results)} texts "
            f"(operation={operation}): {failed[0].error or 'empty vector'}"
        )
    return [r.vector for r in results]


def _expressible_task_type(p: Profile, task_type: str) -> str:
    """The task type this route can actually carry, or "" for symmetric.

    Only the Gemini/Vertex family has the field. Sending it to an OpenAI-shaped
    deployment does not degrade quietly: LiteLLM puts an unrecognised embedding
    param into `extra_body`, and OpenAI answers 400 for an argument it does not
    know — so a workspace that pointed its embeddings profile at OpenAI would
    stop embedding altogether rather than lose the asymmetry. Degrade to
    symmetric instead: one embedding space, no asymmetry, still working.
    """
    if not task_type or not p.google_family:
        return ""
    return task_type


def _embedding_kwargs(p: Profile, inputs: list[str], task_type: str) -> dict:
    """Request kwargs for a LiteLLM embedding call.

    `task_type` (Gemini's asymmetric document/query embeddings) is forwarded
    on both transports — only for a vendor that has the field, see
    `_expressible_task_type`. It used to be gateway-only because the direct
    Google case went through the native SDK; now that it goes through LiteLLM
    too, dropping it here would flip every direct-key install to symmetric
    embeddings — silently, which is the drift this module's tests exist to
    make loud. `dimensions` rides along on both routes as well; the proxy's
    ``drop_params: true`` discards it for vendors that don't take it, so a
    non-Google embeddings model behind the same gateway still works.
    """
    kwargs: dict = {"model": p.litellm_model, "input": inputs, "api_key": p.api_key}
    if p.via_gateway:
        # Required: LiteLLM's embedding path does NOT read LITELLM_PROXY_API_BASE,
        # it falls back to OPENAI_API_BASE — i.e. straight to api.openai.com.
        kwargs["api_base"] = p.gateway_url
    expressible = _expressible_task_type(p, task_type)
    if expressible:
        kwargs["task_type"] = expressible
    if p.dimensions:
        kwargs["dimensions"] = p.dimensions
    return kwargs


def _assert_embeddings_route(p: Profile, workspace_id: str) -> None:
    """Loudly refuse to embed through a gateway route that drifted away from
    the model/width the Qdrant collection was built with.

    Only the gateway path is guarded: routing is what makes the drift invisible
    (the deployment name stays ``celmis-{ws}-embed`` whatever it points at). The
    direct path names its model in the request itself, and the fields it sends
    are pinned by tests/llm/test_embedding_requests_do_not_drift.py.
    """
    if not p.via_gateway:
        return
    from src.llm import gateway
    gateway.assert_embeddings_compatible(
        p, gateway.route_for("embeddings", workspace_id),
    )


def _litellm_embed(
    p: Profile, texts: list[str], *, task_type: str, operation: str,
    workspace_id: str,
) -> list[list[float]]:
    """Embed `texts` through LiteLLM — every provider, gateway or direct.

    The direct-Google case used to be the DELIBERATE EXCEPTION here: a native
    ``GeminiClient`` branch, kept because an embedding is baked into a stored
    Qdrant collection and a transport that drifts from the vectors the
    collection was built with degrades search silently rather than failing.
    That exception was retired on evidence, not on faith: the installed
    LiteLLM's ``gemini/`` route was captured at the wire posting the same
    ``batchEmbedContents`` body the SDK posted — same ``models/<model>``
    string, same ``taskType``, same ``outputDimensionality`` — and the
    request fields this function sends are pinned exactly in
    tests/llm/test_embedding_requests_do_not_drift.py, so a LiteLLM upgrade
    that stops forwarding either param fails a test instead of an index.

    What the native branch guaranteed is kept, not remembered:
      * the ledger row (surface "embeddings", the bare model, provider) via
        `_record_litellm_spend`, as the non-Google branch always did;
      * the audit record the native client wrote for direct-Google calls
        (mode="embedding", stamped with the tenant) — gateway and non-Google
        calls stay unaudited here exactly as they were;
      * the retry the native client carried (tenacity, 5 attempts on
        429/5xx/network) as ``num_retries=4`` — an index run meets quota
        blips as a matter of course, and losing the retry would trade a
        30-second wait for a dead run.
    """
    import litellm

    if not p.api_key:
        if p.google_family:
            # The native branch raised this from its client factory; keep the
            # type and the sentence — callers and operators know them.
            from src.llm.keys import LLMCredentialError
            raise LLMCredentialError(
                "no Google/Gemini API key for this workspace — add one on the "
                "LLM Setup page"
            )
        raise RuntimeError(
            f"no API key configured for embeddings provider {p.provider!r}"
        )
    _assert_embeddings_route(p, workspace_id)
    from src.ops.telemetry import record_llm_call
    record_llm_call()
    kwargs = _embedding_kwargs(p, texts, task_type)
    if p.is_google:
        kwargs["num_retries"] = 4  # ≈ the native client's stop_after_attempt(5)
        from src.security.audit import get_audit_logger
        tracker = get_audit_logger().track(
            workspace_id=workspace_id,
            mode="embedding",
            model=p.model,
            operation=operation,
            extra={"task_type": task_type, "batch_size": len(texts),
                   "transport": "litellm"},
        )
    else:
        tracker = nullcontext(None)
    with tracker as record:
        resp = litellm.embedding(**kwargs)
        # LiteLLM returns data in request order.
        vectors = [item["embedding"] for item in resp.data]
        tokens = _embedding_tokens(resp, texts)
        if record is not None:
            record.input_tokens_estimated = tokens
            record.extra["dimensions"] = len(vectors[0]) if vectors else 0
        _record_litellm_spend(
            p, operation=operation, workspace_id=workspace_id, tokens_in=tokens,
        )
    return vectors


def embed(text: str, *, task_type: str | None = None, operation: str = "embed_query",
          workspace_id: str = "default"):
    """Embed one text. Returns the raw vector (list[float]).

    `task_type=None` means the configured QUERY side — this function's callers
    are searches. Pass one explicitly for the document side (a vault note being
    written), and see `_effective_task_type` for what the setting does to it.

    Routing: the configured Embedder when `embedding_provider` is not "gemini";
    otherwise the shared `embeddings` profile through LiteLLM (see
    `_litellm_embed` for why that now includes Google). The profile's model is
    workspace-shared; `workspace_id` supplies a key fallback and the tenant on
    the audit record.
    """
    task_type = _effective_task_type(task_type, query=True)
    embedder = _configured_embedder()
    if embedder is not None:
        return _seam_embed(
            embedder, [text], task_type=task_type,
            operation=operation, workspace_id=workspace_id,
        )[0]
    p = _routed("embeddings", workspace_id)
    return _litellm_embed(
        p, [text], task_type=task_type, operation=operation,
        workspace_id=workspace_id,
    )[0]


def embed_batch(
    texts: list[str], *, task_type: str | None = None,
    operation: str = "embed_batch", workspace_id: str = "default",
) -> list[list[float]]:
    """Embed many texts. Returns one vector per text, in order.

    `task_type=None` means the configured DOCUMENT side — a batch is what
    indexing looks like.

    Indexing goes through here too — otherwise picking a non-Google embeddings
    provider in the UI would silently keep writing Gemini vectors and the
    search index would quietly disagree with the query side.
    """
    if not texts:
        return []
    task_type = _effective_task_type(task_type, query=False)
    embedder = _configured_embedder()
    if embedder is not None:
        return _seam_embed(
            embedder, texts, task_type=task_type,
            operation=operation, workspace_id=workspace_id,
        )
    p = _routed("embeddings", workspace_id)
    return _litellm_embed(
        p, texts, task_type=task_type, operation=operation,
        workspace_id=workspace_id,
    )


def embedding_dimensions() -> int:
    """Vector width the configured embeddings path produces — the Qdrant
    collection must be created with exactly this.

    The seam is asked first, for the same reason `embed` asks it first: when
    `embedding_provider` is not "gemini", the profile describes a provider this
    install does not call, and its 3072 would build a collection that rejects
    every vector the install can actually produce.
    """
    from src.config import get_settings

    s = get_settings()
    if s.embedding_provider != "gemini":
        if s.embedding_dimensions:
            return int(s.embedding_dimensions)
        # Refuse rather than guess. The width of a model nobody has called yet
        # is unknown, and the two ways of being wrong are not symmetric: too
        # small and every upsert fails, too large and it fails too — both after
        # a full index run. Setting it is one line and it is written down.
        raise ValueError(
            f"EMBEDDING_PROVIDER={s.embedding_provider!r} needs EMBEDDING_DIMENSIONS: "
            "the vector width of a self-hosted model cannot be guessed, and a "
            "collection built at the wrong width rejects every vector. Set it to "
            "what your model returns (768 for nomic-embed-text, 1024 for bge-m3)."
        )
    p = resolve_profile("embeddings")
    if p.dimensions:
        return int(p.dimensions)
    return int(s.gemini_embedding_dimensions)


def reset_caches() -> None:
    """Drop cached Gemini clients + gateway routes (call after a profile/key
    change — a rotated key must not keep flowing through the old deployment)."""
    # Through sys.modules, NOT an import: gemini_client pulls google-genai at
    # module scope, and an air-gapped install has no google-genai on disk at
    # all (see tests/indexing/test_embedder_local.py). If the module was never
    # imported, its client cache was never filled — nothing to clear.
    import sys
    gemini_client = sys.modules.get("src.llm.gemini_client")
    if gemini_client is not None:
        gemini_client._gemini_cache.clear()
    try:
        from src.llm import gateway
        gateway.reset_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("gateway_cache_reset_failed err=%s", exc)
