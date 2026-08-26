"""Vue SFC extractor (Phase 5b).

Approach (without a Node runtime):
    1. Parse the .vue file via get_parser("vue") — tree-sitter-vue (inherits HTML)
    2. Find the <script> or <script setup> element
    3. Read the start_tag attributes → determine lang ("ts"/"tsx"/None)
    4. Extract the byte range of raw_text (the script content)
    5. Re-parse via TypeScriptExtractor with `included_ranges` —
       this gives correct row/col mapping automatically (confirmed by research)
    6. Optional: template directive_attribute → TEMPLATE_REF edges (confidence=weak)

Covered:
    - <script>, <script setup>
    - <script lang="ts">, <script setup lang="ts">
    - <script lang="tsx"> (rare)
    - <script> + <script setup> together (Vue 3 hybrid) — both scripts
    - Composable macros (defineProps, defineEmits, withDefaults and so on) —
      they are parsed as call_expression in the TS extractor automatically

NOT covered:
    - Type-only generics in macros (e.g. defineProps<T>) — no type resolution
      is done; the macro is registered as CALLS
    - Auto-imports (Nuxt) — without a RepoContext.auto_imports list
    - A template of the form <script lang="x"> — fallback to JS

Phase: 5b. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Parser, Point, Range
from tree_sitter_language_pack import get_language, get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import (
    ExtractionResult,
    SymbolExtractor,
)
from src.indexing.graph.languages.typescript import TypeScriptExtractor

logger = logging.getLogger(__name__)


# ─── attribute helpers ─────────────────────────────────────────────


def _start_tag_attrs(start_tag) -> dict[str, str | bool]:
    """Extract the attrs from a <script ...> start_tag.

    Format:
        <script setup lang="ts"> →  {"setup": True, "lang": "ts"}
        <script>                 →  {}
    """
    out: dict[str, str | bool] = {}
    for a in start_tag.named_children:
        if a.type != "attribute":
            continue

        name_node = next((c for c in a.named_children if c.type == "attribute_name"), None)
        if name_node is None:
            continue
        name = name_node.text.decode("utf-8")

        # Look for value node
        value_node = next(
            (c for c in a.named_children if c.type in ("quoted_attribute_value", "attribute_value")),
            None,
        )
        if value_node is None:
            # boolean attr (`setup` with no value)
            out[name] = True
            continue

        if value_node.type == "quoted_attribute_value":
            inner = next((c for c in value_node.named_children if c.type == "attribute_value"), None)
            out[name] = inner.text.decode("utf-8") if inner else ""
        else:
            out[name] = value_node.text.decode("utf-8")

    return out


@dataclass
class _ScriptBlock:
    """A single <script> block from an SFC."""

    start_tag_node: object
    raw_text_node: object | None  # None if <script />
    is_setup: bool
    lang: str  # "javascript" | "typescript" | "tsx"


def _detect_script_lang(attrs: dict[str, str | bool]) -> str:
    """Map Vue lang attr → grammar identifier."""
    lang = attrs.get("lang")
    if isinstance(lang, str):
        if lang in ("ts", "typescript"):
            return "typescript"
        if lang == "tsx":
            return "tsx"
    return "javascript"


def _find_script_blocks(root_node) -> list[_ScriptBlock]:
    """Find all <script> elements in an SFC (there can be 2 — <script> + <script setup>)."""
    blocks: list[_ScriptBlock] = []
    for child in root_node.named_children:
        if child.type != "script_element":
            continue

        start_tag = next((c for c in child.named_children if c.type == "start_tag"), None)
        if start_tag is None:
            continue

        raw_text = next((c for c in child.named_children if c.type == "raw_text"), None)
        attrs = _start_tag_attrs(start_tag)

        blocks.append(_ScriptBlock(
            start_tag_node=start_tag,
            raw_text_node=raw_text,
            is_setup=bool(attrs.get("setup", False)),
            lang=_detect_script_lang(attrs),
        ))
    return blocks


# ─── extractor ─────────────────────────────────────────────────────


class VueExtractor(SymbolExtractor):
    """Vue SFC extractor — uses included ranges (included_ranges)
    to re-parse the script content via TypeScriptExtractor."""

    language = "vue"
    extensions = (".vue",)

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = Path(repo_root).resolve() if repo_root else (
            ctx.repo_root if ctx else None
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        try:
            vue_parser = get_parser("vue")
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"vue_parser_init_failed: {e}"])

        vue_tree = vue_parser.parse(source)
        result = ExtractionResult()

        if vue_tree.root_node.has_error:
            result.parse_errors.append("vue_parse_errors_present")

        blocks = _find_script_blocks(vue_tree.root_node)
        if not blocks:
            # SFC without a script — empty symbol set, OK
            return result

        # We delegate to TypeScriptExtractor for every script block.
        # We use included_ranges so that row/col stay relative to the .vue file.
        ts_extractor = TypeScriptExtractor(ctx=self.ctx, repo_root=self.repo_root)

        from src.indexing.graph.languages.typescript import WalkContext

        for block in blocks:
            if block.raw_text_node is None:
                continue
            raw = block.raw_text_node
            grammar_id = block.lang

            # We create a fresh Parser instance (not the shared get_parser)
            # to avoid race conditions through the `included_ranges` property.
            try:
                lang = get_language(grammar_id)
                ts_parser = Parser(lang)
            except Exception as e:  # noqa: BLE001
                result.parse_errors.append(
                    f"script_parser_failed lang={grammar_id}: {e}"
                )
                continue

            # `included_ranges` is a property on the Parser instance (post 0.22 API)
            ts_parser.included_ranges = [Range(
                start_byte=raw.start_byte,
                end_byte=raw.end_byte,
                start_point=Point(*raw.start_point),
                end_point=Point(*raw.end_point),
            )]
            ts_tree = ts_parser.parse(source)

            # Reused TS walker — row/col positions are automatically relative
            # to the .vue file
            rel = ts_extractor._relpath(file_path)
            walk_ctx = WalkContext(
                file=rel,
                source=source,
                language=grammar_id,
            )

            # We save the state before walking this block — so that we can
            # mark only the new symbols as exported (for <script setup>).
            n_syms_before = len(result.symbols)
            ts_extractor._walk_program(ts_tree.root_node, walk_ctx, result)

            # Vue SFC specifics: <script setup> top-level declarations are
            # automatically "exposed" by the component.
            if block.is_setup:
                for sym in result.symbols[n_syms_before:]:
                    sym.is_exported = True

        return result
