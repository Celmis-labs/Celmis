"""The identifiers a finding's claim is about, for folding findings that share a root cause.

Why this exists. Several findings in one file, under one rule, can describe
one defect from different angles: "the total check runs only when `$coupon` is
set", "`if ($coupon)` lets a cart without a coupon skip the check" and "`&&
$coupon` means the check does not run". It is one defect, a guard written on
the wrong variable, reported at three places, and the title-token comparison
that folds copies down a file scores them near 0.0, because three sentences
about one variable share no words. A markup pair (a `<figure>` left open at one
line, its caption left outside it at another) scores 0.0 for the same reason.

What the sentences DO share is the thing they are about: the variable, the
function, the tag. Those are written the same way by every agent (in
backticks, with a `$`, as `Class::NAME`, as `<tag>`), which is why they work
where wording does not. This module extracts them and decides when two
findings name enough of the same ones to be the same defect.

The test is built to be wrong on the side of keeping both comments:

* a key that every file has (`$data`, `$result`, `$this`, a bare `<div>`) is
  discarded, so it cannot tie two unrelated findings together;
* the shared keys must cover at least half of the SMALLER finding's keys, so
  two findings that each name several things and happen to overlap in one
  stay apart;
* the call sites bound the comparison to findings of one file and one rule.

Two findings about the same builtin (`Array.isArray`) in two different
functions share nothing here: the function names differ and the builtin is a
call, not a key.
"""

from __future__ import annotations

import re

from src.review.agents.verifier_evidence import _BACKTICK, _DOLLAR, _IDENT, _SCOPE

_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9]{0,9})\b")

#: Tags every template has; sharing one says nothing about a root cause.
_COMMON_TAGS = frozenset([
    "div", "span", "p", "br", "hr", "li", "ul", "ol", "td", "tr", "th",
    "table", "tbody", "thead", "b", "i", "u", "em", "strong", "input", "img",
    "script", "style", "html", "body", "head", "form", "label", "button",
    "php", "string", "int", "array", "list", "map", "set", "dict",
])

#: Names that are variables in every file. `$request` in two findings of one
#: file is two findings about a request, not one finding.
_GENERIC = frozenset([
    "data", "result", "results", "value", "values", "item", "items", "row",
    "rows", "key", "keys", "val", "args", "params", "request", "response",
    "count", "index", "list", "self", "this", "tmp", "temp", "name", "type",
    "text", "str", "obj", "arr", "array", "node", "error", "err", "res",
    "req", "ret", "line", "lines", "file", "path", "input", "output", "user",
    "id", "ids", "url", "null", "true", "false", "none", "class", "function",
    "return", "string", "int", "bool", "foreach", "while", "else", "elseif",
    "if", "for", "isset", "empty",
])

#: A key must be at least this long without its sigil.
MIN_KEY_LENGTH = 3

#: Share of the smaller finding's keys that must also be the other's.
MIN_KEY_OVERLAP = 0.5


def _usable(name: str) -> bool:
    bare = name.lstrip("$#.").removesuffix("()")
    return len(bare) >= MIN_KEY_LENGTH and bare.lower() not in _GENERIC


def root_cause_keys(title: str, body: str) -> frozenset[str]:
    """Backticked identifiers, `$variables`, `Class::member` and `<tag>`s that
    the finding's title and body name, minus the ones too common to mean
    anything. Empty when it names none, in which case it merges with nothing."""
    text = "\n".join(s for s in (title, body) if s)
    keys: set[str] = set()
    for m in _BACKTICK.finditer(text):
        tok = m.group(1).strip()
        if _IDENT.match(tok) and _usable(tok):
            keys.add(tok.removesuffix("()"))
    for m in _DOLLAR.finditer(text):
        if _usable(m.group(0)):
            keys.add(m.group(0))
    for m in _SCOPE.finditer(text):
        keys.add(f"{m.group(1)}::{m.group(2)}")
    for m in _TAG.finditer(text):
        tag = m.group(1).lower()
        if tag not in _COMMON_TAGS:
            keys.add(f"<{tag}>")
    return frozenset(keys)


def share_a_root_cause(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two key sets name enough of the same things to be one defect."""
    if not a or not b:
        return False
    shared = a & b
    return bool(shared) and len(shared) / min(len(a), len(b)) >= MIN_KEY_OVERLAP
