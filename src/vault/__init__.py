"""Vault — writing and reading Markdown files with frontmatter."""

from src.vault.reader import VaultNote, VaultReader
from src.vault.writer import VaultWriter

__all__ = ["VaultReader", "VaultNote", "VaultWriter"]
