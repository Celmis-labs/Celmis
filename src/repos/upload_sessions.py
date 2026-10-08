"""Resumable, chunked archive upload.

A reverse proxy or CDN in front of the API often caps a request body well below
the archive size (for example ~100 MB), so a large archive cannot travel in one
request. Instead the client opens a session, sends numbered parts (each at most
``chunk_size`` bytes, in any order, any number of times), then completes it:
the parts are joined in order, checked against the declared size (and sha-256
when given) and handed to the same safe-extraction path as a one-request
upload.

Everything lives under ``<workspace_dir>/uploads/<session_id>/`` (the data
directory, not a small tmpfs): ``meta.json`` and ``part-<n>`` files. A session
is removed on complete, on abort, when it expires, and at startup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.repos.upload import ArchiveError, StoredArchive, archive_kind, max_archive_bytes

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 32 * 1024 * 1024
MIN_CHUNK_BYTES = 1024
#: Strictly below the ~100 MB body limit many proxies enforce.
MAX_CHUNK_BYTES = 90 * 1024 * 1024
#: Room for the HTTP framing around a raw part body.
BODY_MARGIN = 1024 * 1024
SESSION_TTL_SECONDS = 24 * 3600
_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_READ = 1024 * 1024


class SessionError(ValueError):
    """A refusal with the HTTP status the router should answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def chunk_bytes() -> int:
    """The part size handed to clients (``CELMIS_UPLOAD_CHUNK_BYTES``), clamped."""
    try:
        value = int(os.environ.get("CELMIS_UPLOAD_CHUNK_BYTES", DEFAULT_CHUNK_BYTES))
    except ValueError:
        value = DEFAULT_CHUNK_BYTES
    return max(MIN_CHUNK_BYTES, min(value, MAX_CHUNK_BYTES))


def part_body_cap() -> int:
    return chunk_bytes() + BODY_MARGIN


def session_ttl() -> int:
    try:
        return max(60, int(os.environ.get("CELMIS_UPLOAD_SESSION_TTL", SESSION_TTL_SECONDS)))
    except ValueError:
        return SESSION_TTL_SECONDS


def root_dir() -> Path:
    from src.config import get_settings

    return get_settings().workspace_dir / "uploads"


@dataclass
class Session:
    id: str
    user_id: str
    workspace_id: str
    slug: str
    name: str
    filename: str
    size: int
    sha256: str
    chunk_size: int
    created_at: float
    expires_at: float

    @property
    def parts(self) -> int:
        return -(-self.size // self.chunk_size)

    def expected(self, n: int) -> int:
        """The exact byte length part ``n`` must have."""
        return self.size - n * self.chunk_size if n == self.parts - 1 else self.chunk_size

    @property
    def dir(self) -> Path:
        return root_dir() / self.id

    def part_path(self, n: int) -> Path:
        return self.dir / f"part-{n:06d}"

    def received(self) -> list[int]:
        out = []
        for n in range(self.parts):
            p = self.part_path(n)
            if p.is_file() and p.stat().st_size == self.expected(n):
                out.append(n)
        return out


def _free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def create(*, user_id: str, workspace_id: str, slug: str, name: str, filename: str,
           size: int, sha256: str = "") -> Session:
    """Validate the declared upload and open a session. No bytes are accepted
    before this passes (extension, size, disk space, sha format)."""
    try:
        archive_kind(filename)
    except ArchiveError as exc:
        raise SessionError(str(exc)) from None
    limit = max_archive_bytes()
    if size <= 0:
        raise SessionError("The archive size must be greater than zero.")
    if size > limit:
        raise SessionError(f"The archive is larger than {limit // (1024 * 1024)} MB.", 413)
    sha256 = (sha256 or "").strip()
    if sha256 and not _SHA.match(sha256):
        raise SessionError("sha256 must be 64 hexadecimal characters.")
    cleanup_expired()
    root = root_dir()
    # Parts plus the assembled copy, plus the unpacked tree later: refuse early.
    if _free_bytes(root) < 2 * size:
        raise SessionError(
            f"Not enough free disk space to accept this upload (need about "
            f"{2 * size // (1024 * 1024)} MB free).", 507)
    now = time.time()
    s = Session(id=uuid.uuid4().hex, user_id=user_id, workspace_id=workspace_id,
                slug=slug, name=name, filename=Path(filename).name[:200], size=size,
                sha256=sha256.lower(), chunk_size=chunk_bytes(), created_at=now,
                expires_at=now + session_ttl())
    s.dir.mkdir(parents=True, exist_ok=True)
    (s.dir / "meta.json").write_text(json.dumps(s.__dict__), encoding="utf-8")
    return s


def load(session_id: str, *, workspace_id: str, user_id: str) -> Session:
    """The session, or SessionError 404 (also for someone else's, and expired)."""
    if not _ID.match(session_id or ""):
        raise SessionError("Unknown upload session.", 404)
    meta = root_dir() / session_id / "meta.json"
    try:
        s = Session(**json.loads(meta.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        raise SessionError("Unknown upload session.", 404) from None
    if s.workspace_id != workspace_id or s.user_id != user_id:
        raise SessionError("Unknown upload session.", 404)
    if s.expires_at <= time.time():
        discard(s.id)
        raise SessionError("This upload session has expired. Start a new one.", 404)
    return s


def part_path_for_write(s: Session, n: int) -> Path:
    if n < 0 or n >= s.parts:
        raise SessionError(f"Part index must be between 0 and {s.parts - 1}.")
    return s.dir / f"part-{n:06d}.tmp-{uuid.uuid4().hex[:8]}"


def commit_part(s: Session, n: int, tmp: Path) -> None:
    """Accept a fully written part body; a re-sent part replaces the earlier one."""
    if tmp.stat().st_size != s.expected(n):
        tmp.unlink(missing_ok=True)
        raise SessionError(
            f"Part {n} must be exactly {s.expected(n)} bytes; got a different length.")
    os.replace(tmp, s.part_path(n))


def assemble(s: Session) -> StoredArchive:
    """Join the parts in order and verify size and (when declared) sha-256."""
    have = set(s.received())
    missing = [n for n in range(s.parts) if n not in have]
    if missing:
        shown = ", ".join(map(str, missing[:20])) + (" …" if len(missing) > 20 else "")
        raise SessionError(f"Missing parts: {shown}.", 409)
    target = s.dir / "assembled"
    digest = hashlib.sha256()
    total = 0
    try:
        with open(target, "wb") as out:
            for n in range(s.parts):
                with open(s.part_path(n), "rb") as src:
                    while chunk := src.read(_READ):
                        digest.update(chunk)
                        total += len(chunk)
                        out.write(chunk)
    except OSError:
        target.unlink(missing_ok=True)
        raise SessionError("Could not assemble the upload (disk full?).", 507) from None
    if total != s.size:
        target.unlink(missing_ok=True)
        raise SessionError(f"The assembled size {total} differs from the declared {s.size}.", 422)
    if s.sha256 and digest.hexdigest() != s.sha256:
        target.unlink(missing_ok=True)
        raise SessionError("The sha256 of the assembled archive does not match.", 422)
    return StoredArchive(target, digest.hexdigest(), total)


def discard(session_id: str) -> None:
    if _ID.match(session_id or ""):
        shutil.rmtree(root_dir() / session_id, ignore_errors=True)


def cleanup_expired() -> int:
    """Remove expired (or unreadable) session directories. Returns how many."""
    root = root_dir()
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            expires = json.loads((d / "meta.json").read_text(encoding="utf-8"))["expires_at"]
        except (OSError, ValueError, KeyError):
            # No readable meta: leave a brand-new directory alone (it is being
            # created), remove an old one.
            expires = d.stat().st_mtime + session_ttl()
        if float(expires) <= now:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("upload_sessions_cleaned removed=%d", removed)
    return removed
