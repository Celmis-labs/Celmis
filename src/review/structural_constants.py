"""Deterministic check: an added PHP line names a class constant nobody defines.

Why this exists:
    A misspelled constant (`Model::STATUS_PENDNG` for `STATUS_PENDING`) is a fatal
    "Undefined constant" at the first run of that line, and a model finds it
    only in some runs. Whether a constant exists is not a judgement call: it
    is a text search over the repository. So this is a proven finding, like the
    ast-grep rules in `structural.py`, produced without tokens.

What counts as a reference:
    `Name::CONST`, `self::CONST`, `static::CONST`, `parent::CONST` on an ADDED
    line of a PHP file, where CONST is UPPER_CASE. `Name::class`, method calls
    (`Name::FOO()`), static properties (`Name::$x`) and enum cases written in
    CamelCase are not references of this kind and never match.

When it is a finding (all of these, otherwise silence):
    * the checkout the code graph was built from exists and holds the pull
      request's head commit (no clone -> skip, nothing is said);
    * NO definition of that constant name exists anywhere in the tree at the
      head commit, vendor directories included, because constants come from
      libraries. A definition is `const NAME`, `case NAME` (enum), a
      `define('NAME'` / `define("NAME"` call, or a continuation line of a
      multi-constant `const` statement (`    NAME = 1,`). The head commit
      already contains whatever the diff itself adds, so a constant defined by
      the same pull request is found;
    * for `Name::`, the class `Name` is declared in the repository. A class
      that is not (a built-in such as `PDO`, a library that is not tracked)
      has constants we cannot see, so we say nothing;
    * the class involved does not extend or implement something that is not
      declared in the repository either: its constants may be inherited from
      there. Same reasoning, same silence;
    * every search ran to completion. A search cut at its output cap proves
      nothing, so it ends in silence as well.

Head commit not in the clone (an open pull request): `code_revision` picks
the PR's base commit when the clone has it (the pull request's own code: base
plus diff), and only otherwise the clone's HEAD, which is NEWER than the pull
request (a constant defined there later hides the typo - the one case this
check stays blind to, and says so in the finding). The diff is applied on top as text. A constant definition
ADDED by the diff counts as present; one REMOVED by the diff counts as gone only
when no other definition remains. The finding says so. Every other guard above
(class declared in the repository, no unseen parent, complete searches) is kept
unchanged, so a class the pull request itself adds stays silent.

Fail-open: any git error, timeout or unexpected input means "no finding".

The suggestion: the closest constant of the same class (edit distance <= 2),
read from the class's own declaration file.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.review.code_revision import (
    KIND_BASE_PLUS_DIFF,
    KIND_HEAD,
    KIND_NONE,
    resolve_code_revision,
)
from src.review.models import Finding, FindingSeverity, HunkSide, TemplateText

logger = logging.getLogger(__name__)

RULE_ID = "structural.undefined-class-constant"

#: Seconds one git invocation may take, and lines it may return. A name that
#: matches more lines than the cap cannot be proven absent, so it is skipped.
GIT_TIMEOUT_SECONDS = 20.0
GIT_MAX_LINES = 20_000

#: Findings per pull request. A diff that trips this many is rewriting a
#: namespace, and the first few already say so.
MAX_FINDINGS = 20

PHP_SUFFIXES = (".php", ".phtml", ".inc", ".php5", ".php7")

_REFERENCE = re.compile(
    r"(?<![\w$>\\])((?:\\?[A-Za-z_]\w*)(?:\\[A-Za-z_]\w*)*)::([A-Z][A-Z0-9_]*)\b(?!\s*\()"
)
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_SHA = re.compile(r"[0-9A-Fa-f]{7,64}")
_RELATIVE = ("self", "static", "parent")


class _Incomplete(Exception):
    """A search could not run to completion: no verdict may be drawn."""


def _git(root: Path, args: list[str]) -> list[str]:
    """Run git; its stdout lines. `[]` for "no match" (exit 1, no output).

    Raises `_Incomplete` when git could not run, failed, timed out or hit the
    line cap — all of which are "cannot tell", never "absent".
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace",
        )
    except OSError as exc:
        raise _Incomplete(str(exc)) from exc
    lines: list[str] = []
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if len(lines) >= GIT_MAX_LINES or time.monotonic() > deadline:
                raise _Incomplete("search cut short")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and lines):
        raise _Incomplete(f"git exit {proc.returncode}")
    return lines


def _has_commit(root: Path, sha: str) -> bool:
    if not _SHA.fullmatch(sha or ""):
        return False
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _grep(root: Path, rev: str, *flags_and_pattern: str) -> list[tuple[str, int, str]]:
    """`git grep -n` at `rev` -> (path, line, text). Raises `_Incomplete`."""
    out = []
    prefix = f"{rev}:"
    for ln in _git(root, ["grep", "-n", "-I", *flags_and_pattern, rev]):
        if not ln.startswith(prefix):
            continue
        parts = ln[len(prefix):].split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((parts[0], int(parts[1]), parts[2]))
    return out


def _decl_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"\bconst\b[^;]*\b{name}\b|\bcase\s+{name}\b|"
        rf"\bdefine\s*\(\s*['\"]{name}['\"]|^\s*{name}\s*=[^=>]"
    )


def _definition_hits(name: str, root: Path, rev: str) -> list[tuple[str, int, str]]:
    """Lines at `rev` that define the constant `name`."""
    decl = _decl_pattern(name)
    return [h for h in _grep(root, rev, "-w", "-F", "-e", name) if decl.search(h[2])]


def _is_defined(name: str, root: Path, rev: str) -> bool:
    """True when some line at `rev` defines the constant `name`."""
    return bool(_definition_hits(name, root, rev))


def _diff_php_lines(pr: Any) -> tuple[list[str], set[tuple[str, str]]]:
    """(added line texts, removed (path, stripped text)) of the PHP files in the diff."""
    added: list[str] = []
    removed: set[tuple[str, str]] = set()
    for h in getattr(pr, "hunks", None) or []:
        path = getattr(h, "file_path", "") or ""
        if not path.lower().endswith(PHP_SUFFIXES):
            continue
        for raw in (getattr(h, "content", "") or "").splitlines():
            if raw.startswith(("@@", "+++", "---")):
                continue
            if raw.startswith("+"):
                added.append(raw[1:])
            elif raw.startswith("-"):
                removed.add((path, raw[1:].strip()))
    return added, removed


def _is_defined_with_diff(name: str, root: Path, pr: Any, rev: str = "HEAD") -> bool:
    """Definition check when the head commit is not in the clone.

    The clone's HEAD stands in for the base; the diff is applied on top of it
    as text. A definition ADDED by the diff counts as present. One REMOVED by
    the diff counts as gone only when every definition found in the clone is a
    line the diff removes (same file, same text): any other definition keeps
    the constant defined.
    """
    added, removed = _diff_php_lines(pr)
    decl = _decl_pattern(name)
    if any(re.search(rf"\b{name}\b", t) and decl.search(t) for t in added):
        return True
    hits = _definition_hits(name, root, rev)
    return any((p, t.strip()) not in removed for p, _n, t in hits)


def _class_declarations(cls: str, root: Path, rev: str) -> list[tuple[str, int, str]]:
    pattern = rf"(class|interface|trait|enum)[[:space:]]+{cls}([^A-Za-z0-9_]|$)"
    found = _grep(root, rev, "-E", "-e", pattern)
    return [
        h for h in found
        if re.search(rf"\b(class|interface|trait|enum)\s+{cls}\b", h[2])
        and not h[2].lstrip().startswith(("*", "//", "#"))
    ]


def _parents_of(decl_text: str) -> list[str]:
    """Class names after `extends` / `implements` on a declaration line."""
    m = re.search(r"\b(?:extends|implements)\b", decl_text)
    if not m:
        return []
    tail = decl_text[m.start():].split("{", 1)[0]
    words = re.findall(r"[\\\w]+", re.sub(r"\b(extends|implements)\b", " ", tail))
    return [w.rsplit("\\", 1)[-1] for w in words if w.rsplit("\\", 1)[-1]]


def _inherits_from_unknown(decl_text: str, root: Path, rev: str) -> bool:
    """True when the declaration extends/implements a class we cannot see."""
    return any(not _class_declarations(p, root, rev) for p in _parents_of(decl_text))


def _show(root: Path, rev: str, path: str) -> list[str]:
    try:
        return _git(root, ["show", f"{rev}:{path}"])
    except _Incomplete:
        return []


def _enclosing_class_line(root: Path, rev: str, path: str) -> str | None:
    for ln in _show(root, rev, path):
        if re.search(r"^\s*(?:abstract\s+|final\s+)*(class|trait|interface|enum)\s+\w+", ln):
            return ln
    return None


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _closest(name: str, cls_hits: list[tuple[str, int, str]], root: Path, rev: str) -> str | None:
    """The constant of the declared class nearest `name` (distance <= 2)."""
    best: tuple[int, str] | None = None
    for path, _n, _t in cls_hits[:3]:
        for ln in _show(root, rev, path):
            for m in re.finditer(r"\bconst\b[^=;]*?\b([A-Z][A-Z0-9_]*)\s*=", ln):
                cand = m.group(1)
                d = _levenshtein(name, cand)
                if cand != name and d <= 2 and (best is None or d < best[0]):
                    best = (d, cand)
    return best[1] if best else None


def _added_lines(hunk: Any) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    line = (getattr(hunk, "new_start", 1) or 1) - 1
    for raw in (getattr(hunk, "content", "") or "").splitlines():
        if raw.startswith(("@@", "+++", "---")) or raw.startswith("-"):
            continue
        line += 1
        if raw.startswith("+"):
            out.append((line, raw[1:]))
    return out


def _code_of(text: str) -> str:
    """The line without string literals and trailing comments."""
    s = _STRING.sub("''", text)
    s = re.split(r"//|(?<![\w$])#", s, maxsplit=1)[0]
    return s


def find_undefined_constants(pr: Any, root: Path | None) -> list[Finding]:
    """Proven findings for added PHP lines naming a constant defined nowhere.

    Never raises; `[]` when there is no clone or no head commit in it.
    """
    try:
        return _find(pr, root)
    except Exception as exc:  # noqa: BLE001 - fail open, always
        logger.info("undefined_constant_check_skipped err=%s", exc)
        return []


def _find(pr: Any, root: Path | None) -> list[Finding]:
    if root is None:
        return []
    # Which revision stands for the pull request's code (`code_revision`):
    # the head commit; else the PR's base commit with the diff overlaid
    # (`_is_defined_with_diff`); else the clone's HEAD - NEWER than the pull
    # request, so a constant defined there proves nothing about the PR. The
    # last two are labelled in the finding.
    revision = resolve_code_revision(pr, root)
    if revision.kind == KIND_NONE:
        return []
    rev = revision.rev
    fallback = revision.kind != KIND_HEAD
    refs: list[tuple[str, int, str, str, str]] = []  # path, line, class, const, text
    for h in getattr(pr, "hunks", None) or []:
        path = getattr(h, "file_path", "") or ""
        if not path.lower().endswith(PHP_SUFFIXES):
            continue
        for lineno, text in _added_lines(h):
            stripped = text.lstrip()
            if stripped.startswith(("*", "/*", "//", "#")):
                continue
            for m in _REFERENCE.finditer(_code_of(text)):
                refs.append((path, lineno, m.group(1).rsplit("\\", 1)[-1], m.group(2), text.strip()))
    if not refs:
        return []

    defined: dict[str, bool] = {}
    class_info: dict[str, list[tuple[str, int, str]]] = {}
    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for path, lineno, cls, name, text in refs:
        key = (path, lineno, name)
        if key in seen:
            continue
        seen.add(key)
        try:
            if name not in defined:
                defined[name] = (_is_defined_with_diff(name, root, pr, rev) if fallback
                                 else _is_defined(name, root, rev))
            if defined[name]:
                continue
            relative = cls.lower() in _RELATIVE
            if relative:
                decl = _enclosing_class_line(root, rev, path)
                if decl is None or _inherits_from_unknown(decl, root, rev):
                    continue
                m = re.search(r"\b(?:class|trait|interface|enum)\s+(\w+)", decl)
                target = m.group(1) if m else ""
                if cls.lower() == "parent":
                    # `parent::` is the parent's constant; the class itself is
                    # not where it would be declared.
                    parents = _parents_of(decl)
                    target = parents[0] if parents else ""
                if not target:
                    continue
            else:
                target = cls
            if target not in class_info:
                class_info[target] = _class_declarations(target, root, rev)
            decls = class_info[target]
            if not decls or any(_inherits_from_unknown(d[2], root, rev) for d in decls):
                continue
            near = _closest(name, decls, root, rev)
        except _Incomplete as exc:
            logger.info("undefined_constant_search_incomplete const=%s err=%s", name, exc)
            continue
        shown = f"{cls}::{name}"
        if not fallback:
            where, note, variant = "anywhere in the repository at this commit", "", "repo"
        elif revision.kind == KIND_BASE_PLUS_DIFF:
            where = "in the pull request's base commit or in this pull request's diff"
            note = ("\n\n_Checked against the pull request's base commit plus this "
                    "diff: its head commit was not available locally._")
            variant = "base_plus_diff"
        else:
            where = "in the repository's checked-out branch or in this pull request's diff"
            note = ("\n\n_Checked against the checked-out branch (NEWER than the "
                    "pull request) plus this diff: the pull request's head commit "
                    "was not available locally, nor was its base commit._")
            variant = "branch_plus_diff"
        hint = f" The closest constant of `{target}` is `{near}`." if near else ""
        title = f"Undefined class constant {shown}"
        body = (
            f"`{shown}` is used here, but no constant named `{name}` is defined "
            f"{where} (searched `const`, enum "
            f"`case`, `define()`; vendor directories included). PHP raises a "
            f"fatal `Undefined constant` error when this line runs.{hint}\n\n"
            f"```\n{text[:200]}\n```{note}"
        )
        suggestion = f"Use `{near}`." if near else None
        findings.append(Finding(
            file_path=path,
            line=lineno,
            side=HunkSide.RIGHT,
            severity=FindingSeverity.ERROR,
            title=title,
            body=body,
            suggestion=suggestion,
            agent="structural",
            rule_id=RULE_ID,
            confidence=1.0,
            evidence_kind="proven",
            template=TemplateText(
                key=RULE_ID, title=title, body=body, suggestion=suggestion,
                args={"variant": variant, "shown": shown, "name": name,
                      "near": near or "", "target": target, "snippet": text[:200]}),
        ))
        if len(findings) >= MAX_FINDINGS:
            break
    return findings


def clone_root_for(pr: Any) -> Path | None:
    """The checkout the code graph was built from for `pr`, or None."""
    try:
        # The clone's location lives on the application settings (`repo_path`),
        # not on the review settings: asking the review settings found no
        # clone on a real server, and every consumer of it went quiet.
        from src.config import get_settings
        from src.review.graph_context import _clone_root, graph_slug_candidates

        settings = get_settings()
        for slug in graph_slug_candidates(pr):
            root = _clone_root(slug, settings)
            if root is not None:
                return root
    except Exception as exc:  # noqa: BLE001
        logger.debug("undefined_constant_no_clone err=%s", exc)
    return None


RootResolver = Callable[[Any], "Path | None"]
