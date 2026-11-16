"""Procedures of a BSL module, read the way the indexer reads them.

The indexer's own tokenizer (`_strip`: comments cut, string literals emptied,
`|` continuation lines followed) and header patterns are reused, so a procedure
here starts and ends where `outline` and `read_symbol` say it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.indexing.graph.languages.bsl import _END, _EXPORT, _HEADER, _strip


@dataclass
class Param:
    name: str
    by_value: bool
    default: str | None  # None: the parameter is required


@dataclass
class Procedure:
    name: str
    kind: str            # "Процедура" / "Function" … as written
    start: int           # 1-based line of the header
    end: int             # 1-based line of КонецПроцедуры (or the last line)
    exported: bool
    params: list[Param]
    directives: list[str] = field(default_factory=list)
    code: list[tuple[int, str]] = field(default_factory=list)  # (0-based line, stripped code)

    @property
    def min_args(self) -> int:
        return sum(1 for p in self.params if p.default is None)

    @property
    def max_args(self) -> int:
        return len(self.params)

    def signature(self) -> str:
        parts = []
        for p in self.params:
            s = ("Знач " if p.by_value else "") + p.name
            parts.append(s if p.default is None else f"{s} = {p.default}")
        return f"{self.name}({', '.join(parts)})" + (" Экспорт" if self.exported else "")


def split_top(text: str, sep: str = ",") -> list[str]:
    """``text`` split at ``sep`` outside parentheses, brackets and quotes."""
    out, depth, buf, quoted = [], 0, [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == sep and depth == 0:
                out.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def _closing(text: str, start: int) -> int:
    """Index of the parenthesis closing the one at ``start``, or -1."""
    depth, quoted = 0, False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
    return -1


_VAL = re.compile(r"^(?:Знач|Val)\s+", re.IGNORECASE)


def parse_params(header: str) -> list[Param]:
    """Parameters of a header like `Функция Ф(Знач А, Б = ", ", В = 1) Экспорт`.

    ``header`` may be the stripped code (string literals emptied to `""`) or
    the raw text; a default value is kept as written."""
    open_at = header.find("(")
    if open_at < 0:
        return []
    close = _closing(header, open_at)
    inner = header[open_at + 1: close if close >= 0 else len(header)]
    if not inner.strip():
        return []
    params = []
    for raw in split_top(inner):
        piece = raw.strip()
        if not piece:
            continue
        by_value = bool(_VAL.match(piece))
        piece = _VAL.sub("", piece)
        name, eq, default = piece.partition("=")
        params.append(Param(name.strip(), by_value, default.strip() if eq else None))
    return params


def procedures(text: str) -> list[Procedure]:
    """Every procedure and function of ``text``, in order."""
    lines = text.replace("\r\n", "\n").split("\n")
    kept = split_lines(text)
    out: list[Procedure] = []
    in_string = False
    directives: list[str] = []
    current: Procedure | None = None
    header: dict | None = None
    for i, raw in enumerate(lines):
        code, in_string = _strip(raw, in_string)
        stripped = code.strip()
        if header is not None:
            header["text"] += " " + stripped
            header["raw"] += " " + kept[i][0].strip()
            header["open"] += code.count("(") - code.count(")")
            if header["open"] <= 0:
                current = _finish(header, out)
                header = None
            continue
        if current is not None:
            current.code.append((i, code))
            if _END.match(code):
                current.end = i + 1
                current = None
            continue
        if not stripped:
            directives = []
            continue
        if stripped.startswith("&"):
            directives.append(stripped)
            continue
        m = _HEADER.match(code)
        if m is None:
            continue
        header = {"name": m["name"], "kw": m["kw"], "line": i + 1, "text": stripped,
                  "raw": kept[i][0].strip(), "open": code.count("(") - code.count(")"), "directives": directives}
        directives = []
        if header["open"] <= 0:
            current = _finish(header, out)
            header = None
    for p in out:
        if p.end <= 0:
            p.end = len(lines)
    return out


def _finish(h: dict, out: list[Procedure]) -> Procedure:
    proc = Procedure(name=h["name"], kind=h["kw"], start=h["line"], end=0,
                     exported=bool(_EXPORT.search(h["text"])),
                     params=parse_params(h["raw"]), directives=h["directives"])
    out.append(proc)
    return proc


def body_text(text: str, proc: Procedure) -> str:
    """The raw lines of ``proc`` with trailing whitespace normalized."""
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(ln.rstrip() for ln in lines[proc.start - 1: proc.end])


def strip_module(text: str) -> list[str]:
    """Every line of ``text`` with comments cut and string literals emptied."""
    out, in_string = [], False
    for raw in text.replace("\r\n", "\n").split("\n"):
        code, in_string = _strip(raw, in_string)
        out.append(code)
    return out


def split_lines(text: str) -> list[tuple[str, str]]:
    """(code with its string literals kept, comment) of every line of ``text``.

    A string literal continues on lines that start with `|`; a line inside one
    that starts with `//` is commented out, quotes and all."""
    out: list[tuple[str, str]] = []
    in_string = False
    for line in text.replace("\r\n", "\n").split("\n"):
        pos = 0
        if in_string:
            stripped = line.lstrip()
            if stripped.startswith("//"):
                out.append(("", stripped[2:]))
                continue
        code_end, comment = len(line), ""
        while pos < len(line):
            if in_string:
                end = line.find('"', pos)
                if end < 0:
                    pos = len(line)
                    break
                if line.startswith('""', end):
                    pos = end + 2
                    continue
                in_string = False
                pos = end + 1
                continue
            q, c = line.find('"', pos), line.find("//", pos)
            if c >= 0 and (q < 0 or c < q):
                code_end, comment = c, line[c + 2:]
                break
            if q < 0:
                break
            in_string = True
            pos = q + 1
        out.append((line[:code_end], comment))
    return out


def string_literals(text: str) -> list[tuple[int, str]]:
    """(1-based line where it starts, value) of every string literal of
    ``text``, a multi-line one joined with its `|` prefixes removed."""
    out: list[tuple[int, str]] = []
    buf: list[str] | None = None
    start = 0
    for i, (code, _comment) in enumerate(split_lines(text)):
        pos = 0
        if buf is not None:
            s = code.lstrip()
            pos = len(code) - len(s) + (1 if s.startswith("|") else 0)
        while pos <= len(code):
            if buf is not None:
                end = code.find('"', pos)
                while end >= 0 and code.startswith('""', end):
                    end = code.find('"', end + 2)
                if end < 0:
                    buf.append(code[pos:].replace('""', '"'))
                    buf.append("\n")
                    break
                buf.append(code[pos:end].replace('""', '"'))
                out.append((start, "".join(buf)))
                buf = None
                pos = end + 1
                continue
            q = code.find('"', pos)
            if q < 0:
                break
            buf, start, pos = [], i + 1, q + 1
    if buf is not None:
        out.append((start, "".join(buf)))
    return out
