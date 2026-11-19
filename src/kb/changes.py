"""The log of notes that changed: which document was written, rewritten,
removed or marked for review, and when.

Generation rewrites a module's note after its code changes; an import replaces
cards; a check marks a card stale. A team that keeps its own cards on top of
the generated notes needs to know which ones moved since it last looked, and
"regenerate" alone did not say. Every such change is one JSON line in
``<vault>/.celmis_changes.jsonl``, appended under a lock and trimmed to the
newest :data:`KEEP` lines, so the log stays small and costs nothing to read.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

FILE = ".celmis_changes.jsonl"
KEEP = 5000
#: Most entries one read returns.
MAX_READ = 1000
KINDS = ("created", "updated", "deleted", "stale", "fresh")

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record(vault_dir: Path, path: str, kind: str, *, by: str = "", commit: str = "") -> None:
    """Append one change; never raises (a log must not break a write)."""
    entry = {"at": _now(), "path": path, "change": kind}
    if by:
        entry["by"] = by
    if commit:
        entry["commit"] = commit[:40]
    try:
        vault_dir.mkdir(parents=True, exist_ok=True)
        log = vault_dir / FILE
        with _LOCK:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if log.stat().st_size > KEEP * 400:
                lines = log.read_text(encoding="utf-8").splitlines()[-KEEP:]
                log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("kb_change_log_failed path=%s err=%s", path, type(exc).__name__)


def parse_since(value: str) -> datetime | None:
    """``7d`` / ``12h`` / ``30m`` ago, or an ISO date or time; None if neither."""
    v = (value or "").strip()
    if not v:
        return None
    unit = v[-1:].lower()
    if unit in ("d", "h", "m") and v[:-1].isdigit():
        n = int(v[:-1])
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(UTC) - delta
    try:
        when = datetime.fromisoformat(v)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def read(vault_dir: Path, *, since: datetime | None = None, limit: int = MAX_READ) -> list[dict]:
    """Changes newest first, at or after ``since``; one line per note (its latest)."""
    log = vault_dir / FILE
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            at = datetime.fromisoformat(entry["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if since is not None and at < since:
            break
        if entry.get("path") in seen:
            continue
        seen.add(entry.get("path"))
        out.append(entry)
        if len(out) >= min(limit, MAX_READ):
            break
    return out
