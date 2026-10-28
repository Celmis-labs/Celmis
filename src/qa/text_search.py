"""Plain-text search over a repository's files.

The graph only knows the languages that have a tree-sitter extractor. A
repository in anything else (BSL, XML configuration, plain
text …) — typically one added from an archive — has no symbols, and Q&A / MCP
search would find nothing in it. This module is the language-agnostic floor:
walk the files the caller may see, skip binaries, and match words.

Matching is Unicode-aware (Cyrillic identifiers are normal in such code) and
case-insensitive; names are compared in NFC. Everything is bounded: files
larger than ``MAX_TEXT_FILE_BYTES`` are not read, a search stops at its time
budget, and results are capped.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from src.access.file_scope import FileScope
from src.security.secret_files import content_verdict, is_secret_path

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_TIME_BUDGET_S = 12.0
SNIPPET_CHARS = 240

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target",
})
_BINARY_EXTS = frozenset({
    ".bin", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".bz2", ".xz", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".class", ".o", ".a", ".pyc", ".wasm",
    ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".psd", ".db", ".sqlite",
    ".epf", ".erf", ".cf", ".cfu", ".dt",
})
_WORD = re.compile(r"\w{2,}", re.UNICODE)
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "what", "where", "which", "how",
    "does", "are", "is", "in", "of", "to", "a", "an", "де", "що", "як", "які",
    "який", "яка", "для", "або", "це", "чи", "цей", "ця", "та", "на", "по",
    "где", "что", "как", "или", "это",
    "explain", "describe", "show", "tell", "поясни", "поясніть", "розкажи", "опиши",
    "опишіть", "объясни", "расскажи",
})


def norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def terms_of(text: str, *, limit: int = 12) -> list[str]:
    """Distinct searchable words of a question, longest first."""
    words = {norm(w) for w in _WORD.findall(unicodedata.normalize("NFC", text))}
    words = {w for w in words if w not in _STOP and not w.isdigit()}
    return sorted(words, key=lambda w: (-len(w), w))[:limit]


def looks_binary(head: bytes) -> bool:
    return b"\x00" in head


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def iter_text_files(root: Path, scope: FileScope | None = None) -> Iterator[tuple[str, Path]]:
    """``(repo-relative path, file)`` for every candidate text file under
    ``root``, in a stable order, honouring the scope and skipping binaries.

    A secret file (``.env``, keys, credential stores: ``is_secret_path``) is
    never a candidate, for every caller and whatever the access rules say: this
    walk is the single door of the plain-text search and of the ranking that
    picks files for a prompt, so a caller that forgets its own gate still cannot
    open one. Nor is a data file whose CONTENT is a secret (a PEM in
    ``data/notes.txt``, a ``kind: Secret`` manifest anywhere in a YAML, a data
    file too large to be looked through): the name says nothing there. The scope
    only narrows further."""
    root_s = str(root)
    for dirpath, dirnames, filenames in os.walk(root_s):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in _BINARY_EXTS:
                continue
            full = Path(dirpath) / fn
            if full.is_symlink():
                continue
            on_disk = os.path.relpath(full, root_s)
            rel = unicodedata.normalize("NFC", on_disk).replace(os.sep, "/")
            if scope is not None and not scope.allows(rel):
                continue
            # The content is read under the name the file has on disk, not the NFC spelling.
            if is_secret_path(rel) or content_verdict(root, on_disk) != "ok":
                continue
            yield rel, full


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read(MAX_TEXT_FILE_BYTES + 1)
    except OSError:
        return None
    if looks_binary(data[:4096]):
        return None
    return decode(data)


@dataclass(frozen=True)
class TextHit:
    path: str
    line: int
    snippet: str
    score: int


def search_text(
    root: Path, query: str, *, scope: FileScope | None = None, limit: int = 15,
    per_file: int = 3, time_budget: float = DEFAULT_TIME_BUDGET_S,
    visible=None,  # noqa: ANN001 — callable(rel) -> bool, the access gate
) -> tuple[list[TextHit], bool]:
    """Lines that contain the query (as a phrase, else any of its words).

    Returns ``(hits, complete)``; ``complete`` is False when the time budget
    ran out before every file was read. Hits are ordered by how many distinct
    words the line has, then by file name match.
    """
    phrase = norm(query.strip())
    words = terms_of(query)
    if len(phrase) < 2 and not words:
        return [], True
    deadline = time.monotonic() + time_budget
    hits: list[TextHit] = []
    complete = True
    for rel, full in iter_text_files(root, scope):
        if time.monotonic() > deadline:
            complete = False
            break
        if visible is not None and not visible(rel):
            continue
        name_bonus = 2 if any(w in norm(rel) for w in words) else 0
        text = _read_text(full)
        if text is None:
            if name_bonus:
                hits.append(TextHit(rel, 0, "(file name matches)", name_bonus))
            continue
        low = norm(text)
        if phrase not in low and not any(w in low for w in words):
            if name_bonus:
                hits.append(TextHit(rel, 0, "(file name matches)", name_bonus))
            continue
        taken = 0
        for n, (raw, line) in enumerate(zip(text.splitlines(), low.splitlines(), strict=False), 1):
            score = (3 if phrase in line else 0) + sum(1 for w in words if w in line)
            if not score:
                continue
            hits.append(TextHit(rel, n, raw.strip()[:SNIPPET_CHARS], score + name_bonus))
            taken += 1
            if taken >= per_file:
                break
    hits.sort(key=lambda h: (-h.score, h.path, h.line))
    return hits[:limit], complete


def rank_files(
    root: Path, question: str, *, scope: FileScope | None = None, limit: int = 8,
    time_budget: float = DEFAULT_TIME_BUDGET_S, visible=None,  # noqa: ANN001
    extra_terms=(),  # noqa: ANN001 — stems from src.qa.query_terms (translations)
) -> list[str]:
    """Repo-relative paths most worth reading for ``question``: files ranked by
    how many distinct question words they contain (path matches count double).

    Words are compared as stems (case endings and ru/uk spellings levelled,
    :mod:`src.qa.query_terms`); ``extra_terms`` are further stems to look for."""
    from src.qa.query_terms import fold, lexical_terms

    words = list(dict.fromkeys([*lexical_terms(question), *extra_terms]))
    if not words:
        return []
    deadline = time.monotonic() + time_budget
    scored: list[tuple[int, str]] = []
    for rel, full in iter_text_files(root, scope):
        if time.monotonic() > deadline:
            break
        if visible is not None and not visible(rel):
            continue
        text = _read_text(full)
        low = fold(text) if text is not None else ""
        frel = fold(rel)
        score = sum(1 for w in words if w in low) + 2 * sum(1 for w in words if w in frel)
        if score:
            scored.append((score, rel))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [rel for _, rel in scored[:limit]]
