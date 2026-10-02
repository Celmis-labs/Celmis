"""Per-repo ignore globs — paths a repository's review never looks at.

`RepoReviewPolicy.ignore_globs` is a list of gitignore-ish patterns such as
`docs/**`, `*.snap` or `migrations/*.py`. The install-wide skip lists in
`ReviewSettings` (lockfiles, build dirs, binaries) are env-only and apply to
every repository; these are the repository's own additions on top.

They are applied in the orchestrator, AFTER the policy is loaded — the diff is
parsed inside the provider before any policy exists, so a filter inside
`parse_unified_diff` would have nothing to read. Ignored paths are appended to
`PullRequest.skipped_files`, which is what keeps the "all changed files were
filtered out" message truthful when the globs cover the whole change.

Pattern rules (a deliberate subset of .gitignore, enough to be predictable):

  - a pattern with no `/` matches the file NAME at any depth: `*.snap`,
    `CHANGELOG.md`;
  - a pattern with a `/` is anchored at the repository root: `docs/*.md`
    matches `docs/a.md` but not `api/docs/a.md`; a leading `/` is allowed
    and means the same;
  - `*` and `?` stay inside one path segment, `**` crosses segments:
    `docs/**` is everything under docs, `**/fixtures/**` is any fixtures dir;
  - a trailing `/` means "this directory and everything in it": `vendor/`;
  - no negation (`!`) — a pattern that starts with one is refused by
    `validate_ignore_globs` rather than silently read as a literal `!`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

#: Limits checked on save. A glob list is configuration, not data.
MAX_GLOBS = 200
MAX_GLOB_LENGTH = 300
#: Wildcards per pattern. Matching is linear whatever the pattern (see
#: `_Glob`), so this is about readable configuration, not safety.
MAX_STARS = 8


# ─── Matching ───────────────────────────────────────────────────────
#
# Not a regex. The first version translated `**` to an unanchored `.*`, and a
# pattern such as `**a**a**a**a**a**a**b` made Python's backtracking engine
# take seconds per path, growing about ninefold with every extra `a**` — on a
# worker shared by every tenant, from a setting any repo reviewer can save.
# Possessive/atomic tricks do not save it here: `*` and `**` match different
# alphabets (`*` never crosses `/`), so "take the first occurrence" is not
# always right. The pattern is compiled into a small NFA instead and the path
# is walked once, carrying the set of live states, so the cost is bounded by
# len(path) × len(pattern) whatever the pattern looks like.

_LIT, _ONE, _CLASS, _STAR, _DSTAR, _DSTAR_SLASH = range(6)


class _Glob:
    """One compiled pattern: tokens plus the epsilon closure of each state."""

    __slots__ = ("tokens", "_closure")

    def __init__(self, tokens: list[tuple[int, Any]]):
        self.tokens = tokens
        n = len(tokens)
        closure: list[frozenset[int]] = [frozenset()] * (n + 1)
        closure[n] = frozenset((n,))
        for i in range(n - 1, -1, -1):
            kind = tokens[i][0]
            if kind in (_STAR, _DSTAR, _DSTAR_SLASH):
                # Each of these may match nothing.
                closure[i] = frozenset((i,)) | closure[i + 1]
            else:
                closure[i] = frozenset((i,))
        self._closure = closure

    def match(self, path: str) -> bool:
        tokens = self.tokens
        n = len(tokens)
        states = self._closure[0]
        for ch in path:
            nxt: set[int] = set()
            for i in states:
                if i == n:
                    continue
                kind, arg = tokens[i]
                if kind == _LIT:
                    if ch == arg:
                        nxt |= self._closure[i + 1]
                elif kind == _ONE:
                    if ch != "/":
                        nxt |= self._closure[i + 1]
                elif kind == _CLASS:
                    if ch != "/" and arg.fullmatch(ch):
                        nxt |= self._closure[i + 1]
                elif kind == _STAR:
                    if ch != "/":
                        nxt |= self._closure[i]
                elif kind == _DSTAR:
                    nxt |= self._closure[i]
                else:  # _DSTAR_SLASH: `(?:.*/)?` — any run ending in a `/`
                    nxt |= self._closure[i]
                    if ch == "/":
                        nxt |= self._closure[i + 1]
            if not nxt:
                return False
            states = frozenset(nxt)
        return n in states


def _tokens(pattern: str) -> list[tuple[int, Any]]:
    """One glob → NFA tokens (no anchors). Raises ValueError on a bad class."""
    out: list[tuple[int, Any]] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i >= 2:
                # `**/` — zero or more whole segments; a bare `**` — anything.
                if j < n and pattern[j] == "/":
                    out.append((_DSTAR_SLASH, None))
                    i = j + 1
                    continue
                out.append((_DSTAR, None))
            else:
                out.append((_STAR, None))
            i = j
            continue
        if ch == "?":
            out.append((_ONE, None))
        elif ch == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                out.append((_LIT, ch))
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                try:
                    # One character against one class: no repetition, so no
                    # backtracking whatever the class says.
                    rx = re.compile(f"[{body}]")
                except re.error as exc:
                    raise ValueError(
                        f"{pattern!r}: bad character class [{pattern[i + 1:end]}] "
                        f"({exc})") from None
                out.append((_CLASS, rx))
                i = end + 1
                continue
        else:
            out.append((_LIT, ch))
        i += 1
    return out


@lru_cache(maxsize=512)
def _compile(pattern: str) -> _Glob | None:
    """None for a pattern that matches nothing on purpose (blank, comment,
    negation). Raises ValueError for one that cannot be compiled."""
    p = pattern.strip()
    if not p or p.startswith("#") or p.startswith("!"):
        return None
    directory = p.endswith("/")
    p = p.strip("/")
    if not p:
        return None
    body = _tokens(p)
    if "/" in p or directory:
        # Anchored at the root. A directory pattern covers its whole subtree.
        if directory:
            return _Glob([*body, (_LIT, "/"), (_DSTAR, None)])
        return _Glob(body)
    # Name-only: the last segment, at any depth.
    return _Glob([(_DSTAR_SLASH, None), *body])


def path_ignored(path: str, globs: Iterable[str] | None) -> bool:
    """Whether `path` (repo-relative, forward slashes) matches any glob."""
    if not globs or not path:
        return False
    norm = path.replace("\\", "/").lstrip("/")
    for g in globs:
        try:
            rx = _compile(str(g))
        except ValueError:
            # Stored before validation refused it; it matches nothing.
            continue
        if rx is not None and rx.match(norm):
            return True
    return False


def validate_ignore_globs(globs: Sequence[str] | None) -> list[str]:
    """Clean a list for storage: stripped, de-duplicated, order kept.

    Raises ValueError naming the first pattern that cannot work. Blank lines
    are dropped (the UI edits one pattern per line), negation is refused
    rather than stored to match nothing.
    """
    if not globs:
        return []
    if len(globs) > MAX_GLOBS:
        raise ValueError(f"at most {MAX_GLOBS} ignore globs, got {len(globs)}")
    cleaned: list[str] = []
    for raw in globs:
        g = str(raw).strip()
        if not g:
            continue
        if len(g) > MAX_GLOB_LENGTH:
            raise ValueError(f"{g[:40]!r}… is longer than {MAX_GLOB_LENGTH} characters")
        if g.startswith("!"):
            raise ValueError(
                f"{g!r}: negation is not supported — list only what to ignore")
        if g.startswith("#"):
            raise ValueError(f"{g!r}: comments are not supported in this list")
        if any(ch in g for ch in ("\n", "\r", "\t")):
            raise ValueError(f"{g!r}: one pattern per entry")
        if g.strip("/") == "" or g.strip("/*") == "":
            # `**`, `/`, `*` — every file. That is "disable the review",
            # which the policy has its own switch for.
            raise ValueError(
                f"{g!r} matches every file — switch the review off for this "
                f"repository instead")
        if g.count("*") > MAX_STARS:
            raise ValueError(
                f"{g!r}: at most {MAX_STARS} `*` per pattern — split it into "
                f"several simpler patterns")
        if _compile(g) is None:  # raises ValueError for a bad class
            raise ValueError(f"{g!r} is not a usable pattern")
        cleaned.append(g)
    return list(dict.fromkeys(cleaned))


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def filter_raw_diff(raw_diff: str, globs: Iterable[str] | None) -> str:
    """`raw_diff` without the file sections whose path matches a glob.

    For the engine that reads the raw text (claude_code) and for the size cap,
    which measures the text. A section is dropped when EITHER side of its
    `diff --git a/X b/Y` header matches, so a rename into or out of an ignored
    directory is ignored as a whole. A diff without git headers is returned
    unchanged: nothing can be attributed to a path safely.
    """
    globs = list(globs or [])
    if not globs or not raw_diff:
        return raw_diff
    lines = raw_diff.splitlines(keepends=True)
    if not any(line.startswith("diff --git ") for line in lines):
        return raw_diff
    out: list[str] = []
    keep = True
    for line in lines:
        if line.startswith("diff --git "):
            m = _DIFF_HEADER.match(line.rstrip("\n"))
            if m:
                keep = not (path_ignored(m.group(1), globs)
                            or path_ignored(m.group(2), globs))
            else:
                keep = True
        if keep:
            out.append(line)
    return "".join(out)
