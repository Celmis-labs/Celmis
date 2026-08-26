"""A lease has to mean "a worker is alive on this", not "we guessed ten minutes".

`dequeue_one` stamped `locked_until = now + CELMIS_JOB_LEASE_SECONDS` once and
nothing ever moved it. Past that instant the reclaim at the top of
`dequeue_one` handed the row to the next worker — while the first was still
inside the handler. With `CELMIS_SYNC_WORKER_CONCURRENCY` at its default of 2,
"the next worker" is a live sibling loop in the same process, so the job did
not merely look abandoned: it was picked up and run a second time.

Measured on this install, across 517 real reviews: 3.5% ran longer than the
600-second default (p99 1486s, max 1578s). About one review in twenty-nine was
paid for twice, wrote two run rows, and raced itself to post comments on one
pull request. The bench harness saw the symptom from outside — "PR already
queued" for jobs its own depth gauge reported as an empty queue — and read it
as a provider problem.

TWO DEFECTS, AND THE SECOND HID THE FIRST.

`_worker_id()` was `hostname-pid`. Every worker loop in one container shares
both, so the string answered "which process holds this job" — a question
nobody asked — while the question it is named for had no answer. That made
every `locked_by = :w` guard vacuous between siblings: when loop 1 reclaimed
loop 0's expired lease the owner string did not change, so loop 0 could go on
renewing, and later completing, a job loop 1 was by then running. A guard that
cannot fail is not a guard, and it is exactly the guard the renewal needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

import src.sync.queue as jq
import src.sync.worker as worker_mod

# ─── a worker id names a worker ──────────────────────────────────────


def test_two_slots_in_one_process_are_two_workers():
    """The whole point: same host, same pid, different worker."""
    ids = []
    for slot in (0, 1):
        jq.set_worker_slot(slot)
        ids.append(jq._worker_id())
    assert len(set(ids)) == 2, f"both loops call themselves {ids[0]!r}"


def test_an_unset_slot_still_yields_an_id():
    """Anything calling the queue outside a worker loop — the API process
    enqueuing, a script, a test — must not crash for want of a slot."""
    jq._SLOT.set(None)
    assert jq._worker_id()


def test_the_slot_is_per_task_not_per_process():
    """The loops are asyncio tasks sharing one interpreter. A module global
    would have the last loop to start overwrite every earlier one."""
    seen: dict[int, str] = {}

    async def _loop(slot: int) -> None:
        jq.set_worker_slot(slot)
        await asyncio.sleep(0)  # let the sibling run and set its own
        seen[slot] = jq._worker_id()

    asyncio.run(_asyncio_both(_loop))
    assert seen[0] != seen[1], seen


async def _asyncio_both(fn):
    await asyncio.gather(fn(0), fn(1))


def test_the_worker_loop_claims_its_slot():
    """The setter is useless if nothing calls it. Read off the source's AST so
    a renamed local or a reordered line cannot quietly satisfy it."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(worker_mod._worker_loop))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "set_worker_slot"
    ]
    assert calls, "_worker_loop never tells the queue which loop it is"


# ─── the renewal ─────────────────────────────────────────────────────


class _Result:
    def __init__(self, rowcount): self.rowcount = rowcount


class _Conn:
    def __init__(self, rowcount=1):
        self.rowcount, self.statements, self.params = rowcount, [], []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        self.params.append(params or {})
        return _Result(self.rowcount)

    def __enter__(self): return self

    def __exit__(self, *a): return False


class _Engine:
    def __init__(self, conn): self._conn = conn

    def begin(self): return self._conn


@pytest.fixture
def conn(monkeypatch):
    c = _Conn()
    monkeypatch.setattr(jq, "_engine", lambda: _Engine(c))
    return c


def test_renewing_moves_the_deadline_forward(conn):
    jq.set_worker_slot(0)
    assert jq.renew_lease("job-1") is True
    sql = " ".join(conn.statements[0].split())
    assert "SET locked_until" in sql
    assert conn.params[0]["id"] == "job-1"


def test_the_renewal_only_touches_a_row_this_worker_owns(conn):
    """Without `locked_by` in the WHERE, a worker that already lost the row
    would take it back from whoever is running it now — turning the fix into a
    louder version of the bug."""
    jq.set_worker_slot(1)
    jq.renew_lease("job-1")
    sql = " ".join(conn.statements[0].split())

    assert "locked_by = :w" in sql
    assert "status = 'running'" in sql, "a finished job is not ours to renew"
    assert conn.params[0]["w"] == jq._worker_id()


def test_losing_the_row_is_reported_not_swallowed(monkeypatch):
    c = _Conn(rowcount=0)
    monkeypatch.setattr(jq, "_engine", lambda: _Engine(c))
    assert jq.renew_lease("job-1") is False


# ─── the heartbeat ───────────────────────────────────────────────────


#: Captured before anything monkeypatches the name — a stand-in for
#: `asyncio.sleep` that calls the patched `asyncio.sleep` is an infinite
#: recursion, which is exactly what the first draft of this file did.
_REAL_SLEEP = asyncio.sleep


@pytest.fixture
def beat(monkeypatch):
    """`_hold_lease` with the clock and the queue call replaced, so the test
    runs in milliseconds rather than in lease-widths."""
    calls: list[str] = []

    async def _instant(_delay):
        await _REAL_SLEEP(0)

    monkeypatch.setattr(jq, "lease_seconds", lambda: 30.0)
    monkeypatch.setattr(asyncio, "sleep", _instant)

    def _install(outcomes):
        seq = list(outcomes)

        def _renew(job_id):
            calls.append(job_id)
            out = seq.pop(0) if seq else True
            if isinstance(out, Exception):
                raise out
            return out

        monkeypatch.setattr(jq, "renew_lease", _renew)
        return calls

    return _install


def _beat_a_few_times(coro, *, until: int = 1, calls=None, ticks: int = 2000):
    """Let the heartbeat run until it has beaten `until` times, then stop it.

    COUNTING EVENT-LOOP TURNS IS NOT COUNTING HEARTBEATS. This yielded a fixed
    twelve times and assumed three renewals had happened by then — true on an
    idle laptop and false on a shared runner, where the same twelve turns go to
    somebody else's work. The suite was green here and red there, and the
    failure said "it gave up on the first hiccup" about code that had not given
    up on anything.

    Waiting for the OBSERVABLE EVENT instead makes the test independent of how
    the machine schedules: it stops as soon as the renewals it is about have
    happened, and the tick budget is only a backstop so a genuinely stuck beat
    fails instead of hanging. It is an endless loop by design; the test is what
    it did on the way.
    """

    async def _main():
        task = asyncio.create_task(coro)
        for _ in range(ticks):
            if task.done():
                break
            if calls is not None and len(calls) >= until:
                break
            await _REAL_SLEEP(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_main())


def test_the_beat_keeps_renewing_while_the_handler_runs(beat):
    calls = beat([True, True, True])
    _beat_a_few_times(worker_mod._hold_lease("job-1", "review"),
                      until=2, calls=calls)
    assert len(calls) >= 2, "one renewal is a stamp, not a heartbeat"


def test_a_failed_renewal_does_not_end_the_beat(beat):
    """A database blip is not a lost lease. Renewing at a third of the width
    is what buys the retries — two may be lost before anything reclaims."""
    calls = beat([RuntimeError("connection reset"), True, True])
    _beat_a_few_times(worker_mod._hold_lease("job-1", "review"),
                      until=3, calls=calls)
    assert len(calls) >= 3, "it gave up on the first hiccup"


def test_losing_the_lease_stops_the_beat_and_says_so(beat, caplog):
    calls = beat([False])
    with caplog.at_level(logging.ERROR, logger=worker_mod.logger.name):
        _beat_a_few_times(worker_mod._hold_lease("job-1", "review"))

    assert len(calls) == 1, "it kept renewing a row it no longer owns"
    assert any("job_lease_lost" in r.getMessage() for r in caplog.records), (
        "the duplicate run is the one thing that must never be silent"
    )


def test_the_beat_paces_itself_from_the_lease_not_a_literal():
    """A second copy of the number is a second thing to forget to change."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(worker_mod._hold_lease))
    assert any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "lease_seconds"
        for n in ast.walk(tree)
    ), "the interval is not derived from the configured lease width"


# ─── and it always stops ─────────────────────────────────────────────


@pytest.fixture
def process(monkeypatch):
    """`_process` with the queue writes recorded and one handler we control."""
    seen: dict[str, object] = {"beats": []}

    class _Beat:
        def __init__(self): self.cancelled = False

        def cancel(self): self.cancelled = True

    def _create_task(coro):
        coro.close()  # never actually run the heartbeat here
        b = _Beat()
        seen["beats"].append(b)
        return b

    monkeypatch.setattr(worker_mod.asyncio, "create_task", _create_task)
    monkeypatch.setattr(worker_mod.asyncio, "to_thread",
                        lambda fn, *a, **kw: _done(fn.__name__))
    seen["marks"] = []

    async def _done(name):
        seen["marks"].append(name)

    def _run_with(handler):
        monkeypatch.setitem(worker_mod._HANDLERS, "review", handler)
        asyncio.run(worker_mod._process(
            {"id": "j-1", "kind": "review", "attempts": 1, "max_attempts": 3}))
        return seen

    return _run_with


def test_the_beat_stops_when_the_handler_returns(process):
    async def _ok(job): return None

    seen = process(_ok)
    assert seen["beats"] and all(b.cancelled for b in seen["beats"])


def test_the_beat_stops_when_the_handler_raises(process):
    async def _boom(job): raise RuntimeError("nope")

    seen = process(_boom)
    assert seen["beats"] and all(b.cancelled for b in seen["beats"]), (
        "a heartbeat outliving its job holds a lease on a row nobody is "
        "working — the same lie, pointing the other way"
    )


def test_the_beat_stops_when_the_job_is_cancelled(process):
    async def _cancel(job): raise jq.JobCancelled("by user")

    seen = process(_cancel)
    assert seen["beats"] and all(b.cancelled for b in seen["beats"])
