"""Batches now leave several at a time — and everything else is unchanged.

Batching cut the request COUNT (20 chunks stopped being 20 POSTs). What it
could not touch is the wall clock: batch N+1 still sat idle while batch N
crossed the network, so a repository of thousands of chunks paid every round
trip end to end. `_flush_concurrently` overlaps a bounded number of them on a
thread pool over the SAME sync guarded client.

Concurrency is where quiet wrongness comes from, so this file is written as
the list of things that must survive it, one test each:

  * input ORDER — results are re-joined in submission order, and the server
    here answers the earliest batch slowest so that completion order and
    submission order genuinely disagree;
  * one audit record PER TEXT, carrying the tenant;
  * retry/backoff per batch, and the batch → single-text fallback that keeps
    one poison chunk from taking its 63 neighbours down;
  * the declared-vs-actual width check PROPAGATING out of the run — a
    per-thread swallow into one chunk's `error` field would be the original
    green-run-empty-index bug wearing a thread pool;
  * a NON_RETRYABLE egress refusal still not falling back to per-item retries;
  * the batch_size=1 providers (the base class, which is what the Gemini-era
    machinery is) untouched, on the calling thread, one after another.

And two claims about the shape of the thing. It is BOUNDED in both senses that
matter — never more than `max_concurrent_batches` requests in flight, and never
more than a few windows' worth of redacted text read ahead of what has been
yielded, which is the half a pool's `max_workers` does not give you. And at
`max_concurrent_batches == 1` it is today's code: no pool is built at all and
every send happens on the caller's own thread.

The measurement is at the bottom. Complexity that does not buy time should be
deleted rather than explained, so the speedup is timed against a server with a
real delay rather than asserted in a comment.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import Settings
from src.indexing.vectors.embedder import (
    EMBED_CONCURRENCY,
    OpenAICompatibleEmbedder,
    _BaseEmbedder,
)
from src.llm.gateway import EmbeddingConfigMismatch
from src.security.audit import AuditLogger
from src.security.egress import EgressBlockedError
from src.security.redactor import Redactor

DIMS = 8

#: The real one, captured before any test can monkeypatch `time.sleep` on the
#: module object. The retry tests below patch the embedder's backoff away, and
#: that patch lands on the shared `time` module — the fake server must keep
#: its own latency or those tests would silently stop being about waiting.
_SLEEP = time.sleep


# ─── a real embeddings server that takes real time ──────────────────


def _index_in(text: str) -> int:
    """The chunk number the text carries, e.g. 'text 41' → 41."""
    return int(re.findall(r"\d+", text)[-1])


class _Handler(BaseHTTPRequestHandler):
    """One vector per input, and the vector says WHICH input it answers.

    Vector for "text 41" is [41.0] * DIMS. That makes a reordering bug show up
    as wrong values rather than as a count that happens to match — the failure
    mode being guarded is a chunk being stored under its neighbour's vector,
    which is valid, wrong, and invisible until search quality rots.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        inputs = body.get("input") or [""]
        server = self.server

        with server.lock:
            server.requests.append(inputs)
            server.in_flight += 1
            server.peak_in_flight = max(server.peak_in_flight, server.in_flight)
        try:
            _SLEEP(server.delay(inputs))
            status, payload = server.answer(server, inputs)
        finally:
            with server.lock:
                server.completions.append(inputs)
                server.in_flight -= 1

        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence the stderr spam
        pass


def _vectors(server, inputs):
    return 200, {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i,
             "embedding": [float(_index_in(text))] * server.width}
            for i, text in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


@pytest.fixture
def local_server():
    """A real HTTP server on loopback — threaded, so it can actually answer
    several lanes at once. Mocking the transport would make every timing
    number in this file a measurement of the mock."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.lock = threading.Lock()
    server.requests = []          # inputs of each request, in ARRIVAL order
    server.completions = []       # …and in the order they were ANSWERED
    server.in_flight = 0
    server.peak_in_flight = 0
    server.width = DIMS
    server.delay = lambda inputs: 0.0
    server.answer = _vectors
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def _settings(server, **overrides) -> Settings:
    host, port = server.server_address[0], server.server_address[1]
    base = {
        "embedding_provider": "openai_compatible",
        "embedding_base_url": f"http://{host}:{port}/v1",
        "embedding_model": "nomic-embed-text",
        "embedding_dimensions": DIMS,
        "embedding_timeout_seconds": 10,
        "egress_allowed_hosts": [],
        "egress_allow_private_network": True,
    }
    base.update(overrides)
    return Settings(**base)


class _Recording(OpenAICompatibleEmbedder):
    """The real embedder, plus a note of which thread each send happened on.

    Which thread is the subject here, not an implementation detail: "no pool
    at lanes=1" and "several lanes at lanes=4" are only checkable from the
    client side, because the server sees a thread per connection either way.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sender_threads: list[threading.Thread] = []
        self.single_calls = 0
        self.batch_calls = 0

    def _post_embeddings(self, texts, task_type):
        self.sender_threads.append(threading.current_thread())
        return super()._post_embeddings(texts, task_type)

    def _embed_one(self, text, task_type):
        self.single_calls += 1
        return super()._embed_one(text, task_type)

    def _embed_many(self, texts, task_type):
        self.batch_calls += 1
        return super()._embed_many(texts, task_type)


def _embedder(server, audit, *, lanes=None, batch=None, **overrides) -> _Recording:
    emb = _Recording(
        settings=_settings(server, **overrides), audit=audit,
        redactor=Redactor(fail_closed=True),
    )
    if lanes is not None:
        emb.max_concurrent_batches = lanes
    if batch is not None:
        emb.batch_size = batch
    return emb


def _chunks(n: int) -> list[tuple[str, str]]:
    return [(f"c{i}", f"text {i}") for i in range(n)]


# ─── 1. order survives, even when completion order disagrees ────────


def test_results_come_back_in_input_order_when_the_first_batch_is_slowest(
    local_server, audit,
):
    """The one thing concurrency is most likely to break, arranged so that
    trusting completion order cannot accidentally pass.

    The window holding chunk 0 takes 200ms and every other window takes none,
    so the lanes behind it finish long before it does. An implementation that
    yielded as futures completed would hand chunk 0's caller chunk 8's vector
    — and `_seam_embed` zips these straight into Qdrant points.

    Arrival order at the server is not asserted, because it is exactly what
    this change gave up: four lanes race to the socket and the winner varies
    run to run. Completion order is the deterministic part, and it is the part
    the result order must not follow."""
    def slow_first(inputs):
        return 0.2 if any(_index_in(t) == 0 for t in inputs) else 0.0

    local_server.delay = slow_first
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    results = list(emb.embed_documents(_chunks(40)))

    assert [r.chunk_id for r in results] == [f"c{i}" for i in range(40)]
    # The values prove the pairing, not just the count.
    assert [r.vector[0] for r in results] == [float(i) for i in range(40)]
    assert all(r.error is None for r in results)
    # And the arrangement really did invert the order: chunk 0's window was
    # answered after the three that were sent alongside it, and its results
    # still came out first.
    answered = [_index_in(inputs[0]) for inputs in local_server.completions]
    assert answered.index(0) >= 3, (
        f"the slow window did not actually finish late ({answered}) — this "
        f"test would pass on completion order too"
    )


def test_the_two_lane_counts_produce_identical_results(local_server, audit):
    """Same inputs, same vectors, same order, whether or not a pool is used.
    Concurrency is supposed to be invisible in the output — this is that
    sentence as an assertion."""
    serial = list(_embedder(local_server, audit, lanes=1, batch=8)
                  .embed_documents(_chunks(40)))
    parallel = list(_embedder(local_server, audit, lanes=4, batch=8)
                    .embed_documents(_chunks(40)))

    assert [(r.chunk_id, r.vector, r.error) for r in serial] == \
           [(r.chunk_id, r.vector, r.error) for r in parallel]


def test_a_redaction_failure_still_holds_its_place_in_a_concurrent_run(
    local_server, audit, monkeypatch,
):
    """A chunk that never reaches the transport is buffered into the window
    rather than yielded early, and the pool must not shuffle that. Chunk 5 is
    poisoned at the redactor: it must come back FIFTH, failed, with its
    neighbours intact around it."""
    emb = _embedder(local_server, audit, lanes=4, batch=4)
    real = emb.redactor.redact

    def refuse_chunk_five(text, *args, **kwargs):
        if _index_in(text) == 5:
            raise RuntimeError("boom")
        return real(text, *args, **kwargs)

    monkeypatch.setattr(emb.redactor, "redact", refuse_chunk_five)
    results = list(emb.embed_documents(_chunks(20)))

    assert [r.chunk_id for r in results] == [f"c{i}" for i in range(20)]
    assert "redaction_failed" in (results[5].error or "")
    assert [i for i, r in enumerate(results) if r.error] == [5]
    assert [r.vector[0] for r in results if not r.error] == \
           [float(i) for i in range(20) if i != 5]


# ─── 2. the audit trail is per text, and per tenant ─────────────────


def test_a_concurrent_run_writes_one_audit_record_per_text(local_server, audit):
    """The accounting contract (src/llm/completion._seam_embed documents it)
    counts TEXTS, not requests and not lanes. It survived batching; it has to
    survive the pool, which is also the check that four threads appending to
    one JSONL do not produce a torn line."""
    emb = _embedder(local_server, audit, lanes=4, batch=8)
    n = 40

    list(emb.embed_documents(_chunks(n), repo="acme/repo",
                             workspace_id="ws-acme", operation="embed_batch"))

    lines = audit.log_path.read_text().splitlines()
    assert len(lines) == n, "one record per text, not per request and not per lane"
    records = [json.loads(line) for line in lines]  # torn lines fail here
    assert {r["workspace_id"] for r in records} == {"ws-acme"}, (
        "an unattributed record is visible to global admins only — a tenant "
        "whose indexing ran on four threads would lose its Usage page"
    )
    assert {r["mode"] for r in records} == {"embedding"}
    assert {r["repo"] for r in records} == {"acme/repo"}
    assert {r["extra"]["chunk_id"] for r in records} == {f"c{i}" for i in range(n)}


# ─── 3. retry, and the fallback for a poison chunk ──────────────────


def test_a_batch_that_fails_twice_is_retried_on_its_own_lane(
    local_server, audit, monkeypatch,
):
    """Backoff is per batch and stays per batch: the window holding chunk 0
    is refused twice and then succeeds, while the other lanes carry on. A
    retry that had become global would have re-sent every window."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # the ladder, not the wait
    seen = {"n": 0}

    def flaky_first(server, inputs):
        if any(_index_in(t) == 0 for t in inputs):
            with server.lock:
                seen["n"] += 1
                attempt = seen["n"]
            if attempt <= 2:
                return 500, {"error": "server hiccup"}
        return _vectors(server, inputs)

    local_server.answer = flaky_first
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    results = list(emb.embed_documents(_chunks(40), max_retries=3))

    assert [r.error for r in results] == [None] * 40
    assert [r.vector[0] for r in results] == [float(i) for i in range(40)]
    # 5 windows + the two refused attempts, and nothing else was re-sent.
    assert len(local_server.requests) == 7


def test_one_poison_chunk_falls_back_per_text_without_taking_its_window(
    local_server, audit, monkeypatch,
):
    """A batch fails as a unit — one over-long text 400s the whole request —
    so losing 7 good chunks to one bad neighbour would break the per-chunk
    isolation the module promises. The fallback runs inside the lane that
    owns the window, so the other lanes never notice."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    def reject_batches_holding_thirteen(server, inputs):
        if len(inputs) > 1 and any(_index_in(t) == 13 for t in inputs):
            return 400, {"error": "input too long"}
        if len(inputs) == 1 and _index_in(inputs[0]) == 13:
            return 400, {"error": "input too long"}
        return _vectors(server, inputs)

    local_server.answer = reject_batches_holding_thirteen
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    results = list(emb.embed_documents(_chunks(40), max_retries=1))

    assert [r.chunk_id for r in results] == [f"c{i}" for i in range(40)]
    assert [i for i, r in enumerate(results) if r.error] == [13]
    assert [r.vector[0] for r in results if not r.error] == \
           [float(i) for i in range(40) if i != 13]
    # The fallback is per TEXT and only for the window that failed: 8 single
    # sends, not 40.
    assert emb.single_calls == 8


# ─── 4. the width check propagates out of the pool ──────────────────


def test_a_width_mismatch_raises_out_of_a_concurrent_run(local_server, audit):
    """The bug this guards is the one where every error became a warning: a
    1024-wide collection, a 768-wide model, every upsert rejected, the run
    green over an empty index.

    A thread pool is a brand-new way to reproduce it — an exception raised on
    a lane and filed into that window's results would look exactly like a
    per-chunk failure. `pytest.raises` around `list()` is the whole point."""
    local_server.width = 4  # the model contradicts EMBEDDING_DIMENSIONS=8
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    with pytest.raises(EmbeddingConfigMismatch) as excinfo:
        list(emb.embed_documents(_chunks(80)))

    message = str(excinfo.value)
    assert "8" in message and "4" in message, "both numbers, or it is not a diagnosis"
    assert "EMBEDDING_DIMENSIONS" in message


def test_the_mismatch_stops_the_run_instead_of_draining_the_windows(
    local_server, audit,
):
    """Ten windows are queued; the answer is known after the first four are in
    flight. Whatever has not been submitted is cancelled, so a misconfigured
    server is not handed the rest of the repository while the exception
    travels."""
    local_server.width = 4
    emb = _embedder(local_server, audit, lanes=EMBED_CONCURRENCY, batch=8)

    with pytest.raises(EmbeddingConfigMismatch):
        list(emb.embed_documents(_chunks(80)))

    assert len(local_server.requests) <= EMBED_CONCURRENCY, (
        f"the run kept sending after the config was known wrong: "
        f"{len(local_server.requests)} requests"
    )


# ─── 5. a refusal is a decision, on any number of lanes ─────────────


def test_an_egress_refusal_does_not_become_per_item_retries(local_server, audit):
    """The allowlist saying no to a batch would say no to each of its texts
    too, 8 times, slower. NON_RETRYABLE means the window is refused as a
    window — and putting the refusal on four threads must not turn it into
    four times as many refusals."""
    emb = _embedder(local_server, audit, lanes=4, batch=4,
                    egress_allow_private_network=False)

    results = list(emb.embed_documents(_chunks(12), max_retries=3))

    assert len(results) == 12
    assert all("EgressBlockedError" in (r.error or "") for r in results)
    assert emb.single_calls == 0, "a policy decision fell back to per-item retries"
    assert emb.batch_calls == 3, "one refused attempt per window, no backoff ladder"
    assert local_server.requests == [], "nothing reached the server"


def test_the_refusal_type_is_still_what_callers_catch(local_server, audit):
    """The result carries the name, and the exception class is unchanged by
    travelling through a future."""
    emb = _embedder(local_server, audit, lanes=4, batch=4,
                    egress_allow_private_network=False)
    with pytest.raises(EgressBlockedError):
        emb._post_embeddings(["text 0"], emb.task_document)
    assert list(emb.embed_documents(_chunks(8)))[0].error.startswith(
        "EgressBlockedError",
    )


# ─── 6. bounded, and 1 is today ─────────────────────────────────────


def test_the_pool_never_exceeds_its_lane_count(local_server, audit):
    """Bounded is the property that keeps this safe to ship: a local model
    server is easy to overwhelm and a hosted one answers 429. Twenty windows
    go through a four-lane pool and the server must never see a fifth request
    in flight, however far into the run it is."""
    local_server.delay = lambda inputs: 0.02
    emb = _embedder(local_server, audit, lanes=EMBED_CONCURRENCY, batch=4)

    list(emb.embed_documents(_chunks(80)))

    assert len(local_server.requests) == 20
    assert local_server.peak_in_flight <= EMBED_CONCURRENCY, (
        f"unbounded: {local_server.peak_in_flight} requests in flight at once"
    )
    assert local_server.peak_in_flight > 1, "nothing actually overlapped"


def test_the_run_reads_only_a_few_windows_ahead_of_what_it_has_yielded(
    local_server, audit,
):
    """The other half of "bounded", and the half `max_workers` does not give.

    A pool caps how many requests are IN FLIGHT no matter how much is handed
    to it — everything extra just queues inside the executor, holding its
    redacted texts. So an implementation that submitted every window up front
    would still pass the in-flight assertion above while reading a
    hundred-thousand-chunk repository into memory, redacted, before yielding
    result one.

    Here the chunks arrive from a generator that counts what has been pulled,
    and only ONE result is taken. `_flush_concurrently` iterates its windows
    lazily and stops at a full queue, so the read-ahead is a handful of
    windows and not the repository."""
    pulled = {"n": 0}

    def counted_chunks():
        for pair in _chunks(4000):
            pulled["n"] += 1
            yield pair

    local_server.delay = lambda inputs: 0.01
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    stream = emb.embed_documents(counted_chunks())
    try:
        first = next(stream)
        assert first.chunk_id == "c0"
        # lanes windows queued + the one being read when the queue filled.
        assert pulled["n"] <= (4 + 1) * 8, (
            f"read {pulled['n']} chunks ahead to yield one — the whole input "
            f"is being buffered"
        )
    finally:
        stream.close()  # and the pool shuts down with it


def test_two_lanes_means_two(local_server, audit):
    """The bound is the configured number, not a constant that happens to be
    4 — an install that must serialise harder gets what it asked for."""
    local_server.delay = lambda inputs: 0.02
    emb = _embedder(local_server, audit, lanes=2, batch=4)

    list(emb.embed_documents(_chunks(40)))

    assert local_server.peak_in_flight <= 2


def test_one_lane_is_the_loop_this_module_has_always_run(local_server, audit):
    """`max_concurrent_batches = 1` builds no pool at all: every send happens
    on the caller's own thread, one request in flight, windows arriving in
    input order. That is the code that ran before this change, and it is what
    an install pinned to 1 gets back."""
    local_server.delay = lambda inputs: 0.01
    caller = threading.current_thread()
    emb = _embedder(local_server, audit, lanes=1, batch=8)

    results = list(emb.embed_documents(_chunks(40)))

    assert set(emb.sender_threads) == {caller}, "a worker thread was used at lanes=1"
    assert local_server.peak_in_flight == 1
    # Arrival order is window order — the property the pool deliberately drops.
    assert [_index_in(inputs[0]) for inputs in local_server.requests] == \
           [0, 8, 16, 24, 32]
    assert [r.vector[0] for r in results] == [float(i) for i in range(40)]


def test_more_than_one_lane_really_uses_more_than_one_thread(local_server, audit):
    """The mirror image, so the test above cannot pass by the pool being
    broken rather than skipped."""
    local_server.delay = lambda inputs: 0.02
    caller = threading.current_thread()
    emb = _embedder(local_server, audit, lanes=4, batch=8)

    list(emb.embed_documents(_chunks(40)))

    assert caller not in emb.sender_threads
    assert len(set(emb.sender_threads)) > 1


def test_a_single_window_run_never_builds_a_pool(local_server, audit):
    """Retrieval is one query, one window, and a thread around a single
    request is pure overhead. Both entry points stay on the caller's thread
    when there is nothing to overlap."""
    caller = threading.current_thread()

    emb = _embedder(local_server, audit, lanes=4, batch=64)
    emb.embed_query("how does login work 7")
    assert emb.sender_threads == [caller]

    small = _embedder(local_server, audit, lanes=4, batch=64)
    list(small.embed_documents(_chunks(10)))
    assert small.sender_threads == [caller]


# ─── 7. the batch_size=1 providers are untouched ────────────────────


class _StubEmbedder(_BaseEmbedder):
    """One text in, one vector out — the whole provider surface the base class
    knows about, and the shape every batch_size=1 provider has."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.sent: list[str] = []
        self.threads: list[threading.Thread] = []

    @property
    def model(self) -> str:
        return "stub-embed"

    @property
    def declared_dimensions(self) -> int:
        return DIMS

    @property
    def task_document(self) -> str:
        return "DOC"

    @property
    def task_query(self) -> str:
        return "QUERY"

    def _embed_one(self, text: str, task_type: str) -> list[float]:
        self.threads.append(threading.current_thread())
        self.sent.append(text)
        time.sleep(0.01)  # a round trip, so an overlap would be visible
        return [float(_index_in(text))] * DIMS


def test_a_one_text_per_call_provider_still_sends_one_at_a_time(audit):
    """The default is 1 lane for the same reason the default batch size is 1
    text: it is the value that is correct against every provider, including
    one whose server is single-threaded or whose client is not safe to share.
    Raising it is a per-provider decision, and no provider inherits it."""
    caller = threading.current_thread()
    emb = _StubEmbedder(settings=Settings(), audit=audit,
                        redactor=Redactor(fail_closed=True))
    assert emb.batch_size == 1

    results = list(emb.embed_documents(_chunks(6)))

    assert set(emb.threads) == {caller}, "a batch_size=1 provider got a thread pool"
    assert emb.sent == [f"text {i}" for i in range(6)], "sent in input order"
    assert [r.vector[0] for r in results] == [float(i) for i in range(6)]


# ─── 8. the measurement ─────────────────────────────────────────────


def _time_run(server, audit, lanes: int, *, windows: int, batch: int) -> float:
    server.requests.clear()
    server.peak_in_flight = 0
    emb = _embedder(server, audit, lanes=lanes, batch=batch)
    chunks = _chunks(windows * batch)
    started = time.perf_counter()
    results = list(emb.embed_documents(chunks))
    elapsed = time.perf_counter() - started
    assert [r.vector[0] for r in results] == [float(i) for i in range(len(chunks))]
    assert len(server.requests) == windows
    return elapsed


def test_four_lanes_are_measurably_faster_than_one(local_server, audit, capsys):
    """The number this change exists for, timed rather than argued.

    12 windows against a server that takes 50ms per request. Serially that is
    12 × 50ms of pure waiting; on four lanes it is three rounds of it. The
    threshold is deliberately loose — CI machines are noisy and the point is
    that the win is REAL, not that it is exactly 4×. If this ever fails, the
    honest response is to delete the pool, not to loosen the number further.
    """
    local_server.delay = lambda inputs: 0.05

    serial = _time_run(local_server, audit, 1, windows=12, batch=8)
    parallel = _time_run(local_server, audit, 4, windows=12, batch=8)

    with capsys.disabled():
        print(f"\n  12 windows x 50ms: pool=1 {serial:.3f}s, "
              f"pool=4 {parallel:.3f}s, speedup {serial / parallel:.2f}x")

    assert serial > 0.5, "the fake server stopped being slow; the timing means nothing"
    assert parallel < serial * 0.6, (
        f"four lanes bought nothing: {serial:.3f}s serial vs {parallel:.3f}s"
    )
