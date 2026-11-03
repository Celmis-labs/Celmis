"""Versions of an uploaded repository: record them, and replace one with the next.

An uploaded repository keeps ONE identity (its slug, hence its ``upload/<slug>``
name, its registry row, its tokens, grants, shares, projects and chats) while
its content changes version by version. A new version is brought in like this:

  1. the archive is unpacked into a staging workspace with the SAME safety rules
     as the first upload (`extract_archive`), never into the served tree;
  2. the staging workspace is indexed (graph, symbols, 1C edges, grep index)
     exactly as a served one would be, with the real indexer;
  3. only when all of that worked are the served files, graph, grep index and
     the archive record swapped for the staged ones — a handful of renames, so a
     reader sees the old version up to that moment and the new one after it;
  4. what the old version left behind that the new one does not replace (the
     vault notes of a file that no longer exists, their vectors) is removed.

If anything before step 3 fails, nothing served has been touched: the old
version keeps answering and the error says why.

The history lives in ``versions.json`` next to the archive record
(``upload.json``) in the repository's data directory. It is not part of what a
swap replaces, and it goes with the repository when that is purged.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VERSIONS_FILE = "versions.json"
#: Kept in the history; older entries fall off (the newest ones are what a person reads).
MAX_HISTORY = 100
#: Files of the data directory that survive a swap: they are not derived from the code.
_PERSISTENT = frozenset({VERSIONS_FILE, "upload.json", ".update.lock", ".grep-index.lock"})
#: How many removed paths an answer lists (the count is always complete).
SAMPLE_PATHS = 20


class UpdateError(Exception):
    """A refusal with the HTTP status the router should answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# ─── the record ──────────────────────────────────────────────────────


def _data_dir(slug: str) -> Path:
    from src.config import get_settings

    return get_settings().repo_data_path(slug)


def record_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """One history entry from an ``upload.json`` body."""
    return {
        "version": int(meta.get("version") or 1),
        "sha256": str(meta.get("sha256") or ""),
        "uploaded_at": str(meta.get("uploaded_at") or ""),
        "uploaded_by": str(meta.get("uploaded_by") or ""),
        "filename": str(meta.get("filename") or ""),
        "size": int(meta.get("size") or 0),
        "files": int(meta.get("files") or 0),
        "added": meta.get("added"),
        "removed": meta.get("removed"),
        "changed": meta.get("changed"),
    }


def read_versions(slug: str) -> list[dict[str, Any]]:
    """The history, oldest first. A repository uploaded before versions were
    recorded has its one archive as version 1."""
    try:
        data = json.loads((_data_dir(slug) / VERSIONS_FILE).read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return [d for d in data if isinstance(d, dict)]
    except Exception:  # noqa: BLE001 — no record is a normal answer
        pass
    from src.repos.upload import read_upload_meta

    meta = read_upload_meta(slug)
    return [record_from_meta(meta)] if meta and meta.get("sha256") else []


def current_version(slug: str) -> dict[str, Any] | None:
    history = read_versions(slug)
    return history[-1] if history else None


def next_number(slug: str) -> int:
    from src.repos.upload import read_upload_meta

    seen = [int(v.get("version") or 0) for v in read_versions(slug)]
    seen.append(int((read_upload_meta(slug) or {}).get("version") or 0))
    return max(seen) + 1


def append_version(slug: str, record: dict[str, Any]) -> None:
    """Add ``record`` to the history (atomic write). Never raises: the record is
    bookkeeping, and a repository that was swapped in is not undone by it."""
    try:
        data = _data_dir(slug)
        data.mkdir(parents=True, exist_ok=True)
        history = [h for h in read_versions(slug) if h.get("version") != record.get("version")]
        history.append(record)
        tmp = data / f".{VERSIONS_FILE}.{os.getpid()}"
        tmp.write_text(json.dumps(history[-MAX_HISTORY:]), encoding="utf-8")
        os.replace(tmp, data / VERSIONS_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload_version_record_failed slug=%s err=%s", slug, exc)


# ─── what changed ────────────────────────────────────────────────────


def count_files(root: Path) -> int:
    return sum(len(files) for _d, _s, files in os.walk(root))


def _manifest(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            try:
                out[str(full.relative_to(root))] = full.stat().st_size
            except OSError:
                continue
    return out


def _same_bytes(a: Path, b: Path) -> bool:
    def digest(p: Path) -> bytes:
        h = hashlib.blake2b()
        with open(p, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.digest()

    try:
        return digest(a) == digest(b)
    except OSError:
        return False


@dataclass
class TreeDiff:
    added: int = 0
    removed: int = 0
    changed: int = 0
    removed_paths: list[str] = field(default_factory=list)


def diff_trees(old: Path, new: Path) -> TreeDiff:
    """Files added, removed and changed between two trees. A file is read only
    when its size is unchanged, so an update of a large tree costs a listing
    plus the bytes of the files that might not have changed."""
    before, after = _manifest(old), _manifest(new)
    out = TreeDiff()
    for rel, size in after.items():
        if rel not in before:
            out.added += 1
        elif before[rel] != size or not _same_bytes(old / rel, new / rel):
            out.changed += 1
    out.removed_paths = sorted(rel for rel in before if rel not in after)
    out.removed = len(out.removed_paths)
    return out


# ─── vault notes of files that are gone ──────────────────────────────


def prune_vault(slug: str, removed: list[str], workspace_id: str | None = None) -> int:
    """Delete the generated notes (and their vectors) that describe a file the
    new version no longer has. A note names its file in its front matter
    (``path``). Best effort: a failure leaves the note, never the update undone."""
    if not removed:
        return 0
    from src.config import get_settings

    gone = set(removed)
    try:
        vault = get_settings().repo_vault_path(slug)
    except Exception:  # noqa: BLE001
        return 0
    if not vault.is_dir():
        return 0
    import frontmatter

    from src.vault.writer import VaultWriter

    victims: list[tuple[Path, str]] = []
    for note in vault.rglob("*.md"):
        try:
            meta = frontmatter.load(note).metadata
        except Exception:  # noqa: BLE001
            continue
        path = str(meta.get("path") or "")
        if path and path in gone:
            victims.append((note, str(note.relative_to(vault))))
    if not victims:
        return 0
    ids = [VaultWriter.note_id(slug, rel) for _n, rel in victims]
    try:
        from qdrant_client.models import PointIdsList

        from src.retrieval.vector_store import get_vector_client

        client = get_vector_client()
        collection = get_settings().qdrant_collection
        if client.collection_exists(collection):
            client.delete(collection_name=collection, points_selector=PointIdsList(points=ids))
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload_version_vector_prune_failed slug=%s err=%s", slug, exc)
    for note, _rel in victims:
        note.unlink(missing_ok=True)
    with contextlib.suppress(Exception):
        from src.vault.reverse_index import invalidate_cache

        invalidate_cache(slug)
    return len(victims)


# ─── the update ──────────────────────────────────────────────────────


@dataclass
class UpdateResult:
    version: dict[str, Any]
    added: int
    removed: int
    changed: int
    symbols: int
    files_indexed: int
    removed_paths: list[str]
    notes_removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "added": self.added, "removed": self.removed,
            "changed": self.changed, "symbols": self.symbols,
            "files_indexed": self.files_indexed,
            "removed_paths": self.removed_paths[:SAMPLE_PATHS],
            "notes_removed": self.notes_removed,
        }


@contextlib.contextmanager
def _update_lock(slug: str) -> Iterator[None]:
    data = _data_dir(slug)
    data.mkdir(parents=True, exist_ok=True)
    with open(data / ".update.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError("Another version of this repository is being applied.", 409) \
                from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _swap(pairs: list[tuple[Path, Path | None]], graveyard: Path) -> None:
    """Replace each ``live`` by its staged ``new`` (None = remove ``live``), moving
    what was there into ``graveyard``. On a failure everything moved is put back."""
    done: list[tuple[Path, Path | None]] = []  # (live, parked old)
    try:
        for live, new in pairs:
            parked: Path | None = None
            if live.exists():
                parked = graveyard / f"{len(done)}-{live.name}"
                os.replace(live, parked)
            done.append((live, parked))
            if new is not None:
                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(new, live)
    except BaseException:
        for live, parked in reversed(done):
            with contextlib.suppress(OSError):
                if live.is_dir() and not live.is_symlink():
                    shutil.rmtree(live, ignore_errors=True)
                else:
                    live.unlink(missing_ok=True)
                if parked is not None:
                    os.replace(parked, live)
        raise


def apply_new_version(
    stored, filename: str, slug: str, *, uploaded_by: str = "",  # noqa: ANN001
    workspace_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> UpdateResult:
    """Replace the served version of the uploaded repository ``slug`` with the
    archive ``stored`` — extracted and indexed first, swapped in last.

    Raises `UpdateError` (nothing served was changed) for an archive that is not
    acceptable or does not index; the message is for the person who uploaded it.
    """
    from src.config import get_settings
    from src.groups.indexer import UPLOAD_PREFIX, GroupIndexer
    from src.groups.models import RepoGroup
    from src.indexing.graph.pipeline import _graph_has_symbols
    from src.repos.upload import ArchiveError, extract_archive, read_upload_meta, upload_revision

    def say(phase: str) -> None:
        if progress:
            progress(phase)

    settings = get_settings()
    live_tree = settings.repo_path(slug)
    current = upload_revision(slug)
    if current is None or not live_tree.is_dir():
        raise UpdateError("This repository was not uploaded from an archive.", 404)
    if stored.sha256 == current:
        raise UpdateError("This archive is already the current version.", 409)

    work = settings.workspace_dir / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    with _update_lock(slug):
        stage = Path(tempfile.mkdtemp(prefix=f"update-{slug[:40]}-", dir=work))
        try:
            say("unpacking")
            (stage / "x").mkdir()
            try:
                root = extract_archive(stored.path, filename, stage / "x")
            except ArchiveError as exc:
                raise UpdateError(str(exc)) from None
            stage_settings = settings.model_copy(update={"workspace_dir": stage / "w"})
            staged_tree = stage_settings.repo_path(slug)
            staged_tree.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root, staged_tree)

            say("comparing")
            diff = diff_trees(live_tree, staged_tree)
            # The same files packed again (a fresh zip has new timestamps, so
            # new bytes) are not a new version.
            if not (diff.added or diff.removed or diff.changed):
                raise UpdateError(
                    "This archive holds the same files as the current version.", 409)
            previous = read_upload_meta(slug) or {}
            number = next_number(slug)
            meta: dict[str, Any] = {
                "sha256": stored.sha256, "size": stored.size,
                "filename": Path(filename).name[:200],
                "uploaded_at": datetime.now(UTC).isoformat(), "uploaded_by": uploaded_by,
                "version": number, "files": count_files(staged_tree),
                "added": diff.added, "removed": diff.removed, "changed": diff.changed,
            }
            if previous.get("name"):
                meta["name"] = previous["name"]
            staged_data = stage_settings.repo_data_path(slug)
            staged_data.mkdir(parents=True, exist_ok=True)
            (staged_data / "upload.json").write_text(json.dumps(meta), encoding="utf-8")

            say("indexing")
            indexer = GroupIndexer(
                group=RepoGroup(name=f"_solo_{slug[:50]}", repos=[f"{UPLOAD_PREFIX}{slug}"]),
                settings=stage_settings,
            )
            try:
                per_repo, _extractions = indexer._index_one_repo(
                    f"{UPLOAD_PREFIX}{slug}", None)
            except Exception as exc:  # noqa: BLE001
                from src.repos.indexing import safe_detail

                logger.warning("upload_version_index_failed slug=%s err=%s", slug, exc)
                raise UpdateError(
                    safe_detail(f"The new version could not be indexed: {exc}")) from None
            if per_repo.symbols == 0 and _graph_has_symbols(settings.repo_graph_path(slug)):
                raise UpdateError(
                    "The new version holds no code the indexer understands, while the "
                    "current one does. Check that this is the right archive.")
            try:
                from src.mcp_server.dev_profile import git_io

                git_io.build_index_at(staged_tree, staged_data, stored.sha256)
            except Exception:  # noqa: BLE001 — grep builds it lazily if this fails
                logger.warning("upload_version_grep_index_failed slug=%s", slug)

            say("swapping")
            live_data = settings.repo_data_path(slug)
            staged_names = {p.name for p in staged_data.iterdir()}
            pairs: list[tuple[Path, Path | None]] = [(live_tree, staged_tree)]
            for name in sorted(staged_names - {"upload.json"}):
                pairs.append((live_data / name, staged_data / name))
            for p in live_data.iterdir() if live_data.is_dir() else []:
                if p.name not in staged_names and p.name not in _PERSISTENT:
                    pairs.append((p, None))
            pairs.append((live_data / "upload.json", staged_data / "upload.json"))
            graveyard = stage / "old"
            graveyard.mkdir()
            _swap(pairs, graveyard)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    record = record_from_meta(meta)
    append_version(slug, record)
    from src.repos.index_state import record_index_success

    record_index_success(
        slug, sha=stored.sha256, files=per_repo.files_processed, full_rebuild=True,
        branch=f"v{number}",
    )
    say("cleaning")
    notes = prune_vault(slug, diff.removed_paths, workspace_id)
    with contextlib.suppress(Exception):
        from src.groups.indexer import enqueue_materialize_for_repo

        enqueue_materialize_for_repo(slug, enqueued_by=f"upload_version:{slug}")
    return UpdateResult(
        version=record, added=diff.added, removed=diff.removed, changed=diff.changed,
        symbols=per_repo.symbols, files_indexed=per_repo.files_processed,
        removed_paths=diff.removed_paths, notes_removed=notes,
    )


__all__ = [
    "MAX_HISTORY", "TreeDiff", "UpdateError", "UpdateResult", "append_version",
    "apply_new_version", "count_files", "current_version", "diff_trees", "next_number",
    "prune_vault", "read_versions", "record_from_meta",
]
