"""Opaque, stateless pagination cursors.

A cursor is `h.off.s`: `h` fingerprints the arguments the first page was
asked with (so a cursor cannot be replayed against a different question),
`off` is where the next page starts in the ranked list, and `s` is a short
digest of the index revisions of the repositories that list was ranked on. If
any of them moved the offset no longer points at the same rows, so the call
answers page 1 and says why instead of silently skipping or repeating hits.

A tool that walks the repositories in batches (`grep`) hands out `h.off.s.b.r`
once it is past the first batch: `b` is the index of the batch's first
repository in the caller's ordered list and `r` digests the repositories before
it. If one of those appeared or vanished, `b` no longer points at the same
repository, so the call answers page 1 and says why. A cursor with `off` 0
carries no revision check: nothing of the batch has been read yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

STALE_NOTE = "cursor stale: index moved; showing page 1"
STALE_PART_NOTE = "cursor stale: index moved; showing the first page of this part"
REPOS_MOVED_NOTE = "cursor stale: the list of repositories changed; showing page 1"
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


def roster_digest(slugs: Sequence[str]) -> str:
    """Eight hex chars over an ordered list of repositories (order matters)."""
    return hashlib.sha256("\n".join(slugs).encode()).hexdigest()[:8]


def encode(h: str, off: int, shas: Mapping[str, str], *, batch: int = 0,
           before: Sequence[str] = ()) -> str:
    """`<query hash>.<offset>.<pin digest>[.<batch start>.<roster digest>]`:
    at most about 40 characters, whatever the fleet size. `before` is the
    ordered list of repositories ahead of the batch that starts at `batch`."""
    base = f"{h}.{int(off)}.{pin_digest(shas)}"
    if batch > 0:
        return f"{base}.{int(batch)}.{roster_digest(before)}"
    return base


def decode(cursor: str) -> dict[str, Any] | None:
    parts = (cursor or "").split(".")
    if len(parts) not in (3, 5) or len(cursor) > 64 or not parts[1].isdigit():
        return None
    out: dict[str, Any] = {"h": parts[0], "off": int(parts[1]), "s": parts[2],
                           "b": 0, "r": ""}
    if len(parts) == 5:
        if not parts[3].isdigit() or int(parts[3]) <= 0:
            return None
        out["b"], out["r"] = int(parts[3]), parts[4]
    return out


def start_of(cursor: str, h: str, slugs: Sequence[str]) -> tuple[int, str]:
    """(index of the first repository of the cursor's batch, note).

    0 for page 1, no cursor, a cursor of another query or garbage (`resolve`
    words those). A batch cursor whose repositories-before digest no longer
    matches `slugs` - one appeared or vanished ahead of it - answers (0, note):
    start over instead of skipping or repeating repositories. Call it BEFORE
    searching, because the batch decides which repositories are searched."""
    data = decode(cursor)
    if data is None or data.get("h") != h or data["b"] <= 0:
        return 0, ""
    if data["b"] >= len(slugs) or data["r"] != roster_digest(slugs[:data["b"]]):
        return 0, REPOS_MOVED_NOTE
    return int(data["b"]), ""


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
    if data["off"] == 0 and data["b"] > 0:
        return 0, ""  # the start of a batch: nothing of it was read, nothing to be stale
    if data.get("s") != pin_digest(shas):
        return 0, STALE_NOTE
    return int(data["off"]), ""
