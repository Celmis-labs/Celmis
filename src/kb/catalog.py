"""A repository's documentation read back with its front matter.

Generated notes and imported cards live side by side in the vault; this reads
the front matter of every note once and gives each the same shape, so a list,
a filter and the link graph treat them alike. Filtering here is exact and
case-blind, on the fields people ask for (type, status, domain, author, lang,
id, a 1C object) and on any other key of a card's own front matter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

from src.kb.objects import canonical
from src.vault.reader import vault_note_files

logger = logging.getLogger(__name__)

#: Notes read from one vault. The docs API lists at most this many as well.
MAX_NOTES = 2000

#: Filter keys answered from Celmis's own fields; anything else is looked up in
#: the card's front matter as written.
KNOWN_FILTERS = ("type", "status", "domain", "author", "lang", "id", "object", "tag")


@dataclass
class NoteInfo:
    path: str  # inside the vault: modules/sales.md, kb/processes/reserve.md
    title: str
    type: str
    status: str = ""  # effective status of a card; "" for a generated note
    card: bool = False
    kb: dict[str, Any] = field(default_factory=dict)
    fm: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""  # the repository path a generated note documents
    cross_refs: list[str] = field(default_factory=list)
    #: A module note's callers (``callers_modules``): the notes that call into it.
    callers: list[str] = field(default_factory=list)
    #: Every repository path a generated note was written from (a process note
    #: spans several); a caller whose rules hide one of them does not see the note.
    source_paths: list[str] = field(default_factory=list)
    updated_at: str = ""
    words: int = 0

    @property
    def generated(self) -> bool:
        """Written by Celmis from the code (a process note), not by a person."""
        return self.kb.get("origin") == "generate"

    def denied_by(self, dec: Any) -> bool:  # noqa: ANN401
        """Does ``dec`` (an access decision) hide a path this note documents?"""
        paths = ([self.source_path] if self.source_path else []) + self.source_paths
        return any(dec.path_denied(p) for p in paths)

    def summary(self) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "title": self.title, "type": self.type, "card": self.card}
        if self.card:
            d.update({
                "origin": self.kb.get("origin", ""),
                "status": self.status, "id": self.kb.get("id", ""),
                "domain": self.kb.get("domain", ""), "objects": self.kb.get("objects", []),
                "stale": (self.kb.get("staleness") or {}).get("state") == "stale",
            })
        return d


def title_of(meta: dict, body: str, path: Path) -> str:
    for key in ("title", "name", "module"):
        value = meta.get(key)
        if value:
            return str(value)
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem.replace("_", " ").replace("-", " ")


def effective_status(kb: dict) -> str:
    """A card's status, with a stale card reading as ``needs-review``."""
    if (kb.get("staleness") or {}).get("state") == "stale":
        return "needs-review"
    return str(kb.get("status") or "")


def load(directory: Path, full: Path) -> tuple[NoteInfo, str] | None:
    """(NoteInfo, body) of one note file, or None when it cannot be read."""
    try:
        post = frontmatter.load(full)
    except Exception:  # noqa: BLE001 — one unreadable note is not an outage
        return None
    meta = post.metadata if isinstance(post.metadata, dict) else {}
    kb = meta.get("kb") if isinstance(meta.get("kb"), dict) else {}
    fm = meta.get("fm") if isinstance(meta.get("fm"), dict) else {}
    refs = meta.get("cross_refs") or []
    info = NoteInfo(
        path=str(full.relative_to(directory)),
        title=title_of(meta, post.content, full),
        type=str(meta.get("type") or ""),
        status=effective_status(kb) if kb else "",
        card=bool(kb),
        kb=kb,
        fm=fm,
        source_path=str(meta.get("path") or ""),
        cross_refs=[str(r) for r in refs] if isinstance(refs, list) else [],
        callers=[str(r) for r in meta.get("callers_modules") or []]
        if isinstance(meta.get("callers_modules"), list) else [],
        source_paths=[str(p) for p in kb.get("files") or []]
        if kb.get("origin") == "generate" and isinstance(kb.get("files"), list) else [],
        updated_at=str(meta.get("updated_at") or meta.get("analyzed_at") or ""),
        words=len(post.content.split()),
    )
    return info, post.content


def scan(directory: Path) -> list[NoteInfo]:
    """Every readable note of the vault at ``directory``, in path order."""
    out: list[NoteInfo] = []
    for full in vault_note_files(directory)[:MAX_NOTES]:
        got = load(directory, full)
        if got is not None:
            out.append(got[0])
    return out


def _fold(v: Any) -> str:  # noqa: ANN401
    return str(v).strip().lower()


def matches(note: NoteInfo, filters: dict[str, str]) -> bool:
    """Does ``note`` satisfy every filter? Unknown keys read the card's front matter."""
    for key, want in filters.items():
        if want in (None, ""):
            continue
        w = _fold(want)
        if key == "type":
            if _fold(note.type) != w and _fold(note.fm.get("type", "")) != w:
                return False
        elif key == "status":
            if _fold(note.status) != w:
                return False
        elif key == "object":
            obj = canonical(want) or want
            if obj not in (note.kb.get("objects") or []):
                return False
        elif key == "tag":
            if w not in [_fold(t) for t in note.kb.get("tags") or []]:
                return False
        elif key in ("domain", "author", "lang", "id"):
            if _fold(note.kb.get(key, "")) != w:
                return False
        else:
            value = note.fm.get(key)
            if isinstance(value, list):
                if w not in [_fold(x) for x in value]:
                    return False
            elif value is None or _fold(value) != w:
                return False
    return True


def parse_where(text: str) -> dict[str, str]:
    """``domain=sales owner=ops`` → a filter dict. Values may be quoted."""
    import shlex

    out: dict[str, str] = {}
    try:
        parts = shlex.split(text or "")
    except ValueError:
        parts = (text or "").split()
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k:
                out[k[:60]] = v.strip()[:200]
    return out
