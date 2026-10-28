"""BslExtractor: 1C:Enterprise modules (synthetic fixtures, acme-style names)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo
from src.indexing.graph.languages.bsl import BslExtractor, module_identity
from src.indexing.graph.languages.factory import build_default_registry, supported_suffixes
from src.indexing.graph.resolver import HeuristicResolver

COMMON = '''﻿// Acme helpers.
// Second doc line.
&AtServer
Function GetTitle(Val Item, Mode = Undefined) Export
\tNote = "Procedure Fake() in a string";
\t// Procedure Hidden() in a comment
\tText = "first line
\t|Procedure InString()
\t|second ""quoted"" line";
\tIf Item <> Undefined Then
\t\tReturn Normalize(Item);
\tEndIf;
\tReturn New Structure("a", 1);
EndFunction

Процедура normalize_all (
\tList,
\tCount) Экспорт
\tДля Каждого Элемент Из List Цикл
\t\tNormalize(Элемент);
\t\tЭтотОбъект.GetTitle(Элемент);
\t\tAcmeStore.Save(Элемент); // trailing Other.Call()
\tКонецЦикла;
КонецПроцедуры

Async Procedure Normalize(Item)
\tВозврат;
EndProcedure

#Region Internal
Function Plain()
\tReturn 1;
EndFunction
#EndRegion
'''


def _run(rel: str, text: str = COMMON, root: Path | None = None):
    ex = BslExtractor(repo_root=root)
    return ex.extract(Path(rel), source=text.replace("\n", "\r\n").encode("utf-8"))


def _syms(res):
    return {s.name: s for s in res.symbols}


def test_the_extractor_is_registered_for_bsl_and_onescript():
    reg = build_default_registry()
    assert reg.match(Path("a/Module.bsl"), read_for_sniff=False).language == "bsl"
    assert reg.match(Path("tool.os"), read_for_sniff=False).language == "bsl"
    assert {".bsl", ".os"} <= supported_suffixes()


@pytest.mark.parametrize(("rel", "expected"), [
    ("CommonModules/AcmeUtils/Ext/Module.bsl", "AcmeUtils"),
    ("Catalogs/Items/Ext/ObjectModule.bsl", "Catalogs.Items.ObjectModule"),
    ("Documents/Order/Ext/ManagerModule.bsl", "Documents.Order.ManagerModule"),
    ("Catalogs/Items/Forms/ItemForm/Ext/Form/Module.bsl", "Catalogs.Items.Forms.ItemForm"),
    ("Catalogs/Items/Commands/Open/Ext/CommandModule.bsl", "Catalogs.Items.Commands.Open"),
    ("InformationRegisters/Prices/Ext/RecordSetModule.bsl", "InformationRegisters.Prices.RecordSetModule"),
    ("dump/src/CommonModules/AcmeUtils/Module.bsl", "AcmeUtils"),
    ("src/Catalogs/Items/ObjectModule.bsl", "Catalogs.Items.ObjectModule"),
    ("src/Catalogs/Items/Forms/ItemForm/Module.bsl", "Catalogs.Items.Forms.ItemForm"),
    ("src/Configuration/SessionModule.bsl", "Configuration.SessionModule"),
    ("Ext/ManagedApplicationModule.bsl", "ManagedApplicationModule"),
    ("scripts/deploy_tool.os", "deploy_tool"),
])
def test_module_identity_follows_the_dump_layout(rel, expected):
    assert module_identity(rel) == expected


def test_methods_signatures_exports_docs_and_ranges():
    res = _run("CommonModules/AcmeUtils/Ext/Module.bsl")
    syms = _syms(res)
    assert set(syms) == {
        "AcmeUtils", "AcmeUtils.GetTitle", "AcmeUtils.normalize_all",
        "AcmeUtils.Normalize", "AcmeUtils.Plain",
    }
    title = syms["AcmeUtils.GetTitle"]
    assert title.kind == "function" and title.is_exported and title.language == "bsl"
    assert title.signature.startswith("&AtServer Function GetTitle(Val Item")
    assert title.signature.endswith("Export")
    assert title.docstring == "Acme helpers.\nSecond doc line."
    assert title.start_line == 4 and title.end_line == 14
    multi = syms["AcmeUtils.normalize_all"]
    assert multi.is_exported and "Count) Экспорт" in multi.signature
    assert not syms["AcmeUtils.Normalize"].is_exported
    assert syms["AcmeUtils.Normalize"].signature.startswith("Async Procedure")
    assert syms["AcmeUtils.Plain"].start_line > syms["AcmeUtils.Normalize"].end_line


def test_strings_comments_and_keywords_do_not_make_symbols_or_calls():
    res = _run("CommonModules/AcmeUtils/Ext/Module.bsl")
    calls = {(e.from_id.split("::")[1], e.raw_target) for e in res.edges if e.kind == "CALLS"}
    names = set(_syms(res))
    assert not {"Fake", "Hidden", "InString"} & {n.split(".")[-1] for n in names}
    # Normalize( in GetTitle resolves in-file; `Structure(` after New, If( are no calls
    assert all(r not in ("Structure", "If", "Fake", "Hidden", "InString", "Other.Call")
               for _f, r in calls)
    by_id = {s.id: s.name for s in res.symbols}
    resolved = {(by_id[e.from_id], by_id[e.to_id]) for e in res.edges
                if e.kind == "CALLS" and e.to_id}
    assert ("AcmeUtils.GetTitle", "AcmeUtils.Normalize") in resolved       # case-insensitive
    assert ("AcmeUtils.normalize_all", "AcmeUtils.Normalize") in resolved
    assert ("AcmeUtils.normalize_all", "AcmeUtils.GetTitle") in resolved   # ЭтотОбъект.
    assert ("AcmeUtils.normalize_all", "AcmeStore.Save") in {
        (e.from_id.split("::")[1], e.raw_target) for e in res.edges if e.to_id is None}


def test_a_commented_out_line_inside_a_multiline_string_keeps_the_string_open():
    text = ('Procedure A()\n\tQ = "select\n\t//|dropped";\n\t|from t\n\t|where x";\nEndProcedure\n'
            'Procedure B()\n\tCallee();\nEndProcedure\n')
    syms = _syms(_run("Ext/Module.bsl", text))
    assert {"Module.A", "Module.B"} <= set(syms)


def test_object_module_methods_are_methods_of_the_object():
    res = _run("Catalogs/Items/Ext/ObjectModule.bsl",
               "&НаСервере\nПроцедура ПередЗаписью(Отказ)\nКонецПроцедуры\n")
    syms = _syms(res)
    assert syms["Catalogs.Items.ObjectModule.ПередЗаписью"].kind == "method"
    assert syms["Catalogs.Items.ObjectModule"].kind == "file_module"


def test_a_manager_call_targets_the_manager_module_in_either_language():
    text = "Procedure Run()\n\tCatalogs.Items.Create();\n\tСправочники.Items.Create();\nEndProcedure\n"
    res = _run("CommonModules/Caller/Ext/Module.bsl", text)
    targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
    assert targets == {"Catalogs.Items.ManagerModule.Create"}


def test_two_modules_resolve_a_qualified_call_case_insensitively():
    lib = _run("CommonModules/AcmeStore/Ext/Module.bsl",
               "Procedure Save(Item) Export\nEndProcedure\n")
    mgr = _run("Catalogs/Items/Ext/ManagerModule.bsl",
               "Function Create() Export\n\tReturn 1;\nEndFunction\n")
    caller = _run("Documents/Order/Ext/ObjectModule.bsl",
                  "Procedure Post()\n\tacmestore.save(1);\n\tCatalogs.Items.Create();\n"
                  "\tVar.Method();\nEndProcedure\n")
    symbols = [*lib.symbols, *mgr.symbols, *caller.symbols]
    edges = [*lib.edges, *mgr.edges, *caller.edges]
    ctx = RepoContext(repo_root=Path("."))
    out = HeuristicResolver(ctx, symbols).resolve_edges(edges)
    names = {s.id: s.name for s in symbols}
    got = {(names[e.from_id], names[e.to_id]) for e in out if e.kind == "CALLS" and e.to_id}
    assert got == {
        ("Documents.Order.ObjectModule.Post", "AcmeStore.Save"),
        ("Documents.Order.ObjectModule.Post", "Catalogs.Items.ManagerModule.Create"),
    }


def test_a_duplicate_method_name_keeps_both_with_distinct_ids():
    res = _run("Ext/Module.bsl", "Function A()\nEndFunction\nFunction A()\nEndFunction\n")
    ids = [s.id for s in res.symbols if s.kind == "function"]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_an_oversized_file_is_skipped_and_named(monkeypatch):
    from src.indexing.graph.languages import bsl

    monkeypatch.setattr(bsl, "MAX_BSL_BYTES", 10)
    res = _run("Module.bsl", "Procedure Run()\nEndProcedure\n")
    assert res.symbols == [] and res.parse_errors


def test_every_edge_is_an_edgeinfo_with_known_kinds():
    res = _run("CommonModules/AcmeUtils/Ext/Module.bsl")
    assert all(isinstance(e, EdgeInfo) and e.kind in {"DEFINED_IN", "CALLS"} for e in res.edges)
