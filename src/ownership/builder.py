"""Ownership graph builder.

For each file in the repo (up to a cap):
    * `git log --since=<lookback>` bucketed by author → top authors
    * CODEOWNERS globs matched → owning teams/users
    * `primary_owner` = the top-committer whose author matches a
      CODEOWNERS entry when both agree, else top-committer email.

The result is a dict per-path so ``get_owner("src/foo.py")`` is O(1).
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODEOWNERS_LOCATIONS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")

# Cap files we blame — prevents nightmare on a 50k-file monorepo. Anything
# beyond this stays "unowned" and falls back to CODEOWNERS glob match.
_MAX_FILES_BLAMED = 400


def compute_ownership(
    repo_slug: str,
    *,
    lookback_days: int = 90,
    computed_by: str | None = None,
) -> str | None:
    """Rebuild the ownership snapshot for ``repo_slug``. Returns snapshot id
    or None if the repo clone isn't available."""
    from src.config import get_settings

    settings = get_settings()
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists() or not (repo_path / ".git").exists():
        logger.warning("ownership_no_clone repo=%s path=%s", repo_slug, repo_path)
        return None

    files = _list_tracked_files(repo_path)
    if not files:
        logger.warning("ownership_no_files repo=%s", repo_slug)
        return None

    codeowners = _parse_codeowners(repo_path)
    since = _iso_since(lookback_days)

    paths_map: dict[str, dict[str, Any]] = {}
    author_counter: Counter[str] = Counter()

    for f in files[:_MAX_FILES_BLAMED]:
        top_authors = _top_authors_for_file(repo_path, f, since)
        matched_owners = _match_codeowners(codeowners, f)
        primary = None
        if top_authors:
            primary = top_authors[0]["email"] or top_authors[0]["name"]
            author_counter[primary] += top_authors[0]["commits"]
        paths_map[f] = {
            "top_authors": top_authors,
            "codeowners": matched_owners,
            "primary_owner": primary,
        }

    if len(files) > _MAX_FILES_BLAMED:
        # Files beyond cap — populate CODEOWNERS-only entries so lookups
        # still work; primary_owner=None flags them as "not-blamed".
        for f in files[_MAX_FILES_BLAMED:]:
            paths_map[f] = {
                "top_authors": [],
                "codeowners": _match_codeowners(codeowners, f),
                "primary_owner": None,
            }

    stats = {
        "files_total": len(files),
        "files_blamed": min(len(files), _MAX_FILES_BLAMED),
        "distinct_authors": len(author_counter),
        "top_owners": [
            {"identity": ident, "commits": n}
            for ident, n in author_counter.most_common(10)
        ],
    }

    snapshot_id = str(uuid.uuid4())
    _persist(
        snapshot_id=snapshot_id,
        repo_slug=repo_slug,
        lookback_days=lookback_days,
        paths=paths_map,
        stats=stats,
        computed_by=computed_by,
    )
    logger.info("ownership_rebuilt repo=%s files=%d authors=%d",
                repo_slug, stats["files_total"], stats["distinct_authors"])
    return snapshot_id


def load_snapshot(repo_slug: str) -> dict[str, Any] | None:
    from sqlalchemy import create_engine, desc, select
    from sqlalchemy.orm import Session

    from src.db.models import OwnershipSnapshot
    from src.db.session import get_database_url

    url = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as s:
            row = s.execute(
                select(OwnershipSnapshot)
                .where(OwnershipSnapshot.repo_slug == repo_slug)
                .order_by(desc(OwnershipSnapshot.computed_at))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "repo_slug": row.repo_slug,
                "computed_at": row.computed_at.isoformat(),
                "lookback_days": row.lookback_days,
                "paths": dict(row.paths or {}),
                "stats": dict(row.stats or {}),
            }
    finally:
        engine.dispose()


def lookup_owner(repo_slug: str, path: str) -> dict[str, Any] | None:
    """O(1) lookup for a specific path. Falls back to prefix-match on the
    nearest ancestor directory that has CODEOWNERS coverage."""
    snap = load_snapshot(repo_slug)
    if snap is None:
        return None
    paths = snap["paths"]
    if path in paths:
        return paths[path]
    # Prefix fallback: try shorter directory prefixes.
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:i]) + "/"
        for p, meta in paths.items():
            if p.startswith(prefix) and meta.get("codeowners"):
                return {
                    "top_authors": [],
                    "codeowners": meta["codeowners"],
                    "primary_owner": None,
                    "matched_via": f"prefix:{prefix}",
                }
    return None


# ─── Internals ───────────────────────────────────────────────────────


def _iso_since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def _list_tracked_files(repo_path: Path) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    # Skip common noise so we blame code, not lockfiles.
    keep = []
    skip_suffixes = (".lock", ".png", ".jpg", ".svg", ".pdf", ".map", ".min.js", ".min.css")
    skip_prefixes = ("node_modules/", "vendor/", "dist/", "build/", ".venv/")
    for f in files:
        if any(f.endswith(s) for s in skip_suffixes):
            continue
        if any(f.startswith(p) for p in skip_prefixes):
            continue
        keep.append(f)
    return keep


def _top_authors_for_file(
    repo_path: Path, path: str, since: str, *, limit: int = 3,
) -> list[dict[str, Any]]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "log", f"--since={since}",
             "--format=%an\t%ae", "--", path],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    counter: Counter[tuple[str, str]] = Counter()
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        name = parts[0] if parts else ""
        email = parts[1] if len(parts) > 1 else ""
        counter[(name, email)] += 1
    return [
        {"name": n, "email": e, "commits": c}
        for (n, e), c in counter.most_common(limit)
    ]


def _parse_codeowners(repo_path: Path) -> list[tuple[str, list[str]]]:
    for loc in _CODEOWNERS_LOCATIONS:
        p = repo_path / loc
        if p.exists():
            try:
                lines = p.read_text().splitlines()
            except OSError:
                continue
            entries: list[tuple[str, list[str]]] = []
            for raw in lines:
                s = raw.split("#", 1)[0].strip()
                if not s:
                    continue
                bits = s.split()
                if len(bits) < 2:
                    continue
                pattern, owners = bits[0], bits[1:]
                entries.append((pattern, owners))
            return entries
    return []


_GLOB_TRANSFORMS = (
    ("**/", "*/"),  # simplistic — good enough for common cases
)


def _match_codeowners(entries: list[tuple[str, list[str]]], path: str) -> list[str]:
    # Last match wins (GitHub semantics) — iterate reversed, first match wins here.
    for pat, owners in reversed(entries):
        norm = pat
        for a, b in _GLOB_TRANSFORMS:
            norm = norm.replace(a, b)
        if _codeowners_match(norm, path):
            return owners
    return []


def _codeowners_match(pattern: str, path: str) -> bool:
    # Directory rule (ends with '/'): matches any file under it.
    if pattern.endswith("/"):
        return path.startswith(pattern.lstrip("/"))
    # Absolute (leading '/') vs relative (name anywhere).
    p = pattern.lstrip("/")
    if fnmatch.fnmatch(path, p):
        return True
    # Relative pattern (no leading '/') also matches the name at any depth.
    return not pattern.startswith("/") and fnmatch.fnmatch(path, f"*/{p}")


def _persist(
    *,
    snapshot_id: str,
    repo_slug: str,
    lookback_days: int,
    paths: dict[str, Any],
    stats: dict[str, Any],
    computed_by: str | None,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy import delete as sql_delete
    from sqlalchemy.orm import Session

    from src.db.models import OwnershipSnapshot
    from src.db.session import get_database_url

    url = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as s:
            # Only keep the latest snapshot per repo — the storage cost of
            # JSONB with 10k paths is nontrivial otherwise.
            s.execute(sql_delete(OwnershipSnapshot)
                      .where(OwnershipSnapshot.repo_slug == repo_slug))
            s.add(OwnershipSnapshot(
                id=snapshot_id, repo_slug=repo_slug,
                lookback_days=lookback_days,
                paths=paths, stats=stats, computed_by=computed_by,
            ))
            s.commit()
    finally:
        engine.dispose()
