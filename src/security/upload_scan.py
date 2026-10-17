"""The secret scan of an uploaded pack: what is refused, what is waved through, what was allowed.

The output guards of the MCP server (``mcp_redact``) withhold anything that
might be a secret, because a leak there is a leak to a reader we do not control.
A pack upload has a different failure: a refusal stops the WHOLE upload, so a
false positive costs the author the pack. This module reads an upload with the
same rules, then looks again at each line they refuse, and only that line:

* a line with the marker ``celmis:allow-secret`` is a conscious exception
  (an example, a pattern). It is accepted and REPORTED (path, line, rule - never
  the text) so that nobody is surprised later;
* a line whose only "secret" is an obvious placeholder is accepted: ``<token>``,
  ``<paste your key here>``, ``{{VARIABLE}}``, ``{API_KEY}``, ``${VAR}``, ``$VAR``,
  ``****``, ``xxxx``, an ellipsis; the format of a date command (``+%FT%TZ``); a
  regular expression where a value would stand (``password=.*``);
* a Markdown heading that reads as a title ("# Secrets: how we handle them") is
  prose without a value.

How a line is looked at again: each obvious placeholder, and only where a value
stands (after ``=``, ``:``, ``Bearer``, ``--password`` ...), is replaced by one
neutral reference and the line is scanned again by the strict rules. Anything
else on the line that looks like a secret is therefore still found: a real value
next to a placeholder is refused, a placeholder glued to more characters is not a
placeholder, and a random-looking value in angle brackets is a value. Private
keys and vendor token formats are never waved through, only marked.

Only the upload uses this. ``mcp_redact.scan_lines`` and the output redaction are
untouched and as aggressive as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.security.mcp_redact import looks_like_secret_literal, scan_lines

#: The literal text that exempts the line it stands on from the refusal.
ALLOW_MARKER = "celmis:allow-secret"

#: Findings of these kinds are never judged by shape; only the marker can accept them.
_NEVER_WAVED_THROUGH = frozenset({"private-key", "pem-block"})

#: What a placeholder is replaced by for the second look: a reference every rule accepts.
_NEUTRAL = "${PLACEHOLDER}"

#: A placeholder ends where a value ends: at the end of the line, a space, a
#: quote, a closing bracket or a separator. ``<token>abc`` is not a placeholder.
_END = r"""(?=$|[\s"'`),;\]}&])"""

#: Where a value stands: after ``=`` or ``:``, after an authorization scheme, after a password option.
_LEAD = (
    r"""(?P<lead>(?:[=:]|(?i:\b(?:bearer|basic|token|apikey|digest))[ \t]+|"""
    r"""(?<![\w-])(?:--password|--passwd|--pass|-p)[ \t]+)[ \t]*["'`]?)"""
)

_ANGLE = r"<(?P<angle>[^<>\n]{1,80})>"
_DOUBLE_BRACE = r"\{\{[^{}\n]{1,80}\}\}"
_UPPER_BRACE = r"\{(?P<upper>[A-Z][A-Z0-9_]{1,40})\}"
_VARIABLE = r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*"
_MASKED = r"\*{3,}|[xX]{4,}|…|\.{3}|•{3,}"
_PLACEHOLDER = re.compile(
    rf"{_LEAD}(?P<ph>{_ANGLE}|{_DOUBLE_BRACE}|{_UPPER_BRACE}|{_VARIABLE}|{_MASKED}){_END}")

#: A regular expression where a value would stand: ``.*``, ``\S+``, ``\\w+``, ``[^&]*``,
#: ``(.+)``, an alternation of names (``|token=|secret=``). It must be made of nothing else.
_REGEX_UNIT = (
    r"(?:\.|\\{1,2}[A-Za-z]|\[\^?[^\]\n]{1,40}\]|\([^()\n]{0,40}\))(?:[*+?]|\{\d+(?:,\d*)?\})?"
    r"|[*+?^$]|\|[A-Za-z_]{2,24}=?|\|"
)
_REGEX_VALUE = re.compile(rf"(?P<lead>=)(?P<ph>(?:{_REGEX_UNIT})+){_END}")

#: The format argument of a date command (``+%Y-%m-%dT%H:%M:%SZ``): conversions and
#: separators only, quoted or not. Its colons look like ``user:password`` to a rule.
_STRFTIME = re.compile(
    r"""(?P<lead>(?<![\w%+]))(?P<ph>\+(?=[^\s]*%[A-Za-z])(?:%[-_0^#]?[A-Za-z]|%%|[-:._/,]|[TZ])+)"""
    r"""(?![\w%])""")

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(?P<text>\S.*?)[ \t]*#*[ \t]*$")
_PLAIN_WORD = re.compile(r"[^\W\d_][^\W\d_'’ʼ-]*[.,!?]?")  # letters of any script
_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")


@dataclass
class UploadScan:
    """The result for one file: ``refused`` and ``allowed`` are ``[(line, label)]`` (1-based)."""

    refused: list[tuple[int, str]] = field(default_factory=list)
    allowed: list[tuple[int, str]] = field(default_factory=list)


def _angle_is_a_value(inner: str) -> bool:
    """``<paste your key here>`` and ``<token>`` are placeholders; ``<aB3dE6gH9jK2mN5pQ8sT>`` is a value.
    A word inside the brackets that looks generated makes the whole of it a value."""
    return any(
        looks_like_secret_literal(word)
        or (len(word) >= 12 and word.isalnum() and any(c.isdigit() for c in word)
            and any(c.isalpha() for c in word))
        for word in inner.split())


def _upper_is_a_name(name: str) -> bool:
    """``{API_KEY}`` and ``{TOKEN}`` are names; ``{AB3DE6GH9JK2MN5PQ8ST}`` is not."""
    return "_" in name or not any(c.isdigit() for c in name)


def _neutralised(line: str) -> str:
    """``line`` with each placeholder that stands where a value stands replaced by one reference."""

    def placeholder(m: re.Match[str]) -> str:
        if m.group("angle") is not None and _angle_is_a_value(m.group("angle")):
            return m.group(0)
        if m.group("upper") is not None and not _upper_is_a_name(m.group("upper")):
            return m.group(0)
        return m.group("lead") + _NEUTRAL

    def kept_lead(m: re.Match[str]) -> str:
        return m.group("lead") + _NEUTRAL

    out = _PLACEHOLDER.sub(placeholder, line)
    out = _REGEX_VALUE.sub(kept_lead, out)
    return _STRFTIME.sub(kept_lead, out)


def _is_a_title(line: str) -> bool:
    """A Markdown heading of the form ``# Name: a few plain words`` - a title, not an assignment."""
    heading = _HEADING.match(line)
    if heading is None:
        return False
    text = heading.group("text")
    if any(c in text for c in "=@/\\<>{}$`\"()[]"):
        return False
    if any(c.isdigit() for c in text):
        return False
    _name, colon, rest = text.partition(":")
    words = rest.split()
    return bool(colon) and len(words) >= 2 and all(_PLAIN_WORD.fullmatch(w) for w in words)


def _fenced_lines(lines: list[str]) -> set[int]:
    """1-based numbers of the lines inside a fenced code block (the fences included)."""
    inside: set[int] = set()
    fence: str | None = None
    for number, line in enumerate(lines, 1):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(0).strip()[0]
                inside.add(number)
        else:
            inside.add(number)
            if m and m.group(0).strip()[0] == fence:
                fence = None
    return inside


def _is_markdown(path: str) -> bool:
    return path.lower().endswith((".md", ".markdown"))


def scan_upload(text: str, *, source_hint: str = "") -> UploadScan:
    """Read one uploaded file. ``refused`` is what stops the upload, ``allowed`` the
    marked lines that would have (see the module docstring)."""
    found = scan_lines(text, source_hint=source_hint, window_long_runs=True)
    result = UploadScan()
    if not found:
        return result
    lines = text.split("\n")
    fenced: set[int] | None = None
    marked: set[int] = set()
    for number, label in found:
        line = lines[number - 1] if 0 < number <= len(lines) else ""
        if ALLOW_MARKER in line:
            if number not in marked:
                marked.add(number)
                result.allowed.append((number, label))
            continue
        if label in _NEVER_WAVED_THROUGH:
            result.refused.append((number, label))
            continue
        if _is_markdown(source_hint) and _is_a_title(line):
            if fenced is None:
                fenced = _fenced_lines(lines)
            if number not in fenced:
                continue
        neutral = _neutralised(line)
        if neutral != line and not scan_lines(neutral, source_hint=source_hint, window_long_runs=True):
            continue
        result.refused.append((number, label))
    return result


__all__ = ["ALLOW_MARKER", "UploadScan", "scan_upload"]
