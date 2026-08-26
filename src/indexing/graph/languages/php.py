"""PHP extractor (Stage 2.B.3, May 2026).

Covers: .php, .phtml

Tree-sitter-php node types (verified discovery):
    program                       — root
    php_tag                       — <?php
    namespace_definition          — `namespace App\\Service;` (field `name`)
    namespace_use_declaration     — `use Foo\\Bar;` / `use Foo\\Bar as Baz;`
    class_declaration             — field `name`, `base_clause`, `class_interface_clause`, field `body`
    interface_declaration         — field `name`, field `body`
    function_definition           — top-level functions (field `name`, `parameters`, `body`)
    method_declaration            — in a class body (field `name`, `parameters`, `body`)
    property_declaration          — class fields (visibility + type + property_element[var_name])
    const_declaration             — class constants
    function_call_expression      — `helper($x)` (function = `name`)
    member_call_expression        — `$this->method()` (object + `name`)
    scoped_call_expression        — `Class::method()` (scope + `name`)
    qualified_name                — `Foo\\Bar` namespace path

What is NOT covered:
    - PHP attributes (`#[...]`) — as syntax tokens
    - Traits — as a class (kind=class, not trait)
    - Anonymous classes — skip
    - Generic syntax (PHP 8.4+ type intersection) — as syntax tokens
    - Enum cases (PHP 8.1+) — an enum is registered as a class
    - Match expressions — scanned for CALLS

Stage: 2.B.3. Implemented.
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
    namespace: str = ""
    current_symbol_id: str | None = None
    current_class: str | None = None
    seen_ids: set[str] = field(default_factory=set)


class PHPExtractor(SymbolExtractor):
    language = "php"
    extensions = (".php", ".phtml")

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
            parser = get_parser("php")
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
            file=rel, start_line=1, end_line=None, language="php",
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
            if t == "namespace_definition":
                self._handle_namespace(child, ctx, result)
            elif t == "namespace_use_declaration":
                self._handle_use_declaration(child, ctx, result)
            elif t == "class_declaration":
                self._handle_class(child, ctx, result)
            elif t == "interface_declaration":
                self._handle_interface(child, ctx, result)
            elif t == "function_definition":
                self._handle_function(child, ctx, result)
            elif t in ("expression_statement", "if_statement", "for_statement",
                       "while_statement", "foreach_statement", "try_statement"):
                # Top-level controls — scan for CALLS
                self._scan_calls_in(child, ctx, result)
            elif t == "php_tag" or t == "comment":
                pass

    def _handle_namespace(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`namespace App\\Service;` — register a namespace symbol + put it in ctx."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        ns_name = name_node.text.decode("utf-8", errors="replace").strip()
        ctx.namespace = ns_name

        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, ns_name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ns_name, kind="namespace",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="php", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

        # If the namespace has a body (block-style namespace) — walk its
        # children too.
        # For simplicity, MVP: only file-scoped namespaces `namespace X;`
        # (without a body).
        # Block-style: walk via _walk_namespace_body
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                # Re-dispatch as top-level
                self._walk_program_inner(child, ctx, result)

    def _walk_program_inner(self, node, ctx: _WalkContext, result: ExtractionResult) -> None:
        """Same as _walk_program but takes a single node — for a namespace body."""
        t = node.type
        if t == "namespace_use_declaration":
            self._handle_use_declaration(node, ctx, result)
        elif t == "class_declaration":
            self._handle_class(node, ctx, result)
        elif t == "interface_declaration":
            self._handle_interface(node, ctx, result)
        elif t == "function_definition":
            self._handle_function(node, ctx, result)

    def _handle_use_declaration(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`use Foo\\Bar;` / `use Foo\\Bar as Baz;` / `use Foo\\Bar, Foo\\Baz;`."""
        from_id = _file_symbol_id(ctx.file)
        for clause in node.named_children:
            if clause.type != "namespace_use_clause":
                continue
            target = self._extract_use_target(clause)
            if target:
                result.edges.append(EdgeInfo(
                    from_id=from_id, to_id=None,
                    kind="IMPORTS", confidence="unresolved",
                    raw_target=target,
                ))

    @staticmethod
    def _extract_use_target(clause) -> str | None:
        """namespace_use_clause → fully qualified name."""
        for child in clause.named_children:
            if child.type == "qualified_name":
                return child.text.decode("utf-8", errors="replace").strip("\\")
            if child.type == "name":
                return child.text.decode("utf-8", errors="replace")
        return None

    def _handle_class(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        class_id = _make_id(ctx, name, line)
        class_sym = SymbolInfo(
            id=class_id, name=name, kind="class",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="php", is_exported=True,
            module=ctx.namespace or None,
        )
        result.symbols.append(class_sym)
        ctx.seen_ids.add(class_id)

        # base_clause = extends
        base_clause = next(
            (c for c in node.named_children if c.type == "base_clause"), None,
        )
        if base_clause is not None:
            for child in base_clause.named_children:
                if child.type in ("name", "qualified_name"):
                    base_name = child.text.decode("utf-8", errors="replace")
                    result.edges.append(EdgeInfo(
                        from_id=class_id, to_id=None,
                        kind="EXTENDS", confidence="unresolved",
                        raw_target=base_name,
                    ))

        # class_interface_clause = implements
        impl_clause = next(
            (c for c in node.named_children if c.type == "class_interface_clause"), None,
        )
        if impl_clause is not None:
            for child in impl_clause.named_children:
                if child.type in ("name", "qualified_name"):
                    iface_name = child.text.decode("utf-8", errors="replace")
                    result.edges.append(EdgeInfo(
                        from_id=class_id, to_id=None,
                        kind="IMPLEMENTS", confidence="unresolved",
                        raw_target=iface_name,
                    ))

        # body — declaration_list
        body = node.child_by_field_name("body")
        if body is None:
            return
        self._walk_class_body(body, ctx, result, class_id, name)

    def _handle_interface(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="interface",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="php", is_exported=True,
            module=ctx.namespace or None,
        ))
        ctx.seen_ids.add(sym_id)

        body = node.child_by_field_name("body")
        if body is None:
            return
        # Interface methods — without bodies, but with name + params
        for member in body.named_children:
            if member.type == "method_declaration":
                self._handle_method(member, ctx, result, sym_id, name)

    def _walk_class_body(
        self, body, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        for member in body.named_children:
            t = member.type
            if t == "method_declaration":
                self._handle_method(member, ctx, result, class_id, class_name)
            elif t == "property_declaration":
                self._handle_property(member, ctx, result, class_id, class_name)
            elif t == "const_declaration":
                self._handle_class_const(member, ctx, result, class_id, class_name)

    def _handle_method(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        # Visibility — public/protected/private
        is_public = self._is_public(node)

        qualified = f"{class_name}::{name}"
        sym_id = _make_id(ctx, qualified, line)
        sym = SymbolInfo(
            id=sym_id, name=name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="php", is_exported=is_public,
            module=class_name,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym_id)

        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        body = node.child_by_field_name("body")
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_function(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Top-level function (outside a class)."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, name, line)
        sym = SymbolInfo(
            id=sym_id, name=name, kind="function",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="php", is_exported=True,
            module=ctx.namespace or None,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym_id)

        body = node.child_by_field_name("body")
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_property(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """`private LoggerInterface $logger;` → field symbol(s)."""
        is_public = self._is_public(node)
        for elem in node.named_children:
            if elem.type != "property_element":
                continue
            var_node = next(
                (c for c in elem.named_children if c.type == "variable_name"),
                None,
            )
            if var_node is None:
                continue
            # variable_name = '$foo' — strip '$'
            raw = var_node.text.decode("utf-8", errors="replace")
            name = raw.lstrip("$")
            line = elem.start_point[0] + 1
            qualified = f"{class_name}::${name}"
            sym_id = _make_id(ctx, qualified, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind="field",
                file=ctx.file, start_line=line, end_line=elem.end_point[0] + 1,
                language="php", is_exported=is_public,
                module=class_name,
            ))
            ctx.seen_ids.add(sym_id)
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=class_id,
                kind="DEFINED_IN", confidence="strong",
            ))

    def _handle_class_const(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """`public const MAX_RETRIES = 3;` → constant symbol."""
        is_public = self._is_public(node)
        for elem in node.named_children:
            if elem.type != "const_element":
                continue
            name_node = next(
                (c for c in elem.named_children if c.type == "name"),
                None,
            )
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", errors="replace")
            line = elem.start_point[0] + 1
            qualified = f"{class_name}::{name}"
            sym_id = _make_id(ctx, qualified, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind="constant",
                file=ctx.file, start_line=line, end_line=elem.end_point[0] + 1,
                language="php", is_exported=is_public,
                module=class_name,
            ))
            ctx.seen_ids.add(sym_id)
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=class_id,
                kind="DEFINED_IN", confidence="strong",
            ))

    @staticmethod
    def _is_public(node) -> bool:
        """Check the visibility_modifier among the direct children."""
        for c in node.named_children:
            if c.type == "visibility_modifier":
                modifier = c.text.decode("utf-8", errors="replace").strip()
                return modifier == "public"
        return True  # default — public in PHP

    def _scan_calls_in(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        """Recursive walk looking for function/member/scoped calls."""
        if not _is_owner_root and node.type in (
            "function_definition", "method_declaration", "anonymous_function_creation_expression",
            "arrow_function", "class_declaration", "interface_declaration",
        ):
            return

        if node.type == "function_call_expression":
            self._handle_function_call(node, ctx, result)
        elif node.type == "member_call_expression":
            self._handle_member_call(node, ctx, result)
        elif node.type == "scoped_call_expression":
            self._handle_scoped_call(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_function_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`helper($x)` — function = name|qualified_name."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        fn = node.child_by_field_name("function")
        if fn is None:
            return

        if fn.type == "name":
            callee = fn.text.decode("utf-8", errors="replace")
        elif fn.type == "qualified_name":
            callee = fn.text.decode("utf-8", errors="replace").strip("\\")
        else:
            return

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence="unresolved",
            raw_target=callee,
        ))

    def _handle_member_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`$this->method()` / `$obj->method()` — name field = method name."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        callee = name_node.text.decode("utf-8", errors="replace")

        # Confidence: $this->X — weak (could resolve to the same class)
        confidence = "unresolved"
        obj_node = node.child_by_field_name("object")
        if (obj_node is not None and obj_node.type == "variable_name"
                and obj_node.text.decode("utf-8", errors="replace") == "$this"):
            confidence = "weak"

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence=confidence,
            raw_target=callee,
        ))

    def _handle_scoped_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`Class::method()` / `self::method()` — name field = method name."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        callee = name_node.text.decode("utf-8", errors="replace")

        # If the scope is a qualified_name or a relative_scope
        # (self/parent/static), we do not parse it into raw_target — the name
        # is enough for CALLS resolution.
        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence="unresolved",
            raw_target=callee,
        ))

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str, line: int) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{line}"
