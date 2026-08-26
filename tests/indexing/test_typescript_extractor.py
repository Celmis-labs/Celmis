"""Tests for TypeScriptExtractor — Phase 5a.

Покриває: ts/tsx/js/jsx/cjs/mjs через одну логіку.
Golden fixtures inline (textwrap.dedent) для точного контролю над AST.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.indexing.graph.languages.typescript import (
    TypeScriptExtractor,
    _grammar_for,
)

# ─── grammar selection ──────────────────────────────────────────────


def test_grammar_for_ts():
    assert _grammar_for("foo.ts") == "typescript"
    assert _grammar_for(Path("a/b/c.ts")) == "typescript"


def test_grammar_for_tsx():
    assert _grammar_for("foo.tsx") == "tsx"


def test_grammar_for_js_variants():
    for ext in (".js", ".jsx", ".cjs", ".mjs"):
        assert _grammar_for(f"x{ext}") == "javascript"


def test_grammar_unknown_falls_back_to_javascript():
    assert _grammar_for("README.md") == "javascript"  # default


# ─── helpers ────────────────────────────────────────────────────────


@pytest.fixture
def extract():
    """Helper: extract from inline source, optional ext.

    Повертає ExtractionResult з symbols, але filter'ом виключає синтетичний
    `file_module` symbol — тести фокусуються на real declarations.
    """
    def _extract(source: str, file_path: str = "test.ts"):
        ext = TypeScriptExtractor()
        result = ext.extract(Path(file_path), source.encode("utf-8"))
        # Filter file_module + DEFINED_IN edges — для clarity тестів
        result.symbols = [s for s in result.symbols if s.kind != "file_module"]
        result.edges = [e for e in result.edges if e.kind != "DEFINED_IN"]
        return result
    return _extract


# ─── functions ──────────────────────────────────────────────────────


def test_function_declaration_basic(extract):
    res = extract("function foo() { return 1; }")
    assert len(res.symbols) == 1
    s = res.symbols[0]
    assert s.name == "foo"
    assert s.kind == "function"
    assert s.is_exported is False
    assert s.start_line == 1


def test_exported_function(extract):
    res = extract("export function bar() {}")
    assert len(res.symbols) == 1
    assert res.symbols[0].name == "bar"
    assert res.symbols[0].is_exported is True


def test_export_default_function(extract):
    res = extract("export default function () { return 42; }")
    assert len(res.symbols) == 1
    assert res.symbols[0].name == "default"
    assert res.symbols[0].kind == "export_default"
    assert res.symbols[0].is_exported is True


def test_arrow_function_via_const(extract):
    res = extract("const myFn = () => 1;")
    assert len(res.symbols) == 1
    s = res.symbols[0]
    assert s.name == "myFn"
    assert s.kind == "function"


def test_async_function_works(extract):
    res = extract("async function loader() { return await fetch('/x'); }")
    assert len(res.symbols) == 1
    assert res.symbols[0].name == "loader"


# ─── classes ────────────────────────────────────────────────────────


def test_class_with_methods(extract):
    src = textwrap.dedent("""
        class Greeter {
            constructor() { this.x = 1; }
            greet(name) { return `Hello, ${name}`; }
            #private() { return 1; }
        }
    """)
    res = extract(src)
    by_kind = {(s.kind, s.name) for s in res.symbols}
    assert ("class", "Greeter") in by_kind
    assert ("method", "constructor") in by_kind
    assert ("method", "greet") in by_kind
    methods = [s for s in res.symbols if s.kind == "method"]
    for m in methods:
        assert m.module == "Greeter"  # клас — як module символу методу


def test_typescript_class_with_type_annotations(extract):
    src = textwrap.dedent("""
        class Service {
            private readonly url: string;
            getData(id: number): Promise<string> { return Promise.resolve('x'); }
        }
    """)
    res = extract(src)
    classes = [s for s in res.symbols if s.kind == "class"]
    methods = [s for s in res.symbols if s.kind == "method"]
    assert len(classes) == 1
    assert classes[0].name == "Service"
    assert any(m.name == "getData" for m in methods)


def test_exported_class(extract):
    res = extract("export class Foo { bar() {} }")
    cls = next(s for s in res.symbols if s.kind == "class")
    assert cls.is_exported is True


# ─── imports ────────────────────────────────────────────────────────


def test_import_default(extract):
    res = extract("import vue from 'vue';")
    assert len(res.edges) == 1
    e = res.edges[0]
    assert e.kind == "IMPORTS"
    assert e.raw_target == "vue::default"
    assert e.confidence == "unresolved"


def test_import_named(extract):
    res = extract("import { computed, ref } from 'vue';")
    targets = sorted(e.raw_target for e in res.edges)
    assert targets == ["vue::computed", "vue::ref"]


def test_import_namespace(extract):
    res = extract("import * as utils from 'src/utils';")
    assert len(res.edges) == 1
    assert res.edges[0].raw_target == "src/utils::*"


def test_import_side_effect(extract):
    res = extract("import 'normalize.css';")
    assert len(res.edges) == 1
    assert res.edges[0].raw_target == "normalize.css"


def test_import_renamed(extract):
    """`import { a as b } from 'm'` — name полю — це 'a', не 'b'."""
    res = extract("import { computed as c } from 'vue';")
    assert len(res.edges) == 1
    assert res.edges[0].raw_target == "vue::computed"


def test_dynamic_import(extract):
    src = textwrap.dedent("""
        function loadModule() {
            return import('./async-module');
        }
    """)
    res = extract(src)
    dyn_imports = [e for e in res.edges if e.kind == "IMPORTS"]
    assert any(e.raw_target == "./async-module" for e in dyn_imports)


# ─── re-exports ─────────────────────────────────────────────────────


def test_reexport_named(extract):
    res = extract("export { foo, bar } from 'utils';")
    targets = sorted(e.raw_target for e in res.edges if e.kind == "IMPORTS")
    assert "utils::foo" in targets
    assert "utils::bar" in targets


# ─── calls ──────────────────────────────────────────────────────────


def test_call_in_function(extract):
    src = textwrap.dedent("""
        function doSomething() {
            helper();
            other();
        }
    """)
    res = extract(src)
    calls = [e for e in res.edges if e.kind == "CALLS"]
    targets = sorted(e.raw_target for e in calls)
    assert targets == ["helper", "other"]
    # Усі attributed до doSomething
    for c in calls:
        assert c.from_id.endswith("::doSomething")


def test_member_call_uses_property_name(extract):
    """`obj.method()` → callee_name = 'method', не 'obj.method'."""
    src = textwrap.dedent("""
        function f() {
            api.fetchData();
            obj.deeply.nested.method();
        }
    """)
    res = extract(src)
    targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
    assert "fetchData" in targets
    assert "method" in targets


def test_call_in_arrow_function(extract):
    src = "const fn = () => fetcher();"
    res = extract(src)
    calls = [e for e in res.edges if e.kind == "CALLS"]
    assert len(calls) == 1
    assert calls[0].raw_target == "fetcher"
    assert calls[0].from_id.endswith("::fn")


def test_calls_in_class_method(extract):
    src = textwrap.dedent("""
        class Foo {
            doIt() {
                this.helper();
                external();
            }
        }
    """)
    res = extract(src)
    method_calls = [e for e in res.edges if e.kind == "CALLS"]
    sources = {e.from_id for e in method_calls}
    # Calls — атрибутовані до Foo.doIt method, не до class Foo
    assert all("Foo.doIt" in s for s in sources)
    targets = {e.raw_target for e in method_calls}
    assert targets == {"helper", "external"}


def test_top_level_calls_attributed_to_file(extract):
    res = extract("init();", "test.js")
    calls = [e for e in res.edges if e.kind == "CALLS"]
    assert len(calls) == 1
    # No owning symbol — from_id = synthetic file-module symbol id
    assert calls[0].from_id == "test.js::__module__"


# ─── nested functions don't double-walk ─────────────────────────────


def test_nested_function_calls_not_attributed_to_outer(extract):
    """Inner arrow's `inner()` call → attributed до outer (бо ми не реєструємо
    nested functions як symbols у MVP — flat namespace)."""
    src = textwrap.dedent("""
        function outer() {
            const inner = () => {
                deep();
            };
            inner();
        }
    """)
    res = extract(src)
    calls = [e for e in res.edges if e.kind == "CALLS"]
    targets = sorted(e.raw_target for e in calls)
    # outer() called inner. У nested arrow ми не заходимо для CALLS scan
    # (nested functions не мають symbol id), тому deep() пропускається.
    assert "inner" in targets


# ─── disambiguation ────────────────────────────────────────────────


def test_duplicate_names_get_unique_ids(extract):
    """Дві функції з однаковим іменем у файлі → id з line suffix."""
    src = textwrap.dedent("""
        function process() { a(); }
        function helper() {}
        function process() { b(); }
    """)
    res = extract(src)
    process_syms = [s for s in res.symbols if s.name == "process"]
    assert len(process_syms) == 2
    ids = {s.id for s in process_syms}
    assert len(ids) == 2  # унікальні


# ─── path handling ─────────────────────────────────────────────────


def test_relative_path_uses_repo_root(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir()
    f = repo / "src" / "foo.ts"
    f.write_text("export function f() {}")
    ext = TypeScriptExtractor(repo_root=repo)
    res = ext.extract(f)
    # Filter file_module
    real_syms = [s for s in res.symbols if s.kind != "file_module"]
    assert real_syms[0].file == "src/foo.ts"
    assert real_syms[0].id == "src/foo.ts::f"


def test_absolute_path_when_no_repo_root(tmp_path):
    f = tmp_path / "x.js"
    f.write_text("function g() {}")
    ext = TypeScriptExtractor()
    res = ext.extract(f)
    # Без repo_root — повний абс path
    assert res.symbols[0].file.startswith("/")


# ─── parse errors ──────────────────────────────────────────────────


def test_malformed_source_does_not_crash(extract):
    """Tree-sitter error-recovery: малформований код не падає, тегаємо помилку."""
    res = extract("function foo( { mismatched")
    # parse_errors може містити warning, але crash немає
    assert isinstance(res.symbols, list)


# ─── DoD: real-world snippet ────────────────────────────────────────


def test_dod_extract_chain_useEstimate_to_ordersController(extract):
    """Спрощена версія useEstimate.js — перевіряє ключовий ланцюг."""
    src = textwrap.dedent("""
        import ordersController from 'src/models/core/domains/Orders/OrdersController.ts';
        import { computed } from 'vue';

        export const totals = computed(
            () => ordersController.activeOrder?.value?.totals
        );

        export function useEstimate() {
            const store = useStore();
            return { estimate: 1 };
        }
    """)
    res = extract(src, "acme-modules/src/composables/estimate/useEstimate.js")

    # 1. Symbols
    names = {s.name for s in res.symbols}
    assert "totals" in names
    assert "useEstimate" in names

    # 2. Imports
    import_targets = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
    assert any("OrdersController.ts" in t for t in import_targets)
    assert "vue::computed" in import_targets

    # 3. Calls — computed() called by totals declaration owner
    computed_calls = [e for e in res.edges if e.kind == "CALLS" and e.raw_target == "computed"]
    assert any(e.from_id.endswith("::totals") for e in computed_calls)
