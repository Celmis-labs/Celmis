"""What became of a finding — one place says it, any number of listeners hear it.

Three features wanted the same fact: "was this suggestion implemented?" — the
issues ledger (it freezes `close_outcome` when a PR merges), the learning
store (a suggestion nobody takes is a weaker signal than one that was), and
the productivity metrics. Each kept its own tally and the tallies disagreed.
The ledger owns the truth (`review.issues.close_outcome`,
`review.issues.implementation_stats`); this module is how it tells the others
without importing them.

    register_outcome_listener(fn)   — fn(outcome: IssueOutcome) -> None
    emit_outcome(outcome)           — called by the ledger, after its commit

Rules a listener can rely on:
  * `emit_outcome` never raises and never blocks the caller on a listener's
    failure: a listener that raises is logged and skipped, the rest still run;
  * it is called AFTER the ledger committed, so a listener reading the table
    sees the outcome it was told about;
  * an outcome is emitted once per change of fate, not once per recheck;
  * the LATEST event of an `issue_id` is the truth: a merge freezes an open
    issue as `unimplemented`, and the check made at that merge can find the
    PR's own fix already on the branch and say `implemented` for the same
    issue. A listener that counts must replace what it holds for that id, not
    add to it.

Kinds (`IssueOutcome.kind`):
  implemented     — the PR's own suggestion was taken (fixed before or at merge);
  unimplemented   — the PR merged with the issue still open;
  dismissed       — a person (or feedback) dismissed it before the PR closed;
  abandoned       — the PR closed unmerged;
  resolved_later  — a LATER change removed it from the target branch
                    (`fixed_by_pr_number` / `fixed_in_sha` say which);
  reopened        — that later resolution was undone (a revert brought it back).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

OUTCOME_KINDS = (
    "implemented", "unimplemented", "dismissed", "abandoned",
    "resolved_later", "reopened",
)


@dataclass(frozen=True)
class IssueOutcome:
    """One change of fate of one issue."""

    kind: str
    workspace_id: str
    issue_id: str
    repo_slug: str
    pr_provider: str
    pr_repo: str
    pr_number: int
    fingerprint: str
    file_path: str = ""
    rule_id: str | None = None
    agent: str | None = None
    category: str | None = None
    severity: str | None = None
    #: Where a resolution came from (auto_at_merge, auto_head_check, …).
    source: str | None = None
    fixed_by_pr_number: int | None = None
    fixed_in_sha: str | None = None


Listener = Callable[[IssueOutcome], None]

_LISTENERS: list[Listener] = []
_LOCK = threading.Lock()


def register_outcome_listener(fn: Listener) -> Listener:
    """Hear every outcome from now on. Registering the same function twice
    registers it once. Returns `fn`, so it can be used as a decorator."""
    with _LOCK:
        if fn not in _LISTENERS:
            _LISTENERS.append(fn)
    return fn


def unregister_outcome_listener(fn: Listener) -> None:
    with _LOCK:
        if fn in _LISTENERS:
            _LISTENERS.remove(fn)


def listeners() -> list[Listener]:
    with _LOCK:
        return list(_LISTENERS)


def emit_outcome(outcome: IssueOutcome) -> int:
    """Tell every listener. Returns how many heard it without failing."""
    heard = 0
    for fn in listeners():
        try:
            fn(outcome)
            heard += 1
        except Exception as exc:  # noqa: BLE001 — a listener never breaks the ledger
            logger.warning(
                "issue_outcome_listener_failed listener=%s kind=%s issue=%s err_type=%s",
                getattr(fn, "__name__", repr(fn)), outcome.kind, outcome.issue_id,
                type(exc).__name__)
    return heard
