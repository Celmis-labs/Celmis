"""Cascade-delete a repository across every store that holds its data.

A repo leaves traces in six independent places — embedded/remote Qdrant, three
directories on disk, Postgres, two SQLite files, and the YAML group files — and
none of them know about each other. This module is the one place that knows the
full list.

Every step is isolated: a failure is appended to `report.errors` and the
remaining steps still run. Purging is something you reach for when a repo is
already half-broken, so aborting on the first error would leave *more* mess
behind than pushing through.

Call sites: `analyzer repo purge` (CLI) and `DELETE /api/repos/{slug}?purge=true`.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PurgeReport:
    """Per-store outcome. Field names are part of the CLI and HTTP contract."""

    slug: str
    qdrant_points_deleted: int = 0
    clone_dir_removed: bool = False
    clone_dir_path: str = ""
    data_dir_removed: bool = False
    data_dir_path: str = ""
    vault_dir_removed: bool = False
    vault_dir_path: str = ""
    project_repo_links_removed: int = 0
    auto_review_rows_removed: int = 0
    review_run_rows_removed: int = 0
    group_memberships_removed: int = 0
    groups_touched: list[str] = field(default_factory=list)
    orphan_rows_removed: int = 0
    disk_bytes_freed: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dir_size(path: Path) -> int:
    """Bytes under `path`. Best-effort — unreadable entries count as zero."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _rmtree(path: Path, report: PurgeReport, label: str) -> tuple[bool, int]:
    """Delete `path`, returning (removed, bytes_freed)."""
    if not path.exists():
        return False, 0
    size = _dir_size(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        report.errors.append(f"{label}: {exc}")
        return False, 0
    return True, size


def _purge_qdrant(slug: str, report: PurgeReport, workspace_id: str | None) -> None:
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    from src.config import get_settings
    from src.retrieval.vector_store import VectorScope, get_vector_client

    settings = get_settings()
    client = get_vector_client()
    collection = settings.qdrant_collection
    # `repo` is the payload key every vault point is written with — see
    # src/vault/writer.py. Slug, not full_name.
    #
    # AND the workspace. Deleting by slug alone is a cross-tenant DELETE: two
    # workspaces may register repositories whose slugs collide — the slug is
    # `{provider}_{owner}-{name}`, which is not unique across tenants — and
    # purging one would silently take the other's vectors with it. The read
    # side was scoped first; a delete that is not scoped is the same hole
    # pointing the other way, and it destroys rather than discloses.
    scope = VectorScope.for_workspace(workspace_id)
    flt = Filter(must=[
        FieldCondition(key="repo", match=MatchValue(value=slug)),
        *scope.must_conditions(),
    ])

    if not client.collection_exists(collection):
        return
    # Count first: delete() reports no count, and the number is the only
    # feedback the user gets that the vault was actually indexed.
    report.qdrant_points_deleted = client.count(
        collection_name=collection, count_filter=flt, exact=True,
    ).count
    if report.qdrant_points_deleted:
        client.delete(collection_name=collection, points_selector=flt)


def _purge_disk(slug: str, report: PurgeReport) -> None:
    from src.config import get_settings

    settings = get_settings()
    clone = settings.repo_path(slug)
    data = settings.repo_data_path(slug)
    vault = settings.repo_vault_path(slug)

    report.clone_dir_path = str(clone)
    report.data_dir_path = str(data)
    report.vault_dir_path = str(vault)

    for path, label, attr in (
        (clone, "clone dir", "clone_dir_removed"),
        (data, "data dir", "data_dir_removed"),
        (vault, "vault dir", "vault_dir_removed"),
    ):
        removed, freed = _rmtree(path, report, label)
        setattr(report, attr, removed)
        report.disk_bytes_freed += freed


def _sqlite_delete(db_path: Path, sql: str, params: tuple[Any, ...]) -> int:
    """Run a DELETE against a store's SQLite file. 0 if the file/table is absent."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        return conn.execute(sql, params).rowcount
    except sqlite3.OperationalError:
        # Table not created yet — nothing of ours is in there.
        return 0
    finally:
        conn.close()


def _purge_sqlite(slug: str, report: PurgeReport) -> None:
    """auto_review_config + review_runs, across *all* users.

    Unlike the lightweight delete (which hides the repo for one user), purge is
    workspace-wide, so the per-user store API doesn't fit — we go at the files
    directly.
    """
    from src.api.auto_review import get_auto_review_store
    from src.api.review_runs import get_review_run_store

    ar_store = get_auto_review_store()
    # review_runs stores the *provider* repo name (owner/name), not our slug,
    # so resolve it from the config rows before deleting them.
    full_names = {
        cfg.full_name for cfg in ar_store.list_all()
        if cfg.repo_slug == slug and cfg.full_name
    }

    try:
        report.auto_review_rows_removed = _sqlite_delete(
            ar_store.db_path,
            "DELETE FROM auto_review_config WHERE repo_slug = ?", (slug,),
        )
    except sqlite3.Error as exc:
        report.errors.append(f"auto_review_config: {exc}")

    runs_db = get_review_run_store().db_path
    for full_name in full_names:
        try:
            report.review_run_rows_removed += _sqlite_delete(
                runs_db,
                "DELETE FROM review_runs WHERE pr_repo = ? OR pr_ref LIKE ?",
                (full_name, f"%{full_name}%"),
            )
        except sqlite3.Error as exc:
            report.errors.append(f"review_runs({full_name}): {exc}")


def _purge_groups(slug: str, report: PurgeReport) -> None:
    from src.groups.manager import GroupManager
    from src.sync.git_providers import parse_repo_url

    mgr = GroupManager()
    # PATHS, not names. `list()` walks the flat layout AND every tenant
    # directory, but `load(name)` resolves a bare name against the flat
    # address — so a tenant-scoped group was listed, failed to open, and its
    # membership survived a purge that reported success.
    for _path, group in mgr.iter_groups():
        if group.project_id:
            # A project-derived group is a VIEW: its membership lives in
            # `project_repos`, which _purge_postgres deletes from directly.
            # Rewriting it here would raise, and succeeding would be worse —
            # a YAML file beside the project, free to disagree with it.
            continue
        name = group.name

        # Groups store whatever identifier form the user typed (URL, owner/name,
        # provider:owner/name), so match on the parsed slug rather than the raw
        # string. RepoGroup.remove_repo() can't take our local slug directly —
        # it re-parses its argument, and a local slug has no owner/name shape.
        kept = []
        hit = False
        for ident in group.repos:
            try:
                if parse_repo_url(ident).slug == slug:
                    hit = True
                    continue
            except ValueError:
                pass  # unparseable entry — leave it alone, it isn't ours
            kept.append(ident)

        if hit:
            group.repos = kept
            mgr.save(group)
            report.group_memberships_removed += 1
            report.groups_touched.append(name)


async def _purge_postgres(slug: str, session: Any, report: PurgeReport) -> None:
    from sqlalchemy import delete

    from src.db.models import (
        DeprecatedSymbol,
        OwnershipSnapshot,
        ProjectRepo,
        RepoAccessRule,
        RepoIndexState,
        RepoReviewPolicy,
        RepoSummary,
        RepoTeamAccess,
    )

    result = await session.execute(
        delete(ProjectRepo).where(ProjectRepo.repo_slug == slug)
    )
    report.project_repo_links_removed = result.rowcount or 0

    # Everything else keyed by repo_slug. Left behind, these resurface as stale
    # summaries and index state if the same repo is ever re-added.
    for model in (
        RepoReviewPolicy, RepoIndexState, RepoSummary, OwnershipSnapshot,
        DeprecatedSymbol, RepoTeamAccess, RepoAccessRule,
    ):
        try:
            res = await session.execute(delete(model).where(model.repo_slug == slug))
            report.orphan_rows_removed += res.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{model.__tablename__}: {exc}")

    await session.commit()


async def purge_repo(
    slug: str,
    *,
    session: Any,
    workspace_id: str | None = None,
    skip_qdrant: bool = False,
    skip_disk: bool = False,
) -> PurgeReport:
    """Delete every trace of `slug`. Non-raising — inspect `report.errors`.

    `session` is an open AsyncSession; this function commits it.
    """
    started = time.monotonic()
    report = PurgeReport(slug=slug)

    if not skip_qdrant:
        try:
            _purge_qdrant(slug, report, workspace_id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"qdrant: {exc}")

    if not skip_disk:
        try:
            _purge_disk(slug, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"disk: {exc}")

    try:
        await _purge_postgres(slug, session, report)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"postgres: {exc}")
        with contextlib.suppress(Exception):
            await session.rollback()

    try:
        _purge_sqlite(slug, report)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"sqlite: {exc}")

    try:
        _purge_groups(slug, report)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"groups: {exc}")

    report.elapsed_seconds = time.monotonic() - started
    logger.info(
        "repo_purged slug=%s points=%d mb=%.2f links=%d runs=%d groups=%d errors=%d",
        slug, report.qdrant_points_deleted, report.disk_bytes_freed / 1048576,
        report.project_repo_links_removed, report.review_run_rows_removed,
        report.group_memberships_removed, len(report.errors),
    )
    return report
