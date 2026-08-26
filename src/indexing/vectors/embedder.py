"""Embeddings — the one call that ships the customer's source code off the box.

Everything else a tenant runs is a completion: stateless, routable, replaceable.
Indexing is not. It reads every file in the repository and posts it somewhere,
and until now that somewhere was Google, unconditionally, because the client
was `genai.Client` built inside this module with no seam. A regulated buyer
could not install this product at any configuration.

So this module is the OFF-GOOGLE half of the seam, one implementation behind
one protocol:

    OpenAICompatibleEmbedder  — POST {base_url}/embeddings, which is the shape
                                Ollama, vLLM, llama.cpp, TEI and Infinity all
                                serve. ONE client for all of them; a class per
                                vendor would be five copies of the same POST.

WHO REACHES THIS: `src.llm.completion.embed` / `embed_batch`, which is the
single door every embedding in the product goes through — vault notes, Q&A
queries, code chunks. It consults `embedding_provider` first and only then the
workspace's `embeddings` profile, so the air-gap switch cannot be overridden
by a UI selection. Until that wiring existed this module was a capability
nobody could reach: correct, tested, and called from nowhere in `src/`.

THERE WAS A SECOND IMPLEMENTATION HERE, and it was the same kind of thing: a
`GeminiEmbedder` holding its own `genai.Client`, described as "what every
existing install runs". It was not. The single door above answers
`embedding_provider` FIRST, and `completion._configured_embedder()` returns
None — not this module's Gemini class — whenever that value is "gemini",
precisely so the Gemini path keeps the four things this seam knows nothing
about: the workspace profile, the per-workspace key, the gateway route and the
spend ledger. "gemini" is also the default, so `get_embedder` was only ever
reached with the other value, and the class was unreachable on every install
there is. It was deleted rather than exempted: an unreachable second way to
send the customer's source code to a vendor is not a fallback, it is a bypass
waiting for a caller, and the policy is that every model call leaves through
the LiteLLM gateway.

Every embedding call:
    1. Redaction (security/redactor.py) — mandatory before sending
    2. The provider call, with retry/backoff
    3. Audit record (security/audit.py)

The local client sends through :func:`src.security.egress.build_http_client`,
which is what makes "no egress" checkable rather than claimed — see
tests/indexing/test_embedder_local.py, which empties the allowlist entirely
and embeds anyway.

THREADS, not async. Batches leave this module several at a time (see
:data:`EMBED_CONCURRENCY`) on a small pool over the one sync guarded client.
Async was the obvious alternative and it is the wrong one here. The work is a
POST and then waiting, which threads overlap just as well as a loop does; the
sync `httpx.Client` is thread-safe and already the guarded one, so the
allowlist keeps applying per request; and `embed_documents` is a generator
whose consumers are sync the whole way up — `completion._seam_embed` does
`list(...)`, `embed_batch` returns a list, and the indexer and the vault
reader call that straight. Colouring this call async would rewrite every one
of them to buy the same overlap, for no additional speed.

Phase: 7b. Implemented.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from itertools import chain
from typing import Protocol, runtime_checkable

from src.config import Settings, get_settings

# EmbeddingConfigMismatch lives in the gateway because that is where every
# other "your embeddings no longer match your index" refusal is raised; the
# local embedder reuses it so callers have ONE type to catch. Safe for the
# air-gapped install: `src.llm` re-exports lazily (PEP 562) and gateway pulls
# no provider SDK at module scope — see tests/indexing/test_embedder_local.py.
from src.llm.gateway import EmbeddingConfigMismatch
from src.security.audit import AuditLogger, get_audit_logger
from src.security.egress import EgressBlockedError
from src.security.redactor import Redactor

logger = logging.getLogger(__name__)

#: Documents per POST on the OpenAI-compatible path. The /embeddings schema
#: takes a list, and sending one text at a time was measured at 20 HTTP
#: round-trips for 20 chunks — an indexing run is thousands. 64 cuts the
#: round-trips by that factor while keeping a request comfortably inside
#: default body limits (Ollama, vLLM and TEI all accept it). A constant, not
#: a setting: nobody has needed to tune it, and a knob nobody turns is
#: documentation that lies.
EMBED_BATCH_SIZE = 64

#: Batches in flight at once on a provider path that opts in — see
#: `_BaseEmbedder.max_concurrent_batches`, which is where the number is
#: actually applied and where an install can override it.
#:
#: Batching cut the request COUNT. What it could not touch is that batch N+1
#: still sat idle while batch N crossed the network, so a repository of
#: thousands of chunks paid every round trip end to end. Overlapping them is
#: the whole win, and it is a small one to build: the work is one POST and
#: then waiting, `httpx.Client` is documented thread-safe (its connection pool
#: is what threads share), and the client here is the guarded one, so the
#: allowlist keeps applying per request exactly as it did on one thread.
#:
#: 4, and deliberately timid, because the far end is usually the operator's
#: own box serving ONE model: Ollama and llama.cpp overlap the round trip but
#: not the forward pass. Measured against a fake server shaped that way — 1024
#: chunks, 40ms round trip, one lock around the model — 1 lane took 1.64s,
#: 2 took 0.87s, 4 took 0.72s, 8 took 0.71s and 16 took 0.77s. Past a handful
#: of lanes the queue has simply moved inside the server: 4 collects almost
#: all of the available win, the doubling to 8 bought 2% for twice the memory
#: held in flight (lanes × EMBED_BATCH_SIZE texts), and 16 was slower than 4.
#: A hosted OpenAI-compatible endpoint fails the other way — 429s, each paying
#: the 1/2/4-second backoff ladder and undoing the speedup it was raised for.
#: Both ends say the same thing: overlap enough to hide the latency, not
#: enough to become the load.
#:
#: tests/indexing/test_batches_overlap_instead_of_queueing.py times the win
#: rather than asserting it, so complexity that stopped paying shows up as a
#: failing test instead of a comment nobody re-checks.
EMBED_CONCURRENCY = 4


# ─── data ──────────────────────────────────────────────────────────


@dataclass
class EmbeddingResult:
    """The result of one embed call."""

    chunk_id: str
    vector: list[float]
    dimensions: int
    model: str
    redaction_stats: dict
    error: str | None = None


# ─── protocol ──────────────────────────────────────────────────────


@runtime_checkable
class Embedder(Protocol):
    """What callers of this module actually use — and nothing more.

    Two methods, because two are what exist at the call sites: documents go in
    during indexing, a query goes in during retrieval, and the split is not
    cosmetic (the two sides are embedded asymmetrically, which is what
    `task_type` means on Gemini and what the prefix settings mean elsewhere).

    No `close()`, no `dimensions()`, no batch-size knob: nobody calls them, and
    a protocol that lists methods nobody calls is a protocol the second
    implementation has to fake.

    `workspace_id` and `operation` are keyword-only and optional because they
    are not what an embedder DOES — they are who the call was for and what it
    was part of. They exist because the audit trail is per tenant: a record
    written without a workspace is visible to global admins only, so an
    install that switched providers would drop every embedding call out of its
    owner's audit page. That is attribution living in the data, and the
    provider layer is where it enters.
    """

    def embed_documents(
        self,
        chunks: Iterable[tuple[str, str]],
        repo: str | None = None,
        max_retries: int = 3,
        *,
        workspace_id: str | None = None,
        operation: str = "embed",
    ) -> Iterator[EmbeddingResult]:
        ...

    def embed_query(
        self,
        text: str,
        repo: str | None = None,
        *,
        workspace_id: str | None = None,
        operation: str = "embed",
    ) -> list[float]:
        ...


# ─── shared machinery ──────────────────────────────────────────────


class _BaseEmbedder:
    """Redaction, audit, retry and per-chunk error isolation.

    This is the part that must NOT differ between providers. A local backend
    that skipped redaction, or that stopped the whole run on one bad chunk,
    would be a different product wearing the same interface — so the loop lives
    here once and each provider supplies only `_embed_one`.
    """

    #: Exceptions that must not be retried — retrying a policy decision just
    #: spends 7 seconds of backoff arriving at the same refusal.
    NON_RETRYABLE: tuple[type[BaseException], ...] = ()

    #: Exceptions that must PROPAGATE out of the run instead of being recorded
    #: on the chunk. Per-chunk isolation exists so one bad file cannot end an
    #: index run — but a configuration mismatch is equally wrong for every
    #: chunk that will follow, and isolating it 9,000 times reproduces the
    #: exact bug it was built to catch: every error swallowed as a warning,
    #: a green run, an empty index.
    PROPAGATE: tuple[type[BaseException], ...] = ()

    #: Documents per provider request. 1 — one text per call — stays the
    #: default because it is the only value that is correct everywhere: an API
    #: that rejects a list batch (gemini-embedding did, which is where this
    #: default came from) fails on every chunk, while an API that accepts one
    #: merely does more round-trips. Raising it is a per-provider decision made
    #: against that provider's schema.
    batch_size: int = 1

    #: Batches allowed in flight at once. 1 — strictly one after another — is
    #: the default for the same reason `batch_size` is 1: it is the value that
    #: is correct against every provider, including one whose server is
    #: single-threaded or whose SDK is not safe to share. At 1 this class runs
    #: the loop it has always run, on the calling thread, with no pool built at
    #: all; raising it is a per-provider decision made against that provider's
    #: transport, and the OpenAI-compatible path takes it (EMBED_CONCURRENCY)
    #: because its client is `httpx.Client`, which is thread-safe by
    #: documentation and by the connection pool it shares.
    #:
    #: This attribute — not an env var — is the configuration surface: an
    #: install that must serialise against a fragile model server writes
    #: `embedder.max_concurrent_batches = 1` at the seam. Same judgement as
    #: `EMBED_BATCH_SIZE`: a setting nobody turns is documentation that lies,
    #: and this one would additionally need forwarding through compose to
    #: exist in the only supported deployment.
    max_concurrent_batches: int = 1

    def __init__(
        self,
        settings: Settings | None = None,
        redactor: Redactor | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redactor = redactor or Redactor(fail_closed=self.settings.redaction_fail_closed)
        self.audit = audit or get_audit_logger()

    # ── provider hooks ─────────────────────────────────────────────

    @property
    def model(self) -> str:
        raise NotImplementedError

    @property
    def declared_dimensions(self) -> int:
        raise NotImplementedError

    @property
    def task_document(self) -> str:
        raise NotImplementedError

    @property
    def task_query(self) -> str:
        raise NotImplementedError

    def _embed_one(self, text: str, task_type: str) -> list[float]:
        """Send ONE already-redacted text. Raise on failure; the loop retries."""
        raise NotImplementedError

    def _embed_many(self, texts: list[str], task_type: str) -> list[list[float]]:
        """Send a WINDOW of already-redacted texts; one vector per text, in order.

        Reached only for windows of two or more (see `_flush`), which only
        exist when `batch_size` > 1. The default keeps a subclass that raises
        `batch_size` without overriding this correct — just not faster.
        """
        return [self._embed_one(text, task_type) for text in texts]

    def _dimensions_for(self, vector: list[float]) -> int:
        """Width to report. Default is the CONFIGURED value, not the measured
        one — a subclass that wants the truth overrides this."""
        return self.declared_dimensions

    # ─── public API ────────────────────────────────────────────────

    def embed_documents(
        self,
        chunks: Iterable[tuple[str, str]],  # (chunk_id, text) pairs
        repo: str | None = None,
        max_retries: int = 3,
        *,
        workspace_id: str | None = None,
        operation: str = "embed",
    ) -> Iterator[EmbeddingResult]:
        """Embed code chunks one by one on the document side.

        Yields EmbeddingResult per chunk. An error on one chunk does not stop
        the loop — it just records the error into that result.

        Args:
            chunks: iterable of (chunk_id, text). chunk_id — for idempotency.
            repo: repo slug — for the audit.
            max_retries: per chunk transient error retry count.
            workspace_id: the tenant this call was made for — for the audit.
            operation: what the call was part of (embed_note, embed_batch, …),
                which is how the Usage page tells indexing from searching.
        """
        return self._embed_iter(
            chunks,
            task_type=self.task_document,
            repo=repo,
            max_retries=max_retries,
            workspace_id=workspace_id,
            operation=operation,
        )

    def embed_query(
        self,
        text: str,
        repo: str | None = None,
        *,
        workspace_id: str | None = None,
        operation: str = "embed",
    ) -> list[float]:
        """Embed a user's natural-language query on the query side.

        Single call. The redactor runs here too — a question is not code, but
        people paste passwords into questions.
        """
        results = list(self._embed_iter(
            [("query", text)],
            task_type=self.task_query,
            repo=repo,
            max_retries=3,
            workspace_id=workspace_id,
            operation=operation,
        ))
        if not results or results[0].error:
            err = results[0].error if results else "no_result"
            raise RuntimeError(f"embed_query failed: {err}")
        return results[0].vector

    # ─── core loop ────────────────────────────────────────────────

    def _embed_iter(
        self,
        chunks: Iterable[tuple[str, str]],
        task_type: str,
        repo: str | None,
        max_retries: int,
        workspace_id: str | None = None,
        operation: str = "embed",
    ) -> Iterator[EmbeddingResult]:
        """Redact chunk by chunk, send window by window, yield in input order.

        A window is `batch_size` chunks — so exactly one by default, where this
        reduces to the loop the module has always run. Chunks whose redaction
        fails are buffered too, not yielded early: callers of embed_documents
        zip results against inputs, so order is part of the contract.

        Windows may travel CONCURRENTLY (`max_concurrent_batches`), and input
        order survives it because the results are re-joined in submission
        order, not in completion order. Two shapes deliberately never build a
        pool: `max_concurrent_batches == 1`, and a run that turns out to be a
        single window — which is every `embed_query`, the retrieval hot path,
        where a thread would be pure overhead around one request.
        """
        size = max(1, self.batch_size)
        lanes = max(1, self.max_concurrent_batches)
        windows = self._redacted_windows(chunks, size)

        if lanes == 1:
            for window in windows:
                yield from self._flush(
                    window, task_type, repo, max_retries, workspace_id, operation,
                )
            return

        first = next(windows, None)
        if first is None:
            return
        second = next(windows, None)
        if second is None:
            yield from self._flush(
                first, task_type, repo, max_retries, workspace_id, operation,
            )
            return
        yield from self._flush_concurrently(
            chain([first, second], windows), lanes,
            task_type, repo, max_retries, workspace_id, operation,
        )

    def _redacted_windows(
        self,
        chunks: Iterable[tuple[str, str]],
        size: int,
    ) -> Iterator[list[tuple[str, str | None, dict, str | None]]]:
        """Redact on the CALLING thread and hand out full windows of `size`.

        Redaction stays here, off the pool, on purpose. It is CPU work behind
        the GIL, so a worker thread would win nothing, and it is the one step
        that must have happened before a byte goes out — keeping it in the
        single thread that reads `chunks` means no arrangement of the pool can
        put an unredacted text on the wire, and no second thread ever touches
        the Redactor (whose thread-safety nobody has established).
        """
        #: (chunk_id, redacted_text | None, redaction_stats, redaction_error | None)
        window: list[tuple[str, str | None, dict, str | None]] = []
        for chunk_id, raw_text in chunks:
            try:
                redacted, rstats = self.redactor.redact(
                    raw_text, source_hint=f"embed:{chunk_id}", mode="code",
                )
                window.append((chunk_id, redacted, rstats.as_dict(), None))
            except Exception as e:  # noqa: BLE001 — fail-closed redactor
                window.append(
                    (chunk_id, None, {}, f"redaction_failed: {type(e).__name__}"),
                )
            if len(window) >= size:
                yield window
                window = []
        if window:
            yield window

    def _flush_concurrently(
        self,
        windows: Iterable[list[tuple[str, str | None, dict, str | None]]],
        lanes: int,
        task_type: str,
        repo: str | None,
        max_retries: int,
        workspace_id: str | None,
        operation: str,
    ) -> Iterator[EmbeddingResult]:
        """`lanes` windows in flight; results re-joined in SUBMISSION order.

        The whole of the concurrency is this one block, and its shape is
        chosen for the two things that must not change. Order: a window is
        yielded when the future at the HEAD of the queue is done, never when
        the fastest one finishes, so a fast batch 7 waits its turn behind a
        slow batch 3 exactly as it did when they ran in series. Boundedness:
        the queue is never longer than `lanes`, and `windows` is read lazily
        rather than listed, so a run holds `lanes` requests and at most
        `lanes + 1` windows of redacted text however long the repository is.
        That second bound is the one `max_workers` does NOT give — an executor
        handed every window still accepts them all and holds their texts in
        its own queue.

        A PROPAGATE exception comes back out of `.result()` on this thread,
        into the caller of embed_documents, still the exception the provider
        raised. Nothing about it is per-thread: swallowing a width mismatch
        into one chunk's `error` field is the original green-run-empty-index
        bug, and a pool that quietly did that per lane would be the same bug
        with a nicer traceback.
        """
        pending: deque[Future[list[EmbeddingResult]]] = deque()
        # shutdown(wait=True) on the way out — including the way out through a
        # propagated exception or a caller that abandons the generator. Threads
        # holding a socket to the customer's model server do not outlive the
        # run that opened them.
        with ThreadPoolExecutor(
            max_workers=lanes, thread_name_prefix="embed",
        ) as pool:
            try:
                for window in windows:
                    while len(pending) >= lanes:
                        yield from pending.popleft().result()
                    pending.append(pool.submit(
                        self._flush, window, task_type, repo, max_retries,
                        workspace_id, operation,
                    ))
                while pending:
                    yield from pending.popleft().result()
            finally:
                # Whatever is still queued is work for a run that is over.
                # Cancelling before shutdown means a mismatch on batch 3 does
                # not go on to send batches 5..8 to a server we already know
                # is misconfigured.
                for future in pending:
                    future.cancel()

    def _flush(
        self,
        window: list[tuple[str, str | None, dict, str | None]],
        task_type: str,
        repo: str | None,
        max_retries: int,
        workspace_id: str | None,
        operation: str,
    ) -> list[EmbeddingResult]:
        """Embed one window's sendable chunks and return every chunk, in order.

        A list, not a generator, because this is the unit of work a pool
        thread runs to completion: a half-consumed generator handed back
        across threads would leave the provider call to happen on whichever
        thread pulled next. It was already eager in practice — the sends
        happened on the first `next()` and the caller drove it immediately —
        so the sequential path sees no change.
        """
        sendable = [entry for entry in window if entry[3] is None]

        if len(sendable) == 1:
            # One text takes the single-text path: the same wire bytes, and
            # byte-identical behaviour for every provider with batch_size=1.
            chunk_id, redacted, rstats, _ = sendable[0]
            outcomes = iter([self._embed_single(
                chunk_id, redacted or "", rstats, task_type, repo,
                max_retries, workspace_id, operation,
            )])
        elif sendable:
            outcomes = iter(self._embed_window(
                sendable, task_type, repo, max_retries, workspace_id, operation,
            ))
        else:
            outcomes = iter(())

        results: list[EmbeddingResult] = []
        for chunk_id, _redacted, _rstats, redaction_error in window:
            if redaction_error is not None:
                results.append(EmbeddingResult(
                    chunk_id=chunk_id, vector=[],
                    dimensions=self.declared_dimensions,
                    model=self.model, redaction_stats={},
                    error=redaction_error,
                ))
            else:
                results.append(next(outcomes))
        return results

    def _embed_single(
        self,
        chunk_id: str,
        redacted: str,
        rstats: dict,
        task_type: str,
        repo: str | None,
        max_retries: int,
        workspace_id: str | None,
        operation: str,
    ) -> EmbeddingResult:
        """One text, with retry — the loop this module has always run."""
        model = self.model
        with self.audit.track(
            mode="embedding",
            model=model,
            operation=operation,
            workspace_id=workspace_id,
            repo=repo,
            extra={"task_type": task_type, "chunk_id": chunk_id},
        ) as record:
            record.redaction = rstats

            vec: list[float] = []
            err: str | None = None
            for attempt in range(max_retries):
                try:
                    vec = self._embed_one(redacted, task_type)
                    break
                except Exception as e:  # noqa: BLE001
                    if isinstance(e, self.PROPAGATE):
                        # Misconfiguration, not misfortune: equally wrong for
                        # every chunk after this one. audit.track records the
                        # error on the way out.
                        raise
                    err = f"{type(e).__name__}: {str(e)[:200]}"
                    if isinstance(e, self.NON_RETRYABLE):
                        logger.warning(
                            "embed_refused chunk=%s err=%s", chunk_id, err,
                        )
                        break
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # 1, 2, 4 seconds
                        continue
                    logger.warning(
                        "embed_failed chunk=%s attempts=%d err=%s",
                        chunk_id, attempt + 1, err,
                    )

            if err and not vec:
                record.error = err
                record.input_tokens_estimated = len(redacted) // 4
            else:
                record.input_tokens_estimated = len(redacted) // 4
                record.output_tokens_estimated = 0  # embeddings not generative

        return EmbeddingResult(
            chunk_id=chunk_id,
            vector=vec,
            dimensions=self._dimensions_for(vec),
            model=model,
            redaction_stats=rstats,
            error=err if not vec else None,
        )

    def _embed_window(
        self,
        sendable: list[tuple[str, str | None, dict, str | None]],
        task_type: str,
        repo: str | None,
        max_retries: int,
        workspace_id: str | None,
        operation: str,
    ) -> list[EmbeddingResult]:
        """A window of texts in ONE provider request, with the same retries.

        Failure handling differs from the single path in one deliberate way:
        once the retries are spent, a retryable error falls back to the
        single-text path per chunk. A batch fails as a unit — one over-long
        text 400s the whole request — and losing 63 good chunks to one bad
        neighbour would break the per-chunk isolation this module promises.
        A NON_RETRYABLE refusal does not fall back: the allowlist saying no
        to the batch would say no to each text too, 64 times, slower.
        """
        model = self.model
        texts = [redacted or "" for _cid, redacted, _rs, _err in sendable]

        vectors: list[list[float]] | None = None
        err: str | None = None
        refused = False
        for attempt in range(max_retries):
            try:
                vectors = self._embed_many(texts, task_type)
                if len(vectors) != len(texts):
                    raise RuntimeError(
                        f"_embed_many returned {len(vectors)} vectors "
                        f"for {len(texts)} texts"
                    )
                break
            except Exception as e:  # noqa: BLE001
                if isinstance(e, self.PROPAGATE):
                    raise
                vectors = None
                err = f"{type(e).__name__}: {str(e)[:200]}"
                if isinstance(e, self.NON_RETRYABLE):
                    refused = True
                    logger.warning(
                        "embed_refused batch=%d err=%s", len(texts), err,
                    )
                    break
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 1, 2, 4 seconds
                    continue
                logger.warning(
                    "embed_batch_failed size=%d attempts=%d err=%s "
                    "— retrying per chunk",
                    len(texts), attempt + 1, err,
                )

        if vectors is None and not refused:
            return [
                self._embed_single(
                    chunk_id, redacted or "", rstats, task_type, repo,
                    max_retries, workspace_id, operation,
                )
                for chunk_id, redacted, rstats, _ in sendable
            ]

        results: list[EmbeddingResult] = []
        for position, (chunk_id, redacted, rstats, _) in enumerate(sendable):
            vec = vectors[position] if vectors is not None else []
            # One audit record PER TEXT, batched wire or not: the accounting
            # contract (see src/llm/completion._seam_embed) counts texts, and
            # a per-request record would shrink a tenant's visible usage 64×
            # the day batching landed.
            with self.audit.track(
                mode="embedding",
                model=model,
                operation=operation,
                workspace_id=workspace_id,
                repo=repo,
                extra={"task_type": task_type, "chunk_id": chunk_id},
            ) as record:
                record.redaction = rstats
                if err and not vec:
                    record.error = err
                    record.input_tokens_estimated = len(redacted or "") // 4
                else:
                    record.input_tokens_estimated = len(redacted or "") // 4
                    record.output_tokens_estimated = 0  # embeddings not generative
            results.append(EmbeddingResult(
                chunk_id=chunk_id,
                vector=vec,
                dimensions=self._dimensions_for(vec),
                model=model,
                redaction_stats=rstats,
                error=err if not vec else None,
            ))
        return results


# ─── OpenAI-compatible (local / self-hosted) ───────────────────────


class OpenAICompatibleEmbedder(_BaseEmbedder):
    """POST {base_url}/embeddings — the shape every local server already speaks.

    Ollama, vLLM, llama.cpp's server, HuggingFace TEI and Infinity all expose
    this route. Pointing `embedding_base_url` at one of them is the entire
    integration; there is deliberately no per-vendor subclass, because the
    differences between them (auth token or not, `dimensions` honoured or
    ignored) are settings, not code.

    Asymmetric retrieval: there is no `task_type` field in the OpenAI schema,
    so the models that need document/query differentiation take it as a literal
    text prefix (`search_document: ` / `search_query: ` for nomic-embed-text,
    `query: ` for e5). Those are settings — an empty prefix is correct for the
    models that don't want one, and silently wrong-but-working for the ones
    that do, which is why they are written down rather than guessed.
    """

    #: The allowlist saying no is a decision, not a hiccup.
    NON_RETRYABLE = (EgressBlockedError,)

    #: A declared width the server contradicts is wrong for every chunk —
    #: it is raised out of the run instead of being recorded 9,000 times.
    PROPAGATE = (EmbeddingConfigMismatch,)

    #: The one provider path whose schema takes a list — so it takes a list.
    batch_size = EMBED_BATCH_SIZE

    #: …and the one whose transport can carry several at once: `httpx.Client`
    #: is thread-safe, so the lanes share the client and its connection pool
    #: rather than each opening their own.
    max_concurrent_batches = EMBED_CONCURRENCY

    def __init__(
        self,
        settings: Settings | None = None,
        redactor: Redactor | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        super().__init__(settings=settings, redactor=redactor, audit=audit)
        self._client = None
        # The client is built on first use and SHARED by every lane, so the
        # building itself has to happen once. Without this lock four threads
        # starting together each build one, three get dropped on the floor
        # holding an open connection pool, and the guarded client the run
        # actually uses is decided by a race.
        self._client_lock = threading.Lock()
        # Once-per-instance width check (see _assert_declared_width): the
        # configuration cannot change under a live instance, so the first
        # vector answers for all of them.
        self._width_checked = False
        base = (self.settings.embedding_base_url or "").rstrip("/")
        if not base:
            raise ValueError(
                "embedding_provider='openai_compatible' needs EMBEDDING_BASE_URL "
                "(e.g. http://127.0.0.1:11434/v1 for Ollama)"
            )
        if not self.settings.embedding_model:
            raise ValueError(
                "embedding_provider='openai_compatible' needs EMBEDDING_MODEL "
                "(e.g. nomic-embed-text)"
            )
        self.endpoint = f"{base}/embeddings"

    def _get_client(self):
        # Taken unconditionally rather than double-checked: an uncontended
        # lock costs nothing next to an HTTP round trip, and double-checked
        # locking is a pattern that has to be re-argued every time somebody
        # reads it.
        with self._client_lock:
            if self._client is None:
                # Through the egress whitelist ON PURPOSE. The local backend is
                # the one that claims to send nothing outward, so it is the one
                # that must be provably subject to the check rather than exempt
                # from it.
                from src.security.egress import build_http_client

                self._client = build_http_client(
                    allowed_hosts=self.settings.egress_allowed_hosts,
                    timeout=float(self.settings.embedding_timeout_seconds),
                    allow_private_network=self.settings.egress_allow_private_network,
                )
            return self._client

    @property
    def model(self) -> str:
        return self.settings.embedding_model

    @property
    def declared_dimensions(self) -> int:
        return self.settings.embedding_dimensions

    @property
    def task_document(self) -> str:
        return "RETRIEVAL_DOCUMENT"

    @property
    def task_query(self) -> str:
        return "RETRIEVAL_QUERY"

    def _dimensions_for(self, vector: list[float]) -> int:
        # `embedding_dimensions` may be 0 ("whatever the server returns"), so
        # the truth is the vector in hand.
        return len(vector) if vector else self.declared_dimensions

    def _prefix_for(self, task_type: str) -> str:
        if task_type == self.task_query:
            return self.settings.embedding_query_prefix
        return self.settings.embedding_document_prefix

    def _embed_one(self, text: str, task_type: str) -> list[float]:
        return self._post_embeddings([text], task_type)[0]

    def _embed_many(self, texts: list[str], task_type: str) -> list[list[float]]:
        return self._post_embeddings(texts, task_type)

    def _post_embeddings(self, texts: list[str], task_type: str) -> list[list[float]]:
        """ONE request for the whole list.

        Measured before batching: 20 chunks cost 20 HTTP round-trips against
        a schema that took a list all along. The prefix is applied to every
        text — the side (document/query) is a property of the call, not of
        the batch size.
        """
        prefix = self._prefix_for(task_type)
        payload: dict = {
            "model": self.model,
            "input": [prefix + text for text in texts],
        }
        # Only sent when explicitly configured: Matryoshka truncation is
        # honoured by vLLM and ignored by others, and an unasked-for field is
        # how you get a 400 from the strict ones.
        if self.settings.embedding_dimensions:
            payload["dimensions"] = self.settings.embedding_dimensions

        headers = {"Content-Type": "application/json"}
        key = self.settings.embedding_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        resp = self._get_client().post(self.endpoint, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"embeddings endpoint returned {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        data = body.get("data") or []
        if len(data) != len(texts):
            raise RuntimeError(
                f"embeddings response has {len(data)} vectors for "
                f"{len(texts)} inputs: {str(body)[:200]}"
            )

        entries: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise RuntimeError(f"malformed embeddings response: {str(body)[:200]}")
            vector = item["embedding"]
            if not isinstance(vector, list) or not vector:
                # base64 `encoding_format` would land here; we never request it, so
                # this means the server answered something we should not store.
                raise RuntimeError(
                    f"embeddings response is not a float vector: {type(vector)}"
                )
            # The schema ties each vector to its input by `index` and promises
            # nothing about order; a server that omits it keeps its wire order
            # (the sort below is stable and `position` stands in).
            index = item.get("index")
            entries.append((
                index if isinstance(index, int) else position,
                [float(x) for x in vector],
            ))
        entries.sort(key=lambda pair: pair[0])
        vectors = [vector for _index, vector in entries]

        if vectors:
            self._assert_declared_width(vectors[0])
        return vectors

    def _assert_declared_width(self, vector: list[float]) -> None:
        """The declared width, checked against the first vector that arrives.

        History of the bug this guards: one wrong digit in EMBEDDING_DIMENSIONS
        (1024 declared, 768-wide model) built a 1024-wide Qdrant collection,
        the server kept answering 768, every upsert was rejected — and the
        vault writer records each rejection as a warning, so the run ended
        green over an EMPTY index. The declared value used to be reported as
        truth without ever meeting a vector.

        Once per instance, by flag: configuration cannot change between chunk
        12 and chunk 13, and every vector in one model's output is as wide as
        the first.

        The flag is read and written from several lanes without a lock, and
        that is safe in the only direction that matters. Two threads that both
        arrive before it is set both compare against the same declared value
        and reach the same verdict — the worst case is one redundant `len()`,
        never a mismatch that goes unnoticed, because the flag can only ever
        be set on the path where the widths agreed.
        """
        if self._width_checked:
            return
        declared = self.settings.embedding_dimensions
        actual = len(vector)
        if declared and actual != declared:
            raise EmbeddingConfigMismatch(
                f"EMBEDDING_DIMENSIONS={declared} but {self.model!r} returned a "
                f"{actual}-wide vector. Set EMBEDDING_DIMENSIONS={actual} and "
                f"re-index — a collection created at width {declared} silently "
                f"rejects every {actual}-wide point."
            )
        self._width_checked = True


# ─── selection ─────────────────────────────────────────────────────


def get_embedder(
    settings: Settings | None = None,
    redactor: Redactor | None = None,
    audit: AuditLogger | None = None,
) -> Embedder:
    """The embedder for an install that has moved OFF Gemini.

    Not "the configured embedder, defaulting to Gemini" — that is what the
    docstring said while the caller made it untrue.
    `completion._configured_embedder()` returns None for "gemini" and never
    calls this, so the default value has always meant "do not ask this module".

    Deliberately NOT cached: the implementation holds a network client, and a
    process-wide singleton keyed on nothing is how a settings change stops
    taking effect until restart.
    """
    s = settings or get_settings()
    provider = (s.embedding_provider or "").strip()
    if provider == "openai_compatible":
        return OpenAICompatibleEmbedder(settings=s, redactor=redactor, audit=audit)
    if provider == "gemini":
        # Refuse, rather than resurrect the class that used to answer here.
        # Gemini embeddings are not this module's business: they carry a
        # workspace profile, a per-workspace key, a gateway route and a spend
        # ledger row, and go out through `src.llm.completion.embed`. An
        # embedder handed back from here would have none of those and would
        # still produce plausible vectors — which is the failure that does not
        # announce itself, because a wrong-but-working embedding is only
        # visible as worse search results months later.
        raise ValueError(
            "embedding_provider='gemini' does not resolve to an embedder here: "
            "Gemini embeddings go through src.llm.completion.embed / "
            "embed_batch, which applies the workspace profile, key, gateway "
            "route and spend ledger. This module is the off-Google seam, and "
            "reaching it with 'gemini' means a caller skipped that door."
        )
    # Not a typo guard — `Settings.known_embedding_provider` (src/config.py)
    # already rejects an unknown value at construction, so nothing misspelt
    # reaches this line. This catches the OTHER way the selector goes wrong:
    # a provider added to config.EMBEDDING_PROVIDERS without a branch here.
    # The old `else: GeminiEmbedder` fallthrough would have accepted it and
    # sent the customer's source code to Google under the new provider's name.
    # Indexing is the one call that ships source code, so the selector refuses
    # rather than picks.
    raise ValueError(
        f"embedding_provider={provider!r} passes config validation but has no "
        "implementation in this module — a provider was added to "
        "config.EMBEDDING_PROVIDERS without one. Refusing rather than falling "
        "back to Gemini: this is the call that sends source code off the box."
    )
