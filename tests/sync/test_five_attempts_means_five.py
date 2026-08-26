"""`max_attempts=5` ran the job six times.

Found on a live queue: eight dead `index_repo_full` rows, every one of them
reading `try 6/5` — attempt six of a maximum of five. The Jobs page was not
lying; the queue really did run each of them one extra time.

`claim()` SELECTs the row, then UPDATEs `attempts = attempts + 1`, and returns
the row it read — the count from BEFORE this attempt. The worker hands that
stale number to `mark_failure`, which stops at `attempts >= max_attempts`. So
the fifth run reports four, retries, and only the sixth run reports five.

The same stale number has two more consequences, both of which read as
cosmetic until you notice they come from one cause:

  * the first attempt is logged as `attempt=0` — an attempt that has not
    happened yet, numbered zero;
  * the first backoff is `_BACKOFF_BASE * 2 ** (0 - 1)` — half the base delay,
    so the first retry of a failing job comes sooner than any configuration
    says it should.

For an indexing job the extra run is a wasted clone. For a review job it is an
extra LLM call, billed, on work already known to be failing — which is the
version of this bug that costs money rather than patience.

`claim()` returns the number of THIS attempt now: the first is 1, the fifth is
5, and five means five.
"""
from __future__ import annotations

import pytest

from src.sync import queue as jq


class _Recorder:
    """Stands in for the database, counting what the queue would have done."""

    def __init__(self, max_attempts: int = 5):
        self.max_attempts = max_attempts
        self.attempts = 0          # what the row holds
        self.status = "pending"
        self.runs: list[int] = []  # the attempt number each run was told
        self.delays: list[float] = []

    def claim(self) -> dict | None:
        """The read-then-increment `claim()` does, minus SQL."""
        if self.status != "pending":
            return None
        row = {"id": "j1", "kind": "k", "payload": {},
               "attempts": self.attempts, "max_attempts": self.max_attempts}
        self.attempts += 1
        self.status = "running"
        return jq._claimed_attempt(row)

    def fail(self, attempts: int) -> None:
        """`mark_failure`'s decision, minus SQL."""
        if attempts >= self.max_attempts:
            self.status = "dead"
            return
        self.delays.append(min(jq._BACKOFF_CAP,
                               jq._BACKOFF_BASE * (2 ** (attempts - 1))))
        self.status = "pending"


def _run_until_dead(rec: _Recorder) -> None:
    for _ in range(50):                       # a bound, not an expectation
        job = rec.claim()
        if job is None:
            return
        rec.runs.append(job["attempts"])
        rec.fail(job["attempts"])
    raise AssertionError("the job never died — the queue would retry for ever")


def test_a_job_that_always_fails_runs_exactly_max_attempts_times():
    rec = _Recorder(max_attempts=5)
    _run_until_dead(rec)
    assert len(rec.runs) == 5, (
        f"max_attempts=5 produced {len(rec.runs)} runs. For a review job every "
        f"extra run is another billed LLM call on work already failing."
    )
    assert rec.status == "dead"


def test_the_first_attempt_is_numbered_one():
    """`attempt=0` in the log is an attempt that has not happened."""
    rec = _Recorder()
    assert rec.claim()["attempts"] == 1


def test_the_last_attempt_is_numbered_max():
    rec = _Recorder(max_attempts=5)
    _run_until_dead(rec)
    assert rec.runs == [1, 2, 3, 4, 5], rec.runs


def test_the_first_backoff_is_the_base_delay_not_half_of_it():
    rec = _Recorder(max_attempts=5)
    _run_until_dead(rec)
    assert rec.delays[0] == pytest.approx(jq._BACKOFF_BASE), (
        "the first retry of a failing job came sooner than any setting says"
    )
    assert rec.delays == pytest.approx(
        [min(jq._BACKOFF_CAP, jq._BACKOFF_BASE * 2 ** i) for i in range(4)]
    ), "the doubling no longer starts at the base"


def test_one_attempt_means_one_run():
    """The degenerate setting has to hold too, or the fix is an off-by-one
    pointing the other way."""
    rec = _Recorder(max_attempts=1)
    _run_until_dead(rec)
    assert rec.runs == [1]


def test_the_claim_helper_does_not_mutate_the_row_it_was_given():
    """The worker logs and the failure decision must see the same number.

    Returning a mutated copy of the SELECTed row is fine; mutating the mapping
    the driver handed back is how two readers end up disagreeing.
    """
    row = {"id": "j1", "attempts": 3, "max_attempts": 5}
    out = jq._claimed_attempt(row)
    assert out["attempts"] == 4
    assert row["attempts"] == 3, "the source row was modified in place"


def test_the_worker_passes_the_number_claim_gave_it():
    """A correct claim() with a worker that recomputes would be no fix at all."""
    import inspect

    src = inspect.getsource(__import__("src.sync.worker", fromlist=["_process"])._process)
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert 'attempts=job["attempts"]' in body, (
        "the worker no longer forwards the claimed attempt number"
    )
    assert "+ 1" not in body.split("mark_failure")[-1][:200], (
        "the worker is adjusting the number itself — two places now decide it"
    )
