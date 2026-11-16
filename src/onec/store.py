"""Short-lived storage of uploaded 1C files.

An upload goes in with a *ticket*: a random, single-use URL token the MCP tool
hands its caller, bound to that caller (user and workspace) and good for
:data:`TICKET_TTL`. The file that comes in is kept under a *handle*
(`x` + 16 hex digits) for :data:`HANDLE_TTL`, readable only by the caller the
ticket was issued to. Nothing is committed or indexed; expired entries are
removed lazily, on the next issue or read.

Only a digest of a ticket is stored, so a copy of the directory does not hold
a usable ticket. Limits: :data:`MAX_BYTES` per file, :data:`MAX_PER_USER`
live handles per caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

TICKET_TTL = 15 * 60
HANDLE_TTL = 24 * 3600
MAX_BYTES = 20 * 1024 * 1024
MAX_PER_USER = 20
HANDLE_RE = re.compile(r"^x[0-9a-f]{16}$")
_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_LOCK = threading.Lock()


class StoreError(ValueError):
    """A refused upload or read; the message is safe to show."""


@dataclass
class Stored:
    handle: str
    file_name: str
    size: int
    sha256: str
    created: float
    user_id: str
    workspace_id: str
    path: Path

    def read(self) -> bytes:
        return (self.path / "file.bin").read_bytes()


def _root() -> Path:
    from src.config import settings

    return Path(settings.workspace_dir) / "onec-external"


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cleanup(root: Path | None = None, now: float | None = None) -> None:
    root = root or _root()
    now = now or time.time()
    for sub, ttl in (("tickets", TICKET_TTL), ("files", HANDLE_TTL)):
        base = root / sub
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            meta = _read_json(entry / "meta.json") if entry.is_dir() else _read_json(entry)
            made = (meta or {}).get("created", 0)
            if now - made > ttl:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)


def issue_ticket(user_id: str, workspace_id: str) -> str:
    """A new upload ticket for this caller."""
    root = _root()
    with _LOCK:
        cleanup(root)
        (root / "tickets").mkdir(parents=True, exist_ok=True)
        ticket = secrets.token_urlsafe(24)
        _write_json(root / "tickets" / f"{_digest(ticket)}.json",
                    {"user_id": user_id, "workspace_id": workspace_id,
                     "created": time.time()})
    return ticket


def redeem(ticket: str, data: bytes, file_name: str) -> Stored:
    """Store ``data`` under a new handle, spending ``ticket``. Raises StoreError."""
    if not _TICKET_RE.match(ticket or ""):
        raise StoreError("unknown or expired upload link")
    if not data:
        raise StoreError("the file is empty")
    if len(data) > MAX_BYTES:
        raise StoreError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    root = _root()
    with _LOCK:
        cleanup(root)
        tpath = root / "tickets" / f"{_digest(ticket)}.json"
        meta = _read_json(tpath)
        if meta is None:
            raise StoreError("unknown or expired upload link")
        tpath.unlink(missing_ok=True)  # single use, whatever happens next
        user, ws = meta.get("user_id", ""), meta.get("workspace_id", "")
        mine = [s for s in _all(root) if s.user_id == user and s.workspace_id == ws]
        if len(mine) >= MAX_PER_USER:
            for old in sorted(mine, key=lambda s: s.created)[: len(mine) - MAX_PER_USER + 1]:
                shutil.rmtree(old.path, ignore_errors=True)
        handle = "x" + secrets.token_hex(8)
        folder = root / "files" / handle
        folder.mkdir(parents=True)
        (folder / "file.bin").write_bytes(data)
        info = {"handle": handle, "file_name": clean_name(file_name), "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "created": time.time(),
                "user_id": user, "workspace_id": ws}
        _write_json(folder / "meta.json", info)
    return _stored(info, folder)


def _stored(info: dict, folder: Path) -> Stored:
    return Stored(handle=info["handle"], file_name=info["file_name"], size=info["size"],
                  sha256=info["sha256"], created=info["created"], user_id=info["user_id"],
                  workspace_id=info["workspace_id"], path=folder)


def _all(root: Path) -> list[Stored]:
    base = root / "files"
    out = []
    if base.is_dir():
        for entry in base.iterdir():
            info = _read_json(entry / "meta.json")
            if info and HANDLE_RE.match(info.get("handle", "")):
                out.append(_stored(info, entry))
    return out


def get(handle: str, user_id: str, workspace_id: str) -> Stored | None:
    """The upload ``handle`` of this caller, or None (unknown, expired or not theirs)."""
    if not HANDLE_RE.match(handle or ""):
        return None
    root = _root()
    folder = root / "files" / handle
    info = _read_json(folder / "meta.json")
    if info is None:
        return None
    if time.time() - info.get("created", 0) > HANDLE_TTL:
        with _LOCK:
            shutil.rmtree(folder, ignore_errors=True)
        return None
    if info.get("user_id") != user_id or info.get("workspace_id") != workspace_id:
        return None
    return _stored(info, folder)


def mine(user_id: str, workspace_id: str) -> list[Stored]:
    root = _root()
    now = time.time()
    return sorted((s for s in _all(root) if s.user_id == user_id
                   and s.workspace_id == workspace_id and now - s.created <= HANDLE_TTL),
                  key=lambda s: s.created, reverse=True)


_NAME_BAD = re.compile(r"[^\w.\- ()]+")


def clean_name(name: str) -> str:
    """A display name for an uploaded file: the last path part, its UTF-8
    restored when a client sent it as Latin-1, unsafe characters replaced."""
    raw = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    try:
        fixed = raw.encode("latin-1").decode("utf-8")
        if fixed != raw:
            raw = fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    cleaned = _NAME_BAD.sub("_", raw).strip(" .")
    return cleaned[:120] or "upload"
