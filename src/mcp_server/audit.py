"""The MCP call log: who asked what of which repositories, and how much came back.

One row per tool call in ``mcp_call_log`` and the same facts mirrored to the
action audit (``mcp.call``). A row holds the tool name, the repositories the
call touched, the token and person behind it, the outcome, the size of the
answer and a one-way digest of the arguments. It never holds argument values,
the answer or a secret: an audit trail that stored the query would be the
largest copy of the code anybody could read.

The write is off the request path (a bounded queue and one worker thread) and
every failure is swallowed — an audit problem must not break the call it
records. The queue drops the OLDEST pending row rather than blocking when it
is full (counted, and warned about); queued rows are flushed at exit.
A call refused at the HTTP edge (revoked, expired, unknown, legacy or
wrong-holder token) is logged by :func:`record_denied`.

Retention: rows older than ``CELMIS_MCP_AUDIT_RETENTION_DAYS`` are deleted by
the worker, at most once an hour.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
_WORKER: threading.Thread | None = None
_LOCK = threading.Lock()
_LAST_PURGE = 0.0
_PURGE_EVERY_SECONDS = 3600.0
_MAX_REPOS = 50
#: Rows dropped because the queue was full, since the last warning.
_DROPPED = 0
#: A denied attempt is logged at most once per token and reason in this window,
#: so a revoked token hammering the endpoint cannot flood the log (and push
#: real rows out of the queue).
_DENIED_EVERY_SECONDS = 60.0
_DENIED_SEEN: dict[tuple[str, str], float] = {}


def retention_days() -> int:
    try:
        from src.config import get_settings

        return max(1, int(get_settings().celmis_mcp_audit_retention_days))
    except Exception:  # noqa: BLE001
        return 180


def _caller_facts() -> dict[str, Any]:
    """Who is behind the call in flight. Never raises."""
    facts: dict[str, Any] = {
        "workspace_id": "", "user_id": "", "token_id": None, "kind": "", "client_id": "",
    }
    try:
        from src.mcp_server.identity import resolve_caller

        caller = resolve_caller()
        facts.update(
            workspace_id=caller.workspace_id or "",
            user_id=caller.user_id or "",
            token_id=caller.token_id,
            kind=caller.kind or "",
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is not None:
            facts["client_id"] = str(getattr(token, "client_id", "") or "")[:80]
    except Exception:  # noqa: BLE001
        pass
    return facts


def record_call(rec) -> None:  # noqa: ANN001 — a callctx.CallRecord
    """Queue the audit row for ``rec``. Never raises, never blocks."""
    try:
        row = {
            "ts": datetime.now(UTC),
            "tool": rec.tool,
            "profile": rec.profile,
            "repos": sorted(rec.repos)[:_MAX_REPOS],
            "status": rec.status,
            "result_bytes": int(rec.result_bytes),
            "result_items": int(rec.result_items),
            "duration_ms": int(rec.duration_ms),
            "args_hash": rec.args_hash,
            **_caller_facts(),
        }
        _enqueue(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_audit_record_failed err=%s", type(exc).__name__)


def _enqueue(row: dict[str, Any]) -> None:
    global _DROPPED
    _ensure_worker()
    try:
        _QUEUE.put_nowait(row)
    except queue.Full:
        try:
            _QUEUE.get_nowait()          # drop the oldest, keep the newest
            _QUEUE.task_done()
        except queue.Empty:
            pass
        with _LOCK:
            _DROPPED += 1
            dropped = _DROPPED
            if dropped == 1 or dropped % 100 == 0:
                logger.warning("mcp_audit_queue_full dropped=%d — oldest rows lost", dropped)
        try:
            _QUEUE.put_nowait(row)
        except queue.Full:
            logger.warning("mcp_audit_queue_full — a call was not logged")


def dropped_rows() -> int:
    """How many audit rows the full queue has discarded in this process."""
    return _DROPPED


#: Why a call was refused at the edge, as a short code. The verifier knows the
#: sentence; the log keeps the category, never a value.
def _denial_code(reason: str) -> str:
    text = (reason or "").lower()
    for needle, code in (
        ("revoked", "revoked"), ("expired", "expired"),
        ("does not belong", "wrong_holder"), ("not recognis", "unknown"),
        ("no longer a member", "left_workspace"), ("predates", "legacy"),
        ("could not confirm", "unverifiable"),
    ):
        if needle in text:
            return code
    return "refused"


def record_denied(payload: dict | None, reason: str, *, client_id: str = "") -> None:
    """Log a call refused before any tool ran: a revoked, expired, unknown,
    legacy or wrong-holder token. ``payload`` is a signature-verified claim set;
    the row carries the token id and the person from it, a reason code, and no
    arguments (the request body is not even read at this point). Never raises."""
    try:
        from src.mcp_server.identity import grant_claim, token_workspace

        token_id = grant_claim(payload)
        sub = str((payload or {}).get("sub") or "")
        user_id = sub.split(":", 1)[1] if sub.startswith(("user:", "client:")) else sub
        code = _denial_code(reason)
        key = (token_id or user_id, code)
        now = time.monotonic()
        with _LOCK:
            if now - _DENIED_SEEN.get(key, -1e9) < _DENIED_EVERY_SECONDS:
                return
            _DENIED_SEEN[key] = now
            if len(_DENIED_SEEN) > 4096:
                _DENIED_SEEN.clear()
        _enqueue({
            "ts": datetime.now(UTC), "tool": f"(refused:{code})", "profile": "",
            "repos": [], "status": "denied", "result_bytes": 0, "result_items": 0,
            "duration_ms": 0, "args_hash": "",
            "workspace_id": token_workspace(payload) or "", "user_id": user_id,
            "token_id": token_id, "kind": str((payload or {}).get("typ") or ""),
            "client_id": str(client_id or (payload or {}).get("client_id") or "")[:80],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_audit_denied_failed err=%s", type(exc).__name__)


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_run, name="mcp-audit", daemon=True)
        _WORKER.start()


def _run() -> None:
    while True:
        row = _QUEUE.get()
        try:
            write_row(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp_audit_write_failed err=%s", type(exc).__name__)
        finally:
            _QUEUE.task_done()


def write_row(row: dict[str, Any]) -> None:
    """Persist one audit row, mirror it to the action audit, apply retention."""
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import McpCallLog

    with Session(_sync_engine()) as s:
        s.add(McpCallLog(
            ts=row["ts"], workspace_id=row["workspace_id"], user_id=row["user_id"],
            token_id=row["token_id"], kind=row["kind"], client_id=row["client_id"],
            tool=row["tool"], profile=row["profile"], repos=list(row["repos"]),
            status=row["status"], result_bytes=row["result_bytes"],
            result_items=row["result_items"], duration_ms=row["duration_ms"],
            args_hash=row["args_hash"],
        ))
        s.commit()
        _purge_if_due(s)
    try:
        from src.security.audit import record_action

        record_action(
            action="mcp.call", actor_id=row["user_id"] or None,
            workspace_id=row["workspace_id"] or None, target=row["tool"],
            detail={k: row[k] for k in (
                "token_id", "kind", "profile", "repos", "status",
                "result_bytes", "result_items", "duration_ms", "args_hash")},
        )
    except Exception:  # noqa: BLE001
        pass


def _purge_if_due(session) -> None:  # noqa: ANN001
    global _LAST_PURGE
    now = time.monotonic()
    if now - _LAST_PURGE < _PURGE_EVERY_SECONDS:
        return
    _LAST_PURGE = now
    purge(session)


def purge(session) -> int:  # noqa: ANN001
    """Delete rows past retention. Returns how many went."""
    from sqlalchemy import delete

    from src.db.models import McpCallLog

    cutoff = datetime.now(UTC) - timedelta(days=retention_days())
    res = session.execute(delete(McpCallLog).where(McpCallLog.ts < cutoff))
    session.commit()
    return int(res.rowcount or 0)


def flush(timeout: float = 5.0) -> bool:
    """Wait until queued rows are written (tests, shutdown). True when drained."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _QUEUE.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return _QUEUE.unfinished_tasks == 0


def _flush_at_exit() -> None:
    """The worker is a daemon thread: give the rows still queued a moment to land."""
    with contextlib.suppress(Exception):
        flush(3.0)


atexit.register(_flush_at_exit)

__all__ = ["dropped_rows", "flush", "purge", "record_call", "record_denied",
           "retention_days", "write_row"]
