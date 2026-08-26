"""Cross-repo semantic drift detector (Stage 7, May 2026).

The job: when a PR changes a string literal / config constant / env var in
one repo of a group — find ALL the other repos where that same old value is
still hardcoded. This detects semantic config drift that neither
tree-sitter nor HTTP-route matchers catch.

Canonical example:
    repo `etl/src/config.py`:  EMBEDDING_MODEL = "gemini-embedding-2"
    repo `chat/src/config.py`: EMBEDDING_MODEL = "gemini-embedding-2"

A PR in `etl` changes it to `"gemini-embedding-3"`. Without drift detection
the LLM has no signal at all that `chat` needs to be synced too → embedding
vectors diverge → Qdrant queries break silently.

Detector:
    1. Parses the diff hunks for **removed lines** (`-` prefix).
    2. Extracts candidate strings: `"..."`, `'...'`, or the RHS of an
       assignment (`MODEL = "..."`, `MODEL: str = "..."`).
    3. Filter — keeps only the interesting values:
       - length 3-200 chars
       - not all-lowercase common words
       - has at least one of: dash, underscore, dot, digit, camelCase, version-like
    4. For each value runs a subprocess `grep -rln` over the other repo paths
       in the group.
    5. Returns a DriftReport with the matches.

The output is used by the orchestrator to enrich AgentContext before the
LLM call (the architect agent gets the signal as "authoritative", not as a
hint).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from src.config import get_settings
from src.groups import get_group_manager
from src.review.models import PullRequest
from src.sync.git_providers import parse_repo_url

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriftMatch:
    """One match — the old value was found in another repo."""

    other_repo_slug: str
    file: str            # relative path in the other repo
    line: int
    excerpt: str         # the text of the line (truncated)


@dataclass
class DriftHit:
    """A removed value + the list of repos where it is still hardcoded."""

    value: str
    pr_file: str         # where it was removed in the current PR
    pr_line: int
    matches: list[DriftMatch] = field(default_factory=list)


@dataclass
class DriftReport:
    """Summary of drift detection for a single PR."""

    group_name: str | None
    hits: list[DriftHit] = field(default_factory=list)
    repos_scanned: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def has_drift(self) -> bool:
        return any(h.matches for h in self.hits)

    def to_facts(self, *, max_matches_per_hit: int = 20) -> dict:
        """The report as DATA, for the UI.

        `to_markdown` below renders the same thing for the architect's prompt,
        and for a long time that was the only exit: the one deterministic check
        in the product reached the user through a language model's summary of
        it. They saw what the model chose to mention, not the fact — which is
        exactly backwards for the finding that needs no defending.

        Every field here is checkable in five seconds: the value, the file it
        was removed from, and each sibling repository that still hardcodes it,
        with file, line and the line itself.
        """
        return {
            "group": self.group_name,
            "repos_scanned": list(self.repos_scanned),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "hits": [
                {
                    "value": h.value,
                    "removed_from": {"file": h.pr_file, "line": h.pr_line},
                    "still_in": [
                        {"repo": m.other_repo_slug, "file": m.file,
                         "line": m.line, "excerpt": m.excerpt}
                        for m in h.matches[:max_matches_per_hit]
                    ],
                    "truncated": max(0, len(h.matches) - max_matches_per_hit),
                }
                for h in self.hits if h.matches
            ],
        }

    @property
    def was_checked(self) -> bool:
        """Whether a cross-repo scan actually ran.

        Distinct from `has_drift`, and the distinction is the whole point: a
        repository that belongs to no group is never scanned at all, and there
        is no group manager reachable from the HTTP API, so on a normally
        provisioned workspace this is False every time.

        Hits count as evidence of a scan on their own. `repos_scanned` is the
        primary signal, but a report carrying matches was obviously produced by
        something that looked, and reading it as "not checked" would suppress
        real drift — which the first version of this property did, and two
        existing tests caught.
        """
        return bool(self.repos_scanned or self.hits)

    def to_markdown(self, *, max_matches_per_hit: int = 5) -> str:
        """Render for injection into the architect prompt."""
        if not self.was_checked:
            # NOT the same sentence as "checked and clean", because a model
            # given the old ambiguous "(no cross-repo drift detected)" wrote
            # this into a production review, as a CRITICAL finding:
            #
            #   "Cross-repo search found no consumers referencing
            #    SETTLEMENT_TOPIC … in billing or gateway's indexed code"
            #
            # All four occurrences were live at that moment. No search had
            # happened — the repo was in no group. The model rendered an
            # absent check as a completed one and reported a confident false
            # negative on the exact question the feature exists to answer.
            #
            # The instruction is explicit because a bare "not checked" was not
            # enough: the model still needs telling that silence is not
            # evidence.
            return (
                "NOT CHECKED — no cross-repo scan ran for this pull request "
                "(this repository is not in a repo group, so there were no "
                "sibling repositories to search).\n"
                "You therefore have NO information about other repositories. "
                "Do NOT write that a cross-repo search found nothing, that no "
                "consumers reference a changed value, or that other services "
                "appear unaffected — nothing looked. If a change to a shared "
                "constant or endpoint could affect other repositories, say "
                "that it needs checking, never that it was checked."
            )
        if not self.has_drift:
            return (
                f"Checked {len(self.repos_scanned)} sibling repo(s) in group "
                f"`{self.group_name}` — no cross-repo drift: no removed value "
                "still appears in any of them."
            )
        lines: list[str] = []
        lines.append(
            f"**⚠ Cross-repo drift detected** (group: `{self.group_name}`, "
            f"scanned {len(self.repos_scanned)} sibling repos):"
        )
        for h in self.hits:
            if not h.matches:
                continue
            lines.append(
                f"\n- Value `{h.value!r}` removed at `{h.pr_file}:{h.pr_line}` "
                f"still hardcoded in {len(h.matches)} location(s):"
            )
            for m in h.matches[:max_matches_per_hit]:
                lines.append(
                    f"    - `{m.other_repo_slug}/{m.file}:{m.line}` — `{m.excerpt}`"
                )
            if len(h.matches) > max_matches_per_hit:
                lines.append(
                    f"    - … and {len(h.matches) - max_matches_per_hit} more"
                )
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Value extraction from the diff
# ──────────────────────────────────────────────────────────────────────


# Match `"..."` or `'...'` without a line break. Not greedy.
_STRING_RE = re.compile(r"""(?:'([^'\n]{3,200})'|"([^"\n]{3,200})")""")


def _is_interesting(value: str) -> bool:
    """Filter common noise — skip generic words / format strings."""
    if not (3 <= len(value) <= 200):
        return False
    v = value.strip()
    if not v:
        return False
    # all spaces / pure punctuation
    if not any(c.isalnum() for c in v):
        return False
    # format string placeholders only
    if v in {"{}", "{0}", "%s", "%d"}:
        return False
    # path-like fragments: skip very short ones without distinguishing marks
    has_distinguishing = (
        "-" in v or "_" in v or "." in v
        or any(c.isupper() for c in v) or any(c.isdigit() for c in v)
    )
    if not has_distinguishing:
        return False
    # common english/python words to skip
    if v.lower() in {
        "true", "false", "none", "null", "undefined", "self", "this",
        "init", "exit", "error", "warning", "info", "debug",
        "string", "number", "object", "array", "boolean",
        "get", "post", "put", "delete", "patch",
    }:
        return False
    # skip pure file extensions
    return re.fullmatch(r"\.[a-z]{1,5}", v) is None


_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


def extract_removed_values(pr: PullRequest) -> list[tuple[str, str, int]]:
    """Extracts the string literals that were REMOVED in the diff.

    Parses the raw hunk content (unified diff format: lines with a `-` prefix
    mark removals; the line number in the old file is counted from the `@@`
    header).

    Returns: list of (value, file_path, line_in_old_file).
    """
    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()

    for hunk in pr.hunks:
        old_line_no: int | None = None
        for raw in (hunk.content or "").split("\n"):
            header = _HUNK_HEADER_RE.match(raw)
            if header:
                old_line_no = int(header.group(1))
                continue
            if old_line_no is None:
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            if raw.startswith("-"):
                text = raw[1:]
                for match in _STRING_RE.finditer(text):
                    value = match.group(1) or match.group(2) or ""
                    if not _is_interesting(value):
                        continue
                    key = (value, hunk.file_path, old_line_no)
                    if key not in seen:
                        seen.add(key)
                        out.append(key)
                old_line_no += 1
            elif raw.startswith("+"):
                # added line — does not affect the old line counter
                continue
            else:
                # context line — present in both old and new
                old_line_no += 1
    logger.debug(
        "drift_extracted pr=%d removed_values=%d", pr.number, len(out),
    )
    return out


# ──────────────────────────────────────────────────────────────────────
# Group resolution
# ──────────────────────────────────────────────────────────────────────


def _find_group_for_repo(
    repo_slug: str, workspace_id: str | None = None,
) -> tuple[str, list[str]] | None:
    """Find the first group this repo belongs to.

    Returns (group_name, list_of_other_repo_slugs) or None.

    `workspace_id` restricts the search to one tenant's groups. Drift GREPS
    every sibling in the group and quotes what it finds into a review comment,
    so a group belonging to another tenant would read and publish their source.
    None keeps the old installation-wide behaviour for the CLI.
    """
    try:
        mgr = get_group_manager()
    except Exception as exc:  # noqa: BLE001
        logger.debug("group_manager_unavailable err=%s", exc)
        return None

    for gname in mgr.list(workspace_id):
        try:
            g = mgr.load(gname, workspace_id)
        except Exception:  # noqa: BLE001
            continue
        slugs_in_group: list[str] = []
        for repo_id in g.repos:
            try:
                parsed = parse_repo_url(repo_id)
                slugs_in_group.append(parsed.slug)
            except Exception:  # noqa: BLE001
                continue
        if repo_slug in slugs_in_group:
            others = [s for s in slugs_in_group if s != repo_slug]
            if others:
                return gname, others
    return None


# ──────────────────────────────────────────────────────────────────────
# Grep
# ──────────────────────────────────────────────────────────────────────


_GREP_INCLUDES = (
    "--include=*.py", "--include=*.ts", "--include=*.tsx",
    "--include=*.js", "--include=*.jsx", "--include=*.vue",
    "--include=*.go", "--include=*.java", "--include=*.kt",
    "--include=*.rs", "--include=*.rb", "--include=*.php",
    "--include=*.cs", "--include=*.c", "--include=*.cpp", "--include=*.h",
    "--include=*.yml", "--include=*.yaml", "--include=*.toml",
    "--include=*.env*", "--include=*.json",
    "--include=Dockerfile*", "--include=Makefile*",
)


def _grep_repo(
    value: str,
    repo_path: Path,
    repo_slug: str,
    *,
    max_matches: int = 20,
    timeout: int = 8,
) -> list[DriftMatch]:
    """Subprocess grep in the given repo. Fixed-string match (-F)."""
    if not repo_path.exists():
        return []
    try:
        result = subprocess.run(
            [
                "grep", "-rnF", *_GREP_INCLUDES,
                value, str(repo_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("drift_grep_failed repo=%s value=%r err=%s",
                       repo_slug, value[:20], exc)
        return []

    matches: list[DriftMatch] = []
    for raw in result.stdout.splitlines()[:max_matches]:
        # `path:lineno:content`
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        raw_path, lineno_str, excerpt = parts
        try:
            line_no = int(lineno_str)
        except ValueError:
            continue
        try:
            rel = Path(raw_path).relative_to(repo_path)
        except ValueError:
            continue
        # skip generated / vendored
        if any(part in {"node_modules", "dist", "build", ".next", "__pycache__",
                        ".venv", "venv", ".git"} for part in rel.parts):
            continue
        excerpt = excerpt.strip()
        if len(excerpt) > 140:
            excerpt = excerpt[:140] + "…"
        matches.append(DriftMatch(
            other_repo_slug=repo_slug,
            file=str(rel),
            line=line_no,
            excerpt=excerpt,
        ))
    return matches


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


def detect_drift(
    pr: PullRequest,
    *,
    max_values: int = 25,
    workspace_id: str | None = None,
) -> DriftReport:
    """Run drift detection. Idempotent, safe to call even if no group exists.

    Args:
        pr: parsed PullRequest with hunks.
        max_values: hard cap on how many removed lines to inspect.
    """
    import time
    t0 = time.time()

    settings = get_settings()
    # local_slug: group membership is stored as ParsedRepo.slug, which has
    # no provider prefix for Bitbucket — pr.repo_slug always has one.
    group_info = _find_group_for_repo(pr.local_slug, workspace_id)
    if group_info is None:
        logger.debug("drift_skipped reason=no_group repo=%s", pr.repo_slug)
        return DriftReport(group_name=None, elapsed_seconds=time.time() - t0)

    group_name, other_slugs = group_info

    removed = extract_removed_values(pr)[:max_values]
    if not removed:
        logger.info(
            "drift_no_values pr=%d repo=%s group=%s",
            pr.number, pr.repo_slug, group_name,
        )
        return DriftReport(
            group_name=group_name,
            repos_scanned=other_slugs,
            elapsed_seconds=time.time() - t0,
        )

    # Resolve repo paths once
    sibling_paths: dict[str, Path] = {
        slug: settings.repo_path(slug) for slug in other_slugs
    }

    hits: list[DriftHit] = []
    for value, file, line in removed:
        hit = DriftHit(value=value, pr_file=file, pr_line=line)
        for slug, rpath in sibling_paths.items():
            hit.matches.extend(_grep_repo(value, rpath, slug))
        if hit.matches:
            hits.append(hit)

    report = DriftReport(
        group_name=group_name,
        hits=hits,
        repos_scanned=other_slugs,
        elapsed_seconds=time.time() - t0,
    )
    logger.info(
        "drift_complete pr=%d group=%s removed_values=%d hits_with_matches=%d "
        "scanned=%d elapsed=%.2fs",
        pr.number, group_name, len(removed), len(hits),
        len(other_slugs), report.elapsed_seconds,
    )
    return report
