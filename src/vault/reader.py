"""VaultReader — parses MD files with frontmatter."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

from src.config import Settings, get_settings
from src.security.secret_files import is_secret_path

logger = logging.getLogger(__name__)


@dataclass
class VaultNote:
    """A parsed MD note."""

    path: Path  # absolute
    relative_path: str  # relative to projects/{repo}/
    repo: str
    metadata: dict[str, Any]
    content: str

    @property
    def type(self) -> str:
        return str(self.metadata.get("type", ""))

    @property
    def symbols(self) -> list[str]:
        return list(self.metadata.get("symbols", []) or [])

    @property
    def keywords(self) -> list[str]:
        return list(self.metadata.get("keywords", []) or [])


def confined_note(root: Path, relative_path: str) -> Path | None:
    """The real path of a note under ``root`` (one repo's vault folder), else None.

    ``relative_path`` can come from a model (``read_vault_note``) or a browser
    (the docs API), so it is judged before anything is opened. Refused, all as
    "no such note": an empty or absolute path, a ``..``
    segment, a NUL byte, a backslash, a secret file's name (``.env.md``,
    ``secrets/x.md``: the same floor every other reader applies), and a path
    that resolves outside ``root`` (a link planted in the vault, which would
    otherwise read another repository's note or any ``.md`` on the server).
    """
    rel = str(relative_path or "")
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    if ".." in rel.split("/") or is_secret_path(rel):
        return None
    try:
        base = Path(root).resolve()
        target = (base / rel).resolve()
        inside = target.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    if not inside.parts or is_secret_path(inside.as_posix()):
        return None
    return target


def note_file(root: Path, relative_path: str) -> Path | None:
    """The real path of an EXISTING note under ``root``, else None.

    :func:`confined_note` plus the "is it a file" question, asked so that no
    operating-system error can escape: an over-long or too-deep name makes
    ``stat`` raise ``OSError`` whose text carries the vault's absolute path, and
    that text would reach the model (the exploration agent) or a browser (the
    docs API as a 500). Any ``OSError`` here reads as "no such note".
    """
    target = confined_note(root, relative_path)
    if target is None:
        return None
    try:
        return target if target.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def vault_note_files(root: Path) -> list[Path]:
    """Every note (``*.md``) under ``root`` that a reader may open, sorted.

    The one walk behind every listing, export and archive of a vault. A note
    counts when :func:`confined_note` accepts its name (no secret-named note,
    nothing that resolves outside ``root``) and it is a REGULAR file: a symlink
    is skipped even when it points inside the vault (the note it points at is
    listed under its own name, so it would only appear twice), and a folder link
    is never descended into, so another repository's notes or any ``.md`` on the
    server cannot be listed through one. Paths come back as found (not resolved),
    so ``path.relative_to(root)`` is the name the note is addressed by.
    """
    base = Path(root)
    found: list[Path] = []
    try:
        if not base.is_dir():
            return []
        walker = os.walk(base, followlinks=False)
        for dirpath, _dirnames, filenames in walker:
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                path = Path(dirpath) / name
                if confined_note(base, path.relative_to(base).as_posix()) is None:
                    continue
                try:
                    if not stat.S_ISREG(os.lstat(path).st_mode):
                        continue
                except OSError:
                    continue
                found.append(path)
    except (OSError, ValueError, RuntimeError):
        # A vault that cannot be walked lists what it did give before it failed.
        pass
    return sorted(found)


class VaultReader:
    """Reads and lists the notes in the vault.

    Every path it opens is confined to the repo's own vault folder
    (:func:`confined_note`), whoever supplied it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def read(self, repo: str, relative_path: str) -> VaultNote | None:
        full = note_file(self.settings.repo_vault_path(repo), relative_path)
        if full is None:
            return None
        return self._load(full, repo)

    def list_notes(self, repo: str, note_type: str | None = None) -> list[VaultNote]:
        """Returns all the notes of a repo (opt. filter by type)."""
        root = self.settings.repo_vault_path(repo)
        notes: list[VaultNote] = []
        # A link planted in the vault, or a secret-named note, is not listed.
        for path in vault_note_files(root):
            note = self._load(path, repo)
            if note is None:
                continue
            if note_type and note.type != note_type:
                continue
            notes.append(note)
        return notes

    def _load(self, path: Path, repo: str) -> VaultNote | None:
        try:
            post = frontmatter.load(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("vault_read_failed path=%s err=%s", path, exc)
            return None
        root = self.settings.repo_vault_path(repo)
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        return VaultNote(
            path=path,
            relative_path=rel,
            repo=repo,
            metadata=dict(post.metadata),
            content=post.content,
        )
