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

#: Limits checked on save. A glob list is configuration, not data.
MAX_GLOBS = 200
MAX_GLOB_LENGTH = 300


def _translate(pattern: str) -> str:
    """One glob → one regex body (no anchors)."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**/` — zero or more whole segments; a bare `**` — anything.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str] | None:
    p = pattern.strip()
    if not p or p.startswith("#") or p.startswith("!"):
        return None
    directory = p.endswith("/")
    p = p.strip("/")
    if not p:
        return None
    if "/" in p or directory:
        # Anchored at the root. A directory pattern covers its whole subtree.
        body = _translate(p)
        if directory:
            return re.compile(f"^{body}/.*$")
        return re.compile(f"^{body}$")
    # Name-only: the last segment, at any depth.
    return re.compile(f"^(?:.*/)?{_translate(p)}$")


def path_ignored(path: str, globs: Iterable[str] | None) -> bool:
    """Whether `path` (repo-relative, forward slashes) matches any glob."""
    if not globs or not path:
        return False
    norm = path.replace("\\", "/").lstrip("/")
    for g in globs:
        rx = _compile(str(g))
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
        if _compile(g) is None:
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
