"""A declaration line for a symbol whose extractor recorded no signature.

Most extractors leave `signature` empty. Rather than print a bare name, take
the declaration from the source: the first line at the symbol's start (past
decorators and attributes), continued over a few lines while the parentheses
are open, cut at the opening `{` (and a trailing `:`), at most `MAX_CHARS` characters once clipped by the caller. Heuristic and
language-agnostic on purpose: it only has to be a readable one-liner.
"""

from __future__ import annotations

MAX_CHARS = 160
_SOURCE_CAP = 600
_MAX_LINES = 6
_SKIP_PREFIXES = ("@", "#[", "//", "#", "/*", "*", '"""', "'''")


def derive_signature(lines: list[str], start_line: int) -> str | None:
    """`lines` is the whole file split on newlines; `start_line` is 1-based."""
    i = max(start_line - 1, 0)
    # Decorators / attributes / comments sit ON the symbol's span in some
    # extractors: advance to the first real declaration line.
    while i < len(lines) and (not lines[i].strip()
                              or lines[i].lstrip().startswith(_SKIP_PREFIXES)):
        i += 1
        if i - (start_line - 1) > _MAX_LINES:
            return None
    if i >= len(lines):
        return None
    parts: list[str] = []
    depth = 0
    for j in range(i, min(i + _MAX_LINES, len(lines))):
        text = lines[j].strip()
        parts.append(text)
        depth += text.count("(") - text.count(")")
        if depth <= 0:
            break
    joined = " ".join(parts)
    k = joined.find("{")
    if k > 0:
        joined = joined[:k]
    joined = joined.rstrip().rstrip(":").rstrip()
    # No cut here: callers redact first and cut to MAX_CHARS second (emit.clip),
    # a cut before redaction can sever the tail of a literal.
    return joined[:_SOURCE_CAP] or None
