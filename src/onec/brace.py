"""The brace format of 1C metadata texts: `{1,{1,0,<uuid>},"Name",{2,"ru","Имя"}}`.

Parsed into nested lists: a quoted value is a :class:`Quoted` (a `str`), a bare
one (number, uuid) a plain `str`. Iterative, so a deep file cannot exhaust the
stack; nesting past :data:`MAX_DEPTH` is refused.
"""

from __future__ import annotations

import re

MAX_DEPTH = 4000
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NIL = "00000000-0000-0000-0000-000000000000"


class Quoted(str):
    """A value that was written in double quotes."""


def parse(text: str) -> list:
    """The top-level values of ``text`` (usually one list). Malformed tails are
    closed as if the braces were balanced."""
    root: list = []
    stack: list[list] = [root]
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if len(stack) > MAX_DEPTH:
                raise ValueError("nesting too deep")
            node: list = []
            stack[-1].append(node)
            stack.append(node)
            i += 1
        elif ch == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
        elif ch == '"':
            j, buf = i + 1, []
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            stack[-1].append(Quoted("".join(buf)))
            i = j + 1
        elif ch in ", \t\r\n﻿":
            i += 1
        else:
            j = i
            while j < n and text[j] not in ',{}"\r\n':
                j += 1
            stack[-1].append(text[i:j].strip())
            i = j
    return root


def headers(tree: list) -> list[tuple[str, str, dict[str, str], str]]:
    """Every object header in ``tree``, in document order: (uuid, name, synonyms
    by language, comment).

    A metadata object starts with `{<n>,0,<uuid>},"<Name>",{<k>,"<lang>","<text>",…},"<Comment>"`.
    """
    out: list[tuple[str, str, dict[str, str], str]] = []
    stack: list[tuple[list, int]] = [(tree, 0)]
    while stack:
        node, k = stack.pop()
        while k < len(node):
            item = node[k]
            k += 1
            if not isinstance(item, list):
                continue
            if (len(item) == 3 and isinstance(item[2], str) and not isinstance(item[2], Quoted)
                    and UUID.match(item[2])
                    and k + 1 < len(node) and isinstance(node[k], Quoted)
                    and isinstance(node[k + 1], list)):
                comment = node[k + 2] if k + 2 < len(node) and isinstance(node[k + 2], Quoted) else ""
                out.append((item[2], str(node[k]), synonyms(node[k + 1]), str(comment)))
            stack.append((node, k))
            stack.append((item, 0))
            break
    return out


def synonyms(node: list) -> dict[str, str]:
    """`{2,"ru","Имя","en","Name"}` → {"ru": "Имя", "en": "Name"}."""
    vals = [v for v in node[1:] if isinstance(v, Quoted)]
    return {str(vals[i]): str(vals[i + 1]) for i in range(0, len(vals) - 1, 2)}


def header_of(text: str, uuid: str = "") -> tuple[str, dict[str, str], str] | None:
    """(name, synonyms, comment) of the object ``uuid`` described in ``text``,
    else of the first object header in it."""
    try:
        found = headers(parse(text))
    except ValueError:
        return None
    for u, name, syn, comment in found:
        if not uuid or u == uuid:
            return name, syn, comment
    return None
