"""Tier 3 — reading code from disk with a token budget and redaction.

We read only the RELEVANT chunks (functions from Tier 2), not the whole file.
We are bounded by an overall token budget — so we prioritize the more
important chunks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Approximate token counter; Gemini uses its own BPE but tiktoken is a sufficient approximation
_ENCODER = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    try:
        return len(_ENCODER.encode(text))
    except Exception:  # noqa: BLE001
        return len(text) // 4  # fallback ~4 chars/token


@dataclass
class CodeSnippet:
    """A single chunk of code for the LLM."""

    file: str  # relative
    start_line: int
    end_line: int
    language: str
    content: str  # already redacted
    symbol_name: str | None = None
    tokens: int = 0

    def as_markdown_block(self) -> str:
        header = f"### 📍 [{self.file}:{self.start_line}]({self.file}#L{self.start_line})"
        return f"{header}\n\n```{self.language}\n{self.content}\n```\n"


@dataclass
class CodeBundle:
    """A set of snippets that stays within the token budget."""

    snippets: list[CodeSnippet] = field(default_factory=list)
    total_tokens: int = 0
    budget: int = 0
    truncated: bool = False

    def as_markdown(self) -> str:
        parts = [s.as_markdown_block() for s in self.snippets]
        if self.truncated:
            parts.append(
                f"\n> ⚠️ Truncated at {self.total_tokens}/{self.budget} tokens. "
                f"Showing {len(self.snippets)} snippets."
            )
        return "\n".join(parts)

    def files_included(self) -> list[str]:
        return sorted({s.file for s in self.snippets})


class CodeReader:
    """Reads code fragments while staying within the budget. Redaction is MANDATORY before returning."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def read_locations(
        self,
        repo_path: Path,
        locations: list[tuple[str, int, int | None]],  # (file, start_line, end_line)
        *,
        budget_tokens: int | None = None,
        context_lines: int = 3,
        redact_content: bool = True,
    ) -> CodeBundle:
        """Reads the specified locations from the repo. Returns a bundle with
        redacted snippets.

        Args:
            locations: list of (file, start, end) — if end=None, we read a sensible block around it
            budget_tokens: overall token budget; by default taken from config
            context_lines: how many lines of context to add before/after
            redact_content: whether to pass through the redactor
        """
        budget = budget_tokens or self.settings.max_code_tokens_per_qa
        bundle = CodeBundle(budget=budget)

        seen: set[tuple[str, int]] = set()
        for file_rel, start, end in locations:
            key = (file_rel, start)
            if key in seen:
                continue
            seen.add(key)

            snippet = self._read_one(repo_path, file_rel, start, end, context_lines)
            if snippet is None:
                continue

            if redact_content:
                from src.security.redactor import redact

                snippet.content, _ = redact(snippet.content, source_hint=file_rel)

            snippet.tokens = _count_tokens(snippet.content)

            if bundle.total_tokens + snippet.tokens > budget:
                bundle.truncated = True
                logger.info(
                    "code_budget_exhausted files=%d tokens=%d/%d",
                    len(bundle.snippets),
                    bundle.total_tokens,
                    budget,
                )
                break

            bundle.snippets.append(snippet)
            bundle.total_tokens += snippet.tokens

        return bundle

    def read_full_file(
        self,
        repo_path: Path,
        file_rel: str,
        *,
        redact_content: bool = True,
    ) -> CodeSnippet | None:
        """Read the file in full. Used with care — only for small/key files."""
        fp = repo_path / file_rel
        if not fp.exists() or not fp.is_file():
            return None
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("read_failed file=%s err=%s", file_rel, exc)
            return None
        if redact_content:
            from src.security.redactor import redact

            text, _ = redact(text, source_hint=file_rel)
        language = _language_for_file(fp)
        return CodeSnippet(
            file=file_rel,
            start_line=1,
            end_line=text.count("\n") + 1,
            language=language,
            content=text,
            tokens=_count_tokens(text),
        )

    def _read_one(
        self,
        repo_path: Path,
        file_rel: str,
        start: int,
        end: int | None,
        context_lines: int,
    ) -> CodeSnippet | None:
        fp = repo_path / file_rel
        if not fp.exists() or not fp.is_file():
            logger.debug("file_not_found %s", file_rel)
            return None
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            logger.warning("read_failed file=%s err=%s", file_rel, exc)
            return None

        # If end is not given — we look for the end of the function heuristically (brace/indent balance)
        if end is None:
            end = _detect_block_end(lines, start, fp.suffix)

        snippet_start = max(1, start - context_lines)
        snippet_end = min(len(lines), end + context_lines)
        snippet = "\n".join(lines[snippet_start - 1 : snippet_end])
        return CodeSnippet(
            file=file_rel,
            start_line=snippet_start,
            end_line=snippet_end,
            language=_language_for_file(fp),
            content=snippet,
        )


def _language_for_file(fp: Path) -> str:
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".vue": "vue",
        ".svelte": "svelte",
        ".php": "php",
        ".go": "go",
    }.get(fp.suffix, "")


_JS_NOOP_PREFIXES = ("import ", "import\t", "import{", "import ", "from ")


def _is_js_noise_line(line: str) -> bool:
    """Whether the line is a top-level import / comment / export-from / empty.

    Such lines must not be the start of a functional block — they do not
    represent business logic, only imports/re-exports.
    """
    s = line.lstrip()
    if not s:
        return True
    if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
        return True
    if s.startswith("import ") or s.startswith("import{") or s.startswith('import"'):
        return True
    # `export { x } from '...'` / `export * from '...'` — re-export
    return s.startswith("export") and " from " in s


def _detect_block_end(lines: list[str], start: int, suffix: str) -> int:
    """Rough heuristic: we find the end of the function. max 100 lines.

    For JS/TS: if the starting line is an import/comment/blank one, we scroll
    forward to the first "meaningful" line (a declaration). Without this the
    heuristic caught the first `{...}` in `import { computed } from 'vue';`
    and returned only the imports instead of the function body.
    """
    max_scan = min(len(lines), start + 100)
    if suffix in (".py",):
        if start - 1 >= len(lines):
            return start
        base_indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        for i in range(start, max_scan):
            line = lines[i]
            if not line.strip():
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= base_indent and i > start:
                return i
        return max_scan

    if suffix in (".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".vue"):
        # Skip top-level imports / comments / blank lines at the beginning
        skip_to = start - 1
        while skip_to < max_scan and skip_to < len(lines) and _is_js_noise_line(lines[skip_to]):
            skip_to += 1
        scan_from = skip_to
    else:
        scan_from = start - 1

    depth = 0
    found_open = False
    for i in range(scan_from, max_scan):
        line = lines[i] if i < len(lines) else ""
        for ch in line:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return i + 1
    return max_scan
