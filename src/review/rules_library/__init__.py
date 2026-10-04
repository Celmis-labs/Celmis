"""The built-in review rules library — curated rules a workspace can adopt.

Each entry is a ready-made `review_rules` row: a title the agents cite back,
instructions, the severity a violation reports at, the files it is about and
a good / bad example. "Add from library" copies an entry into the workspace
(or one repository) as an active or pending rule; the copy is the
workspace's own from then on — editing it never touches the library, and a
library update never rewrites a rule somebody already adopted.

Pure data and two lookups; no database, no FastAPI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from src.review.rules_library.catalog import CATALOG

#: Languages an entry may be tagged with, in the order the page lists them.
LANGUAGES: tuple[str, ...] = (
    "general", "python", "javascript", "typescript", "vue", "php", "sql",
)

#: Categories (tags) an entry may carry.
TAGS: tuple[str, ...] = (
    "correctness", "security", "performance", "maintainability",
    "reliability", "concurrency", "data",
)


@dataclass(frozen=True)
class LibraryRule:
    """One curated rule. `id` is stable: it is what `source_ref` records."""

    id: str
    title: str
    instructions: str
    severity: str
    languages: tuple[str, ...]
    tags: tuple[str, ...]
    path_glob: str = ""
    examples_good: str = ""
    examples_bad: str = ""

    def as_dict(self) -> dict:
        out = asdict(self)
        out["languages"] = list(self.languages)
        out["tags"] = list(self.tags)
        return out


LIBRARY: tuple[LibraryRule, ...] = tuple(LibraryRule(**entry) for entry in CATALOG)
_BY_ID: dict[str, LibraryRule] = {r.id: r for r in LIBRARY}


def get_library_rule(rule_id: str) -> LibraryRule | None:
    """The entry with this id, or None."""
    return _BY_ID.get(str(rule_id or "").strip())


def search_library(
    q: str | None = None, *, language: str | None = None, tag: str | None = None,
) -> list[LibraryRule]:
    """Entries matching every filter given; `q` is a case-folded substring
    of the title, instructions, languages or tags."""
    needle = (q or "").strip().casefold()
    lang = (language or "").strip().lower()
    tag_ = (tag or "").strip().lower()
    out: list[LibraryRule] = []
    for rule in LIBRARY:
        if lang and lang not in rule.languages:
            continue
        if tag_ and tag_ not in rule.tags:
            continue
        if needle:
            hay = " ".join((rule.title, rule.instructions, *rule.languages,
                            *rule.tags)).casefold()
            if needle not in hay:
                continue
        out.append(rule)
    return out


__all__ = [
    "LANGUAGES", "LIBRARY", "LibraryRule", "TAGS", "get_library_rule",
    "search_library",
]
