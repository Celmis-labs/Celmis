"""Links between notes: what a note points at, and what points back.

Five kinds of link, read from what people and the generator already write:

  * ``related`` — ids (or paths) a card lists in its front matter;
  * ``link``    — a markdown link to another note, ``[text](../other.md)``, or a
                  wiki link, ``[[proc-reserve-goods]]`` / ``[[Title]]``;
  * ``replaces``— a card standing in for a generated note;
  * ``ref``     — a generated note's ``cross_refs`` (a feature and its modules);
  * ``called-by`` — a module note's ``callers_modules`` (who calls into it).

A target is looked up in the note's own vault first. A card kept in a knowledge
base of its own is bound to a code repository, and a plain path that names
nothing in its own vault is then looked up in the code repository's vault (the
generated note it replaces, the module it is about). ``<repo>:<path>`` names a
note of another repository outright. Which other repositories may be reached is
the caller's to say (``foreign``): a repository the caller may not read is as
good as absent.

Targets are resolved against the vault: by path, by card id, by file name, by
title. A target that names nothing in the vault is reported once as dangling,
not silently dropped, because a typo in a link is a fact about the card.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.kb.catalog import NoteInfo

_MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>#]+?\.md)>?(?:#[^)\s]*)?\s*(?:\"[^\"]*\")?\)")
_REPO_REF = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*):(?!//)(.+)$", re.DOTALL)
_WIKI = re.compile(r"\[\[([^\]\|#\n]{1,200})(?:#[^\]\|\n]*)?(?:\|[^\]\n]*)?\]\]")
#: Links one note may contribute; past this the rest are ignored.
MAX_LINKS_PER_NOTE = 300
#: Notes drawn in one graph.
MAX_NODES = 600


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    kind: str  # related | link | replaces | ref | called-by
    #: The repository the target is in; "" = the note's own.
    repo: str = ""


#: Another repository's notes, if the caller may read that repository.
Foreign = Callable[[str], "list[NoteInfo] | None"]


class Resolver:
    """Names a note can be reached by, built once per vault."""

    def __init__(self, notes: list[NoteInfo], *, repo: str = "", code_repo: str = "",
                 foreign: Foreign | None = None) -> None:
        self.repo = repo
        self.code_repo = code_repo
        self._foreign = foreign
        self._others: dict[str, Resolver | None] = {}
        self.info = {n.path: n for n in notes}
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

    def other(self, repo: str) -> Resolver | None:
        """The resolver of another repository's vault, or None when the caller may not read it."""
        if not repo or repo == self.repo or self._foreign is None:
            return None
        if repo not in self._others:
            infos = self._foreign(repo)
            self._others[repo] = Resolver(infos, repo=repo) if infos is not None else None
        return self._others[repo]

    def resolve_in(self, ref: str, *, base: str = "") -> tuple[str, str] | None:
        """(repository, path) of ``ref``; the repository is "" for the note's own."""
        r = (ref or "").strip()
        m = _REPO_REF.match(r) if "://" not in r else None
        if m:
            repo, rest = m.group(1), m.group(2)
            if repo == self.repo:
                got = self.resolve(rest, base=base)
                return ("", got) if got else None
            other = self.other(repo)
            got = other.resolve(rest) if other is not None else None
            return (repo, got) if got else None
        got = self.resolve(r, base=base)
        if got:
            return "", got
        other = self.other(self.code_repo)
        if other is not None:
            got = other.resolve(r)
            if got:
                return self.code_repo, got
        return None


#: Kinds the front matter declares, and the ones read off the text.
_DECLARED = ("related", "replaces")
_PLAIN = ("ref", "link")


def links_of(note: NoteInfo, body: str, resolver: Resolver) -> tuple[list[Link], list[str]]:
    """(links out of ``note``, references that name no note)."""
    out: list[Link] = []
    dangling: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    # The card's own path in its knowledge base: relative links are written
    # against it, and it is the vault path without the "kb/" prefix.
    base = note.path

    def add(ref: str, kind: str) -> None:
        if len(out) >= MAX_LINKS_PER_NOTE:
            return
        got = resolver.resolve_in(ref, base=base)
        if got is None:
            if ref not in dangling and kind not in ("ref", "called-by"):
                dangling.append(ref)
            return
        repo, target = got
        # A link in the body to a note the front matter already names says
        # nothing new: one edge per pair, the declared kind winning.
        key = (repo, target, "plain" if kind in _PLAIN else kind)
        if (not repo and target == note.path) or key in seen:
            return
        if kind in _PLAIN and any((repo, target, k) in seen for k in _DECLARED):
            return
        seen.add(key)
        out.append(Link(note.path, target, kind, repo))

    for ref in note.kb.get("related") or []:
        add(str(ref), "related")
    for ref in note.kb.get("replaces") or []:
        add(str(ref), "replaces")
    for ref in note.cross_refs:
        add(ref, "ref")
    for ref in note.callers:
        add(ref, "called-by")
    for m in _MD_LINK.finditer(body):
        add(m.group(1), "link")
    for m in _WIKI.finditer(body):
        add(m.group(1), "link")
    return out, dangling


def _external_node(resolver: Resolver, e: Link) -> dict:
    other = resolver.other(e.repo)
    info = other.info.get(e.target) if other is not None else None
    node = info.summary() if info is not None else {"title": e.target, "type": "", "card": False}
    node.update({"path": f"{e.repo}:{e.target}", "note_path": e.target, "repo": e.repo, "external": True})
    return node


def graph(notes: list[tuple[NoteInfo, str]], *, repo: str = "", code_repo: str = "",
          foreign: Foreign | None = None) -> dict:
    """Nodes and edges of a whole vault (capped), with each note's dangling refs.

    A link to a note of another repository is an edge to an external node, named
    ``<repo>:<path>`` and marked ``external`` with its ``repo``."""
    infos = [n for n, _ in notes][:MAX_NODES]
    resolver = Resolver(infos, repo=repo, code_repo=code_repo, foreign=foreign)
    edges: list[Link] = []
    dangling: dict[str, list[str]] = {}
    for info, body in notes[:MAX_NODES]:
        found, missing = links_of(info, body, resolver)
        edges.extend(found)
        if missing:
            dangling[info.path] = missing
    kept = {n.path for n in infos}
    external: dict[str, dict] = {}
    out_edges: list[dict] = []
    for e in edges:
        if e.repo:
            node = _external_node(resolver, e)
            external.setdefault(node["path"], node)
            out_edges.append({"source": e.source, "target": node["path"], "kind": e.kind, "repo": e.repo})
        elif e.target in kept:
            out_edges.append({"source": e.source, "target": e.target, "kind": e.kind})
    return {
        "nodes": [n.summary() for n in infos] + list(external.values()),
        "edges": out_edges,
        "dangling": dangling,
        "omitted": max(0, len(notes) - MAX_NODES),
    }


def neighbours(path: str, notes: list[tuple[NoteInfo, str]], *, repo: str = "", code_repo: str = "",
               foreign: Foreign | None = None,
               bound: dict[str, list[tuple[NoteInfo, str]]] | None = None) -> dict:
    """Links out of and back into the note at ``path``.

    ``bound``: the notes of knowledge bases bound to this repository as their
    code repository (repository -> its notes the caller may read): their cards'
    links into this vault are back-links too, each with its ``repo``."""
    resolver = Resolver([n for n, _ in notes], repo=repo, code_repo=code_repo, foreign=foreign)
    out: list[dict] = []
    back: list[dict] = []
    dangling: list[str] = []
    titles = {n.path: n.title for n, _ in notes}
    for info, body in notes:
        found, missing = links_of(info, body, resolver)
        if info.path == path:
            for e in found:
                row = {"path": e.target, "kind": e.kind}
                if e.repo:
                    other = resolver.other(e.repo)
                    target = other.info.get(e.target) if other is not None else None
                    row.update({"title": target.title if target else "", "repo": e.repo})
                else:
                    row["title"] = titles.get(e.target, "")
                out.append(row)
            dangling = missing
        else:
            back.extend({"path": e.source, "title": titles.get(e.source, ""), "kind": e.kind}
                        for e in found if not e.repo and e.target == path)
    own = [n for n, _ in notes]
    for other_repo, pairs in sorted((bound or {}).items()):
        if not repo or other_repo == repo:
            continue
        r2 = Resolver([n for n, _ in pairs], repo=other_repo, code_repo=repo,
                      foreign=lambda r, own=own, here=repo: own if r == here else None)
        names = {n.path: n.title for n, _ in pairs}
        for info, body in pairs:
            found, _missing = links_of(info, body, r2)
            back.extend({"path": e.source, "title": names.get(e.source, ""), "kind": e.kind,
                         "repo": other_repo} for e in found if e.repo == repo and e.target == path)
    return {"out": out, "back": back, "dangling": dangling}
