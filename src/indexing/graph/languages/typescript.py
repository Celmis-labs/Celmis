"""TypeScript / JavaScript universal extractor (Phase 5a).

Covers: .ts, .tsx, .js, .jsx, .cjs, .mjs

Grammar choice (per file extension):
    .ts      → tree-sitter-typescript ("typescript" parser)
    .tsx     → tree-sitter-tsx ("tsx" parser, TS + JSX)
    .js/.jsx → tree-sitter-javascript ("javascript" parser)
    .cjs     → javascript
    .mjs     → javascript

Research findings (April 2026):
    - TS inherits from JS — node types are identical (function_declaration, class_declaration,
      import_statement, call_expression, member_expression, lexical_declaration,
      method_definition, arrow_function, export_statement)
    - class_declaration.name: 'identifier' in JS, 'type_identifier' in TS — `child_by_field_name('name').text`
      works for both
    - arrow_function without a name — we take the name from the context (variable_declarator.name or property)
    - export_statement has several forms: declaration, value (for default), export_clause + source
    - source in import_statement is a string node (with quotes); .text.decode().strip("'\"")
    - Parser API ≥0.22: `Parser(lang)` (not set_language), `parser.parse(bytes, included_ranges=...)`
      — included_ranges gives the correct row/col mapping for Vue injection

What is NOT covered at this stage (deferred / weak):
    - destructuring in lexical_declaration: `const {a, b} = ...` — skipped (tagged weak)
    - dynamic imports `import('...')` — as call_expression, separately
    - decorators @ — skipped (TS-specific, not critical for the MVP)
    - re-exports `export { x } from 'm'` — registered as IMPORTS + EXPORTS

Phase: 5a. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Range
from tree_sitter_language_pack import get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


# ─── language detection ────────────────────────────────────────────


_GRAMMAR_BY_EXT = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cts": "typescript",
    ".mts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
}


def _grammar_for(file_path: Path | str) -> str:
    """Returns the identifier for get_parser() based on the file extension."""
    suffix = Path(file_path).suffix.lower()
    return _GRAMMAR_BY_EXT.get(suffix, "javascript")


# ─── walk context ──────────────────────────────────────────────────


@dataclass
class WalkContext:
    """Walk state — needed for symbol id generation and CALLS edge attribution."""

    file: str  # relative path (for symbol id + edge from_id)
    source: bytes  # for node.text decoding fallback
    language: str  # "typescript" | "tsx" | "javascript"
    current_symbol_id: str | None = None  # null at top level, set inside function/method/class
    seen_ids: set[str] = field(default_factory=set)  # for disambiguation


# ─── extractor ─────────────────────────────────────────────────────


class TypeScriptExtractor(SymbolExtractor):
    """Universal TS/JS extractor — shared code for all 6 extensions."""

    language = "typescript"  # generalized label; the real language is decided per file
    extensions = (".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".cts", ".mts")

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
        included_ranges: list[Range] | None = None,
    ) -> None:
        """
        Args:
            ctx: RepoContext (for future resolver enrichment); optional at this stage.
            repo_root: for computing relative file paths.
            included_ranges: for language injection (Vue → script content range).
        """
        self.ctx = ctx
        self.repo_root = Path(repo_root).resolve() if repo_root else (
            ctx.repo_root if ctx else None
        )
        self.included_ranges = included_ranges

    # ─── public API ────────────────────────────────────────────────

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        grammar = _grammar_for(file_path)
        try:
            parser = get_parser(grammar)
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"parser_init_failed grammar={grammar}: {e}"])

        if self.included_ranges:
            tree = parser.parse(source, included_ranges=self.included_ranges)
        else:
            tree = parser.parse(source)

        rel = self._relpath(file_path)
        ctx = WalkContext(file=rel, source=source, language=grammar)
        result = ExtractionResult()

        if tree.root_node.has_error:
            result.parse_errors.append(f"parse_errors_present root.has_error=True file={rel}")

        # We create a synthetic file-module symbol — it is needed so that
        # IMPORTS edges with from_id=ctx.file have a valid target in the graph.
        # It is also the collection point for DEFINED_IN edges from all the
        # symbols of the file.
        file_sym_id = self._file_symbol_id(rel)
        file_sym = SymbolInfo(
            id=file_sym_id,
            name=Path(rel).name,
            kind="file_module",
            file=rel,
            start_line=1,
            end_line=None,
            language=grammar,
        )
        result.symbols.append(file_sym)
        ctx.seen_ids.add(file_sym_id)

        self._walk_program(tree.root_node, ctx, result)

        # DEFINED_IN edges from all symbols to the file-module
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id,
                to_id=file_sym_id,
                kind="DEFINED_IN",
                confidence="strong",
            ))

        return result

    @staticmethod
    def _file_symbol_id(rel: str) -> str:
        """ID of the synthetic file-module symbol."""
        return f"{rel}::__module__"

    # ─── walk top-level ────────────────────────────────────────────

    def _walk_program(self, root_node, ctx: WalkContext, result: ExtractionResult) -> None:
        """Walk over the top-level statements in the program node."""
        for child in root_node.named_children:
            self._dispatch(child, ctx, result, exported=False)

    def _dispatch(self, node, ctx: WalkContext, result: ExtractionResult, exported: bool) -> None:
        """Branching on the node type."""
        t = node.type
        if t == "import_statement":
            self._handle_import(node, ctx, result)
        elif t == "export_statement":
            self._handle_export(node, ctx, result)
        elif t in ("function_declaration", "generator_function_declaration"):
            self._handle_function(node, ctx, result, exported=exported)
        elif t == "class_declaration":
            self._handle_class(node, ctx, result, exported=exported)
        elif t in ("lexical_declaration", "variable_declaration"):
            self._handle_var_declaration(node, ctx, result, exported=exported)
        elif t == "expression_statement":
            # top-level expressions — sometimes IIFEs with functions; we walk
            # recursively looking for calls
            self._scan_calls_in(node, ctx, result)

    # ─── handlers ──────────────────────────────────────────────────

    def _handle_import(self, node, ctx: WalkContext, result: ExtractionResult) -> None:
        """import { a, b } from 'm'  →  IMPORTS edges (unresolved until the resolver)."""
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        source_path = self._strip_quotes(source_node.text.decode("utf-8", errors="replace"))

        # find the import_clause
        clause = next(
            (c for c in node.named_children if c.type == "import_clause"),
            None,
        )

        # Which name and alias the edge carries, per import form:
        # - default: import x from 'm' → name='default', alias='x'
        # - named: import {a as b} from 'm' → name='a', alias='b'
        # - namespace: import * as ns from 'm' → name='*', alias='ns'
        # - side-effect: import 'm' (clause is None) → one IMPORTS with no specific name
        from_id = self._file_symbol_id(ctx.file)  # we bind the import to the file-module symbol

        if clause is None:
            # side-effect import
            result.edges.append(EdgeInfo(
                from_id=from_id,
                to_id=None,
                kind="IMPORTS",
                confidence="unresolved",
                raw_target=source_path,
            ))
            return

        for child in clause.named_children:
            if child.type == "identifier":
                # default import
                result.edges.append(EdgeInfo(
                    from_id=from_id,
                    to_id=None,
                    kind="IMPORTS",
                    confidence="unresolved",
                    raw_target=f"{source_path}::default",
                ))
            elif child.type == "namespace_import":
                result.edges.append(EdgeInfo(
                    from_id=from_id,
                    to_id=None,
                    kind="IMPORTS",
                    confidence="unresolved",
                    raw_target=f"{source_path}::*",
                ))
            elif child.type == "named_imports":
                for spec in child.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name_node = spec.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = name_node.text.decode("utf-8")
                    result.edges.append(EdgeInfo(
                        from_id=from_id,
                        to_id=None,
                        kind="IMPORTS",
                        confidence="unresolved",
                        raw_target=f"{source_path}::{name}",
                    ))

    def _handle_export(self, node, ctx: WalkContext, result: ExtractionResult) -> None:
        """export {decl} | export default {value} | export {a, b} | export * from 'm'."""
        # Re-export: `export { a } from 'm'` → IMPORTS edge
        source_node = node.child_by_field_name("source")
        if source_node is not None:
            source_path = self._strip_quotes(source_node.text.decode("utf-8", errors="replace"))
            file_sym_id = self._file_symbol_id(ctx.file)
            for child in node.named_children:
                if child.type == "export_clause":
                    for spec in child.named_children:
                        if spec.type == "export_specifier":
                            name_node = spec.child_by_field_name("name")
                            if name_node:
                                result.edges.append(EdgeInfo(
                                    from_id=file_sym_id,
                                    to_id=None,
                                    kind="IMPORTS",
                                    confidence="unresolved",
                                    raw_target=f"{source_path}::{name_node.text.decode('utf-8')}",
                                ))
            return

        # Direct declaration export: function / class / const
        decl = node.child_by_field_name("declaration")
        if decl is not None:
            self._dispatch(decl, ctx, result, exported=True)
            return

        # export default <value>
        value = node.child_by_field_name("value")
        if value is not None:
            # If value is an identifier (`export default ordersController`),
            # the name is taken from the identifier (for DoD discoverability).
            # Otherwise — a synthetic "default".
            default_name = (
                value.text.decode("utf-8") if value.type == "identifier" else "default"
            )

            # The ID is always `{file}::default` — so that the resolver easily
            # finds default exports regardless of the actual name.
            default_id = f"{ctx.file}::default"
            sym = SymbolInfo(
                id=default_id,
                name=default_name,
                kind="export_default",
                file=ctx.file,
                start_line=value.start_point[0] + 1,
                end_line=value.end_point[0] + 1,
                language=ctx.language,
                is_exported=True,
            )
            result.symbols.append(sym)
            ctx.seen_ids.add(sym.id)
            # recursively collect the calls inside value
            with_owner = WalkContext(
                file=ctx.file, source=ctx.source, language=ctx.language,
                current_symbol_id=sym.id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(value, with_owner, result)
            return

        # export { a, b } — with no declaration and no value
        for child in node.named_children:
            if child.type == "export_clause":
                for spec in child.named_children:
                    if spec.type == "export_specifier":
                        name_node = spec.child_by_field_name("name")
                        if name_node:
                            # This is only a marker that a locally declared
                            # symbol is exported. No re-extraction (the symbol
                            # was already declared somewhere above).
                            # We could add a separate pass that marks the
                            # corresponding symbols is_exported=True. Skipped
                            # for now — YAGNI.
                            pass

    def _handle_function(
        self, node, ctx: WalkContext, result: ExtractionResult, exported: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node else "<anonymous>"
        line = node.start_point[0] + 1

        sym = SymbolInfo(
            id=self._make_id(ctx, name, line),
            name=name,
            kind="function",
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language=ctx.language,
            is_exported=exported,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym.id)

        # Calls inside the function — we attribute them to it
        body = node.child_by_field_name("body")
        if body is not None:
            inner = WalkContext(
                file=ctx.file, source=ctx.source, language=ctx.language,
                current_symbol_id=sym.id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_class(
        self, node, ctx: WalkContext, result: ExtractionResult, exported: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node else "<anonymous>"
        line = node.start_point[0] + 1

        class_sym = SymbolInfo(
            id=self._make_id(ctx, name, line),
            name=name,
            kind="class",
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language=ctx.language,
            is_exported=exported,
        )
        result.symbols.append(class_sym)
        ctx.seen_ids.add(class_sym.id)

        # methods inside class_body
        body = node.child_by_field_name("body")
        if body is None:
            return

        for member in body.named_children:
            if member.type == "method_definition":
                m_name_node = member.child_by_field_name("name")
                if m_name_node is None:
                    continue
                m_name = m_name_node.text.decode("utf-8")
                m_line = member.start_point[0] + 1
                qualified = f"{name}.{m_name}"
                m_sym = SymbolInfo(
                    id=self._make_id(ctx, qualified, m_line),
                    name=m_name,
                    kind="method",
                    file=ctx.file,
                    start_line=m_line,
                    end_line=member.end_point[0] + 1,
                    language=ctx.language,
                    module=name,  # the class is the symbol's module
                )
                result.symbols.append(m_sym)
                ctx.seen_ids.add(m_sym.id)
                # Edge method → DEFINED_IN → class (for BFS chainability)
                result.edges.append(EdgeInfo(
                    from_id=m_sym.id,
                    to_id=class_sym.id,
                    kind="DEFINED_IN",
                    confidence="strong",
                ))
                # calls inside the method
                m_body = member.child_by_field_name("body")
                if m_body is not None:
                    inner = WalkContext(
                        file=ctx.file, source=ctx.source, language=ctx.language,
                        current_symbol_id=m_sym.id, seen_ids=ctx.seen_ids,
                    )
                    self._scan_calls_in(m_body, inner, result)

    def _handle_var_declaration(
        self, node, ctx: WalkContext, result: ExtractionResult, exported: bool
    ) -> None:
        """const x = 5 / const fn = () => {} / const obj = {...}.

        If value is an arrow_function / function_expression / class_expression,
        we register it as a symbol under the variable's name.
        """
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                # destructuring — skipped in the MVP
                continue
            name = name_node.text.decode("utf-8")
            value_node = declarator.child_by_field_name("value")
            line = declarator.start_point[0] + 1

            kind = "variable"
            if value_node is not None:
                if value_node.type in ("arrow_function", "function_expression"):
                    kind = "function"
                elif value_node.type == "class_expression":
                    kind = "class"
                elif value_node.type == "call_expression":
                    # const x = computed(...) — classification-wise also a function-like export
                    kind = "variable"

            sym = SymbolInfo(
                id=self._make_id(ctx, name, line),
                name=name,
                kind=kind,
                file=ctx.file,
                start_line=line,
                end_line=declarator.end_point[0] + 1,
                language=ctx.language,
                is_exported=exported,
            )
            result.symbols.append(sym)
            ctx.seen_ids.add(sym.id)

            # calls inside value
            if value_node is not None:
                inner = WalkContext(
                    file=ctx.file, source=ctx.source, language=ctx.language,
                    current_symbol_id=sym.id, seen_ids=ctx.seen_ids,
                )
                self._scan_calls_in(value_node, inner, result)

    # ─── calls scan ────────────────────────────────────────────────

    def _scan_calls_in(
        self,
        node,
        ctx: WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        """Recursive walk that looks for call_expression, registers CALLS edges.

        The owner (ctx.current_symbol_id) is taken from the context. If it is
        None — the call is attributed to the file (top-level call).

        `_is_owner_root=True` means this is the root of the scan for the current
        owner — we **do** need to go into the body of such a function/arrow. We
        do not go deeper into nested functions (flat namespace MVP).
        """
        if not _is_owner_root and node.type in (
            "function_declaration", "generator_function_declaration",
            "function_expression", "arrow_function", "class_declaration",
            "class_expression", "method_definition",
        ) and node.child_by_field_name("body") is not None:
            # nested function — skipped (its calls are not attributed to the outer owner)
            return

        if node.type == "call_expression":
            self._handle_call(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_call(self, node, ctx: WalkContext, result: ExtractionResult) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return

        callee_name: str | None = None
        owner_id = ctx.current_symbol_id or self._file_symbol_id(ctx.file)

        if fn.type == "identifier":
            callee_name = fn.text.decode("utf-8")
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            if prop is not None:
                callee_name = prop.text.decode("utf-8")
        elif fn.type == "import":
            # dynamic import('...')
            args = node.child_by_field_name("arguments")
            if args is not None and args.named_child_count > 0:
                first = args.named_children[0]
                if first.type == "string":
                    target = self._strip_quotes(first.text.decode("utf-8"))
                    result.edges.append(EdgeInfo(
                        from_id=owner_id,
                        to_id=None,
                        kind="IMPORTS",
                        confidence="unresolved",
                        raw_target=target,
                    ))
            return

        if not callee_name:
            return

        result.edges.append(EdgeInfo(
            from_id=owner_id,
            to_id=None,
            kind="CALLS",
            confidence="unresolved",
            raw_target=callee_name,
        ))

    # ─── helpers ───────────────────────────────────────────────────

    def _relpath(self, file_path: Path) -> str:
        """The file relative to repo_root, as a string.

        Without repo_root — we return the path as-is (no resolve), so that
        tests and callers can pass relative paths without a cwd dependency.
        """
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)

    def _make_id(self, ctx: WalkContext, name: str, line: int) -> str:
        """Symbol id — disambiguated by line if the name is not unique in the file."""
        base = f"{ctx.file}::{name}"
        if base not in ctx.seen_ids:
            return base
        return f"{base}@{line}"

    @staticmethod
    def _strip_quotes(s: str) -> str:
        s = s.strip()
        if len(s) >= 2 and s[0] in ("'", '"', "`") and s[-1] == s[0]:
            return s[1:-1]
        return s
