"""Review cadence: when a PR is reviewed without anybody asking.

`review_cadence` says it:

* `automatic` — every push is reviewed (the built-in);
* `auto_pause` — the same, until a PR receives `auto_pause_pushes` pushes
  inside `auto_pause_window_minutes`: then its automatic reviews wait for
  `@celmis start-review` (or the Resume button). The push that reaches the
  limit is itself the first one skipped;
* `manual` — a PR is reviewed only when somebody asks.

This module is the pure half: the sliding window, the decision, and the
sentences. The state itself (`review_pull_requests.recent_pushes`,
`review_paused`, …) lives in `src/review/pr_state.py`; the callers are the
webhook, the poller and the orchestrator's `gate_cadence`, which all ask
`decide` so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from src.review import messages

CADENCES: Final[tuple[str, ...]] = ("automatic", "auto_pause", "manual")

#: Why a PR is paused. Only `auto_pause` is a mechanical pause: it lapses
#: when the repository leaves the `auto_pause` cadence. A person's pause
#: (`manual`: the Pause button on the pull-requests page) holds whatever
#: the cadence says, until somebody resumes.
REASON_AUTO: Final = "auto_pause"
REASON_MANUAL: Final = "manual"
PAUSE_REASONS: Final[tuple[str, ...]] = (REASON_AUTO, REASON_MANUAL)


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def prune(pushes: list | None, now: datetime, window_minutes: int) -> list[datetime]:
    """The pushes still inside the window ending at `now`, oldest first.
    Unreadable entries are dropped; entries from the future (a clock step)
    are kept so they cannot be counted twice."""
    cutoff = now - timedelta(minutes=max(1, int(window_minutes)))
    kept = [t for t in (parse_time(p) for p in pushes or []) if t is not None and t > cutoff]
    return sorted(kept)


def should_pause(count: int, limit: int) -> bool:
    return int(count) >= max(1, int(limit))


@dataclass(frozen=True)
class CadenceDecision:
    action: Literal["review", "skip"]
    #: ok | cadence_manual | paused — for logs, stage meta and tests.
    code: str


def pause_holds(cadence: str, paused: bool, reason: str | None) -> bool:
    """Does a stored pause still stop the automatic reviews? A mechanical
    (`auto_pause`) one does only while the cadence is `auto_pause`; a pause a
    person asked for always does."""
    if not paused:
        return False
    if reason == REASON_MANUAL:
        return True
    return cadence == "auto_pause"


def decide(cadence: str | None, paused: bool = False, reason: str | None = None) -> CadenceDecision:
    """Whether an AUTOMATIC trigger may review a PR now."""
    cadence = cadence if cadence in CADENCES else "automatic"
    if pause_holds(cadence, paused, reason):
        return CadenceDecision("skip", "paused")
    if cadence == "manual":
        return CadenceDecision("skip", "cadence_manual")
    return CadenceDecision("review", "ok")


def gate_reason(decision: CadenceDecision, lang: str | None, *, handle: str,
                pushes: int, minutes: int, reason: str | None) -> str:
    """The sentence that says why the gate stopped (stage reason, run row)."""
    if decision.code == "cadence_manual":
        return messages.t("gate.cadence_manual", lang, handle=handle)
    if reason == REASON_AUTO:
        return messages.t("gate.cadence_paused", lang, handle=handle,
                          pushes=pushes, minutes=minutes)
    return messages.t("gate.cadence_paused_manual", lang, handle=handle)


def notice(decision: CadenceDecision, lang: str | None, *, handle: str, pushes: int,
           minutes: int, reason: str | None, last_reviewed_sha: str | None) -> str:
    """The one note a PR gets when its automatic reviews stop: how to resume."""
    if decision.code == "cadence_manual":
        return messages.t("pause.notice_cadence", lang, handle=handle)
    since = (messages.t("pause.since_commit", lang, sha=last_reviewed_sha[:7])
             if last_reviewed_sha else messages.t("pause.since_start", lang))
    key = "pause.notice" if reason == REASON_AUTO else "pause.notice_manual"
    return messages.t(key, lang, handle=handle, pushes=pushes, minutes=minutes,
                      since=since)


def bot_handle() -> str:
    try:
        from src.review.settings import get_review_settings

        return str(get_review_settings().bot_handle)
    except Exception:  # noqa: BLE001 — a sentence never fails a review
        return "@celmis"
