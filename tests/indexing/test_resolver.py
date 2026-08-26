"""Tests for HeuristicResolver — Phase 5c."""

from __future__ import annotations

from pathlib import Path

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.resolver import HeuristicResolver

# ─── helpers ────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, **kwargs) -> RepoContext:
    return RepoContext(repo_root=tmp_path, **kwargs)


def _sym(id: str, name: str, file: str, line: int = 1, kind: str = "function") -> SymbolInfo:
    return SymbolInfo(
        id=id, name=name, kind=kind, file=file, start_line=line, language="typescript",
    )


# ─── External deps ──────────────────────────────────────────────────


def test_external_import_dropped(tmp_path):
    ctx = _ctx(tmp_path, external_deps={"vue", "axios"})
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(from_id="src/foo.ts", to_id=None, kind="IMPORTS", raw_target="vue::ref")]
    resolved = r.resolve_edges(edges)
    # Зовнішня залежність — edge відкидається повністю
    assert resolved == []


def test_scoped_external_dropped(tmp_path):
    ctx = _ctx(tmp_path, external_deps={"@vue/composition-api"})
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(from_id="x.ts", to_id=None, kind="IMPORTS", raw_target="@vue/composition-api::ref")]
    resolved = r.resolve_edges(edges)
    assert resolved == []


def test_subpath_external_dropped(tmp_path):
    """`lodash/get` — теж external якщо `lodash` у deps."""
    ctx = _ctx(tmp_path, external_deps={"lodash"})
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(from_id="x.ts", to_id=None, kind="IMPORTS", raw_target="lodash/get::default")]
    resolved = r.resolve_edges(edges)
    assert resolved == []


# ─── Path alias resolution ──────────────────────────────────────────


def test_alias_resolution(tmp_path):
    """`@/utils/foo` → `src/utils/foo.ts` через alias `@/` → `src/`."""
    (tmp_path / "src/utils").mkdir(parents=True)
    (tmp_path / "src/utils/foo.ts").write_text("export function helper() {}")

    ctx = _ctx(tmp_path, path_aliases={"@/": "src/"})
    syms = [_sym("src/utils/foo.ts::helper", "helper", "src/utils/foo.ts")]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(from_id="src/main.ts", to_id=None, kind="IMPORTS", raw_target="@/utils/foo::helper")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "src/utils/foo.ts::helper"
    assert resolved[0].confidence == "strong"


def test_acme_src_alias_resolution(tmp_path):
    """Real-life pattern: `src/*` → `acme-modules/src/*`."""
    (tmp_path / "acme-modules/src/utils").mkdir(parents=True)
    (tmp_path / "acme-modules/src/utils/file.js").write_text("export function base64ToBlob() {}")

    ctx = _ctx(tmp_path, path_aliases={"src/": "acme-modules/src/"})
    syms = [_sym("acme-modules/src/utils/file.js::base64ToBlob", "base64ToBlob", "acme-modules/src/utils/file.js")]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(from_id="x.ts", to_id=None, kind="IMPORTS", raw_target="src/utils/file::base64ToBlob")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id.endswith("base64ToBlob")
    assert resolved[0].confidence == "strong"


# ─── Relative path ─────────────────────────────────────────────────


def test_relative_import(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/bar.ts").write_text("export function baz() {}")

    ctx = _ctx(tmp_path)
    syms = [_sym("src/bar.ts::baz", "baz", "src/bar.ts")]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(
        from_id="src/foo.ts::caller",
        to_id=None,
        kind="IMPORTS",
        raw_target="./bar::baz",
    )]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "src/bar.ts::baz"


def test_parent_relative_import(tmp_path):
    (tmp_path / "src/sub").mkdir(parents=True)
    (tmp_path / "src/utils.ts").write_text("export function helper() {}")

    ctx = _ctx(tmp_path)
    syms = [_sym("src/utils.ts::helper", "helper", "src/utils.ts")]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(
        from_id="src/sub/foo.ts::caller",
        to_id=None,
        kind="IMPORTS",
        raw_target="../utils::helper",
    )]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "src/utils.ts::helper"


def test_relative_import_with_index_file(tmp_path):
    """`./utils` → `./utils/index.ts` якщо utils — це директорія з index файлом."""
    (tmp_path / "src/utils").mkdir(parents=True)
    (tmp_path / "src/utils/index.ts").write_text("export function helper() {}")

    ctx = _ctx(tmp_path)
    syms = [_sym("src/utils/index.ts::helper", "helper", "src/utils/index.ts")]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(
        from_id="src/main.ts::caller",
        to_id=None,
        kind="IMPORTS",
        raw_target="./utils::helper",
    )]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "src/utils/index.ts::helper"


# ─── Default + side-effect imports ──────────────────────────────────


def test_default_import_resolution(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/component.ts").write_text("export default function () {}")

    ctx = _ctx(tmp_path)
    # Реєструємо default symbol як це б зробив TypeScriptExtractor
    syms = [SymbolInfo(
        id="src/component.ts::default",
        name="default",
        kind="export_default",
        file="src/component.ts",
        start_line=1,
        is_exported=True,
    )]
    r = HeuristicResolver(ctx, syms)

    edges = [EdgeInfo(
        from_id="src/main.ts",
        to_id=None,
        kind="IMPORTS",
        raw_target="./component::default",
    )]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "src/component.ts::default"


def test_unresolvable_import_marked(tmp_path):
    """Якщо ні external, ні файл не знайдено — confidence='unresolved'."""
    ctx = _ctx(tmp_path)
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(
        from_id="x.ts",
        to_id=None,
        kind="IMPORTS",
        raw_target="./missing::foo",
    )]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id is None
    assert resolved[0].confidence == "unresolved"


# ─── CALLS resolution ───────────────────────────────────────────────


def test_call_unique_name_strong(tmp_path):
    ctx = _ctx(tmp_path)
    syms = [_sym("a.ts::doIt", "doIt", "a.ts")]
    r = HeuristicResolver(ctx, syms)
    edges = [EdgeInfo(from_id="b.ts::caller", to_id=None, kind="CALLS", raw_target="doIt")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "a.ts::doIt"
    assert resolved[0].confidence == "strong"


def test_call_ambiguous_name_weak(tmp_path):
    """Декілька символів з тим самим іменем → weak edge до першого."""
    ctx = _ctx(tmp_path)
    syms = [
        _sym("a.ts::process", "process", "a.ts"),
        _sym("b.ts::process", "process", "b.ts"),
    ]
    r = HeuristicResolver(ctx, syms)
    edges = [EdgeInfo(from_id="x.ts", to_id=None, kind="CALLS", raw_target="process")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].confidence == "weak"
    assert resolved[0].to_id in ("a.ts::process", "b.ts::process")


def test_call_unknown_unresolved(tmp_path):
    """Виклик external функції — лишається у графі як unresolved."""
    ctx = _ctx(tmp_path)
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(from_id="x.ts", to_id=None, kind="CALLS", raw_target="fetch")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id is None
    assert resolved[0].confidence == "unresolved"


# ─── Already-resolved edges passthrough ─────────────────────────────


def test_resolved_edges_kept_unchanged(tmp_path):
    ctx = _ctx(tmp_path)
    r = HeuristicResolver(ctx, [])
    edges = [EdgeInfo(from_id="a.ts::x", to_id="b.ts::y", kind="CALLS", confidence="strong")]
    resolved = r.resolve_edges(edges)
    assert len(resolved) == 1
    assert resolved[0].to_id == "b.ts::y"
    assert resolved[0].confidence == "strong"


# ─── DoD: end-to-end integration scenario ───────────────────────────


def test_dod_resolver_chain(tmp_path):
    """Симуляція ланцюга: useEstimate → ordersController → requestQuote.

    1. RepoContext: src/ → acme-modules/src/, vue/vuex external
    2. Symbols: totals, useEstimate, OrdersController клас + requestQuote метод
    3. Edges:
       - useEstimate.js IMPORTS ordersController (default з OrdersController.ts)
       - useEstimate calls computed (vue external — drop)
       - convertOrderData CALLS buildQuotePayload (unique → strong)
    """
    # repo layout
    (tmp_path / "acme-modules/src/composables/estimate").mkdir(parents=True)
    (tmp_path / "acme-modules/src/models/core/domains/Orders").mkdir(parents=True)
    (tmp_path / "acme-modules/src/api/services/gateway").mkdir(parents=True)

    (tmp_path / "acme-modules/src/composables/estimate/useEstimate.js").write_text("// stub")
    (tmp_path / "acme-modules/src/models/core/domains/Orders/OrdersController.ts").write_text("// stub")
    (tmp_path / "acme-modules/src/api/services/gateway/PricingApi.js").write_text("// stub")

    ctx = _ctx(
        tmp_path,
        path_aliases={"src/": "acme-modules/src/"},
        external_deps={"vue", "vuex"},
    )

    oc_file = "acme-modules/src/models/core/domains/Orders/OrdersController.ts"
    api_file = "acme-modules/src/api/services/gateway/PricingApi.js"
    ue_file = "acme-modules/src/composables/estimate/useEstimate.js"

    syms = [
        # OrdersController з default export (синтетичний)
        SymbolInfo(id=f"{oc_file}::default", name="default", kind="export_default",
                   file=oc_file, start_line=1, is_exported=True),
        # method requestQuote
        SymbolInfo(id=f"{oc_file}::OrdersController.requestQuote", name="requestQuote",
                   kind="method", file=oc_file, start_line=10, language="typescript"),
        SymbolInfo(id=f"{oc_file}::OrdersController.convertOrderData", name="convertOrderData",
                   kind="method", file=oc_file, start_line=20, language="typescript"),
        # PricingApi має buildQuotePayload (метод)
        SymbolInfo(id=f"{api_file}::PricingApi.buildQuotePayload", name="buildQuotePayload",
                   kind="method", file=api_file, start_line=30, language="javascript"),
        # useEstimate
        SymbolInfo(id=f"{ue_file}::useEstimate", name="useEstimate",
                   kind="function", file=ue_file, start_line=22, language="javascript"),
    ]
    r = HeuristicResolver(ctx, syms)

    edges = [
        # IMPORTS: useEstimate.js імпортує ordersController з OrdersController.ts
        EdgeInfo(
            from_id=ue_file, to_id=None, kind="IMPORTS",
            raw_target="src/models/core/domains/Orders/OrdersController.ts::default",
        ),
        # IMPORTS external — drop
        EdgeInfo(
            from_id=ue_file, to_id=None, kind="IMPORTS",
            raw_target="vue::computed",
        ),
        # CALLS: convertOrderData викликає buildQuotePayload — unique → strong
        EdgeInfo(
            from_id=f"{oc_file}::OrdersController.convertOrderData",
            to_id=None, kind="CALLS", raw_target="buildQuotePayload",
        ),
    ]
    resolved = r.resolve_edges(edges)

    # vue::computed → drop. Лишається 2 з 3 input.
    assert len(resolved) == 2

    # IMPORTS edge до OrdersController.ts::default
    imports_resolved = [e for e in resolved if e.kind == "IMPORTS"]
    assert len(imports_resolved) == 1
    assert imports_resolved[0].to_id == f"{oc_file}::default"
    assert imports_resolved[0].confidence == "strong"

    # CALLS edge convertOrderData → buildQuotePayload
    calls_resolved = [e for e in resolved if e.kind == "CALLS"]
    assert len(calls_resolved) == 1
    assert calls_resolved[0].to_id == f"{api_file}::PricingApi.buildQuotePayload"
    assert calls_resolved[0].confidence == "strong"
