"""One markdown file in, one checked card out.

A card is a markdown file with optional YAML front matter. Celmis reads a few
keys itself and keeps every other key as it was written, so a team's own
fields (an owner, a migration target, a ticket) survive the import and can be
filtered on later.

Keys Celmis reads:

  ``type``       what the card is (``process``, ``adr``, ``summary`` …); a short
                 lowercase word, default ``note``;
  ``id``         a stable name other cards link to; default: the file name;
  ``title``      default: the first heading, else the file name;
  ``status``     ``draft`` | ``verified`` | ``needs-review``; default ``draft``;
  ``domain``, ``author``, ``lang`` — plain strings, filterable;
  ``tags``       a list of strings;
  ``objects_1c`` the 1C metadata objects the card is about
                 (``Документ.ЗаказКлиента``, ``Documents.ЗаказКлиента`` …);
  ``related``    ids or paths of other cards;
  ``replaces``   generated notes this card stands in for
                 (``modules/sales.md``): search shows the card, not both;
  ``source``     the commit the card was written against (``erp@3f2c1a9``).

The front matter is untrusted input: YAML aliases are refused (a few lines of
them expand to gigabytes), its size and every list are capped, and values are
reduced to JSON types before anything stores them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

#: Front matter larger than this is not front matter.
MAX_FRONT_MATTER_CHARS = 16_000
#: Items kept from any one list (objects, related, tags, a team's own lists).
MAX_LIST = 200
#: Characters kept from any one string value.
MAX_VALUE_CHARS = 500
#: Keys kept from the front matter, and depth of nested mappings.
MAX_KEYS = 100
MAX_DEPTH = 4

STATUSES = ("draft", "verified", "needs-review")
_STATUS_ALIASES = {
    "draft": "draft", "чернетка": "draft", "черновик": "draft", "wip": "draft",
    "verified": "verified", "checked": "verified", "approved": "verified",
    "перевірено": "verified", "проверено": "verified",
    "needs-review": "needs-review", "needs_review": "needs-review",
    "review": "needs-review", "stale": "needs-review",
    "на перегляд": "needs-review", "на пересмотр": "needs-review",
}

#: Note types the documentation generator writes. A card may not take one: the
#: generator reads `module` notes back to group features and would mistake a
#: card for a module it documented.
RESERVED_TYPES = frozenset({"module", "feature", "integration", "overview", "security"})

_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ID_RE = re.compile(r"^[\w.-]{1,120}$", re.UNICODE)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FENCE = "---"


class CardProblem(ValueError):
    """A file that cannot become a card, with a reason a person can act on."""


@dataclass
class Card:
    """A checked card. ``fm`` is the front matter as written (JSON types only)."""

    src_path: str
    body: str
    fm: dict[str, Any]
    type: str
    id: str
    title: str
    status: str
    domain: str = ""
    author: str = ""
    lang: str = ""
    tags: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    source: str = ""
    source_sha: str = ""
    sha: str = ""
    warnings: list[str] = field(default_factory=list)


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors and aliases."""

    def compose_node(self, parent, index):  # noqa: ANN001, ANN201
        if self.check_event(yaml.AliasEvent):
            raise CardProblem("front matter uses a YAML alias; write the value out")
        event = self.peek_event()
        if getattr(event, "anchor", None):
            raise CardProblem("front matter uses a YAML anchor; write the value out")
        return super().compose_node(parent, index)


def split_front_matter(text: str) -> tuple[str | None, str]:
    """(front matter text or None, body). The block opens on the first line."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() in (_FENCE, "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).lstrip("\n")
    raise CardProblem("front matter opens with --- and never closes")


def _plain(value: Any, depth: int = 0) -> Any:  # noqa: ANN401
    """``value`` reduced to JSON types, with every size capped."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if depth >= MAX_DEPTH:
        return str(value)[:MAX_VALUE_CHARS]
    if isinstance(value, list | tuple | set):
        return [_plain(v, depth + 1) for v in list(value)[:MAX_LIST]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:MAX_KEYS]:
            out[str(k)[:100]] = _plain(v, depth + 1)
        return out
    return str(value)[:MAX_VALUE_CHARS]


def parse_front_matter(raw: str) -> dict[str, Any]:
    if len(raw) > MAX_FRONT_MATTER_CHARS:
        raise CardProblem(f"front matter is longer than {MAX_FRONT_MATTER_CHARS} characters")
    try:
        data = yaml.load(raw, Loader=_NoAliasLoader)  # noqa: S506 — SafeLoader subclass
    except CardProblem:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 2})" if mark is not None else ""
        raise CardProblem(f"front matter is not valid YAML{where}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CardProblem("front matter must be a mapping of keys to values")
    return _plain(data)


def _text(value: Any) -> str:  # noqa: ANN401
    if value is None or isinstance(value, dict | list):
        return ""
    return str(value).strip()[:MAX_VALUE_CHARS]


def _strings(value: Any) -> list[str]:  # noqa: ANN401
    """A list of non-empty strings from a list or a comma-separated string."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    out: list[str] = []
    for item in items:
        s = _text(item)
        if s and s not in out:
            out.append(s)
    return out[:MAX_LIST]


def normalize_status(value: str) -> str:
    """One of :data:`STATUSES`, or the value as written when it is none of them."""
    v = (value or "").strip().lower()
    return _STATUS_ALIASES.get(v, v)


def source_sha(source: str) -> str:
    """The commit a ``source`` value names: ``repo@<sha>``, ``<sha>``, or ""."""
    s = (source or "").strip()
    tail = s.rsplit("@", 1)[-1].strip().lower()
    return tail if _SHA_RE.match(tail) else ""


def _slug(text: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-.")
    return s[:120] or "card"


def _title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            t = line.lstrip("#").strip()
            if t:
                return t[:MAX_VALUE_CHARS]
    return fallback


def parse_card(src_path: str, text: str) -> Card:
    """The card stored at ``src_path`` (a path inside the knowledge base).

    Raises :class:`CardProblem` for a file that cannot be read as one; a card
    that can be read but has something odd about it carries ``warnings``.
    """
    raw, body = split_front_matter(text)
    fm = parse_front_matter(raw) if raw is not None else {}
    warnings: list[str] = []
    stem = src_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    ctype = _text(fm.get("type")).lower() or "note"
    if not _TYPE_RE.match(ctype):
        warnings.append(f"type {ctype!r} is not a short lowercase word; using 'note'")
        ctype = "note"
    elif ctype in RESERVED_TYPES:
        warnings.append(f"type {ctype!r} is the generator's own; stored as 'kb-{ctype}'")
        ctype = f"kb-{ctype}"

    cid = _text(fm.get("id"))
    if cid and not _ID_RE.match(cid):
        warnings.append(f"id {cid!r} has spaces or symbols; using {_slug(cid)!r}")
        cid = _slug(cid)
    cid = cid or _slug(stem)

    status = normalize_status(_text(fm.get("status")) or "draft")
    if status not in STATUSES:
        warnings.append(f"status {status!r} is not one of {', '.join(STATUSES)}")

    objects = _strings(fm.get("objects_1c", fm.get("objects")))
    src = _text(fm.get("source"))
    sha = source_sha(src)
    if src and not sha:
        warnings.append(f"source {src!r} names no commit (expected repo@<sha>)")

    return Card(
        src_path=src_path,
        body=body,
        fm=fm,
        type=ctype,
        id=cid,
        title=_text(fm.get("title")) or _title(body, stem.replace("-", " ").replace("_", " ")),
        status=status,
        domain=_text(fm.get("domain")),
        author=_text(fm.get("author")),
        lang=_text(fm.get("lang")).lower(),
        tags=_strings(fm.get("tags")),
        objects=objects,
        related=_strings(fm.get("related")),
        replaces=_strings(fm.get("replaces")),
        source=src,
        source_sha=sha,
        sha=hashlib.sha256(text.encode("utf-8")).hexdigest()[:20],
        warnings=warnings,
    )
