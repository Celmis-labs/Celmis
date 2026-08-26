"""Audit log — append-only JSONL file for all LLM calls.

Does NOT log the raw payload (so that secrets are not duplicated if they
leaked through redaction).
Logs: metadata, hashes, redaction stats, sizes, duration, and which
workspace the call was made for — without that last one the file is a
single undifferentiated installation log that only a global admin can
ever be shown (see src/api/routers/audit.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_workspace_id(value: Any) -> str | None:
    """The tenant a record belongs to, or None when it belongs to no one.

    Mirrors ``src.sync.queue._normalize_workspace`` — a blank string is not a
    tenant — and adds the rule that matters here: the literal ``"default"``
    is NOT an attribution.

    Both LLM clients declare ``workspace_id: str = "default"``, so "default"
    is exactly what arrives when the caller never said which tenant it was
    acting for: ``get_gemini_client()`` in the QA exploration agent,
    ``build_llm_client(user_id)`` with no workspace, every CLI batch run.
    Those calls do belong to someone — we just don't know whom. Writing
    "default" into the record would hand all of them, other tenants'
    repository names included, to whoever happens to own the seeded
    'default' workspace. That is the same reasoning that kept
    ``sync_jobs.workspace_id`` free of a server_default (migration
    d5c1b8e4a730): a row with no tenant must stay a row with no tenant.

    The cost is a legacy single-tenant install whose real workspace IS
    'default' — its records read as unattributed and only a global admin
    sees them. That is the direction to be wrong in.
    """
    if not isinstance(value, str):
        return None
    ws = value.strip()
    if not ws or ws == "default":
        return None
    return ws


@dataclass
class AuditRecord:
    """A single record in audit.jsonl. All fields must be JSON-serializable."""

    request_id: str
    timestamp: str
    mode: str  # "batch" | "qa" | "embedding"
    model: str
    # Which tenant this call was made for. None means the writer did not know
    # one (see normalize_workspace_id), and so does the ABSENCE of the key:
    # every record written before this field existed parses as None, which is
    # why the reader must treat missing and None identically. Untenanted
    # records are readable by global admins only.
    workspace_id: str | None = None
    repo: str | None = None
    module: str | None = None
    operation: str = ""  # "generate" | "ask" | "embed" | ...
    question_hash: str | None = None
    files_sent: list[str] = field(default_factory=list)
    input_tokens_estimated: int = 0
    output_tokens_estimated: int = 0
    redaction: dict[str, Any] = field(default_factory=dict)
    response_hash: str | None = None
    duration_ms: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: WHO did it, and from where. Absent on every LLM-call record and on
    #: every record written before these fields existed — both parse as None.
    #:
    #: The file had no actor at all, and the page over it is called "Audit
    #: log". So a GitHub personal access token written into the credential
    #: store produced no row anywhere; nor did a login, a repository
    #: registration or an index. It answered "what did the model cost" and
    #: could not answer "who changed what, from where" — the only question an
    #: auditor asks. See `record_action` for the other half.
    actor: str | None = None
    actor_id: str | None = None
    ip: str | None = None
    target: str | None = None


class AuditLogger:
    """File-based audit logger. Append-only JSONL with size-based rotation.

    Rotation:
        Before a write the file size is checked. If > max_size_bytes →
        rename audit.jsonl → audit.jsonl.1, audit.jsonl.1 → audit.jsonl.2,
        and so on up to max_files. Older ones are deleted.
        For production, OS logrotate is recommended instead of in-process.
    """

    def __init__(
        self,
        log_path: Path,
        max_size_bytes: int = 50_000_000,  # 50 MB
        max_files: int = 5,
    ) -> None:
        self.log_path = log_path
        self.max_size_bytes = max_size_bytes
        self.max_files = max_files
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = structlog.get_logger("audit")

    def _rotate_if_needed(self) -> None:
        """If the file is > max_size_bytes — perform a rotation."""
        try:
            if not self.log_path.exists():
                return
            if self.log_path.stat().st_size < self.max_size_bytes:
                return
        except OSError:
            return

        # Rotate: audit.jsonl.{n-1} → audit.jsonl.n, ...
        for i in range(self.max_files, 0, -1):
            src = self.log_path.with_name(f"{self.log_path.name}.{i}")
            if i == self.max_files and src.exists():
                with suppress(OSError):
                    src.unlink()  # delete the oldest one
            elif src.exists():
                dst = self.log_path.with_name(f"{self.log_path.name}.{i+1}")
                with suppress(OSError):
                    src.rename(dst)

        # Current → .1
        try:
            self.log_path.rename(self.log_path.with_name(f"{self.log_path.name}.1"))
            self._log.info("audit_rotated", new_path=str(self.log_path))
        except OSError as exc:
            self._log.warning("audit_rotation_failed", error=str(exc))

    def write(self, record: AuditRecord) -> None:
        """Writes one event. Does not raise on error — audit must not block the main flow."""
        try:
            self._rotate_if_needed()
            line = json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._log.info("audit_written", request_id=record.request_id, op=record.operation)
        except OSError as exc:  # noqa: BLE001
            self._log.error("audit_write_failed", error=str(exc))

    @contextmanager
    def track(
        self,
        *,
        mode: str,
        model: str,
        operation: str,
        workspace_id: str | None = None,
        repo: str | None = None,
        module: str | None = None,
        question: str | None = None,
        files_sent: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        """Context manager for tracking one operation from start to finish.

        Example:
            with audit.track(mode="qa", model=..., operation="ask", question=q) as record:
                response = gemini.call(...)
                record.response_hash = audit.hash_response(response)
                record.input_tokens_estimated = ...
        """
        record = AuditRecord(
            request_id=str(uuid.uuid4()),
            timestamp=_iso_now(),
            mode=mode,
            model=model,
            operation=operation,
            workspace_id=normalize_workspace_id(workspace_id),
            repo=repo,
            module=module,
            files_sent=files_sent or [],
            question_hash=_sha256(question) if question else None,
            extra=extra or {},
        )
        start = time.perf_counter()
        try:
            yield record
        except Exception as exc:  # noqa: BLE001
            record.error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            record.duration_ms = int((time.perf_counter() - start) * 1000)
            self.write(record)

    @staticmethod
    def hash_response(text: str) -> str:
        return _sha256(text)


_default_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _default_logger
    if _default_logger is None:
        from src.config import get_settings

        _default_logger = AuditLogger(get_settings().audit_log_path)
    return _default_logger


# ─── Actions, as opposed to model calls ──────────────────────────────


#: Actions worth a row. Deliberately short: an audit trail nobody reads is
#: the same as none, and the useful question is "who touched credentials,
#: access, or what gets reviewed".
ACTION_MODE = "action"


def record_action(
    *,
    action: str,
    actor: str | None = None,
    actor_id: str | None = None,
    workspace_id: str | None = None,
    target: str | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record a security-relevant action. Never raises.

    Written into the same file as the model-call records and distinguished by
    `mode="action"`, so it inherits rotation, retention, tenant scoping,
    filtering and CSV export rather than growing a second half-finished
    version of each.

    `detail` must never carry a secret. Callers pass the SHAPE of what changed
    — a provider name, an account label, a repo slug — never the value. The
    422 handler in `api/main.py` exists because a validation error echoed a
    token; an audit trail that did the same would be worse, because it is
    written on the successful path too.
    """
    import uuid
    from datetime import UTC, datetime

    try:
        get_audit_logger().write(AuditRecord(
            request_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            mode=ACTION_MODE,
            model="",
            workspace_id=normalize_workspace_id(workspace_id),
            operation=action,
            actor=actor,
            actor_id=actor_id,
            ip=ip,
            target=target,
            extra=dict(detail or {}),
            error=error,
        ))
    except Exception:  # noqa: BLE001
        # An audit write must not break the operation it is recording. The
        # writer already swallows OSError; this catches a caller passing
        # something unserializable.
        logging.getLogger(__name__).warning(
            "audit_action_failed action=%s", action)
