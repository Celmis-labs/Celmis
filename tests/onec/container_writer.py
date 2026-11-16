"""Write 1C containers for tests: the format `src.onec.container` reads.

Synthetic only. A container is the 16-byte header, the table of contents at
offset 16, then for every entry its header document (dates and UTF-16LE name)
and its data document, each one block. ``deflate`` raw-deflates the data, as
the top level of an external file does.
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime

NONE = 0x7FFFFFFF
EPOCH = datetime(1, 1, 1)


def ticks(when: datetime) -> int:
    delta = when - EPOCH
    return (delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds) // 100


def _block(data: bytes, nxt: int = NONE, size: int | None = None) -> bytes:
    size = len(data) if size is None else size
    return b"\r\n" + f"{size:08x} {len(data):08x} {nxt:08x} ".encode() + b"\r\n" + data


def _deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def container(entries: list[tuple[str, bytes]], *, deflate: bool = False,
              when: datetime = datetime(2025, 3, 4, 10, 30)) -> bytes:
    """A container of ``entries`` (name, data); data may be another container."""
    stamp = ticks(when)
    docs: list[tuple[bytes, bytes]] = []
    for name, data in entries:
        head = struct.pack("<QQI", stamp, stamp, 0) + name.encode("utf-16-le") + b"\0\0"
        docs.append((head, _deflate(data) if deflate and data else data))
    toc_len = 12 * len(docs)
    pos = 16 + 31 + toc_len
    toc = bytearray()
    body = bytearray()
    for head, data in docs:
        h_at = pos
        hb = _block(head)
        pos += len(hb)
        d_at = pos
        db = _block(data)
        pos += len(db)
        toc += struct.pack("<III", h_at, d_at, NONE)
        body += hb + db
    header = struct.pack("<IIII", NONE, 512, 0, 0)
    return header + _block(bytes(toc)) + bytes(body)


def form_bin(description: str, module: str | None) -> bytes:
    """An ordinary form's `Form.bin`: `form` and `module`, stored as is."""
    entries = [("form", description.encode("utf-8-sig"))]
    if module is not None:
        entries.append(("module", module.encode("utf-8-sig")))
    return container(entries)


def handler(event: int, name: str) -> str:
    """One event binding as a form description holds it."""
    return f"{{{event},e1692cc2-605b-4535-84dd-28440238746c,{{3,\"{name}\",{{1,\"\"}}}}}}"


def form_description(*handlers: str) -> str:
    return "{27,{18,{" + ",".join(handler(i + 1, h) for i, h in enumerate(handlers)) + "}}}"


REPORT_CLASS = "e41aff26-25cf-4bb6-b6c1-3f478a75f374"
PROCESSOR_CLASS = "c3831ec8-d8d5-4f93-8a22-f9bfae07327f"


def header(uuid: str, name: str, synonym: str = "", comment: str = "") -> str:
    syn = f'{{1,"ru","{synonym}"}}' if synonym else "{0}"
    return f'{{0,{{1,0,{uuid}}},"{name}",{syn},"{comment}"}}'


def moxcel(areas: list[str], params: list[str]) -> bytes:
    body = ",".join(f'"{a}",{{1,{{0,{i},0,{i + 1},-1,00000000-0000-0000-0000-00000000000{i}}}}}'
                    for i, a in enumerate(areas))
    cells = ",".join(f'{{"",\"{p}\"}}' for p in params)
    return b"MOXCEL\0" + f"{{{body},{{{cells}}}}}".encode()


def external(name: str, *, kind: str = "report", synonym: str = "", comment: str = "",
             object_module: str | None = None, forms: list[tuple[str, list[str], str | None]] = (),
             templates: list[tuple[str, list[str], list[str]]] = (),
             when: datetime = datetime(2025, 3, 4, 10, 30)) -> bytes:
    """An `.erf` (kind="report") or `.epf`: forms are (name, handlers, module)."""
    main = "11111111-1111-4111-8111-111111111111"
    cls = REPORT_CLASS if kind == "report" else PROCESSOR_CLASS
    entries: list[tuple[str, bytes]] = [
        ("root", f"{{2,{main},}}".encode()),
        (main, f"{{1,{cls},{{3,{header(main, name, synonym, comment)}}}}}".encode("utf-8-sig")),
    ]
    if object_module is not None:
        entries.append((f"{main}.0", container([("text", object_module.encode("utf-8-sig"))],
                                               when=when)))
    for i, (fname, handlers, module) in enumerate(forms):
        u = f"22222222-2222-4222-8222-22222222222{i}"
        entries.append((u, header(u, fname, fname).encode("utf-8-sig")))
        inner = [("form", form_description(*handlers).encode("utf-8-sig"))]
        if module is not None:
            inner.append(("module", module.encode("utf-8-sig")))
        entries.append((f"{u}.0", container(inner, when=when)))
    for i, (tname, areas, params) in enumerate(templates):
        u = f"33333333-3333-4333-8333-33333333333{i}"
        entries.append((u, header(u, tname, tname).encode("utf-8-sig")))
        entries.append((f"{u}.0", moxcel(areas, params)))
    return container(entries, deflate=True, when=when)
