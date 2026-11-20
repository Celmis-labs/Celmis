"""1C object names on cards: one spelling, the graph, and what changed since.

A card names the objects it is about the way people write them:
``Документ.ЗаказКлиента``, ``Document.ЗаказКлиента``, ``Documents.ЗаказКлиента``.
The graph names a metadata object after the directory of a Designer dump,
``Documents.ЗаказКлиента``, and that is the canonical form here: every type
word the indexer knows (Russian or English, singular or plural, any case) maps
to it through the same table the indexer uses, so a card and the code agree.

Staleness compares commits, not texts: a card says which commit of the
configuration it was written against (``source: erp@<sha>``); the objects it
names are looked up in ``git diff <sha>..HEAD`` of that repository's clone, and
a card whose objects changed since is due for review. A repository uploaded as
an archive has no history and the answer is "unknown", said as such.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.indexing.graph.languages.onec import TYPE_BY_NAME, owner_object

logger = logging.getLogger(__name__)


def canonical(name: str) -> str | None:
    """``Documents.X`` for any spelling of ``<Type>.<X>``; None when the first
    segment is not a 1C metadata type. Deeper segments (a form, a module) are
    dropped: a card is about the object."""
    parts = [p.strip() for p in (name or "").strip().split(".")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    plural = TYPE_BY_NAME.get(parts[0].lower())
    return f"{plural}.{parts[1]}" if plural else None


def canonical_all(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """(canonical names in order without repeats, names that are not 1C objects)."""
    good: list[str] = []
    bad: list[str] = []
    for n in names:
        c = canonical(n)
        if c is None:
            bad.append(n)
        elif c not in good:
            good.append(c)
    return good, bad


Lookup = Callable[[str], dict | None]


def graph_lookup(code_repo: str) -> tuple[Lookup | None, Callable[[], None]]:
    """A lookup of canonical names in ``code_repo``'s graph, and its closer.

    ``(None, noop)`` when the repository has no graph yet: nothing can be
    checked, which is not the same as "every object is missing".
    """
    from src.config import get_settings

    path = get_settings().repo_graph_path(code_repo)
    if not path.exists():
        return None, lambda: None
    from src.indexing.graph.graph_store import make_graph_store

    store = make_graph_store(path)

    def lookup(name: str) -> dict | None:
        try:
            rows = store.find_named(name, limit=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb_object_lookup_failed repo=%s err=%s", code_repo, type(exc).__name__)
            return None
        for row in rows:
            if row.get("name") == name:
                return {"kind": row.get("kind") or "", "path": row.get("file") or ""}
        return None

    def close() -> None:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            logger.debug("kb_graph_close_failed repo=%s", code_repo)

    return lookup, close


# ─── staleness ────────────────────────────────────────────────────────


@dataclass
class Staleness:
    """What a check of one card against its configuration found."""

    state: str  # fresh | stale | unknown
    reason: str = ""
    changed: list[str] = field(default_factory=list)
    head: str = ""

    def as_dict(self) -> dict:
        d: dict = {"state": self.state}
        if self.reason:
            d["reason"] = self.reason
        if self.changed:
            d["changed"] = self.changed
        if self.head:
            d["head"] = self.head
        return d


class ChangeIndex:
    """Objects changed in one repository since a commit, computed once per commit."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self._head: str | None = None
        self._cache: dict[str, tuple[set[str] | None, str]] = {}
        self._paths: dict[str, tuple[set[str] | None, str]] = {}

    def head(self) -> str:
        if self._head is None:
            from src.sync.incremental import _git_head

            self._head = _git_head(self.repo_path) or ""
        return self._head

    def changed_since(self, sha: str) -> tuple[set[str] | None, str]:
        """(objects changed in ``sha..HEAD``, "") or (None, why it is unknown)."""
        if sha in self._cache:
            return self._cache[sha]
        from src.sync.incremental import _commit_present, _git_diff

        result: tuple[set[str] | None, str]
        head = self.head()
        if not (self.repo_path / ".git").exists():
            result = (None, "the configuration has no git history (uploaded archive)")
        elif not head:
            result = (None, "the configuration's clone has no commit")
        elif head.startswith(sha):
            result = (set(), "")
        elif not _commit_present(self.repo_path, sha):
            result = (None, f"commit {sha[:12]} is not in the clone (too old or another branch)")
        else:
            diff = _git_diff(self.repo_path, sha, head)
            if diff is None:
                result = (None, "git diff failed")
            else:
                objs: set[str] = set()
                for entry in diff:
                    for rel in entry[1:]:
                        obj = owner_object(rel)
                        if obj:
                            objs.add(obj)
                result = (objs, "")
        self._cache[sha] = result
        return result

    def changed_paths_since(self, sha: str) -> tuple[set[str] | None, str]:
        """(files changed in ``sha..HEAD``, "") or (None, why it is unknown).

        The same question as :meth:`changed_since`, asked of files: a note about
        code that is not a 1C configuration is checked against these."""
        if sha in self._paths:
            return self._paths[sha]
        _objs, why = self.changed_since(sha)
        result: tuple[set[str] | None, str]
        if _objs is None:
            result = (None, why)
        else:
            from src.sync.incremental import _git_diff

            head = self.head()
            diff = [] if head.startswith(sha) else _git_diff(self.repo_path, sha, head)
            if diff is None:
                result = (None, "git diff failed")
            else:
                result = ({rel for entry in diff for rel in entry[1:]}, "")
        self._paths[sha] = result
        return result


def staleness(index: ChangeIndex | None, objects: list[str], sha: str) -> Staleness:
    """Is a card about ``objects``, written at ``sha``, out of date?"""
    if not objects:
        return Staleness("unknown", "the card names no 1C objects")
    if not sha:
        return Staleness("unknown", "the card has no source commit")
    if index is None:
        return Staleness("unknown", "no configuration repository is set for this knowledge base")
    changed, why = index.changed_since(sha)
    if changed is None:
        return Staleness("unknown", why, head=index.head())
    hit = sorted(o for o in objects if o in changed)
    if hit:
        return Staleness("stale", "objects changed after the source commit", hit, index.head())
    return Staleness("fresh", head=index.head())
