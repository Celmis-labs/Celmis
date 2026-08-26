"""C# extractor (Stage 2.B.5, May 2026).

Covers: .cs, .csx

NOTE: the language pack identifier for C# is `csharp` (no underscore;
`c_sharp` does not exist in tree-sitter-language-pack 1.6.x).

Tree-sitter-csharp node types (verified):
    compilation_unit                       — root
    file_scoped_namespace_declaration      — `namespace X;` (C# 10+, new syntax)
    namespace_declaration                  — `namespace X { ... }` (block style)
    using_directive                        — `using System;`
    class_declaration                      — modifier(s), identifier, base_list, declaration_list
    interface_declaration                  — structurally the same
    enum_declaration                       — modifier, identifier, enum_member_declaration_list
    record_declaration                     — C# 9+ records
    field_declaration                      — modifier(s) + variable_declaration
    constructor_declaration                — modifier, identifier (= class name), parameter_list, block
    method_declaration                     — modifier(s), return type, identifier, parameter_list, block
    invocation_expression                  — `f()` or `obj.M()` (children = function + argument_list)
    member_access_expression               — `obj.Member` — last identifier = name
    object_creation_expression             — `new T(...)`
    qualified_name                         — `A.B.C`

base_list in C#: all potential superclasses + interfaces in one list.
Heuristic: a name starting with 'I' + uppercase → interface (convention).
Not perfect, but works for most C# codebases.

What is NOT covered:
    - Generic constraints (`where T : ISomething`) — as syntax tokens
    - Properties (get; set;) — registered as a field
    - Events / delegates — skipped
    - Extension methods — as ordinary methods
    - Partial classes — the parts separately, without merging
    - LINQ query expressions — scanned for CALLS
    - Patterns (`is X y`) — skip

Stage: 2.B.5. Implemented.
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


class CSharpExtractor(SymbolExtractor):
    language = "csharp"
    extensions = (".cs", ".csx")

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
            parser = get_parser("csharp")
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
            file=rel, start_line=1, end_line=None, language="csharp",
        ))
        ctx.seen_ids.add(file_sym_id)

        self._walk_program(tree.root_node, ctx, result)

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
            self._dispatch(child, ctx, result)

    def _dispatch(self, node, ctx: _WalkContext, result: ExtractionResult) -> None:
        t = node.type
        if t == "file_scoped_namespace_declaration":
            self._handle_file_scoped_namespace(node, ctx, result)
        elif t == "namespace_declaration":
            self._handle_namespace_block(node, ctx, result)
        elif t == "using_directive":
            self._handle_using(node, ctx, result)
        elif t == "class_declaration":
            self._handle_type(node, ctx, result, kind="class")
        elif t == "interface_declaration":
            self._handle_type(node, ctx, result, kind="interface")
        elif t == "enum_declaration":
            self._handle_type(node, ctx, result, kind="enum")
        elif t == "record_declaration":
            self._handle_type(node, ctx, result, kind="class")
        elif t == "struct_declaration":
            self._handle_type(node, ctx, result, kind="struct")

    def _handle_file_scoped_namespace(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`namespace X.Y;` (no block body) — everything at file level lives in
        this namespace.

        In tree-sitter-csharp the body is turned into sibling children of the
        compilation_unit.
        """
        ns_node = next(
            (c for c in node.named_children
             if c.type in ("qualified_name", "identifier")),
            None,
        )
        if ns_node is None:
            return
        ns_name = ns_node.text.decode("utf-8", errors="replace")
        ctx.namespace = ns_name

        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, ns_name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ns_name, kind="namespace",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="csharp", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

    def _handle_namespace_block(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`namespace X.Y { ... }` — block style. Register namespace + walk body."""
        ns_node = next(
            (c for c in node.named_children
             if c.type in ("qualified_name", "identifier")),
            None,
        )
        body = next(
            (c for c in node.named_children if c.type == "declaration_list"),
            None,
        )
        if ns_node is None:
            return
        ns_name = ns_node.text.decode("utf-8", errors="replace")
        prev_ns = ctx.namespace
        ctx.namespace = ns_name

        line = node.start_point[0] + 1
        sym_id = _make_id(ctx, ns_name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ns_name, kind="namespace",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="csharp", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

        if body is not None:
            for child in body.named_children:
                self._dispatch(child, ctx, result)

        ctx.namespace = prev_ns

    def _handle_using(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        from_id = _file_symbol_id(ctx.file)
        target_node = next(
            (c for c in node.named_children
             if c.type in ("qualified_name", "identifier")),
            None,
        )
        if target_node is None:
            return
        target = target_node.text.decode("utf-8", errors="replace")
        result.edges.append(EdgeInfo(
            from_id=from_id, to_id=None,
            kind="IMPORTS", confidence="unresolved",
            raw_target=target,
        ))

    def _handle_type(
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
            language="csharp", is_exported=is_public,
            module=ctx.namespace or None,
        ))
        ctx.seen_ids.add(sym_id)

        # base_list — heuristic: 'I*' uppercase → interface
        base_list = next(
            (c for c in node.named_children if c.type == "base_list"), None,
        )
        if base_list is not None:
            for base_child in base_list.named_children:
                base_name = self._extract_type_name(base_child)
                if not base_name:
                    continue
                # Strip generic params
                bare = base_name.split("<", 1)[0]
                if _looks_like_interface(bare):
                    edge_kind = "IMPLEMENTS"
                elif kind == "interface":
                    edge_kind = "EXTENDS"  # an interface inherits another interface
                else:
                    edge_kind = "EXTENDS"
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind=edge_kind, confidence="unresolved",
                    raw_target=bare,
                ))

        # body
        body = next(
            (c for c in node.named_children
             if c.type in ("declaration_list", "enum_member_declaration_list")),
            None,
        )
        if body is None:
            return

        for member in body.named_children:
            t = member.type
            if t == "method_declaration":
                self._handle_method(member, ctx, result, sym_id, name)
            elif t == "constructor_declaration":
                self._handle_constructor(member, ctx, result, sym_id, name)
            elif t == "field_declaration":
                self._handle_field(member, ctx, result, sym_id, name)
            elif t == "property_declaration":
                self._handle_property(member, ctx, result, sym_id, name)
            elif t == "enum_member_declaration":
                self._handle_enum_member(member, ctx, result, sym_id, name)
            elif t in ("class_declaration", "interface_declaration",
                       "enum_declaration", "record_declaration"):
                # Nested type — registered at top level (YAGNI for now)
                inner_kind = (
                    "interface" if t == "interface_declaration"
                    else "enum" if t == "enum_declaration"
                    else "class"
                )
                self._handle_type(member, ctx, result, kind=inner_kind)

    @staticmethod
    def _extract_type_name(node) -> str | None:
        """base_list child → type name string."""
        if node.type in ("identifier", "qualified_name", "predefined_type"):
            return node.text.decode("utf-8", errors="replace")
        if node.type == "generic_name":
            id_node = next(
                (c for c in node.named_children if c.type == "identifier"), None,
            )
            if id_node is not None:
                return id_node.text.decode("utf-8", errors="replace")
        return None

    def _handle_method(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        # Use field 'name' — the return type can be an identifier too (for
        # example "Task"), so taking the first identifier positionally gives a
        # wrong result.
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # Fallback — after the returns field
            returns = node.child_by_field_name("returns")
            if returns is not None:
                # Look for the first identifier AFTER returns
                seen_returns = False
                for c in node.named_children:
                    if c is returns:
                        seen_returns = True
                        continue
                    if seen_returns and c.type == "identifier":
                        name_node = c
                        break
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
            language="csharp", is_exported=is_public,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        body = next(
            (c for c in node.named_children
             if c.type in ("block", "arrow_expression_clause")), None,
        )
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_constructor(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        line = node.start_point[0] + 1
        is_public = self._is_public(node)
        ctor_name = ".ctor"
        qualified = f"{class_name}.{ctor_name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ctor_name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="csharp", is_exported=is_public,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        body = next(
            (c for c in node.named_children if c.type == "block"), None,
        )
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_field(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        is_public = self._is_public(node)
        is_const = self._has_modifier(node, "const")
        # variable_declaration → contains type + variable_declarator(s)
        var_decl = next(
            (c for c in node.named_children if c.type == "variable_declaration"),
            None,
        )
        if var_decl is None:
            return
        for elem in var_decl.named_children:
            if elem.type != "variable_declarator":
                continue
            name_node = next(
                (c for c in elem.named_children if c.type == "identifier"), None,
            )
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", errors="replace")
            line = elem.start_point[0] + 1
            kind = "constant" if is_const else "field"
            qualified = f"{class_name}.{name}"
            sym_id = _make_id(ctx, qualified, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind=kind,
                file=ctx.file, start_line=line, end_line=elem.end_point[0] + 1,
                language="csharp", is_exported=is_public,
                module=class_name,
            ))
            ctx.seen_ids.add(sym_id)
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=class_id,
                kind="DEFINED_IN", confidence="strong",
            ))

    def _handle_property(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """C# property — registered as a field. Use field name (the return type
        is an identifier too).
        """
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # Fallback — second identifier (the first = return type)
            ids = [c for c in node.named_children if c.type == "identifier"]
            if len(ids) >= 2:
                name_node = ids[1]
            elif len(ids) == 1:
                name_node = ids[0]
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1
        is_public = self._is_public(node)
        qualified = f"{class_name}.{name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="field",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="csharp", is_exported=is_public,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

    def _handle_enum_member(
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
            language="csharp", is_exported=True,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

    @staticmethod
    def _is_public(node) -> bool:
        for c in node.named_children:
            if (c.type == "modifier"
                    and c.text.decode("utf-8", errors="replace").strip() == "public"):
                return True
        return False

    @staticmethod
    def _has_modifier(node, modifier: str) -> bool:
        for c in node.named_children:
            if (c.type == "modifier"
                    and c.text.decode("utf-8", errors="replace").strip() == modifier):
                return True
        return False

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
            "record_declaration", "struct_declaration",
            "lambda_expression", "anonymous_method_expression",
        ):
            return

        if node.type == "invocation_expression":
            self._handle_invocation(node, ctx, result)
        elif node.type == "object_creation_expression":
            self._handle_object_creation(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_invocation(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        # First non-arglist named child = function expression
        fn = next(
            (c for c in node.named_children if c.type != "argument_list"),
            None,
        )
        if fn is None:
            return

        callee = self._extract_callee_name(fn)
        if not callee:
            return

        confidence = "unresolved"
        if fn.type == "member_access_expression":
            # this.X — same class
            children = fn.named_children
            if children and children[0].type == "this_expression":
                confidence = "weak"

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence=confidence,
            raw_target=callee,
        ))

    @staticmethod
    def _extract_callee_name(fn_node) -> str | None:
        """Extract the callee name from the various kinds of function expressions."""
        t = fn_node.type
        if t == "identifier":
            return fn_node.text.decode("utf-8", errors="replace")
        if t == "member_access_expression":
            # Last named child = method name (identifier)
            children = fn_node.named_children
            if not children:
                return None
            last = children[-1]
            if last.type == "identifier":
                return last.text.decode("utf-8", errors="replace")
        if t == "qualified_name":
            # A.B.C → C (last segment)
            text = fn_node.text.decode("utf-8", errors="replace")
            return text.rsplit(".", 1)[-1]
        if t == "generic_name":
            id_node = next(
                (c for c in fn_node.named_children if c.type == "identifier"), None,
            )
            if id_node is not None:
                return id_node.text.decode("utf-8", errors="replace")
        return None

    def _handle_object_creation(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`new Foo()` → CALLS edge to Foo."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        # First non-arglist child = type
        type_node = next(
            (c for c in node.named_children if c.type != "argument_list"),
            None,
        )
        if type_node is None:
            return
        callee = self._extract_callee_name(type_node)
        if not callee:
            return
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


def _looks_like_interface(name: str) -> bool:
    """C# convention: 'IFoo' / 'IDisposable' / 'IUserService' — interface names.

    Not perfect (IUserName may be a class), but works for the majority of C#
    codebases. Edge case: classes starting with 'I' — rarely, for example
    'IndexOutOfRange' — then name[1] is lowercase, so the heuristic does not
    fire.
    """
    return len(name) >= 2 and name[0] == "I" and name[1].isupper()
