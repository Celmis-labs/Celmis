"""Java extractor (Stage 2.B.4, May 2026).

Covers: .java

Tree-sitter-java node types (verified discovery):
    program                       — root
    package_declaration           — `package com.example;` → `scoped_identifier`
    import_declaration            — `import java.util.List;` (with optional `static`)
    class_declaration             — modifiers, identifier, superclass, super_interfaces, class_body
    interface_declaration         — modifiers, identifier, interface_body
    enum_declaration              — modifiers, identifier, enum_body
    record_declaration            — Java 14+ records
    field_declaration             — modifiers, type, variable_declarator(s)
    constructor_declaration       — modifiers, identifier, formal_parameters, constructor_body
    method_declaration            — modifiers, return type, identifier, formal_parameters, block
    method_invocation             — optional object, name (identifier), arguments
    object_creation_expression    — `new Foo()` as a call

Field in method_invocation:
    `name` (field) → identifier method name. Guaranteed — we use it for CALLS.

What is NOT covered:
    - Generic type parameters (as syntax tokens)
    - Nested classes — registered as top-level (YAGNI for now)
    - Annotations (@Override) — as modifiers, not resolved
    - Lambda expressions / method references — skipped for CALLS
    - Static initializers — as a block, not as a symbol
    - Switch expressions — scanned for CALLS

Stage: 2.B.4. Implemented.
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
    current_class: str | None = None
    seen_ids: set[str] = field(default_factory=set)


class JavaExtractor(SymbolExtractor):
    language = "java"
    extensions = (".java",)

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
            parser = get_parser("java")
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
            file=rel, start_line=1, end_line=None, language="java",
        ))
        ctx.seen_ids.add(file_sym_id)

        self._walk_program(tree.root_node, ctx, result)

        # DEFINED_IN file_module
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
            if t == "package_declaration":
                self._handle_package(child, ctx, result)
            elif t == "import_declaration":
                self._handle_import(child, ctx, result)
            elif t == "class_declaration":
                self._handle_class(child, ctx, result, kind="class")
            elif t == "interface_declaration":
                self._handle_class(child, ctx, result, kind="interface")
            elif t == "enum_declaration":
                self._handle_class(child, ctx, result, kind="enum")
            elif t == "record_declaration":
                # Java records — as a class with implicit data fields
                self._handle_class(child, ctx, result, kind="class")

    def _handle_package(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        # named_children[0] = scoped_identifier (with the name)
        ident = next(
            (c for c in node.named_children if c.type == "scoped_identifier"),
            None,
        ) or next(
            (c for c in node.named_children if c.type == "identifier"),
            None,
        )
        if ident is None:
            return
        ctx.package_name = ident.text.decode("utf-8", errors="replace")

        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, ctx.package_name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ctx.package_name, kind="package",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="java", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

    def _handle_import(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        from_id = _file_symbol_id(ctx.file)
        ident = next(
            (c for c in node.named_children if c.type == "scoped_identifier"),
            None,
        )
        if ident is not None:
            target = ident.text.decode("utf-8", errors="replace")
        else:
            # Fallback — shouldn't happen for valid Java
            target = node.text.decode("utf-8", errors="replace").strip()
            target = target.removeprefix("import").removesuffix(";").strip()
            target = target.removeprefix("static").strip()

        # asterisks (`java.util.*`) — leave as is
        result.edges.append(EdgeInfo(
            from_id=from_id, to_id=None,
            kind="IMPORTS", confidence="unresolved",
            raw_target=target,
        ))

    def _handle_class(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        kind: str = "class",
    ) -> None:
        name_node = next(
            (c for c in node.named_children if c.type == "identifier"),
            None,
        )
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        is_public = self._is_public(node)
        sym_id = _make_id(ctx, name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind=kind,
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="java", is_exported=is_public,
            module=ctx.package_name or None,
        ))
        ctx.seen_ids.add(sym_id)

        # superclass = extends
        superclass = next(
            (c for c in node.named_children if c.type == "superclass"), None,
        )
        if superclass is not None:
            type_node = next(
                (c for c in superclass.named_children
                 if c.type in ("type_identifier", "scoped_type_identifier", "generic_type")),
                None,
            )
            if type_node is not None:
                base_name = type_node.text.decode("utf-8", errors="replace")
                # Strip generic params for cleaner target
                if "<" in base_name:
                    base_name = base_name.split("<", 1)[0]
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind="EXTENDS", confidence="unresolved",
                    raw_target=base_name,
                ))

        # super_interfaces = implements / extends (for interfaces)
        super_ifaces = next(
            (c for c in node.named_children if c.type == "super_interfaces"), None,
        )
        # For an interface: 'extends_interfaces' instead of 'super_interfaces'
        if super_ifaces is None:
            super_ifaces = next(
                (c for c in node.named_children if c.type == "extends_interfaces"), None,
            )
        if super_ifaces is not None:
            for type_list in super_ifaces.named_children:
                if type_list.type != "type_list":
                    continue
                for t in type_list.named_children:
                    if t.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                        iface_name = t.text.decode("utf-8", errors="replace")
                        if "<" in iface_name:
                            iface_name = iface_name.split("<", 1)[0]
                        edge_kind = (
                            "EXTENDS" if kind == "interface" else "IMPLEMENTS"
                        )
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=None,
                            kind=edge_kind, confidence="unresolved",
                            raw_target=iface_name,
                        ))

        # body
        body = self._get_body(node)
        if body is None:
            return
        self._walk_class_body(body, ctx, result, sym_id, name)

    @staticmethod
    def _get_body(class_node):
        for c in class_node.named_children:
            if c.type in ("class_body", "interface_body", "enum_body", "record_body"):
                return c
        return None

    def _walk_class_body(
        self, body, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        for member in body.named_children:
            t = member.type
            if t == "method_declaration":
                self._handle_method(member, ctx, result, class_id, class_name)
            elif t == "constructor_declaration":
                self._handle_constructor(member, ctx, result, class_id, class_name)
            elif t == "field_declaration":
                self._handle_field(member, ctx, result, class_id, class_name)
            elif t == "class_declaration":
                # Nested class — we register it as top-level with a qualified name
                self._handle_class(member, ctx, result, kind="class")
            elif t == "enum_constant":
                # Enum constant — we register it as a constant
                self._handle_enum_constant(member, ctx, result, class_id, class_name)

    def _handle_method(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        name_node = next(
            (c for c in node.named_children if c.type == "identifier"), None,
        )
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        is_public = self._is_public(node)

        qualified = f"{class_name}.{name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="java", is_exported=is_public,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        # Body — block (interface methods may have no body)
        body = next(
            (c for c in node.named_children if c.type == "block"), None,
        )
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, package_name=ctx.package_name,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_constructor(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        # In a constructor the identifier = class name. We register it as a
        # method with name='<init>'.
        line = node.start_point[0] + 1
        is_public = self._is_public(node)
        ctor_name = "<init>"
        qualified = f"{class_name}.{ctor_name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ctor_name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="java", is_exported=is_public,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        body = next(
            (c for c in node.named_children if c.type == "constructor_body"), None,
        )
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, package_name=ctx.package_name,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_field(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """field_declaration may have multiple variable_declarator: `int x, y;`"""
        is_public = self._is_public(node)
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name_node = next(
                (c for c in decl.named_children if c.type == "identifier"), None,
            )
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", errors="replace")
            line = decl.start_point[0] + 1
            # Final + uppercase → constant (convention)
            is_final = self._has_modifier(node, "final")
            kind = "constant" if (is_final and name.isupper()) else "field"

            qualified = f"{class_name}.{name}"
            sym_id = _make_id(ctx, qualified, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind=kind,
                file=ctx.file, start_line=line, end_line=decl.end_point[0] + 1,
                language="java", is_exported=is_public,
                module=class_name,
            ))
            ctx.seen_ids.add(sym_id)
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=class_id,
                kind="DEFINED_IN", confidence="strong",
            ))

    def _handle_enum_constant(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        name_node = next(
            (c for c in node.named_children if c.type == "identifier"), None,
        )
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        qualified = f"{class_name}.{name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="constant",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="java", is_exported=True,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

    @staticmethod
    def _is_public(node) -> bool:
        modifiers = next(
            (c for c in node.named_children if c.type == "modifiers"), None,
        )
        if modifiers is None:
            return False  # package-private by default
        text = modifiers.text.decode("utf-8", errors="replace")
        return "public" in text

    @staticmethod
    def _has_modifier(node, modifier: str) -> bool:
        modifiers = next(
            (c for c in node.named_children if c.type == "modifiers"), None,
        )
        if modifiers is None:
            return False
        return modifier in modifiers.text.decode("utf-8", errors="replace")

    def _scan_calls_in(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        if not _is_owner_root and node.type in (
            "method_declaration", "constructor_declaration",
            "class_declaration", "interface_declaration", "enum_declaration",
            "lambda_expression",
        ):
            return

        if node.type == "method_invocation":
            self._handle_method_invocation(node, ctx, result)
        elif node.type == "object_creation_expression":
            self._handle_object_creation(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_method_invocation(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        callee = name_node.text.decode("utf-8", errors="replace")

        confidence = "unresolved"
        obj = node.child_by_field_name("object")
        if obj is not None and obj.type == "this":
            confidence = "weak"  # this.method() — likely same class

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence=confidence,
            raw_target=callee,
        ))

    def _handle_object_creation(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`new Foo()` → CALLS edge to Foo (constructor)."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        type_node = next(
            (c for c in node.named_children
             if c.type in ("type_identifier", "scoped_type_identifier", "generic_type")),
            None,
        )
        if type_node is None:
            return
        callee = type_node.text.decode("utf-8", errors="replace")
        if "<" in callee:
            callee = callee.split("<", 1)[0]
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
