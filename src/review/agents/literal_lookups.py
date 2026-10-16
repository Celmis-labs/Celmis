"""Evaluate `name[3][1]` against the literal that defines `name`, for the veto.

Why this exists. A finding can claim that one branch of a `switch` read the
wrong slot of a lookup table: `$rates[3][1]` "is already used by the other
branch". The table is defined a few lines above, in the very window the
verifier was shown, and so may be the second table the result is compared with.
The verifier can still keep the finding, because the check that refutes it is
arithmetic (take the third row, its second element, compare with the other
table's entry) and a small model asked to hold a claim and a table in its head
at once believes the claim. It is a general failure, not a PHP one:
off-by-one and copy-paste findings about index and key expressions are the
bulk of what the defect agents write about, and "the index matches the
mapping defined above" is the first refutation the verifier prompt lists.

So the evidence gatherer evaluates those lookups itself. For every
`name[<literal>]...` on the lines around the finding, it finds the nearest
earlier assignment of `name` to an array/map literal (`[...]`, `array(...)`,
`{...}`), parses the literal, follows the subscripts, and hands the verifier the
computed value next to the expression. A computed line is a fact the verifier
may cite; it is not a verdict — nothing here decides that a finding is wrong.

Deliberately small and conservative: only scalar leaves (numbers, quoted
strings, true/false/null), only literal subscripts, only a definition that is
not reassigned between its line and the use, and nothing at all when the
literal cannot be parsed. A wrong "computed" fact would be worse than none.
"""

from __future__ import annotations

import re

#: Subscript chains `name[1][2]` / `name['k']` with literal subscripts only.
_LOOKUP = re.compile(
    r"(?<![\w$>:.])(\$?[A-Za-z_]\w*)"
    r"((?:\[\s*(?:-?\d+|'[^'\n]*'|\"[^\"\n]*\")\s*\]){1,3})"
)
_ASSIGNED = re.compile(r"\s*(?:[-+.*/%|&^]|\?\?|<<|>>)?=(?![=>])|\s*(?:\+\+|--)")
_SUBSCRIPT = re.compile(r"\[\s*(-?\d+|'[^'\n]*'|\"[^\"\n]*\")\s*\]")

#: Lines scanned upward for the definition; the cap keeps a huge file cheap.
MAX_LOOKBACK_LINES = 400

#: Longest literal that will be parsed.
MAX_LITERAL_CHARS = 2_000

#: Lookups reported for one finding.
MAX_LOOKUPS = 12

_OPENERS = {"[": "]", "{": "}", "(": ")"}


class _Unparsed(Exception):
    """The literal is not one this module will evaluate."""


def _balanced(text: str, start: int) -> int | None:
    """Index just past the bracket group that opens at `start`, or None."""
    stack: list[str] = []
    quote = ""
    i = start
    limit = min(len(text), start + MAX_LITERAL_CHARS)
    while i < limit:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in _OPENERS:
            stack.append(_OPENERS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return None


class _Parser:
    """Recursive descent over `[1 => [5, 2], 'k' => 3]` style literals."""

    _NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
    _STRING = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
    _WORD = re.compile(r"(?i)true|false|null|none")

    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def _ws(self) -> None:
        while self.i < len(self.s):
            if self.s[self.i].isspace():
                self.i += 1
            elif self.s.startswith("//", self.i) or self.s[self.i] == "#":
                nl = self.s.find("\n", self.i)
                self.i = len(self.s) if nl < 0 else nl + 1
            else:
                return

    def parse(self) -> object:
        value = self._value()
        self._ws()
        if self.i != len(self.s):
            raise _Unparsed
        return value

    def _scalar(self) -> object:
        for rx in (self._STRING, self._NUMBER, self._WORD):
            m = rx.match(self.s, self.i)
            if m:
                self.i = m.end()
                if rx is self._STRING:
                    return m.group(1) if m.group(1) is not None else m.group(2)
                if rx is self._NUMBER:
                    t = m.group(0)
                    return float(t) if "." in t else int(t)
                return m.group(0).lower()
        raise _Unparsed

    def _value(self) -> dict:
        self._ws()
        if self.s.lower().startswith("array", self.i):
            j = self.i + 5
            while j < len(self.s) and self.s[j].isspace():
                j += 1
            if j < len(self.s) and self.s[j] == "(":
                self.i = j
        if self.i >= len(self.s):
            raise _Unparsed
        opener = self.s[self.i]
        if opener not in _OPENERS:
            return {"leaf": self._scalar()}
        closer = _OPENERS[opener]
        self.i += 1
        items: dict[object, dict] = {}
        auto = 0
        while True:
            self._ws()
            if self.i >= len(self.s):
                raise _Unparsed
            if self.s[self.i] == closer:
                self.i += 1
                return {"items": items}
            first = self._value()
            self._ws()
            key: object
            if self.s.startswith("=>", self.i) or self.s.startswith(":", self.i):
                self.i += 2 if self.s.startswith("=>", self.i) else 1
                if "leaf" not in first:
                    raise _Unparsed
                key = first["leaf"]
                value = self._value()
            else:
                key, value = auto, first
            if isinstance(key, int):
                auto = max(auto, key + 1)
            items[key] = value
            self._ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1


def _subscript_key(raw: str) -> object:
    raw = raw.strip()
    if raw[0] in "'\"":
        return raw[1:-1]
    return int(raw)


def _render(node: dict, limit: int = 60) -> str:
    if "leaf" in node:
        return repr(node["leaf"]) if isinstance(node["leaf"], str) else str(node["leaf"])
    inner = ", ".join(f"{k!r}: {_render(v, limit)}" for k, v in node["items"].items())
    text = "[" + inner + "]"
    return text if len(text) <= limit else text[: limit - 1] + "…]"


def _definition(lines: list[str], name: str, before: int) -> tuple[int, dict] | None:
    """(1-based line, parsed literal) of the nearest assignment of `name` to a
    literal above line `before`, unless `name` is reassigned in between."""
    head = re.compile(
        r"(?<![\w$>:.])" + re.escape(name) + r"\s*(?::?=)\s*(?:array\s*\(|[\[{])", re.I)
    reassigned = re.compile(
        r"(?<![\w$>:.])" + re.escape(name) + r"\s*(?:\[[^\]\n]*\]\s*)*(?:[-+.*/]|\?\?)?=(?![=>])")
    lo = max(0, before - 1 - MAX_LOOKBACK_LINES)
    for idx in range(before - 1, lo - 1, -1):
        m = head.search(lines[idx])
        if not m:
            continue
        chunk = "\n".join(lines[idx: idx + 60])
        # The match ends on the opening bracket (`array(` on its parenthesis).
        pos = m.end() - 1
        end = _balanced(chunk, pos)
        if end is None:
            return None
        literal = chunk[pos:end]
        try:
            node = _Parser(literal).parse()
        except _Unparsed:
            return None
        for between in lines[idx + literal.count("\n") + 1: before - 1]:
            if reassigned.search(between):
                return None
        return idx + 1, node
    return None


def resolve_lookups(lines: list[str], line: int, radius: int = 10) -> list[str]:
    """Computed values of literal lookups on the lines around `line`.

    Each entry reads `$map[3][1] = 4  (line 59; $map defined at line 37)`.
    An empty list when there is nothing to evaluate, which is most findings.
    """
    if not lines:
        return []
    center = min(max(int(line or 1), 1), len(lines))
    lo, hi = max(1, center - radius), min(len(lines), center + radius)
    out: list[str] = []
    seen: set[str] = set()
    defs: dict[tuple[str, int], tuple[int, dict] | None] = {}
    for n in range(lo, hi + 1):
        for m in _LOOKUP.finditer(lines[n - 1]):
            name, chain = m.group(1), m.group(2)
            if _ASSIGNED.match(lines[n - 1], m.end()):
                continue  # a write, not a read: `$t[1] = 99`, `$t[0] .= 'x'`
            expr = name + re.sub(r"\s+", "", chain)
            if expr in seen:
                continue
            key = (name, n)
            if key not in defs:
                defs[key] = _definition(lines, name, n)
            found = defs[key]
            if found is None:
                continue
            def_line, node = found
            try:
                for sub in _SUBSCRIPT.findall(chain):
                    if "items" not in node:
                        raise KeyError(sub)
                    node = node["items"][_subscript_key(sub)]
            except KeyError:
                continue
            seen.add(expr)
            out.append(f"{expr} = {_render(node)}  (line {n}; {name} defined at line {def_line})")
            if len(out) >= MAX_LOOKUPS:
                return out
    return out
