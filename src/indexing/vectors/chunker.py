"""Code chunker — a wrapper over LlamaIndex CodeSplitter (tree-sitter based).

LlamaIndex CodeSplitter uses tree_sitter_language_pack as its backend
(the same one our extractors use), so the lists of supported languages match.

Research finding (April 2026):
    CodeSplitter returns TextNodes with .start_char_idx / .end_char_idx
    (character offsets), NOT start_line/end_line. We derive the lines
    ourselves by counting `\n` in the source.

API:
    from llama_index.core.node_parser import CodeSplitter
    splitter = CodeSplitter(
        language="typescript",
        chunk_lines=settings.chunk_lines,
        chunk_lines_overlap=settings.chunk_lines_overlap,
        max_chars=settings.chunk_max_chars,
    )
    chunks = splitter.split_text(source)  # list[str]

Phase: 7a. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ─── language detection ────────────────────────────────────────────


#: Where the chunker wants a different grammar from the one the extractor used.
#: One TypeScript extractor walks the whole `.ts/.js/.tsx/.jsx` family because
#: the graph does not care which dialect a symbol came from; a splitter does,
#: and `tsx` is its own grammar. These are the mappings this file had before
#: the registry became the source, kept exactly as they were — they are the
#: measured-good ones, and the registry's coarser answer must not overwrite them.
_GRAMMAR_OVERRIDES = {
    ".ts": "typescript",
    ".cts": "typescript",
    ".mts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".js": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
}

#: Used only when the indexer package cannot be imported. Embedding must not
#: stop because the extractor registry moved; a smaller map costs recall on a
#: search, an exception costs the whole indexing run.
_FALLBACK_LANG_BY_EXT = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cts": "typescript",
    ".mts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
    ".vue": "vue",
    ".py": "python",
    ".php": "php",
    ".go": "go",
}


@lru_cache(maxsize=1)
def _lang_by_ext() -> dict[str, str]:
    """Suffix → tree-sitter grammar, asked of the extractor registry.

    This was a hand-written dict of twelve suffixes while the indexer parsed
    twenty-three languages, and the two were never connected. The visible cost
    was that a Java, C# or C++ file — languages with a hand-written extractor
    for years — produced a full graph and NOT ONE embedding, so "ask the code"
    could not retrieve a line of them; and when sixteen languages were added at
    once, none of them would have been embedded either.

    Derived from the same registry `factory.supported_suffixes()` reads, so a
    language added in one place arrives in all of them. `CodeSplitter` takes
    its parsers from `tree_sitter_language_pack`, the very pack the extractors
    use, so a grammar the registry names is one the splitter can load.
    """
    merged = dict(_FALLBACK_LANG_BY_EXT)
    try:
        from src.indexing.graph.languages.factory import build_default_registry

        for extractor in build_default_registry().extractors():
            language = getattr(extractor, "language", "")
            if not language:
                continue
            for ext in getattr(extractor, "extensions", ()) or ():
                merged.setdefault(ext.lower(), language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chunker_lang_map_fallback err=%s", exc)
    merged.update(_GRAMMAR_OVERRIDES)
    return merged


def lang_for(path: Path | str) -> str | None:
    """Return tree-sitter language identifier for path, or None if unsupported."""
    return _lang_by_ext().get(Path(path).suffix.lower())


# ─── chunk model ───────────────────────────────────────────────────


@dataclass
class CodeChunk:
    """One chunk of code with the metadata for Qdrant."""

    text: str
    file: str           # relative path
    start_line: int     # 1-based, inclusive
    end_line: int       # 1-based, inclusive
    language: str       # tree-sitter language id
    chunk_index: int    # sequence number of the chunk in the file (0..N-1)
    symbol: str | None = None  # if the chunk ↔ one specific symbol
    module: str | None = None

    def chunk_id(self) -> str:
        """Unique ID for Qdrant: file:start_line-end_line:idx."""
        return f"{self.file}:{self.start_line}-{self.end_line}:{self.chunk_index}"

    def to_payload(self) -> dict[str, Any]:
        """Payload for Qdrant — without `text` (the text goes into the
        sparse vector)."""
        d = asdict(self)
        d.pop("text", None)
        return {k: v for k, v in d.items() if v is not None}


# ─── chunker ───────────────────────────────────────────────────────


class CodeChunker:
    """Tree-sitter aware code chunker via LlamaIndex CodeSplitter.

    NB: CodeSplitter cache splitter instance per language (lazy).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._splitters: dict[str, Any] = {}

    def _get_splitter(self, language: str):
        """Lazy create CodeSplitter per language."""
        if language not in self._splitters:
            from llama_index.core.node_parser import CodeSplitter

            self._splitters[language] = CodeSplitter(
                language=language,
                chunk_lines=self.settings.chunk_lines,
                chunk_lines_overlap=self.settings.chunk_lines_overlap,
                max_chars=self.settings.chunk_max_chars,
            )
        return self._splitters[language]

    def chunk_file(
        self,
        file_path: Path | str,
        source: str | None = None,
        rel_path: str | None = None,
    ) -> list[CodeChunk]:
        """Split the file into chunks. source — optional (otherwise we read
        it from disk)."""
        path = Path(file_path)
        language = lang_for(path)
        if language is None:
            return []

        if source is None:
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("chunk_read_failed path=%s err=%s", path, e)
                return []

        if not source.strip():
            return []

        rel = rel_path if rel_path is not None else str(path)

        # Vue: we parse the whole file — CodeSplitter uses the tree-sitter-vue
        # grammar, which knows about <script>/<template>/<style>. That is OK for
        # chunking (one chunk may cover script + template), but retrieval will
        # then have the full SFC context. The alternative — chunking <script>
        # separately as TS — is harder, YAGNI until it becomes a problem.
        try:
            splitter = self._get_splitter(language)
            chunks_text: list[str] = splitter.split_text(source)
        except Exception as e:  # noqa: BLE001
            # A grammar that will not split this file must not delete it from
            # search. Returning [] here is how a Java file could hold a full
            # graph and be unfindable by "ask the code"; a plain line window is
            # a worse chunk than a syntactic one and an infinitely better one
            # than none. Logged at warning because a language that ALWAYS lands
            # here is a mapping bug, not a fact of life.
            logger.warning("chunker_failed path=%s lang=%s err=%s", path, language, e)
            # Built here rather than handed to the loop below: that loop finds
            # each chunk's lines by searching the source forward from a cursor,
            # which cannot place a window that OVERLAPS the previous one — it
            # searches past the overlap's start and silently reports the wrong
            # lines. The fallback already knows exactly where every window
            # begins, so it says so instead of being asked to guess.
            return [
                CodeChunk(
                    text=text, file=rel, start_line=start,
                    end_line=start + text.rstrip("\n").count("\n"),
                    language=language, chunk_index=idx,
                )
                for idx, (text, start) in enumerate(_line_windows(
                    source,
                    lines=self.settings.chunk_lines,
                    overlap=self.settings.chunk_lines_overlap,
                ))
            ]

        # Derive line numbers: we find the position of each chunk in the source.
        # `split_text` returns a subset of the source text. For simplicity —
        # a sequential search with offset tracking.
        out: list[CodeChunk] = []
        cursor = 0
        for idx, chunk_text in enumerate(chunks_text):
            if not chunk_text:
                continue
            # Look for the chunk in the source from cursor — exact substring match
            pos = source.find(chunk_text, cursor)
            if pos < 0:
                # If it is not found — fallback: we simply count the lines as
                # "after the last chunk + chunk_lines_overlap". Not critical.
                pos = cursor
            start_line = source.count("\n", 0, pos) + 1
            end_line = start_line + chunk_text.count("\n")
            cursor = pos + len(chunk_text)
            out.append(CodeChunk(
                text=chunk_text,
                file=rel,
                start_line=start_line,
                end_line=end_line,
                language=language,
                chunk_index=idx,
            ))

        return out


def _line_windows(
    source: str, *, lines: int, overlap: int,
) -> list[tuple[str, int]]:
    """Overlapping windows of whole lines, each with its 1-based first line.

    The chunker of last resort, used only when the grammar refuses a file: the
    alternative is dropping the file out of search entirely, which is how a
    Java file could hold a full graph and be unfindable by a question.

    Returns the start line WITH the text rather than text alone, because the
    caller's usual way of recovering line numbers — searching the source
    forward from a cursor — cannot place a window that overlaps its
    predecessor, and would report lines the chunk does not cover.
    """
    all_lines = source.splitlines(keepends=True)
    if not all_lines:
        return []
    window = max(1, int(lines or 1))
    step = max(1, window - max(0, int(overlap or 0)))
    out: list[tuple[str, int]] = []
    for start in range(0, len(all_lines), step):
        chunk = "".join(all_lines[start:start + window])
        if chunk.strip():
            out.append((chunk, start + 1))
        if start + window >= len(all_lines):
            break
    return out
