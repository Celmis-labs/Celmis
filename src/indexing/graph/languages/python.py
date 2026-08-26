"""Python extractor (Stage 2.B.1, May 2026).

Covers: .py, .pyi

Tree-sitter-python node types (verified discovery):
    module                  — root
    import_statement        — `import os` / `import os.path as osp`
    import_from_statement   — `from typing import List, Optional`
                              `from .module import helper as h`
                              field `module_name` → relative_import|dotted_name
    function_definition     — sync and async (async via the first child token)
    class_definition        — `class X(Base, metaclass=Meta): ...`
                              field `superclasses` → argument_list
                              field `body` → block
    decorated_definition    — wraps function_definition / class_definition
    call                    — `func(args)` or `obj.method(args)`
    attribute               — `obj.attr` (for CALLS resolution)
    assignment              — `CONST = 42` (top-level → variable/constant symbol)

Python does NOT have an explicit export — all top-level symbols are considered
exposed. The underscore prefix `_private` is a convention, but not enforced.

What is NOT covered at this stage (deferred):
    - type aliases via `TypeAlias` / `type Foo = ...` (PEP 695) — skipped for now
    - `__all__` module-level export filter — skip
    - Generic class parameters — as syntax tokens, not resolved
    - Properties / descriptors — as method (with the @property decorator)
    - Metaclasses — only as an attribute keyword, not resolved
    - `*args, **kwargs` — as parameters textually, not expanded

Stage: 2.B.1. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


# ─── walk context ──────────────────────────────────────────────────


@dataclass
class _WalkContext:
    """Walk state — owner_id for CALLS attribution."""

    file: str
    source: bytes
    current_symbol_id: str | None = None
    current_class: str | None = None  # for qualified method names "ClassName.method"
    seen_ids: set[str] = field(default_factory=set)


# ─── extractor ─────────────────────────────────────────────────────


class PythonExtractor(SymbolExtractor):
    """Python extractor — symbols + edges from .py / .pyi."""

    language = "python"
    extensions = (".py", ".pyi")

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

    # ─── public API ────────────────────────────────────────────────

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        try:
            parser = get_parser("python")
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"parser_init_failed: {e}"])

        tree = parser.parse(source)
        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel, source=source)
        result = ExtractionResult()

        if tree.root_node.has_error:
            result.parse_errors.append(f"parse_errors_present file={rel}")

        # File-module symbol
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id,
            name=Path(rel).stem,  # 'foo.py' → 'foo'
            kind="file_module",
            file=rel,
            start_line=1,
            end_line=None,
            language="python",
        ))
        ctx.seen_ids.add(file_sym_id)

        self._walk_program(tree.root_node, ctx, result)

        # DEFINED_IN edges from all symbols → file_module
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            # If the symbol already has a DEFINED_IN to a class — skip (added in _handle_class)
            # We add the file-level one anyway — it eases traversal for cross-mod queries.
            result.edges.append(EdgeInfo(
                from_id=sym.id,
                to_id=file_sym_id,
                kind="DEFINED_IN",
                confidence="strong",
            ))

        return result

    # ─── walk top-level ────────────────────────────────────────────

    def _walk_program(self, root_node, ctx: _WalkContext, result: ExtractionResult) -> None:
        for child in root_node.named_children:
            self._dispatch_top(child, ctx, result)

    def _dispatch_top(self, node, ctx: _WalkContext, result: ExtractionResult) -> None:
        """Top-level dispatch. Python: all top-level — implicitly 'exported'."""
        t = node.type
        if t == "import_statement":
            self._handle_import(node, ctx, result)
        elif t == "import_from_statement":
            self._handle_import_from(node, ctx, result)
        elif t == "function_definition":
            self._handle_function(node, ctx, result, exported=True)
        elif t == "class_definition":
            self._handle_class(node, ctx, result, exported=True)
        elif t == "decorated_definition":
            inner = self._unwrap_decorated(node)
            if inner is not None:
                self._dispatch_top(inner, ctx, result)
        elif t == "assignment":
            self._handle_top_assignment(node, ctx, result)
        elif t == "expression_statement":
            # Top-level call / IIFE-style — scan for CALLS edges
            self._scan_calls_in(node, ctx, result)
        else:
            # Fallback for control-flow (if/while/for/try/with/match):
            # scan calls, but _scan_calls_in skips nested function/class itself.
            # This covers the idiom `if __name__ == "__main__": main()`.
            self._scan_calls_in(node, ctx, result)

    # ─── handlers ──────────────────────────────────────────────────

    def _handle_import(self, node, ctx: _WalkContext, result: ExtractionResult) -> None:
        """`import os` / `import os.path as osp` / `import a, b`"""
        from_id = _file_symbol_id(ctx.file)

        # named_children — list of dotted_name or aliased_import
        for child in node.named_children:
            if child.type == "dotted_name":
                module_path = self._dotted_name_text(child)
                result.edges.append(EdgeInfo(
                    from_id=from_id,
                    to_id=None,
                    kind="IMPORTS",
                    confidence="unresolved",
                    raw_target=module_path,
                ))
            elif child.type == "aliased_import":
                # aliased_import: dotted_name + identifier (alias)
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    # Fallback — the first dotted_name child
                    name_node = next(
                        (c for c in child.named_children if c.type == "dotted_name"),
                        None,
                    )
                if name_node is not None:
                    module_path = self._dotted_name_text(name_node)
                    result.edges.append(EdgeInfo(
                        from_id=from_id,
                        to_id=None,
                        kind="IMPORTS",
                        confidence="unresolved",
                        raw_target=module_path,
                    ))

    def _handle_import_from(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`from typing import List` / `from .module import helper as h, other` /
        `from a.b import *`
        """
        from_id = _file_symbol_id(ctx.file)

        # module_name field — dotted_name (absolute) or relative_import
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return

        module_path = self._module_text(module_node)

        # Import names — everything after module_name (named_children without module_node)
        # In tree-sitter-python: import_from_statement has:
        #   - field 'module_name' (dotted_name|relative_import)
        #   - field 'name' (multiple — one per import target)
        # OR for the wildcard `from x import *` — wildcard_import
        names = []
        # Use field 'name' — this returns multiple matches
        for n in node.children_by_field_name("name"):
            names.append(n)

        # Wildcard `from x import *`
        wildcard = next(
            (c for c in node.named_children if c.type == "wildcard_import"),
            None,
        )

        if wildcard is not None:
            result.edges.append(EdgeInfo(
                from_id=from_id,
                to_id=None,
                kind="IMPORTS",
                confidence="unresolved",
                raw_target=f"{module_path}::*",
            ))
            return

        if not names:
            # Fallback — nodes after module
            module_idx = -1
            for i, c in enumerate(node.named_children):
                if c is module_node:
                    module_idx = i
                    break
            if module_idx >= 0:
                names = node.named_children[module_idx + 1 :]

        for n in names:
            target_name = ""
            if n.type == "dotted_name":
                target_name = self._dotted_name_text(n)
            elif n.type == "aliased_import":
                name_field = n.child_by_field_name("name")
                if name_field is not None:
                    target_name = self._dotted_name_text(name_field)
            elif n.type == "identifier":
                target_name = n.text.decode("utf-8", errors="replace")
            if target_name:
                result.edges.append(EdgeInfo(
                    from_id=from_id,
                    to_id=None,
                    kind="IMPORTS",
                    confidence="unresolved",
                    raw_target=f"{module_path}::{target_name}",
                ))

    def _handle_function(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        exported: bool,
        is_method: bool = False,
        class_name: str | None = None,
    ) -> None:
        """function_definition — computes name, registers symbol, scans the body."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        if is_method and class_name:
            qualified_name = f"{class_name}.{name}"
            sym_id = _make_id(ctx, qualified_name, line)
            kind = "method"
        else:
            sym_id = _make_id(ctx, name, line)
            kind = "function"

        sym = SymbolInfo(
            id=sym_id,
            name=name,
            kind=kind,
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language="python",
            is_exported=exported and not name.startswith("_"),
            module=class_name,
        )
        result.symbols.append(sym)
        ctx.seen_ids.add(sym_id)

        # Scan body for CALLS edges
        body = node.child_by_field_name("body")
        if body is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source,
                current_symbol_id=sym_id, current_class=class_name,
                seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(body, inner, result)

    def _handle_class(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        exported: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        class_id = _make_id(ctx, name, line)
        class_sym = SymbolInfo(
            id=class_id,
            name=name,
            kind="class",
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language="python",
            is_exported=exported and not name.startswith("_"),
        )
        result.symbols.append(class_sym)
        ctx.seen_ids.add(class_id)

        # Superclasses — argument_list, each identifier → EXTENDS edge
        # `class C(Base, metaclass=Meta):` — Base treated as base, metaclass — keyword_argument (skip)
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for arg in superclasses.named_children:
                if arg.type == "identifier":
                    base_name = arg.text.decode("utf-8", errors="replace")
                    result.edges.append(EdgeInfo(
                        from_id=class_id,
                        to_id=None,
                        kind="EXTENDS",
                        confidence="unresolved",
                        raw_target=base_name,
                    ))
                elif arg.type == "attribute":
                    # `class C(module.Base):` — attribute access as base
                    base_name = arg.text.decode("utf-8", errors="replace")
                    result.edges.append(EdgeInfo(
                        from_id=class_id,
                        to_id=None,
                        kind="EXTENDS",
                        confidence="unresolved",
                        raw_target=base_name,
                    ))
                # keyword_argument (metaclass=...) skip

        # Methods inside the body
        body = node.child_by_field_name("body")
        if body is None:
            return

        for member in body.named_children:
            if member.type == "function_definition":
                self._handle_function(
                    member, ctx, result,
                    exported=exported, is_method=True, class_name=name,
                )
                # method.DEFINED_IN.class
                method_id = _make_id_for_method(ctx, name, member)
                if method_id:
                    result.edges.append(EdgeInfo(
                        from_id=method_id,
                        to_id=class_id,
                        kind="DEFINED_IN",
                        confidence="strong",
                    ))
            elif member.type == "decorated_definition":
                inner = self._unwrap_decorated(member)
                if inner is not None and inner.type == "function_definition":
                    self._handle_function(
                        inner, ctx, result,
                        exported=exported, is_method=True, class_name=name,
                    )
                    method_id = _make_id_for_method(ctx, name, inner)
                    if method_id:
                        result.edges.append(EdgeInfo(
                            from_id=method_id,
                            to_id=class_id,
                            kind="DEFINED_IN",
                            confidence="strong",
                        ))
            elif member.type == "assignment":
                # Class-level field — `attr: int = 5` or `attr = "x"`
                # Register it as a field
                self._handle_class_field(member, ctx, result, class_id, name)

    def _handle_top_assignment(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Top-level `CONST = 42` or `x = lambda: ...` and so on.

        Only a simple identifier=value → we register it as variable/constant.
        Tuple unpacking (`a, b = c`), augmented (`x += 1`) skip — YAGNI.
        """
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        name = left.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        # Naming convention — UPPERCASE → constant
        kind = "constant" if name.isupper() and "_" in name or name.isupper() else "variable"
        if name.startswith("_"):
            kind = "variable"  # private convention

        sym_id = _make_id(ctx, name, line)
        result.symbols.append(SymbolInfo(
            id=sym_id,
            name=name,
            kind=kind,
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language="python",
            is_exported=not name.startswith("_"),
        ))
        ctx.seen_ids.add(sym_id)

        # Scan right-hand for calls
        right = node.child_by_field_name("right")
        if right is not None:
            inner = _WalkContext(
                file=ctx.file, source=ctx.source,
                current_symbol_id=sym_id, seen_ids=ctx.seen_ids,
            )
            self._scan_calls_in(right, inner, result)

    def _handle_class_field(
        self, node, ctx: _WalkContext, result: ExtractionResult,
        class_id: str, class_name: str,
    ) -> None:
        """Class body assignment — `class_var: int = 5`."""
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        name = left.text.decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        sym_id = _make_id(ctx, f"{class_name}.{name}", line)
        result.symbols.append(SymbolInfo(
            id=sym_id,
            name=name,
            kind="field",
            file=ctx.file,
            start_line=line,
            end_line=node.end_point[0] + 1,
            language="python",
            module=class_name,
            is_exported=not name.startswith("_"),
        ))
        ctx.seen_ids.add(sym_id)

        result.edges.append(EdgeInfo(
            from_id=sym_id,
            to_id=class_id,
            kind="DEFINED_IN",
            confidence="strong",
        ))

    # ─── calls scan ────────────────────────────────────────────────

    def _scan_calls_in(
        self,
        node,
        ctx: _WalkContext,
        result: ExtractionResult,
        _is_owner_root: bool = True,
    ) -> None:
        """Recursive walk looking for call → CALLS edges. Nested function/class — skip."""
        if not _is_owner_root and node.type in (
            "function_definition", "class_definition", "decorated_definition", "lambda",
        ):
            return  # nested scope

        if node.type == "call":
            self._handle_call(node, ctx, result)

        for child in node.named_children:
            self._scan_calls_in(child, ctx, result, _is_owner_root=False)

    def _handle_call(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """call: `foo(args)` or `obj.method(args)` or `mod.attr.method(args)`."""
        fn = node.child_by_field_name("function")
        if fn is None:
            return

        owner_id = ctx.current_symbol_id or _file_symbol_id(ctx.file)
        callee_name: str | None = None

        if fn.type == "identifier":
            callee_name = fn.text.decode("utf-8", errors="replace")
        elif fn.type == "attribute":
            attr_node = fn.child_by_field_name("attribute")
            if attr_node is not None:
                callee_name = attr_node.text.decode("utf-8", errors="replace")
        elif fn.type == "subscript":
            # foo[X](...) — skip, hard to resolve
            return

        if not callee_name:
            return

        # Skip self.method calls without attribution to the class? No — that is
        # a CALL too, just with lower confidence.
        confidence = "unresolved"
        if fn.type == "attribute":
            obj_node = fn.child_by_field_name("object")
            if obj_node is not None and obj_node.type == "identifier":
                obj_name = obj_node.text.decode("utf-8", errors="replace")
                if obj_name in ("self", "cls"):
                    confidence = "weak"  # self.X — probably a method in the same class

        result.edges.append(EdgeInfo(
            from_id=owner_id,
            to_id=None,
            kind="CALLS",
            confidence=confidence,
            raw_target=callee_name,
        ))

    # ─── helpers ───────────────────────────────────────────────────

    def _unwrap_decorated(self, decorated_node):
        """decorated_definition → inner function_definition / class_definition."""
        for child in decorated_node.named_children:
            if child.type in ("function_definition", "class_definition"):
                return child
        return None

    def _dotted_name_text(self, node) -> str:
        """dotted_name: identifier ('.' identifier)* → 'a.b.c' (raw text)."""
        return node.text.decode("utf-8", errors="replace").strip()

    def _module_text(self, node) -> str:
        """module_name field — dotted_name or relative_import → text."""
        return node.text.decode("utf-8", errors="replace").strip()

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


# ─── helpers (module-level) ──────────────────────────────────────────


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str, line: int) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{line}"


def _make_id_for_method(ctx: _WalkContext, class_name: str, fn_node) -> str | None:
    """Reconstruct method ID — the same logic _handle_function uses."""
    name_node = fn_node.child_by_field_name("name")
    if name_node is None:
        return None
    name = name_node.text.decode("utf-8", errors="replace")
    line = fn_node.start_point[0] + 1
    qualified = f"{class_name}.{name}"
    base = f"{ctx.file}::{qualified}"
    return base if base in ctx.seen_ids else f"{base}@{line}"
