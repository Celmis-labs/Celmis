"""Diff parsing — unidiff (Apache 2.0) wrapper + skip pattern filtering.

A single universal parser for GitHub/GitLab/Bitbucket raw diff output.

We do not use the provider's `/files` endpoints directly (different shapes
between GitHub/GitLab/Bitbucket); instead the providers return raw unified
diff text → one parser.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import PurePosixPath

from unidiff import PatchSet

from src.review.models import Hunk
from src.review.settings import ReviewSettings, get_review_settings

logger = logging.getLogger(__name__)


def parse_unified_diff(
    raw_diff: str,
    *,
    settings: ReviewSettings | None = None,
) -> tuple[list[Hunk], list[str]]:
    """Parse unified diff text → (filtered_hunks, skipped_files).

    Args:
        raw_diff: provider's raw unified diff (`git diff` format)
        settings: Review settings (skip patterns)

    Returns:
        (hunks_to_review, skipped_files) — kept separate so that the caller
        can report "skipped: foo.lock, bar.min.js" to the user in the summary.
    """
    settings = settings or get_review_settings()
    if not raw_diff or not raw_diff.strip():
        return [], []

    try:
        patch_set = PatchSet(raw_diff)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diff_parse_failed: %s — falling back to empty", exc)
        return [], []

    hunks: list[Hunk] = []
    skipped: list[str] = []

    for patched_file in patch_set:
        # `target_file` includes the 'b/' prefix in git format → strip
        new_path = _strip_diff_prefix(patched_file.target_file or "")
        old_path = _strip_diff_prefix(patched_file.source_file or "")

        # A DELETED file has `+++ /dev/null`, so the target side names nothing
        # and the path we care about is the source side. Without this the
        # deletion was skipped as an unreviewable path and then reported to the
        # reviewer as a file literally called "/dev/null" — so a PR that only
        # removes files came back "all changed files were filtered out by skip
        # patterns (lockfiles, binaries, build dirs)", which is an affirmative
        # false statement rather than an error.
        #
        # Removing a permission check or an auth guard is exactly the change
        # this was blind to. models.py already documented the fallback
        # ("new path (post-change); old path if file deleted"); it had simply
        # never been written.
        effective_path = new_path
        if new_path == "/dev/null" and old_path and old_path != "/dev/null":
            effective_path = old_path

        # Skip filter — applied to the path the reviewer will see
        if _should_skip_file(effective_path, settings):
            skipped.append(effective_path)
            continue

        # Binary files — skip and record in hunks for the report
        is_binary = patched_file.is_binary_file
        if is_binary:
            skipped.append(f"{effective_path} (binary)")
            continue

        # Files that are too large — skip
        total_changes = patched_file.added + patched_file.removed
        if total_changes > settings.max_hunk_lines * 4:
            skipped.append(f"{effective_path} ({total_changes} changes — too large)")
            continue

        for hunk in patched_file:
            hunks.append(Hunk(
                # The path the reviewer sees. For a deletion this is the old
                # path, exactly as models.py documents it.
                file_path=effective_path,
                old_file_path=old_path,
                old_start=hunk.source_start,
                old_count=hunk.source_length,
                new_start=hunk.target_start,
                new_count=hunk.target_length,
                content=str(hunk),
                is_binary=is_binary,
                is_new_file=patched_file.is_added_file,
                is_deleted_file=patched_file.is_removed_file,
                is_renamed=patched_file.is_rename,
            ))

    # File limit — review at most N files
    if len(set(h.file_path for h in hunks)) > settings.max_files_reviewed:
        # Keep first N unique files' hunks
        seen: set[str] = set()
        kept: list[Hunk] = []
        dropped_files: set[str] = set()
        for h in hunks:
            if h.file_path in seen:
                kept.append(h)
                continue
            if len(seen) < settings.max_files_reviewed:
                seen.add(h.file_path)
                kept.append(h)
            else:
                dropped_files.add(h.file_path)
        hunks = kept
        for f in sorted(dropped_files):
            skipped.append(f"{f} (file limit reached)")

    logger.info(
        "diff_parsed hunks=%d files=%d skipped=%d",
        len(hunks), len(set(h.file_path for h in hunks)), len(skipped),
    )
    return hunks, skipped


def _strip_diff_prefix(path: str) -> str:
    """Strip 'a/' / 'b/' / '/dev/null' prefixes in git diff paths."""
    p = path.strip()
    if p in ("/dev/null", "dev/null"):
        return p
    if p.startswith("a/"):
        return p[2:]
    if p.startswith("b/"):
        return p[2:]
    return p


def _should_skip_file(path: str, settings: ReviewSettings) -> bool:
    """Check skip patterns: filename glob + directory components."""
    if not path or path == "/dev/null":
        return True

    posix_path = PurePosixPath(path)
    name = posix_path.name

    # Filename pattern (e.g. *.lock)
    for pat in settings.skip_filename_patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat):
            return True

    # Directory component (e.g. node_modules)
    parts = set(posix_path.parts)
    return any(skip_dir in parts for skip_dir in settings.skip_directory_patterns)
