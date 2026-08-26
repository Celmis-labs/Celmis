"""Vault MD reverse-index — maps source-file paths to the vault notes
that mention them.

Purpose: when incremental indexer detects `git diff` changes to
`src/handlers.py`, we need to know which vault notes reference that
file so we can (a) drop stale Qdrant points and (b) enqueue those
notes for regeneration on next run.

Build strategy (metadata-driven, per Option A of the earlier plan):
  * Walk `vault/projects/{repo}/**/*.md`.
  * Parse frontmatter — `path`, `symbols`, `entry_points` fields.
  * For each source file mentioned → append note's relative path.
  * Cache the map as `.celmis_reverse_index.json` under the repo
    vault dir so callers don't re-walk on every incremental run.

Callers:
  * `src/sync/incremental.py` — reads the map, computes affected notes
    per changed file, enqueues `regenerate_note` jobs.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import defaultdict
from typing import Any

import frontmatter

logger = logging.getLogger(__name__)

_INDEX_FILENAME = ".celmis_reverse_index.json"


def build_reverse_index(
    repo_slug: str,
    *,
    force: bool = False,
) -> dict[str, list[str]]:
    """Return {source_file → [note_paths]} for `repo_slug`. Cached on disk;
    pass `force=True` to rebuild.
    """
    from src.config import get_settings
    settings = get_settings()
    vault_root = settings.repo_vault_path(repo_slug)
    if not vault_root.exists():
        return {}
    cache = vault_root / _INDEX_FILENAME
    if not force and cache.exists():
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("reverse_index_cache_corrupt path=%s", cache)

    reverse: dict[str, set[str]] = defaultdict(set)
    for md in vault_root.rglob("*.md"):
        rel = str(md.relative_to(vault_root))
        try:
            post = frontmatter.load(md)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reverse_index_parse_fail path=%s err=%s", md, exc)
            continue
        meta = post.metadata or {}
        for src in _source_files_from_meta(meta):
            reverse[src].add(rel)
    out = {k: sorted(v) for k, v in reverse.items()}
    try:
        cache.write_text(json.dumps(out, indent=2))
    except OSError as exc:
        logger.warning("reverse_index_cache_write_fail err=%s", exc)
    logger.info(
        "reverse_index_built repo=%s source_files=%d notes=%d",
        repo_slug, len(out), sum(len(v) for v in out.values()),
    )
    return out


def _source_files_from_meta(meta: dict[str, Any]) -> list[str]:
    """Extract every source-file path implied by a note's frontmatter."""
    files: set[str] = set()
    # Primary — module notes have `path`.
    if meta.get("path"):
        files.add(str(meta["path"]).strip("/"))
    # Symbols contain "file:function" — split.
    for sym in meta.get("symbols", []) or []:
        s = str(sym)
        if ":" in s:
            f = s.split(":", 1)[0].strip("/")
            if f and "/" in f:
                files.add(f)
    # entry_points — same idea (usually "file:handler_name").
    for ep in meta.get("entry_points", []) or []:
        s = str(ep)
        if ":" in s:
            f = s.split(":", 1)[0].strip("/")
            if f and "/" in f:
                files.add(f)
    return sorted(files)


def affected_notes(
    repo_slug: str, changed_files: list[str],
) -> list[str]:
    """Return note paths that reference any of `changed_files`."""
    rev = build_reverse_index(repo_slug)
    hits: set[str] = set()
    for f in changed_files:
        for note in rev.get(f, []):
            hits.add(note)
    return sorted(hits)


def invalidate_cache(repo_slug: str) -> None:
    from src.config import get_settings
    p = get_settings().repo_vault_path(repo_slug) / _INDEX_FILENAME
    if p.exists():
        with contextlib.suppress(OSError):
            p.unlink()


__all__ = ["build_reverse_index", "affected_notes", "invalidate_cache"]
