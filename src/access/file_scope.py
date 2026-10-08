"""Per-project file scope: which files of a repository a project looks at.

A project can narrow each of its repositories with ``include_globs`` and
``exclude_globs`` (``ProjectRepo``). The rule is the research-access one
(:func:`src.access.resolver.glob_match`): a pattern without a wildcard is a
folder/file prefix, ``**`` crosses folders.

  * ``exclude`` always wins;
  * an empty ``include`` means "everything";
  * a non-empty ``include`` is exhaustive — a path outside it is out of scope.

The scope only ever NARROWS what the caller may read; it is applied on top of
the research-access decision, never instead of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.access.resolver import glob_match

MAX_GLOBS = 50
MAX_GLOB_LEN = 200


@dataclass(frozen=True)
class FileScope:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.include and not self.exclude

    def allows(self, rel_path: str) -> bool:
        if any(glob_match(rel_path, g) for g in self.exclude):
            return False
        if self.include:
            return any(glob_match(rel_path, g) for g in self.include)
        return True


def normalize_globs(globs: Iterable[object] | None) -> list[str]:
    """Trimmed, de-duplicated, non-empty patterns. ValueError past the caps."""
    out: list[str] = []
    for g in globs or []:
        text = str(g or "").strip().replace("\\", "/")
        if not text:
            continue
        if len(text) > MAX_GLOB_LEN:
            raise ValueError(f"a pattern is longer than {MAX_GLOB_LEN} characters")
        if text not in out:
            out.append(text)
    if len(out) > MAX_GLOBS:
        raise ValueError(f"at most {MAX_GLOBS} patterns per list")
    return out


def scope_of(include: Iterable[object] | None, exclude: Iterable[object] | None) -> FileScope:
    return FileScope(
        include=tuple(str(g) for g in (include or []) if str(g).strip()),
        exclude=tuple(str(g) for g in (exclude or []) if str(g).strip()),
    )


def scopes_for_links(links: Iterable[object]) -> dict[str, FileScope]:
    """``{repo_slug: FileScope}`` for the ``ProjectRepo`` rows that narrow
    anything (rows without patterns are left out)."""
    out: dict[str, FileScope] = {}
    for link in links:
        scope = scope_of(getattr(link, "include_globs", None), getattr(link, "exclude_globs", None))
        if not scope.is_empty:
            out[str(link.repo_slug)] = scope
    return out


def apply_scopes(access: Mapping[str, object], scopes: Mapping[str, FileScope] | None) -> dict:
    """Research-access decisions with the project scope laid over them."""
    out = dict(access)
    for slug, scope in (scopes or {}).items():
        dec = out.get(slug)
        if dec is not None and hasattr(dec, "with_scope"):
            out[slug] = dec.with_scope(scope)
    return out
