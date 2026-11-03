"""Helpers shared by the per-language fact extractors."""

from __future__ import annotations

import re
from collections.abc import Iterator

from src.indexing.graph.xrepo.model import HTTP_METHODS, normalize_path

#: A URL read from source: literal pieces and the names of what was spliced in.
Parts = list[tuple[str, str]]  # ("lit", text) | ("var", name)

_AUTHORITY = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:)?//([^/?#]*)(.*)$", re.I | re.S)


def lit(text: str) -> Parts:
    return [("lit", text)]


def var(name: str) -> Parts:
    return [("var", name)]


def merge(*chunks: Parts) -> Parts:
    out: Parts = []
    for chunk in chunks:
        for kind, value in chunk:
            if kind == "lit" and out and out[-1][0] == "lit":
                out[-1] = ("lit", out[-1][1] + value)
            else:
                out.append((kind, value))
    return out


def text_of(node) -> str:  # noqa: ANN001
    return node.text.decode("utf-8", "replace") if node is not None else ""


def line_of(node) -> int:  # noqa: ANN001
    return node.start_point[0] + 1


def walk(node) -> Iterator:  # noqa: ANN001, ANN201
    """Every node below `node`, parents before children."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def url_from_parts(parts: Parts) -> tuple[str, str, str] | None:
    """-> (host, host_hint, normalised path), or None when no path is readable.

    `host` is a literal host without port. `host_hint` is the NAME of the
    variable the URL starts with (`BILLING_URL`), which is the only thing a
    reviewer can say about where an unresolved URL goes.
    """
    hint = ""
    if parts and parts[0][0] == "var":
        hint = parts[0][1]
        parts = parts[1:]
    text = "".join(v if k == "lit" else "{}" for k, v in parts)
    host = ""
    m = _AUTHORITY.match(text) if not hint else None
    if m:
        authority, text = m.group(1), m.group(2)
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if "{" in host:
            host = ""
    if not text.startswith("/"):
        if not (hint or host):
            return None
        text = "/" + text
    if text == "/" and not (hint or host):
        return None
    return host, hint, normalize_path(text)


def method_word(value: str) -> str:
    v = value.strip().upper()
    return v if v in HTTP_METHODS else ""


def clean_hint(name: str) -> str:
    """`process.env.BILLING_URL` / `self.base_url` -> the last name."""
    bits = [b for b in re.split(r"[.\[\]'\"() ]+", name or "") if b]
    return bits[-1] if bits else ""
