"""Go extractor (Stage 2.B.2, May 2026).

Covers: .go

Tree-sitter-go node types (verified discovery):
    source_file              — root
    package_clause           — `package main`
    import_declaration       — single or grouped (import (...))
    import_spec              — one import with an optional alias
    import_spec_list         — group of import_specs
    type_declaration         — wrapping
    type_spec                — `Name struct{...}` | `Name interface{...}`
    struct_type, interface_type
    function_declaration     — top-level func
    method_declaration       — `func (recv *T) Name(...)` — receiver as a
                              parameter before the name
    call_expression          — `f(args)` or `obj.Method(args)`
    selector_expression      — `pkg.Func` / `obj.field`

Receiver extraction:
    method.named_children[0] = parameter_list (receiver), then field_identifier (name)
    receiver type = pointer_type → type_identifier OR type_identifier (value receiver)

What is NOT covered at this stage:
    - Generic functions / type parameters (Go 1.18+ syntax) — as syntax tokens
    - Interface satisfaction (structural typing) — needs type inference
    - Embedded types in a struct — as a field reference
    - init() and main() — as ordinary functions
    - Build tags (//go:build) — out of scope
    - cgo (import "C") — as an ordinary import

Stage: 2.B.2. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


@dataclass
class _WalkContext:
    file: str
    source: bytes
    package_name: str = ""
    current_symbol_id: str | None = None
    seen_ids: set[str] = field(default_factory=set)


class GoExtractor(SymbolExtractor):
    language = "go"
    extensions = (".go",)

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        try:
            parser = get_parser("go")
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"parser_init_failed: {e}"])

        tree = parser.parse(source)
        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel, source=source)
        result = ExtractionResult()

        if tree.root_node.has_error:
            result.parse_errors.append(f"parse_errors_present file={rel}")

        # File-module
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).stem, kind="file_module",
            file=rel, start_line=1, end_line=None, language="go",
        ))
        ctx.seen_ids.add(file_sym_id)

        self._walk_program(tree.root_node, ctx, result)

        # DEFINED_IN file_module for all non-file symbols
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _walk_program(self, root_node, ctx: _WalkContext, result: ExtractionResult) -> None:
        for child in root_node.named_children:
            t = child.type
            if t == "package_clause":
                self._handle_package(child, ctx)
            elif t == "import_declaration":
                self._handle_import_declaration(child, ctx, result)
            elif t == "function_declaration":
                self._handle_function(child, ctx, result)
            elif t == "method_declaration":
                self._handle_method(child, ctx, result)
            elif t == "type_declaration":
                self._handle_type_declaration(child, ctx, result)
            else:
                # Top-level var/const declarations + scan for top-level CALLS
                self._scan_calls_in(child, ctx, result)

    def _handle_package(self, node, ctx: _WalkContext) -> None:
        ident = next(
            (c for c in node.named_children if c.type == "package_identifier"),
            None,
        )
        if ident is not None:
            ctx.package_name = ident.text.decode("utf-8", errors="replace")

    def _handle_import_declaration(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`import "X"` or `import (...)` group."""
        from_id = _file_symbol_id(ctx.file)
        for child in node.named_children:
            if child.type == "import_spec":
                self._handle_import_spec(child, from_id, result)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        self._handle_import_spec(spec, from_id, result)

    def _handle_import_spec(self, spec, from_id: str, result: ExtractionResult) -> None:
        """import_spec: optional package_identifier (alias) + interpreted_string_literal."""
        path_node = next(
            (c for c in spec.named_children
             if c.type in ("interpreted_string_literal", "raw_string_literal")),
            None,
        )
        if path_node is None:
            return
        path = _strip_quotes(path_node.text.decode("utf-8", errors="replace"))
        # Alias if present — not needed for majority resolution, only for
        # diagnostics
        alias_node = next(
            (c for c in spec.named_children if c.type == "package_identifier"),
            None,
        )
        target = path
        if alias_node is not None:
            alias = alias_node.text.decode("utf-8", errors="replace")
            target = f"{path}::{alias}"
        result.edges.append(EdgeInfo(
            from_id=from_id, to_id=None,
            kind="IMPORTS", confidence="unresolved",
            raw_target=target,
        ))

    def _handle_function(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """func Name(...) {...}"""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # Fallback — first identifier child
            name_node = next(
                (c for c in node.named_children if c.type == "identifier"),
                None,
            )
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        sym_id = _make_id(ctx, name, line)
        # Go convention: PascalCase = exported, lowercase = unexported
        is_exported = name[:1].isupper() if name else False

        sym = SymbolInfo(
            id=sym_id, name=name, kind="function",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="go", is_exported=is_exported,
            module=ctx.package_name,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym_id)

        body = node.child_by_field_name("body")
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, package_name=ctx.package_name,
                current_symbol_id=sym_id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_method(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """func (recv *T) Name(...) — the receiver is the first parameter_list."""
        # Receiver: the first parameter_list serving as receiver (positional)
        children = node.named_children
        if len(children) < 2:
            return

        receiver = children[0]
        if receiver.type != "parameter_list":
            return

        receiver_type_name = self._extract_receiver_type(receiver)
        if not receiver_type_name:
            return

        # Method name — field_identifier
        name_node = node.child_by_field_name("name")
        if name_node is None:
            name_node = next(
                (c for c in children[1:] if c.type == "field_identifier"),
                None,
            )
        if name_node is None:
            return

        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        qualified = f"{receiver_type_name}.{name}"
        is_exported = name[:1].isupper() if name else False

        sym_id = _make_id(ctx, qualified, line)
        sym = SymbolInfo(
            id=sym_id, name=name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="go", is_exported=is_exported,
            module=receiver_type_name,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym_id)

        # method.DEFINED_IN.receiver_type — if the type is in the same file
        receiver_type_id = _make_id_lookup(ctx, receiver_type_name)
        if receiver_type_id and receiver_type_id in ctx.seen_ids:
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=receiver_type_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        body = node.child_by_field_name("body")
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, package_name=ctx.package_name,
                current_symbol_id=sym_id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    @staticmethod
    def _extract_receiver_type(receiver_list) -> str | None:
        """parameter_list (receiver) → type name. `(s *Server)` → 'Server'."""
        for param_decl in receiver_list.named_children:
            if param_decl.type != "parameter_declaration":
                continue
            # Type — the second named child after the identifier (s)
            for child in param_decl.named_children:
                if child.type == "pointer_type":
                    inner = next(
                        (c for c in child.named_children if c.type == "type_identifier"),
                        None,
                    )
                    if inner is not None:
                        return inner.text.decode("utf-8", errors="replace")
                elif child.type == "type_identifier":
                    return child.text.decode("utf-8", errors="replace")
                elif child.type == "generic_type":
                    # generic_type → type_identifier
                    inner = next(
                        (c for c in child.named_children if c.type == "type_identifier"),
                        None,
                    )
                    if inner is not None:
                        return inner.text.decode("utf-8", errors="replace")
        return None

    def _handle_type_declaration(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """type X struct {...} / type Y interface {...} / type Z = OtherType."""
        for spec in node.named_children:
            if spec.type != "type_spec":
                continue
            self._handle_type_spec(spec, ctx, result)

    def _handle_type_spec(
        self, spec, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            name_node = next(
                (c for c in spec.named_children if c.type == "type_identifier"),
                None,
            )
        if name_node is None:
            return

        name = name_node.text.decode("utf-8", errors="replace")
        line = spec.start_point[0] + 1

        # Type — the 'type' field (interface_type | struct_type | type alias
        # and so on).
        # Use field_by_name because positional iteration races with the name
        # node (both are type_identifier, but the first is the name and the
        # second is the alias target).
        type_node = spec.child_by_field_name("type")

        kind = "struct"
        if type_node is not None:
            if type_node.type == "interface_type":
                kind = "interface"
            elif type_node.type == "struct_type":
                kind = "struct"
            else:
                # type alias (e.g., type ID int) — registered as a struct
                # for simplicity (Go has no separate "type alias symbol kind")
                kind = "struct"

        is_exported = name[:1].isupper() if name else False
        sym_id = _make_id(ctx, name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind=kind,
            file=ctx.file, start_line=line, end_line=spec.end_point[0] + 1,
            language="go", is_exported=is_exported,
            module=ctx.package_name,
        ))
        ctx.seen_ids.add(sym_id)

    def _scan_calls_in(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        """Recursive call walk. Skip nested func literals (anonymous functions)."""
        if not _is_owner_root and node.type in (
            "function_declaration", "method_declaration", "func_literal",
        ):
            return

        if node.type == "call_expression":
            self._handle_call(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """call_expression: function (identifier | selector_expression)."""
        fn = node.child_by_field_name("function")
        if fn is None:
            return

        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        callee_name: str | None = None

        if fn.type == "identifier":
            callee_name = fn.text.decode("utf-8", errors="replace")
        elif fn.type == "selector_expression":
            # operand.field — the field_identifier is the method/func
            field_node = fn.child_by_field_name("field")
            if field_node is not None:
                callee_name = field_node.text.decode("utf-8", errors="replace")
        elif fn.type == "type_identifier":
            # Type conversion — `string(x)` — skip as a call edge (it is a cast)
            return

        if not callee_name:
            return

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence="unresolved",
            raw_target=callee_name,
        ))

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


# ─── helpers ────────────────────────────────────────────────────────


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str, line: int) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{line}"


def _make_id_lookup(ctx: _WalkContext, name: str) -> str | None:
    """Look up existing symbol id (without line disambiguation)."""
    base = f"{ctx.file}::{name}"
    if base in ctx.seen_ids:
        return base
    return None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in ('"', "'", "`") and s[-1] == s[0]:
        return s[1:-1]
    return s
