"""C++ extractor (Stage 2.B.6, May 2026).

Covers: .cpp, .cc, .cxx, .hpp, .hh, .hxx, .h, .c

Tree-sitter-cpp node types (verified):
    translation_unit            — root
    preproc_include             — #include <X> | "X"
    namespace_definition        — `namespace X { ... }`
    class_specifier             — `class X : public Y { ... }`
    struct_specifier            — `struct X { ... }`
    function_definition         — definition (with a body)
    function_declarator         — `name(params)`. Has a field `declarator` for the name
    qualified_identifier        — `Foo::bar` (for method definitions outside a class)
    field_declaration_list      — class/struct body
    field_declaration           — declaration without a body (methods in a header)
    base_class_clause           — `: public Base, virtual Other`
    call_expression             — `f(args)` / `obj.m()` / `Class::m()`

C++ difficulties:
    - Method definitions can be inline in the class body OR out-of-class
      via qualified_identifier (`UserService::FindById`)
    - Pointer/reference returns wrap the declarator in a pointer_declarator
    - Templates — we do not process the body (template_declaration → unwrap)
    - Operator overloading — `operator==` as the method name

What is NOT covered:
    - Template type parameters / SFINAE
    - Friend declarations
    - Lambda captures / closures
    - Macro expansion (preprocessor)
    - Pragmas
    - Initializer lists in constructors

Stage: 2.B.6. Implemented (MVP).
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


class CppExtractor(SymbolExtractor):
    language = "cpp"
    extensions = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h", ".c")

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
            parser = get_parser("cpp")
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
            file=rel, start_line=1, end_line=None, language="cpp",
        ))
        ctx.seen_ids.add(file_sym_id)

        self._walk_tu(tree.root_node, ctx, result)

        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _walk_tu(self, root_node, ctx: _WalkContext, result: ExtractionResult) -> None:
        for child in root_node.named_children:
            self._dispatch(child, ctx, result)

    def _dispatch(self, node, ctx: _WalkContext, result: ExtractionResult) -> None:
        t = node.type
        if t == "preproc_include":
            self._handle_include(node, ctx, result)
        elif t == "namespace_definition":
            self._handle_namespace(node, ctx, result)
        elif t == "class_specifier":
            self._handle_class(node, ctx, result, kind="class")
        elif t == "struct_specifier" or t == "union_specifier":
            self._handle_class(node, ctx, result, kind="struct")
        elif t == "function_definition":
            self._handle_function_definition(node, ctx, result)
        elif t == "declaration":
            # At top level — this can be a function declaration or a variable
            # For the MVP — we skip it (functions without a body are not
            # registered as symbols)
            pass
        elif t == "template_declaration":
            # Unwrap — we process the inner node
            for inner in node.named_children:
                if inner.type in ("class_specifier", "struct_specifier",
                                  "function_definition", "declaration",
                                  "alias_declaration"):
                    self._dispatch(inner, ctx, result)
        elif t == "linkage_specification":
            # extern "C" { ... } — walk body
            body = next(
                (c for c in node.named_children if c.type == "declaration_list"),
                None,
            )
            if body is not None:
                for inner in body.named_children:
                    self._dispatch(inner, ctx, result)

    def _handle_include(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        from_id = _file_symbol_id(ctx.file)
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        raw = path_node.text.decode("utf-8", errors="replace")
        # Strip <> or ""
        path = raw.strip().strip("<>").strip('"')
        result.edges.append(EdgeInfo(
            from_id=from_id, to_id=None,
            kind="IMPORTS", confidence="unresolved",
            raw_target=path,
        ))

    def _handle_namespace(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        name_node = node.child_by_field_name("name")
        ns_name = name_node.text.decode("utf-8", errors="replace") if name_node else "(anonymous)"
        line = node.start_point[0] + 1

        sym_id = _make_id(ctx, ns_name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=ns_name, kind="namespace",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="cpp", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

        # Walk the body — everything inside belongs to the namespace
        body = node.child_by_field_name("body")
        if body is None:
            return

        prev_ns = ctx.namespace
        ctx.namespace = (
            f"{prev_ns}::{ns_name}" if prev_ns and ns_name != "(anonymous)" else ns_name
        )
        for inner in body.named_children:
            self._dispatch(inner, ctx, result)
        ctx.namespace = prev_ns

    def _handle_class(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        kind: str = "class",
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return  # forward declaration without a body — skip
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        sym_id = _make_id(ctx, name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind=kind,
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="cpp", is_exported=True,
            module=ctx.namespace or None,
        ))
        ctx.seen_ids.add(sym_id)

        # base_class_clause = inheritance
        base_clause = next(
            (c for c in node.named_children if c.type == "base_class_clause"),
            None,
        )
        if base_clause is not None:
            for base in base_clause.named_children:
                base_name = self._extract_type_name(base)
                if base_name:
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="EXTENDS", confidence="unresolved",
                        raw_target=base_name,
                    ))

        # Body — field_declaration_list
        body = node.child_by_field_name("body")
        if body is None:
            return
        self._walk_class_body(body, ctx, result, sym_id, name)

    @staticmethod
    def _extract_type_name(node) -> str | None:
        """base_class_clause child → type name, dropping access modifiers."""
        # node can be an identifier or a type_identifier or
        # template_type / qualified_identifier
        if node.type in ("type_identifier", "identifier"):
            return node.text.decode("utf-8", errors="replace")
        if node.type == "qualified_identifier":
            return node.text.decode("utf-8", errors="replace")
        if node.type == "template_type":
            inner = node.child_by_field_name("name")
            if inner is not None:
                return inner.text.decode("utf-8", errors="replace")
        # access_specifier (public/private/virtual) — skip
        return None

    def _walk_class_body(
        self, body, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        for member in body.named_children:
            t = member.type
            if t == "function_definition":
                # Inline method definition
                self._handle_method_definition(
                    member, ctx, result, class_id, class_name,
                )
            elif t == "field_declaration":
                self._handle_field_declaration(
                    member, ctx, result, class_id, class_name,
                )
            elif t == "declaration":
                # Method declaration without a body (e.g. `User* FindById(int);`)
                self._handle_method_declaration(
                    member, ctx, result, class_id, class_name,
                )
            elif t == "template_declaration":
                # Inner — recurse
                for inner in member.named_children:
                    if inner.type == "function_definition":
                        self._handle_method_definition(
                            inner, ctx, result, class_id, class_name,
                        )
                    elif inner.type == "field_declaration":
                        self._handle_field_declaration(
                            inner, ctx, result, class_id, class_name,
                        )
            elif t in ("class_specifier", "struct_specifier"):
                # Nested class
                inner_kind = "struct" if t == "struct_specifier" else "class"
                self._handle_class(member, ctx, result, kind=inner_kind)

    def _handle_function_definition(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Top-level function. Can be:
            - free function: `void foo() { ... }`
            - method out-of-class: `User* UserService::FindById(int) { ... }`
        """
        name_info = self._extract_declarator_name(node)
        if name_info is None:
            return
        name, qualifier = name_info  # qualifier = 'UserService' for a method, None for a free function
        line = node.start_point[0] + 1

        if qualifier:
            # method definition out-of-class
            qualified = f"{qualifier}::{name}"
            sym_id = _make_id(ctx, qualified, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind="method",
                file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
                language="cpp", is_exported=True,
                module=qualifier,
            ))
            ctx.seen_ids.add(sym_id)
            # If the class is already registered in the same file — DEFINED_IN edge
            class_id = _make_id_lookup(ctx, qualifier)
            if class_id:
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=class_id,
                    kind="DEFINED_IN", confidence="strong",
                ))
        else:
            # free function
            sym_id = _make_id(ctx, name, line)
            result.symbols.append(SymbolInfo(
                id=sym_id, name=name, kind="function",
                file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
                language="cpp", is_exported=True,
                module=ctx.namespace or None,
            ))
            ctx.seen_ids.add(sym_id)

        body = node.child_by_field_name("body")
        if body is not None:
            inner_ctx = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, current_class=qualifier,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner_ctx, result)

    def _handle_method_definition(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """Inline method definition inside the class body."""
        name_info = self._extract_declarator_name(node)
        if name_info is None:
            return
        name, _qualifier = name_info  # the qualifier here should be None for inline
        line = node.start_point[0] + 1

        qualified = f"{class_name}::{name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="cpp", is_exported=True,  # public/private — skip MVP
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        body = node.child_by_field_name("body")
        if body is not None:
            inner_ctx = _WalkContext(
                file=ctx.file, source=ctx.source, namespace=ctx.namespace,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner_ctx, result)

    def _handle_method_declaration(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """Declaration without a body — `User* FindById(int);` in a header file."""
        name_info = self._extract_declarator_name(node)
        if name_info is None:
            return
        name, _ = name_info
        line = node.start_point[0] + 1
        qualified = f"{class_name}::{name}"
        sym_id = _make_id(ctx, qualified, line)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="method",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="cpp", is_exported=True,
            module=class_name,
        ))
        ctx.seen_ids.add(sym_id)
        result.edges.append(EdgeInfo(
            from_id=sym_id, to_id=class_id,
            kind="DEFINED_IN", confidence="strong",
        ))

    def _handle_field_declaration(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """field_declaration: type + declarator(s).
        In C++ this can be:
            - simple field: `int x;`
            - member function declaration: `User* find();`  → method
            - static const: `static const int MAX = 100;`
        """
        # If we find a function_declarator — this is a method declaration
        for child in node.named_children:
            if child.type == "function_declarator":
                name = self._extract_function_name(child)
                if name:
                    line = node.start_point[0] + 1
                    qualified = f"{class_name}::{name}"
                    sym_id = _make_id(ctx, qualified, line)
                    result.symbols.append(SymbolInfo(
                        id=sym_id, name=name, kind="method",
                        file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
                        language="cpp", is_exported=True,
                        module=class_name,
                    ))
                    ctx.seen_ids.add(sym_id)
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=class_id,
                        kind="DEFINED_IN", confidence="strong",
                    ))
                return
            elif child.type == "pointer_declarator":
                inner = next(
                    (c for c in child.named_children if c.type == "function_declarator"),
                    None,
                )
                if inner is not None:
                    name = self._extract_function_name(inner)
                    if name:
                        line = node.start_point[0] + 1
                        qualified = f"{class_name}::{name}"
                        sym_id = _make_id(ctx, qualified, line)
                        result.symbols.append(SymbolInfo(
                            id=sym_id, name=name, kind="method",
                            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
                            language="cpp", is_exported=True,
                            module=class_name,
                        ))
                        ctx.seen_ids.add(sym_id)
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=class_id,
                            kind="DEFINED_IN", confidence="strong",
                        ))
                    return

        # Otherwise — a field
        # We look for the field_identifier as the name
        for child in node.named_children:
            if child.type == "field_identifier":
                name = child.text.decode("utf-8", errors="replace")
                line = node.start_point[0] + 1
                qualified = f"{class_name}::{name}"
                sym_id = _make_id(ctx, qualified, line)
                result.symbols.append(SymbolInfo(
                    id=sym_id, name=name, kind="field",
                    file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
                    language="cpp", is_exported=True,
                    module=class_name,
                ))
                ctx.seen_ids.add(sym_id)
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=class_id,
                    kind="DEFINED_IN", confidence="strong",
                ))
                return

    def _extract_declarator_name(
        self, fn_def_node,
    ) -> tuple[str, str | None] | None:
        """Find the name from function_declarator. Returns (name, qualifier).

        qualifier = the class name for `Foo::method`, None for a free function.
        """
        decl = fn_def_node.child_by_field_name("declarator")
        if decl is None:
            return None
        return self._unwrap_declarator(decl)

    def _unwrap_declarator(
        self, decl_node,
    ) -> tuple[str, str | None] | None:
        """Recursively unwrap pointer/reference declarators down to function_declarator."""
        t = decl_node.type
        if t == "function_declarator":
            return self._extract_function_name_with_qualifier(decl_node)
        if t in ("pointer_declarator", "reference_declarator"):
            inner = decl_node.child_by_field_name("declarator")
            if inner is not None:
                return self._unwrap_declarator(inner)
        return None

    @staticmethod
    def _extract_function_name(function_declarator) -> str | None:
        """function_declarator → name (without the qualifier)."""
        decl = function_declarator.child_by_field_name("declarator")
        if decl is None:
            return None
        if decl.type in ("identifier", "field_identifier", "destructor_name",
                         "operator_name"):
            return decl.text.decode("utf-8", errors="replace")
        if decl.type == "qualified_identifier":
            # Foo::bar — the name = bar (last part)
            text = decl.text.decode("utf-8", errors="replace")
            return text.rsplit("::", 1)[-1]
        return None

    @staticmethod
    def _extract_function_name_with_qualifier(
        function_declarator,
    ) -> tuple[str, str | None] | None:
        """function_declarator → (name, qualifier)."""
        decl = function_declarator.child_by_field_name("declarator")
        if decl is None:
            return None
        if decl.type in ("identifier", "field_identifier"):
            return decl.text.decode("utf-8", errors="replace"), None
        if decl.type in ("destructor_name", "operator_name"):
            return decl.text.decode("utf-8", errors="replace"), None
        if decl.type == "qualified_identifier":
            text = decl.text.decode("utf-8", errors="replace")
            if "::" in text:
                qualifier, name = text.rsplit("::", 1)
                return name, qualifier
            return text, None
        return None

    def _scan_calls_in(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        if not _is_owner_root and node.type in (
            "function_definition", "lambda_expression",
            "class_specifier", "struct_specifier",
        ):
            return

        if node.type == "call_expression":
            self._handle_call(node, ctx, result)
        elif node.type == "new_expression":
            self._handle_new(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        fn = node.child_by_field_name("function")
        if fn is None:
            return

        callee = self._extract_call_target(fn)
        if not callee:
            return

        result.edges.append(EdgeInfo(
            from_id=owner_id, to_id=None,
            kind="CALLS", confidence="unresolved",
            raw_target=callee,
        ))

    @staticmethod
    def _extract_call_target(fn_node) -> str | None:
        t = fn_node.type
        if t == "identifier":
            return fn_node.text.decode("utf-8", errors="replace")
        if t == "field_expression":
            # obj.method / obj->method
            field = fn_node.child_by_field_name("field")
            if field is not None:
                return field.text.decode("utf-8", errors="replace")
        if t == "qualified_identifier":
            # Foo::bar — return bar
            text = fn_node.text.decode("utf-8", errors="replace")
            return text.rsplit("::", 1)[-1]
        if t == "template_function":
            # foo<T>(...)
            name = fn_node.child_by_field_name("name")
            if name is not None:
                return name.text.decode("utf-8", errors="replace")
        return None

    def _handle_new(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`new T(...)` — CALLS to T."""
        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        type_node = node.child_by_field_name("type")
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


def _make_id_lookup(ctx: _WalkContext, name: str) -> str | None:
    base = f"{ctx.file}::{name}"
    return base if base in ctx.seen_ids else None
