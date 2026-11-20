"""Chunking of vault notes for the vector index.

A note used to be ONE point: a metadata header plus the first 6000 characters,
so the tail of a long note (an imported card, a long module note) could never
be found by meaning. Now a note is a few points:

* chunk 0 is exactly the text the single point used to carry (header + start),
  and keeps the note's own id, so short notes and old indexes behave as before;
* the rest of the content is cut into windows on paragraph / heading
  boundaries with a small overlap, each prefixed with a short header (path,
  type, module, title) so it embeds in context.

Every chunk carries the note's whole payload plus ``chunk`` and ``chunks``;
readers collapse chunks back to one hit per note (see :func:`collapse_chunks`).
A point without ``chunk`` is chunk 0 of a one-chunk note.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: What chunk 0 covers of the content — the old single-point limit.
HEAD_CHARS = 6000
#: Target / hard size of a later window, in characters (about 1200 tokens).
WINDOW_CHARS = 4500
#: Characters repeated at the start of the next window.
OVERLAP_CHARS = 300
#: Points per note, chunk 0 included (about 55k characters of content).
MAX_CHUNKS = 12
#: Readers ask Qdrant for this many times the limit, then collapse to notes.
CHUNK_OVERFETCH = 3


def chunk_point_id(note_id: str, index: int) -> str:
    """Deterministic id of a note's chunk; chunk 0 keeps the note's id."""
    if index == 0:
        return note_id
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{note_id}:{index}"))


def chunk_point_ids(note_id: str) -> list[str]:
    """Every id a note could occupy (for deleting by id)."""
    return [chunk_point_id(note_id, i) for i in range(MAX_CHUNKS)]


def _cut(text: str, start: int, window: int) -> int:
    """End of the window starting at ``start``: a paragraph break, else a line
    break, near the target size; a hard cut only for an unbroken run."""
    end = min(start + window, len(text))
    if end >= len(text):
        return len(text)
    floor = start + window // 2
    for sep in ("\n\n", "\n#", "\n"):
        at = text.rfind(sep, floor, end)
        if at != -1:
            return at + 1 if sep == "\n#" else at + len(sep)
    return end


def tail_windows(content: str) -> list[str]:
    """Windows covering the content after the head (chunk 0), uncapped."""
    if len(content) <= HEAD_CHARS:
        return []
    out: list[str] = []
    start = max(0, HEAD_CHARS - OVERLAP_CHARS)
    nl = content.find("\n", start, HEAD_CHARS)  # open on a line boundary
    if nl != -1:
        start = nl + 1
    while start < len(content):
        end = _cut(content, start, WINDOW_CHARS)
        piece = content[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(content):
            break
        nxt = end - OVERLAP_CHARS
        nl = content.find("\n", nxt, end)
        nxt = nl + 1 if nl != -1 else nxt
        start = nxt if nxt > start else end
    return out


def _header(payload: dict[str, Any]) -> str:
    kb = payload.get("kb") if isinstance(payload.get("kb"), dict) else {}
    parts = [f"Path: {payload.get('note_path', '')}", f"Type: {payload.get('type', '')}"]
    if payload.get("module"):
        parts.append(f"Module: {payload['module']}")
    if payload.get("title"):
        parts.append(f"Title: {payload['title']}")
    if payload.get("repo"):
        parts.append(f"Repo: {payload['repo']}")
    status = kb.get("effective_status") or kb.get("status")
    if status:
        parts.append(f"Status: {status}")
    return "\n".join(parts)


def expand_note(
    note_id: str, embed_text: str, payload: dict[str, Any], full_content: str | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(point_id, text, payload)`` for each chunk of one note.

    ``embed_text`` is chunk 0 as the writer builds it; ``full_content`` is the
    note body (None → a single point, the old behaviour)."""
    tails = tail_windows(full_content) if full_content else []
    if len(tails) + 1 > MAX_CHUNKS:
        logger.info(
            "note_chunks_capped path=%s windows=%d kept=%d",
            payload.get("note_path", "?"), len(tails) + 1, MAX_CHUNKS,
        )
        tails = tails[: MAX_CHUNKS - 1]
    total = 1 + len(tails)
    head = _header(payload)
    texts = [embed_text, *(f"{head}\n\n{t}" for t in tails)]
    return [
        (chunk_point_id(note_id, i), text, {**payload, "chunk": i, "chunks": total})
        for i, text in enumerate(texts)
    ]


def collapse_chunks(points: list[Any], limit: int | None = None) -> list[Any]:
    """One point per note — the best-scoring chunk — in score order.

    ``points`` are Qdrant scored points (payload with ``note_path`` and
    ``repo``); points without a note path are kept as they are."""
    seen: dict[tuple[str, str], Any] = {}
    loose: list[Any] = []
    for p in points:
        pl = p.payload or {}
        path = pl.get("note_path")
        if not path:
            loose.append(p)
            continue
        key = (str(pl.get("repo", "")), str(path))
        cur = seen.get(key)
        if cur is None or p.score > cur.score:
            seen[key] = p
    out = sorted([*seen.values(), *loose], key=lambda p: p.score, reverse=True)
    return out[:limit] if limit else out
