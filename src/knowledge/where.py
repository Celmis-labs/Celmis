"""``where=`` - filter pack items by their front-matter fields.

Syntax: ``key=value`` terms separated by spaces (or commas), e.g.
``status=verified lang=uk type=process``.

* AND across terms; ``key=a|b`` means "a or b" for that key.
* A front-matter list matches by membership (``tags=billing`` finds
  ``tags: [billing, invoices]``); scalars match by equality.
* Case-insensitive for keys and values; numbers and booleans compare as text.
* ``status`` also matches the normalised status column.
* Quote a value that holds spaces: ``title="Close the month"``.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

MAX_WHERE = 300
MAX_TERMS = 10


class WhereError(ValueError):
    """The ``where`` text is not ``key=value`` terms."""


Clause = tuple[str, frozenset[str]]

#: Front-matter keys that are never shown nor filterable: ``repos`` holds pointers to
#: repositories, which a reader sees only through the repository access gate.
HIDDEN_KEYS = frozenset({"repos"})


def visible_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The front matter without the keys a reader may not be shown here."""
    return {k: v for k, v in (meta or {}).items() if str(k).casefold() not in HIDDEN_KEYS}


def parse(text: str | None) -> list[Clause]:
    """``[(key, {accepted values})]`` of a ``where`` text; ``[]`` for none."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) > MAX_WHERE:
        raise WhereError(f"where is too long ({MAX_WHERE} characters at most)")
    try:
        words = shlex.split(text.replace(",", " "))
    except ValueError as exc:
        raise WhereError("where has an unclosed quote") from exc
    if len(words) > MAX_TERMS:
        raise WhereError(f"where takes {MAX_TERMS} terms at most")
    merged: dict[str, set[str]] = {}
    for word in words:
        key, sep, value = word.partition("=")
        key, value = key.strip().casefold(), value.strip()
        if not sep or not key or not value:
            raise WhereError(f"'{word[:40]}' is not key=value")
        if key in HIDDEN_KEYS:
            raise WhereError(f"{key} cannot be used in where")
        merged.setdefault(key, set()).update(
            v.strip().casefold() for v in value.split("|") if v.strip())
    return [(k, frozenset(v)) for k, v in merged.items()]


def _texts(value: Any) -> set[str]:
    if isinstance(value, list | tuple):
        out: set[str] = set()
        for v in value:
            out |= _texts(v)
        return out
    if value is None or isinstance(value, dict):
        return set()
    if isinstance(value, bool):
        return {"true" if value else "false"}
    return {str(value).strip().casefold()}


def matches(meta: Mapping[str, Any] | None, status: str | None, clauses: list[Clause]) -> bool:
    """Does an item with this front matter satisfy every clause?"""
    if not clauses:
        return True
    folded: dict[str, Any] = {str(k).casefold(): v for k, v in (meta or {}).items()}
    for key, accepted in clauses:
        have = _texts(folded.get(key))
        if key == "status" and status:
            have.add(status.casefold())
        if not (have & accepted):
            return False
    return True


def compact(meta: Mapping[str, Any] | None, skip: frozenset[str] = frozenset(),
            limit: int = 400) -> str:
    """The front-matter fields on one line, ``key=value`` (lists joined by ``,``),
    for an opened item; long values are clipped."""
    parts: list[str] = []
    for key, value in (meta or {}).items():
        if str(key).casefold() in skip:
            continue
        if isinstance(value, list):
            shown = ",".join(str(v) for v in value if not isinstance(v, dict | list))
        elif isinstance(value, dict):
            continue
        else:
            shown = str(value)
        shown = " ".join(shown.split())
        if shown:
            parts.append(f"{key}={shown[:80]}")
    line = " ".join(parts)
    return line if len(line) <= limit else line[:limit - 1] + "…"
