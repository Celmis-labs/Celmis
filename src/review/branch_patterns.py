"""Target-branch patterns: which base branches a repository reviews.

A list of entries, each a branch name or a glob, optionally negated with a
leading `!` — the shape Kodus accepts as "staging, !master, !main":

    []                          every branch (no restriction)
    ["main", "release/*"]       only these
    ["!main", "!master"]        every branch except these
    ["release/*", "!release/old"]  the includes, minus the excludes

Exclusion wins: a branch matching any `!pattern` is not reviewed, whatever
else matches it. A list of negations only means "everything except those".
Globs are `fnmatch` patterns matched case-sensitively (git branch names are),
so `release/*` matches `release/1.2` and `release/1.2/hotfix` alike; a name
without glob characters matches itself only, exactly as the gate always did.

One function decides for every caller — the orchestrator's target-branch
gate and the webhook's early draft skip — so the two cannot disagree about a
branch. Pure, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase

NEGATION = "!"


def clean_patterns(patterns: Iterable[str] | None) -> list[str]:
    """The entries stripped, blanks and duplicates dropped, order kept."""
    out: list[str] = []
    for raw in patterns or []:
        entry = str(raw or "").strip()
        if entry and entry not in out:
            out.append(entry)
    return out


def split_patterns(patterns: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """(includes, excludes) — the excludes without their `!`."""
    includes: list[str] = []
    excludes: list[str] = []
    for entry in clean_patterns(patterns):
        if entry.startswith(NEGATION):
            body = entry[len(NEGATION):].strip()
            if body:
                excludes.append(body)
        else:
            includes.append(entry)
    return includes, excludes


def pattern_error(entry: str) -> str | None:
    """Why `entry` cannot be a target-branch pattern, or None.

    Only what can never match anything is refused: a bare `!`, a double
    negation, or whitespace inside (git refuses spaces in a ref name)."""
    value = str(entry or "").strip()
    if not value:
        return None
    body = value[len(NEGATION):] if value.startswith(NEGATION) else value
    if not body.strip():
        return f"{entry!r}: a '!' needs a branch name or pattern after it"
    if body.startswith(NEGATION):
        return f"{entry!r}: write a single '!' to exclude a branch"
    if any(ch.isspace() for ch in body.strip()):
        return f"{entry!r}: a branch name has no spaces"
    return None


@dataclass(frozen=True)
class BranchMatch:
    """Whether a branch is reviewed, and the entry that decided it."""

    targeted: bool
    #: "unrestricted" (no list), "unknown" (no branch to test), "excluded"
    #: (a `!pattern` matched), "included" (a pattern matched), "not_excluded"
    #: (negations only, none matched), "unmatched" (no include matched).
    reason: str
    #: The entry that decided it, as configured ("!main", "release/*").
    pattern: str | None = None


def match_branch(branch: str | None, patterns: Iterable[str] | None) -> BranchMatch:
    """Whether `branch` is reviewed under `patterns`, and why."""
    includes, excludes = split_patterns(patterns)
    if not includes and not excludes:
        return BranchMatch(True, "unrestricted")
    if not branch:
        # The gate has always reviewed a PR whose base it could not read.
        return BranchMatch(True, "unknown")
    for body in excludes:
        if fnmatchcase(branch, body):
            return BranchMatch(False, "excluded", f"{NEGATION}{body}")
    if not includes:
        return BranchMatch(True, "not_excluded")
    for entry in includes:
        if fnmatchcase(branch, entry):
            return BranchMatch(True, "included", entry)
    return BranchMatch(False, "unmatched")


def branch_targeted(branch: str | None, patterns: Iterable[str] | None) -> bool:
    """True when a pull request into `branch` is reviewed."""
    return match_branch(branch, patterns).targeted


def _quoted(items: Iterable[str]) -> str:
    vals = [str(x) for x in list(items)[:20]]
    return "[" + ", ".join(f"'{v}'" for v in vals) + "]"


def skip_sentence(branch: str, patterns: Iterable[str] | None) -> str:
    """The stage reason for a branch the patterns leave out."""
    configured = clean_patterns(patterns)
    m = match_branch(branch, configured)
    if m.reason == "excluded":
        return (f"Branch mismatch: target branch '{branch}' is excluded by "
                f"'{m.pattern}' (configured patterns {_quoted(configured)}).")
    return (f"Branch mismatch: target branch '{branch}' does not match "
            f"configured patterns {_quoted(configured)}.")


def pass_sentence(branch: str | None, patterns: Iterable[str] | None) -> str:
    """The stage reason for a branch the patterns let through."""
    configured = clean_patterns(patterns)
    shown = branch or "?"
    m = match_branch(branch, configured)
    if m.reason == "unrestricted":
        return (f"No target-branch restriction configured; target branch "
                f"'{shown}' is reviewed.")
    if m.reason == "unknown":
        return (f"The base branch is not known; patterns {_quoted(configured)} "
                f"could not be checked, so the pull request is reviewed.")
    if m.reason == "not_excluded":
        return (f"Target branch '{shown}' is not excluded by configured "
                f"patterns {_quoted(configured)}.")
    via = "" if m.pattern == branch else f" (via '{m.pattern}')"
    return (f"Target branch '{shown}' matches configured patterns "
            f"{_quoted(configured)}{via}.")
