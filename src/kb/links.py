"""Links between notes: what a note points at, and what points back.

Four kinds of link, read from what people and the generator already write:

  * ``related`` — ids (or paths) a card lists in its front matter;
  * ``link``    — a markdown link to another note, ``[text](../other.md)``, or a
                  wiki link, ``[[proc-reserve-goods]]`` / ``[[Title]]``;
  * ``replaces``— a card standing in for a generated note;
  * ``ref``     — a generated note's ``cross_refs`` (a feature and its modules).

Targets are resolved against the vault: by path, by card id, by file name, by
title. A target that names nothing in the vault is reported once as dangling,
not silently dropped, because a typo in a link is a fact about the card.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from src.kb.catalog import NoteInfo

_MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>#]+?\.md)>?(?:#[^)\s]*)?\s*(?:\"[^\"]*\")?\)")
_WIKI = re.compile(r"\[\[([^\]\|#\n]{1,200})(?:#[^\]\|\n]*)?(?:\|[^\]\n]*)?\]\]")
#: Links one note may contribute; past this the rest are ignored.
MAX_LINKS_PER_NOTE = 300
#: Notes drawn in one graph.
MAX_NODES = 600


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    kind: str  # related | link | replaces | ref


class Resolver:
    """Names a note can be reached by, built once per vault."""

    def __init__(self, notes: list[NoteInfo]) -> None:
        self.paths = {n.path for n in notes}
        self.by_id: dict[str, str] = {}
        self.by_stem: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        for n in notes:
            cid = str(n.kb.get("id") or "")
            if cid:
                self.by_id.setdefault(cid.lower(), n.path)
            stem = n.path.rsplit("/", 1)[-1][:-3].lower()
            self.by_stem.setdefault(stem, n.path)
            self.by_title.setdefault(n.title.strip().lower(), n.path)

    def resolve(self, ref: str, *, base: str = "") -> str | None:
        r = (ref or "").strip().replace("\\", "/")
        if not r or "://" in r:
            return None
        key = r.lower()
        if key in self.by_id:
            return self.by_id[key]
        for cand in (r, r if r.endswith(".md") else f"{r}.md"):
            if base and not cand.startswith("/"):
                joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), cand))
                if joined in self.paths:
                    return joined
            norm = posixpath.normpath(cand.lstrip("/"))
            if norm in self.paths:
                return norm
            if f"kb/{norm}" in self.paths:
                return f"kb/{norm}"
        stem = key.rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        return self.by_stem.get(stem) or self.by_title.get(key)


def links_of(note: NoteInfo, body: str, resolver: Resolver) -> tuple[list[Link], list[str]]:
    """(links out of ``note``, references that name no note)."""
    out: list[Link] = []
    dangling: list[str] = []
    seen: set[tuple[str, str]] = set()
    # The card's own path in its knowledge base: relative links are written
    # against it, and it is the vault path without the "kb/" prefix.
    base = note.path

    def add(ref: str, kind: str) -> None:
        if len(out) >= MAX_LINKS_PER_NOTE:
            return
        target = resolver.resolve(ref, base=base)
        if target is None:
            if ref not in dangling and kind != "ref":
                dangling.append(ref)
            return
        if target == note.path or (target, kind) in seen:
            return
        seen.add((target, kind))
        out.append(Link(note.path, target, kind))

    for ref in note.kb.get("related") or []:
        add(str(ref), "related")
    for ref in note.kb.get("replaces") or []:
        add(str(ref), "replaces")
    for ref in note.cross_refs:
        add(ref, "ref")
    for m in _MD_LINK.finditer(body):
        add(m.group(1), "link")
    for m in _WIKI.finditer(body):
        add(m.group(1), "link")
    return out, dangling


def graph(notes: list[tuple[NoteInfo, str]]) -> dict:
    """Nodes and edges of a whole vault (capped), with each note's dangling refs."""
    infos = [n for n, _ in notes][:MAX_NODES]
    resolver = Resolver(infos)
    edges: list[Link] = []
    dangling: dict[str, list[str]] = {}
    for info, body in notes[:MAX_NODES]:
        found, missing = links_of(info, body, resolver)
        edges.extend(found)
        if missing:
            dangling[info.path] = missing
    kept = {n.path for n in infos}
    return {
        "nodes": [n.summary() for n in infos],
        "edges": [{"source": e.source, "target": e.target, "kind": e.kind}
                  for e in edges if e.target in kept],
        "dangling": dangling,
        "omitted": max(0, len(notes) - MAX_NODES),
    }


def neighbours(path: str, notes: list[tuple[NoteInfo, str]]) -> dict:
    """Links out of and back into the note at ``path``."""
    resolver = Resolver([n for n, _ in notes])
    out: list[dict] = []
    back: list[dict] = []
    dangling: list[str] = []
    titles = {n.path: n.title for n, _ in notes}
    for info, body in notes:
        found, missing = links_of(info, body, resolver)
        if info.path == path:
            out = [{"path": e.target, "title": titles.get(e.target, ""), "kind": e.kind}
                   for e in found]
            dangling = missing
        else:
            back.extend({"path": e.source, "title": titles.get(e.source, ""), "kind": e.kind}
                        for e in found if e.target == path)
    return {"out": out, "back": back, "dangling": dangling}
