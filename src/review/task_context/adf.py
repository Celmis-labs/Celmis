"""Atlassian Document Format to plain, markdown-ish text.

Jira Cloud's REST v3 returns descriptions and comments as an ADF tree. The
model reads text, and the criteria parser (criteria.py) reads the same
markdown-ish shape a pull-request description has: `## Heading`, `- bullet`,
`- [ ] task`. So that is what comes out.

The input is somebody else's document, so the conversion is defensive:

* depth and node count are capped (a hand-built 100 000-node tree costs a
  bounded amount of work, and what is cut is marked, never silently lost);
* attachments and media become `[attachment omitted]`, never a fetched URL;
* a link or inline card is its text and its URL as text — nothing is
  fetched, nothing is followed;
* control characters, zero-width characters and bidi overrides are removed
  (they hide instructions from the human who reads the same text);
* it never raises: a node it does not know contributes its children's text.
"""

from __future__ import annotations

import re
from typing import Any

MAX_DEPTH = 24
MAX_NODES = 5000
OMITTED = "[attachment omitted]"
CUT = "[… cut]"

#: C0/C1 controls except tab and newline; zero-width and joiner characters;
#: bidi embeddings, overrides and isolates; the BOM.
_INVISIBLE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)
_BLANK_RUNS = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """`text` without invisible and control characters, line endings made `\\n`."""
    return _INVISIBLE.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0
        self.cut = False

    def spend(self) -> bool:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.cut = True
            return False
        return True


def adf_to_text(doc: Any, *, max_chars: int | None = None) -> str:
    """The text of an ADF document (or of a plain string, cleaned).

    `max_chars` cuts the result, on a line boundary where there is one."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        text = clean_text(doc).strip()
    elif isinstance(doc, dict):
        budget = _Budget()
        text = _blocks(doc.get("content") if doc.get("type") == "doc" else [doc],
                       budget, 0, "")
        text = _BLANK_RUNS.sub("\n\n", clean_text(text)).strip()
        if budget.cut:
            text = f"{text}\n{CUT}"
    elif isinstance(doc, list):
        text = "\n".join(t for t in (adf_to_text(x) for x in doc) if t)
    else:
        text = clean_text(str(doc)).strip()
    if max_chars is not None and len(text) > max_chars:
        head = text[:max_chars]
        cut = head.rfind("\n")
        text = (head[:cut] if cut > max_chars * 0.6 else head).rstrip() + f"\n{CUT}"
    return text


def _blocks(nodes: Any, budget: _Budget, depth: int, indent: str) -> str:
    out: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict) or not budget.spend():
            if budget.cut:
                break
            continue
        text = _block(node, budget, depth + 1, indent)
        if text:
            out.append(text)
    return "\n\n".join(out)


def _inline(nodes: Any, budget: _Budget, depth: int) -> str:
    parts: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict) or not budget.spend():
            if budget.cut:
                break
            continue
        parts.append(_inline_node(node, budget, depth + 1))
    return "".join(parts)


def _inline_node(node: dict, budget: _Budget, depth: int) -> str:
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = str(node.get("text") or "")
        for mark in node.get("marks") or []:
            if isinstance(mark, dict) and mark.get("type") == "link":
                href = str((mark.get("attrs") or {}).get("href") or "")
                if href and href != text:
                    return f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return str(attrs.get("text") or "@someone")
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "status":
        return f"[{attrs.get('text') or 'status'}]"
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return str(attrs.get("url") or "")
    if kind == "date":
        return str(attrs.get("timestamp") or "")
    if kind in ("media", "mediaInline", "mediaSingle", "mediaGroup"):
        return OMITTED
    if node.get("content") and depth < MAX_DEPTH:
        return _inline(node.get("content"), budget, depth)
    return ""


def _block(node: dict, budget: _Budget, depth: int, indent: str) -> str:
    if depth > MAX_DEPTH:
        budget.cut = True
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content")
    if kind == "paragraph":
        return indent + _inline(content, budget, depth)
    if kind == "heading":
        try:
            level = min(6, max(1, int(attrs.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        return f"{'#' * level} {_inline(content, budget, depth).strip()}"
    if kind in ("bulletList", "orderedList"):
        return _list(node, budget, depth, indent, ordered=kind == "orderedList")
    if kind == "taskList":
        return _tasks(node, budget, depth, indent)
    if kind == "codeBlock":
        return f"{indent}```\n{_inline(content, budget, depth)}\n{indent}```"
    if kind == "blockquote":
        inner = _blocks(content, budget, depth, "")
        return "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
    if kind in ("panel", "expand", "nestedExpand"):
        title = str(attrs.get("title") or "")
        inner = _blocks(content, budget, depth, indent)
        return f"{indent}{title}\n{inner}".strip("\n") if title else inner
    if kind == "table":
        rows = []
        for row in content or []:
            if isinstance(row, dict) and budget.spend():
                cells = [
                    _blocks((c or {}).get("content"), budget, depth + 1, "").replace("\n\n", " ").replace("\n", " ")
                    for c in row.get("content") or [] if isinstance(c, dict)
                ]
                rows.append(" | ".join(cells))
        return "\n".join(rows)
    if kind == "rule":
        return "---"
    if kind in ("media", "mediaSingle", "mediaGroup"):
        return OMITTED
    if kind in ("listItem", "taskItem", "decisionItem", "decisionList", "tableRow",
                "tableCell", "tableHeader", "layoutSection", "layoutColumn"):
        return _blocks(content, budget, depth, indent)
    # Unknown node: whatever text it holds.
    if content:
        return _blocks(content, budget, depth, indent) or _inline(content, budget, depth)
    return _inline_node(node, budget, depth)


def _item_text(item: dict, budget: _Budget, depth: int, indent: str) -> str:
    """A list item: its first paragraph on the bullet line, nested lists below."""
    lines: list[str] = []
    for child in item.get("content") or []:
        if not isinstance(child, dict) or not budget.spend():
            if budget.cut:
                break
            continue
        if child.get("type") in ("bulletList", "orderedList", "taskList"):
            lines.append(_block(child, budget, depth + 1, indent + "  "))
        elif child.get("type") == "paragraph":
            lines.append(_inline(child.get("content"), budget, depth + 1))
        else:
            lines.append(_block(child, budget, depth + 1, ""))
    return "\n".join(x for x in lines if x)


def _list(node: dict, budget: _Budget, depth: int, indent: str, *, ordered: bool) -> str:
    rows = []
    start = int((node.get("attrs") or {}).get("order") or 1) if ordered else 1
    for n, item in enumerate(node.get("content") or [], start):
        if not isinstance(item, dict) or not budget.spend():
            if budget.cut:
                break
            continue
        text = _item_text(item, budget, depth, indent)
        first, _, rest = text.partition("\n")
        marker = f"{n}." if ordered else "-"
        rows.append(f"{indent}{marker} {first}" + (f"\n{rest}" if rest else ""))
    return "\n".join(rows)


def _tasks(node: dict, budget: _Budget, depth: int, indent: str) -> str:
    rows = []
    for item in node.get("content") or []:
        if not isinstance(item, dict) or not budget.spend():
            if budget.cut:
                break
            continue
        if item.get("type") == "taskList":      # nested
            rows.append(_tasks(item, budget, depth + 1, indent + "  "))
            continue
        done = str((item.get("attrs") or {}).get("state") or "").upper() == "DONE"
        text = _inline(item.get("content"), budget, depth + 1).replace("\n", " ").strip()
        rows.append(f"{indent}- [{'x' if done else ' '}] {text}")
    return "\n".join(rows)
