"""The ordered stages of one review run, as the run record keeps them.

A run row used to say how a review ENDED — complete, partial, skipped,
failed — and nothing about how it got there. A review skipped because the
target branch was not in the configured list read exactly like one skipped
because the PR was a draft, and a failed one did not say whether it fell over
fetching the pull request or posting the comments. This records each step as
it happens: name, status, when it started, how long it took, and a sentence
saying why.

The vocabulary is deliberately small — success | skipped | failed | running —
because the page renders a pill per stage and a seventh word would render as
nothing. "skipped" is a decision (a gate closed, a stage switched off);
"failed" is an accident. They are kept apart for the same reason
`agents_skipped` and `agents_failed` are.

Bounded on every axis — number of stages, length of each field, size and
shape of `meta` — so a run row cannot grow without limit, and every reason
passes through `scrub` so a provider's error text cannot carry a token into
the record. Reasons are written from templates in the pipeline, never from an
exception's message; `scrub` is the belt to that pair of braces.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime

SUCCESS = "success"
SKIPPED = "skipped"
FAILED = "failed"
RUNNING = "running"
STATUSES = (SUCCESS, SKIPPED, FAILED, RUNNING)

#: Hard ceilings. A review has about twenty stages; anything past this is a
#: loop somewhere, and the run row is not the place to find out.
MAX_STAGES = 48
MAX_REASON = 400
MAX_NAME = 80
MAX_KEY = 48
MAX_META_KEYS = 8
MAX_META_VALUE = 120

#: Shapes that must never reach a stored sentence: provider tokens, bearer
#: headers, credentials inside a URL, and key=value secrets.
_SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bATBB[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bATATT[A-Za-z0-9_\-=]{8,}"),
    re.compile(r"(?i)\b(?:bearer|basic|token)\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|passwd)"
               r"\s*[=:]\s*[^\s&,;]+"),
)
_URL_USERINFO = re.compile(r"(https?://)[^/\s@]+@")


def scrub(text: object, limit: int = MAX_REASON) -> str:
    """One line of text that is safe to store and show: no secrets, bounded."""
    s = " ".join(str(text or "").split())
    s = _URL_USERINFO.sub(r"\1***@", s)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[redacted]", s)
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ms_between(start_iso: str | None, end: datetime | None = None) -> int | None:
    start = _parse(start_iso)
    if start is None:
        return None
    end = end or datetime.now(UTC)
    return max(0, int((end - start).total_seconds() * 1000))


def _clean_meta(meta: dict | None) -> dict | None:
    """Scalars only, few keys, short strings — the page shows them as chips."""
    if not isinstance(meta, dict) or not meta:
        return None
    out: dict = {}
    for k, v in meta.items():
        if len(out) >= MAX_META_KEYS:
            break
        key = scrub(k, 40)
        if not key or v is None:
            continue
        if isinstance(v, bool | int | float):
            out[key] = v
        else:
            out[key] = scrub(v, MAX_META_VALUE)
    return out or None


def normalize_stage(raw: object) -> dict | None:
    """A stored stage as the API serves it, or None when it is unreadable.

    Rows are written by this module, but a row may have been written by
    another version of it, so every field is re-checked on the way out rather
    than trusted: an unknown status becomes "failed" (never "success"), a
    missing name falls back to the key.
    """
    if not isinstance(raw, dict):
        return None
    key = scrub(raw.get("key"), MAX_KEY)
    if not key:
        return None
    status = str(raw.get("status") or "")
    if status not in STATUSES:
        status = FAILED
    dur = raw.get("duration_ms")
    try:
        dur = None if dur is None else max(0, int(dur))
    except (TypeError, ValueError):
        dur = None
    return {
        "key": key,
        "name": scrub(raw.get("name") or key, MAX_NAME),
        "status": status,
        "started_at": str(raw.get("started_at") or "") or None,
        "duration_ms": dur,
        "reason": scrub(raw.get("reason") or ""),
        "meta": _clean_meta(raw.get("meta")),
    }


def parse_stages(text: str | None) -> list[dict] | None:
    """The stored JSON column, read defensively. None when never recorded."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    out = [s for s in (normalize_stage(x) for x in data[:MAX_STAGES]) if s]
    return out


class StageRecorder:
    """Ordered stages of one run, with an optional sink that persists them.

    Thread-safe: the agents of a review run in a pool and the summary job in
    its own thread. The sink is called after every change with a snapshot and
    is best-effort — a store that cannot be written never fails a review.
    """

    def __init__(
        self,
        *,
        sink: Callable[[list[dict]], None] | None = None,
        stages: list[dict] | None = None,
    ) -> None:
        self._sink = sink
        self._lock = threading.RLock()
        self._stages: list[dict] = []
        #: The sentence that explains how the run ended early, when a gate
        #: closed it. The caller's "finished" stage repeats it.
        self.outcome_reason: str | None = None
        for raw in stages or []:
            st = normalize_stage(raw)
            if st is not None and len(self._stages) < MAX_STAGES:
                self._stages.append(st)

    # ── reading ──────────────────────────────────────────────────

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._stages]

    def get(self, key: str) -> dict | None:
        with self._lock:
            for s in reversed(self._stages):
                if s["key"] == key:
                    return dict(s)
        return None

    # ── writing ──────────────────────────────────────────────────

    def _flush(self) -> None:
        if self._sink is None:
            return
        # A record is never worth a review.
        with contextlib.suppress(Exception):
            self._sink(self.snapshot())

    def _find_running(self, key: str) -> dict | None:
        for s in reversed(self._stages):
            if s["key"] == key and s["status"] == RUNNING:
                return s
        return None

    def _append(self, stage: dict) -> None:
        if len(self._stages) >= MAX_STAGES:
            # Keep the tail meaningful: the last slot is always the newest
            # stage, so a "finished" stage still lands on an overfull run.
            self._stages[-1] = stage
        else:
            self._stages.append(stage)

    def begin(self, key: str, name: str, reason: str = "", *,
              started_at: str | None = None) -> None:
        """A stage is under way."""
        with self._lock:
            self._append({
                "key": scrub(key, MAX_KEY), "name": scrub(name, MAX_NAME),
                "status": RUNNING, "started_at": started_at or now_iso(),
                "duration_ms": None, "reason": scrub(reason), "meta": None,
            })
        self._flush()

    def end(self, key: str, status: str, reason: str = "", *,
            name: str | None = None, meta: dict | None = None,
            ends_run: bool = False) -> None:
        """Close the running stage `key` (or record it outright if it never
        began). `ends_run` marks the gate that stopped the review, so the
        finished stage can repeat its reason."""
        if status not in (SUCCESS, SKIPPED, FAILED):
            status = FAILED
        with self._lock:
            st = self._find_running(key)
            if st is None:
                st = {
                    "key": scrub(key, MAX_KEY), "name": scrub(name or key, MAX_NAME),
                    "status": RUNNING, "started_at": now_iso(),
                    "duration_ms": None, "reason": "", "meta": None,
                }
                self._append(st)
            st["status"] = status
            st["duration_ms"] = ms_between(st["started_at"])
            if reason:
                st["reason"] = scrub(reason)
            if name:
                st["name"] = scrub(name, MAX_NAME)
            if meta:
                st["meta"] = _clean_meta(meta)
            if ends_run and st["reason"]:
                self.outcome_reason = st["reason"]
        self._flush()

    def add(self, key: str, name: str, status: str, reason: str = "", *,
            started_at: str | None = None, duration_ms: int | None = None,
            meta: dict | None = None, ends_run: bool = False) -> None:
        """Record a stage that already happened, in one call."""
        if status not in STATUSES:
            status = FAILED
        with self._lock:
            self._append({
                "key": scrub(key, MAX_KEY), "name": scrub(name, MAX_NAME),
                "status": status, "started_at": started_at or now_iso(),
                "duration_ms": (None if duration_ms is None
                                else max(0, int(duration_ms))),
                "reason": scrub(reason), "meta": _clean_meta(meta),
            })
            if ends_run and reason:
                self.outcome_reason = scrub(reason)
        self._flush()

    def fail_running(self, reason: str) -> None:
        """Every stage still running ends as failed — the run raised."""
        changed = False
        with self._lock:
            for st in self._stages:
                if st["status"] == RUNNING:
                    st["status"] = FAILED
                    st["duration_ms"] = ms_between(st["started_at"])
                    st["reason"] = scrub(reason) or st["reason"]
                    changed = True
        if changed:
            self._flush()

    def finish(self, run_status: str, reason: str = "") -> None:
        """The last stage: how the run ended, timed from its first stage."""
        mapping = {"complete": SUCCESS, "partial": SUCCESS,
                   "skipped": SKIPPED, "failed": FAILED}
        status = mapping.get(str(run_status), FAILED)
        with self._lock:
            # A stage left running here was abandoned, not finished.
            for st in self._stages:
                if st["status"] == RUNNING:
                    st["status"] = FAILED if status == FAILED else SKIPPED
                    st["duration_ms"] = ms_between(st["started_at"])
                    if not st["reason"]:
                        st["reason"] = "Did not finish before the run ended."
            first = self._stages[0]["started_at"] if self._stages else None
            self._append({
                "key": "finished", "name": "Finished", "status": status,
                "started_at": now_iso(),
                "duration_ms": ms_between(first) if first else 0,
                "reason": scrub(reason or self.outcome_reason or ""),
                "meta": None,
            })
        self._flush()


def finish_reason(run_status: str, *, batch=None, outcome_reason: str | None = None,
                  error: str | None = None) -> str:
    """The sentence on the "Finished" stage."""
    if run_status == "skipped":
        return "Skipped — " + (outcome_reason or "no review stage was dispatched")
    if run_status == "failed":
        if error:
            return "Failed — " + error
        failed = list(getattr(batch, "agents_failed", None) or [])
        if failed:
            return "Failed — no review stage produced an answer (" + ", ".join(failed) + ")"
        return "Failed — " + (outcome_reason or "the review did not complete")
    findings = len(getattr(batch, "findings", None) or []) if batch is not None else 0
    verdict = getattr(getattr(batch, "verdict", None), "value", None)
    head = f"{findings} finding{'s' if findings != 1 else ''}"
    if verdict:
        head += f", verdict {verdict}"
    if run_status == "partial":
        failed = list(getattr(batch, "agents_failed", None) or [])
        tail = (f" — missing: {', '.join(failed)}" if failed
                else " — the review was not delivered to the pull request")
        return "Partial — " + head + tail
    return "Complete — " + head


def status_reason(stages: list[dict] | None, *, status: str, summary: str = "",
                  error_message: str | None = None) -> str | None:
    """One sentence for a run row: why it ended the way it did.

    The finished stage's reason when the run recorded stages; otherwise —
    rows written before stages existed — the first line of the error or the
    summary, for the two outcomes a reader needs explained.
    """
    for st in reversed(stages or []):
        if st.get("key") == "finished" and st.get("reason"):
            return str(st["reason"])
    if status in ("skipped", "failed"):
        text = (error_message or summary or "").strip()
        if text:
            return scrub(text.splitlines()[0], 300)
    return None
