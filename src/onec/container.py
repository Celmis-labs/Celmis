"""Read the 1C:Enterprise container format (`.epf`, `.erf`, `Form.bin`, nested entries).

Layout (the 32-bit variant every external file and `Form.bin` uses):

    header       16 bytes: first free block (0x7fffffff), default block size,
                 revision, reserved
    block        b"\\r\\n" + 8 hex (document size) + " " + 8 hex (this block's
                 size) + " " + 8 hex (next block, 0x7fffffff = last) + " \\r\\n",
                 then the block's bytes; a document is a chain of blocks
    table        the document at offset 16: (header address, data address,
                 0x7fffffff) per entry, little-endian u32
    entry header 8 bytes created, 8 bytes modified (ticks of 0.1 ms since
                 0001-01-01, the saving machine's local time), 4 reserved, then
                 the name in UTF-16LE up to a NUL
    entry data   raw deflate at the top level of an external file, stored as is
                 inside a nested container and in `Form.bin`; data that is
                 itself a container nests

The 64-bit variant (16 hex digits per field, 8-byte addresses) is read the same
way. Everything is bounded: input size, entries, nesting depth, the bytes one
entry or the whole file may inflate to, and every block chain is followed at
most once per block, so a crafted file cannot loop or balloon.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_EPOCH = datetime(1, 1, 1)
_BLOCK32 = re.compile(rb"\r\n([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{8}) \r\n")
_BLOCK64 = re.compile(rb"\r\n([0-9A-Fa-f]{16}) ([0-9A-Fa-f]{16}) ([0-9A-Fa-f]{16}) \r\n")


class ContainerError(ValueError):
    """Not a container, or one that breaks a limit; the message says which."""


@dataclass(frozen=True)
class Limits:
    max_input: int = 64 * 1024 * 1024
    max_total: int = 256 * 1024 * 1024
    max_entry: int = 64 * 1024 * 1024
    max_entries: int = 20_000
    max_depth: int = 4


DEFAULT_LIMITS = Limits()


@dataclass
class Entry:
    name: str
    created: datetime | None
    modified: datetime | None
    data: bytes
    children: list[Entry] | None = None
    deflated: bool = False

    def child(self, name: str) -> Entry | None:
        for e in self.children or ():
            if e.name == name:
                return e
        return None


@dataclass
class Container:
    entries: list[Entry] = field(default_factory=list)

    def get(self, name: str) -> Entry | None:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def walk(self, prefix: str = "") -> Iterator[tuple[str, Entry]]:
        """(path, entry) of every entry, nested ones as `parent/child`."""
        stack = [(prefix, e) for e in reversed(self.entries)]
        while stack:
            base, e = stack.pop()
            path = f"{base}/{e.name}" if base else e.name
            yield path, e
            for c in reversed(e.children or ()):
                stack.append((path, c))

    def dates(self) -> tuple[datetime | None, datetime | None]:
        """(earliest created, latest modified) over every entry."""
        made = [e.created for _, e in self.walk() if e.created]
        changed = [e.modified for _, e in self.walk() if e.modified]
        return (min(made) if made else None, max(changed) if changed else None)


def _ticks(raw: int) -> datetime | None:
    if not raw:
        return None
    try:
        return _EPOCH + timedelta(microseconds=raw * 100)
    except OverflowError:
        return None


class _Layout:
    def __init__(self, data: bytes) -> None:
        if len(data) >= 18 and data[:4] == b"\xff\xff\xff\x7f" and _BLOCK32.match(data, 16):
            self.block, self.start, self.none, self.word = _BLOCK32, 16, 0x7FFFFFFF, 4
        elif len(data) >= 22 and data[:8] == b"\xff" * 8 and _BLOCK64.match(data, 20):
            self.block, self.start, self.none, self.word = _BLOCK64, 20, 0xFFFFFFFFFFFFFFFF, 8
        else:
            raise ContainerError("not a 1C container")


def is_container(data: bytes) -> bool:
    try:
        _Layout(data)
    except ContainerError:
        return False
    return True


def _document(data: bytes, addr: int, lay: _Layout, cap: int) -> bytes:
    out = bytearray()
    total: int | None = None
    seen: set[int] = set()
    while addr != lay.none:
        if addr in seen or addr < 0 or addr >= len(data):
            raise ContainerError("broken block chain")
        seen.add(addr)
        m = lay.block.match(data, addr)
        if m is None:
            raise ContainerError("broken block header")
        size, blk, nxt = (int(g, 16) for g in m.groups())
        if total is None:
            total = size
            if total > cap or total > len(data):
                raise ContainerError("entry larger than the file")
        body = m.end()
        out += data[body:body + blk]
        if len(out) >= total:
            break
        addr = nxt
    return bytes(out[: total or 0])


def _inflate(raw: bytes, room: int) -> tuple[bytes, bool]:
    """(data, was deflated). Stored data is returned as is: a deflate stream
    counts only if it decodes to its end with nothing left over."""
    if not raw:
        return raw, False
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(raw, room + 1)
    except zlib.error:
        return raw, False
    if len(out) > room:
        raise ContainerError("an entry inflates past the size limit")
    if not d.eof or d.unused_data or d.unconsumed_tail:
        return raw, False
    return out, True


class _Budget:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.bytes = 0
        self.entries = 0

    def take(self, n: int) -> None:
        self.bytes += n
        self.entries += 1
        if self.bytes > self.limits.max_total:
            raise ContainerError("the file unpacks past the size limit")
        if self.entries > self.limits.max_entries:
            raise ContainerError("too many entries")


def _read(data: bytes, depth: int, budget: _Budget) -> list[Entry]:
    lim = budget.limits
    lay = _Layout(data)
    toc = _document(data, lay.start, lay, lim.max_entry)
    fmt = "<QQQ" if lay.word == 8 else "<III"
    step = 3 * lay.word
    entries: list[Entry] = []
    for i in range(0, len(toc) - step + 1, step):
        head, body, _ = struct.unpack_from(fmt, toc, i)
        if head in (0, lay.none):
            continue
        hdr = _document(data, head, lay, 64 * 1024)
        if len(hdr) < 20:
            raise ContainerError("broken entry header")
        created, modified = struct.unpack_from("<QQ", hdr, 0)
        name = hdr[20:].decode("utf-16-le", errors="replace").split("\0", 1)[0]
        raw = b"" if body == lay.none else _document(data, body, lay, lim.max_entry)
        content, deflated = _inflate(raw, min(lim.max_entry, lim.max_total - budget.bytes))
        budget.take(len(content))
        entry = Entry(name=name, created=_ticks(created), modified=_ticks(modified),
                      data=content, deflated=deflated)
        if depth < lim.max_depth and is_container(content):
            try:
                entry.children = _read(content, depth + 1, budget)
            except ContainerError as exc:
                if "limit" in str(exc) or "too many" in str(exc):
                    raise
                entry.children = None  # looks like one, is not: keep the bytes
        entries.append(entry)
    return entries


def read_container(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Container:
    """Every entry of the container in ``data``. Raises ContainerError."""
    if len(data) > limits.max_input:
        raise ContainerError(f"file larger than {limits.max_input // (1024 * 1024)} MB")
    return Container(_read(data, 0, _Budget(limits)))


def text_of(data: bytes) -> str | None:
    """The text of an entry (UTF-8, BOM optional), or None for binary bytes."""
    if b"\0" in data[:8000]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def form_module(data: bytes, limits: Limits = DEFAULT_LIMITS) -> str | None:
    """The module text of an ordinary form's `Form.bin`, or None (no module, or
    not a container)."""
    try:
        box = read_container(data, limits)
    except ContainerError:
        return None
    entry = box.get("module")
    if entry is None:
        return None
    if entry.children is not None:  # a module stored with its compiled image
        entry = entry.child("text")
        if entry is None:
            return None
    return text_of(entry.data)
