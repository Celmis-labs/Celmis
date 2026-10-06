"""Opaque, stateless pagination cursors.

A cursor is `h.off.s`: `h` fingerprints the arguments the first page was
asked with (so a cursor cannot be replayed against a different question),
`off` is where the next page starts in the ranked list, and `s` is a short
digest of the index revisions of the repositories that list was ranked on. If
any of them moved the offset no longer points at the same rows, so the call
answers page 1 and says why instead of silently skipping or repeating hits.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

STALE_NOTE = "cursor stale: index moved; showing page 1"
MISMATCH_NOTE = "cursor does not belong to this query; showing page 1"


def fingerprint(**args: Any) -> str:
    """Short hash of the question's arguments (cursor and paging excluded)."""
    blob = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def pin_digest(shas: Mapping[str, str]) -> str:
    """Eight hex chars over the sorted (slug, sha) pairs: constant size however
    many repositories the list was ranked on."""
    blob = "\n".join(f"{k}\t{v}" for k, v in sorted(shas.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def encode(h: str, off: int, shas: Mapping[str, str]) -> str:
    """`<query hash>.<offset>.<pin digest>`: about 20 characters, whatever the fleet size."""
    return f"{h}.{int(off)}.{pin_digest(shas)}"


def decode(cursor: str) -> dict[str, Any] | None:
    parts = (cursor or "").split(".")
    if len(parts) == 3 and len(cursor) <= 64 and parts[1].isdigit():
        return {"h": parts[0], "off": int(parts[1]), "s": parts[2]}
    return None


def resolve(cursor: str, h: str, shas: Mapping[str, str]) -> tuple[int, str]:
    """(offset, note) for a cursor handed back by the client.

    An empty cursor is page 1, silently. A cursor that is garbage, belongs to
    another query, or was ranked on other revisions is page 1 plus a note.
    """
    if not cursor:
        return 0, ""
    data = decode(cursor)
    if data is None or data.get("h") != h:
        return 0, MISMATCH_NOTE
    if data.get("s") != pin_digest(shas):
        return 0, STALE_NOTE
    return int(data["off"]), ""
