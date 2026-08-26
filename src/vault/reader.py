"""VaultReader — parses MD files with frontmatter."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

from src.config import Settings, get_settings

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


class VaultReader:
    """Reads and lists the notes in the vault."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def read(self, repo: str, relative_path: str) -> VaultNote | None:
        full = self.settings.repo_vault_path(repo) / relative_path
        if not full.exists():
            return None
        return self._load(full, repo)

    def list_notes(self, repo: str, note_type: str | None = None) -> list[VaultNote]:
        """Returns all the notes of a repo (opt. filter by type)."""
        root = self.settings.repo_vault_path(repo)
        if not root.exists():
            return []
        notes: list[VaultNote] = []
        for path in root.rglob("*.md"):
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
