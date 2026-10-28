"""OneCMetadataExtractor and its links to the BSL modules (synthetic dump)."""

from __future__ import annotations

from pathlib import Path

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.graph_store import name_tokens
from src.indexing.graph.languages.bsl import BslExtractor
from src.indexing.graph.languages.factory import build_default_registry
from src.indexing.graph.languages.onec import (
    data_object,
    module_identity,
    owner_object,
)
from src.indexing.graph.languages.onec_metadata import OneCMetadataExtractor
from src.indexing.graph.resolver import HeuristicResolver
from src.mcp_server.dev_profile import rank

NS = ('xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
      'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')


def _syn(**langs: str) -> str:
    items = "".join(f"<v8:item><v8:lang>{k}</v8:lang><v8:content>{v}</v8:content></v8:item>"
                    for k, v in langs.items())
    return f"<Synonym>{items}</Synonym>"


DOCUMENT = f"""﻿<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NS} version="2.18"><Document uuid="1">
<Properties><Name>AcmeOrder</Name>{_syn(ru="Заказ Acme", uk="Замовлення Acme", en="Acme order")}
<Comment>Customer order</Comment></Properties>
<ChildObjects>
<Attribute uuid="2"><Properties><Name>Partner</Name>{_syn(ru="Партнёр", uk="Партнер")}</Properties></Attribute>
<TabularSection uuid="3"><Properties><Name>Lines</Name></Properties><ChildObjects>
<Attribute uuid="4"><Properties><Name>Item</Name></Properties></Attribute>
<Attribute uuid="5"><Properties><Name>Qty</Name></Properties></Attribute>
</ChildObjects></TabularSection>
<Form>MainForm</Form>
</ChildObjects></Document></MetaDataObject>"""

SUBSYSTEM = f"""<?xml version="1.0"?>
<MetaDataObject {NS}><Subsystem uuid="9"><Properties><Name>Sales</Name>{_syn(uk="Продажі")}
<Content><xr:Item xsi:type="xr:MDObjectRef">Document.AcmeOrder</xr:Item>
<xr:Item xsi:type="xr:MDObjectRef">CommonModule.AcmeUtils</xr:Item>
<xr:Item xsi:type="xr:MDObjectRef">Document.Missing</xr:Item></Content>
</Properties></Subsystem></MetaDataObject>"""

COMMON_XML = f"""<?xml version="1.0"?>
<MetaDataObject {NS}><CommonModule uuid="7"><Properties><Name>AcmeUtils</Name>
{_syn(ru="Утилиты")}</Properties></CommonModule></MetaDataObject>"""


def _meta(rel: str, xml: str):
    return OneCMetadataExtractor().extract(Path(rel), source=xml.encode("utf-8"))


def test_an_object_is_a_symbol_named_like_its_modules_with_every_synonym():
    res = _meta("Documents/AcmeOrder.xml", DOCUMENT)
    (sym,) = res.symbols
    assert sym.name == "Documents.AcmeOrder" and sym.kind == "vendor.onec.document"
    assert sym.language == "onec" and sym.file == "Documents/AcmeOrder.xml"
    assert "uk: Замовлення Acme" in sym.docstring and "ru: Заказ Acme" in sym.docstring
    assert "en: Acme order" in sym.docstring and "comment: Customer order" in sym.docstring
    assert "Partner (Партнёр)" in sym.docstring
    assert sym.signature == ("Document; attributes: Partner; tabular: Lines(Item, Qty); "
                             "forms: MainForm")
    # the BSL side names the object's modules with this very prefix
    assert module_identity("Documents/AcmeOrder/Ext/ObjectModule.bsl").startswith(sym.name + ".")


def test_other_xml_and_hostile_xml_make_no_symbols():
    assert _meta("Documents/readme.xml", "<root/>").symbols == []
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><MetaDataObject>&a;</MetaDataObject>'
    res = _meta("Documents/Bomb.xml", bomb)
    assert res.symbols == [] and res.parse_errors
    assert _meta("Documents/Broken.xml", "<MetaDataObject><Document>").parse_errors


def test_the_registry_sends_a_type_folder_xml_here_and_other_xml_nowhere():
    reg = build_default_registry()
    assert reg.match(Path("Documents/AcmeOrder.xml"), read_for_sniff=False).language == "onec"
    assert reg.match(Path("Catalogs/Items/Forms/F/Ext/Form.xml"), read_for_sniff=False) is None


def test_owner_object_and_data_object_names():
    assert owner_object("CommonModules/AcmeUtils/Ext/Module.bsl") == "CommonModules.AcmeUtils"
    assert owner_object("Catalogs/Items/Forms/F/Ext/Form/Module.bsl") == "Catalogs.Items"
    assert owner_object("src/Documents/Order/ObjectModule.bsl") == "Documents.Order"
    assert owner_object("scripts/tool.os") is None
    assert data_object("РегистрНакопления", "Stock") == "AccumulationRegisters.Stock"
    assert data_object("catalog", "Items") == "Catalogs.Items"
    assert data_object("CommonModule", "X") is None and data_object("Query", "X") is None


def test_modules_subsystems_and_queries_link_to_the_objects():
    doc = _meta("Documents/AcmeOrder.xml", DOCUMENT)
    sub = _meta("Subsystems/Sales.xml", SUBSYSTEM)
    cm = _meta("CommonModules/AcmeUtils.xml", COMMON_XML)
    bsl = BslExtractor()
    text = ('Procedure Post() Export\n\tQuery.Text = "SELECT * FROM\n\t|\tinformationregister.Stock'
            '\n\t|WHERE x";\n\tDocuments.acmeorder.FindByNumber(1);\n\tCatalogs.Gone.Find();\nEndProcedure\n')
    mod = bsl.extract(Path("Documents/AcmeOrder/Ext/ObjectModule.bsl"), source=text.encode())
    util = bsl.extract(Path("CommonModules/AcmeUtils/Ext/Module.bsl"),
                       source=b"Procedure X() Export\nEndProcedure\n")
    stock = _meta("InformationRegisters/Stock.xml", COMMON_XML.replace(
        "CommonModule", "InformationRegister").replace("AcmeUtils", "Stock"))
    results = [doc, sub, cm, mod, util, stock]
    symbols = [s for r in results for s in r.symbols]
    edges = [e for r in results for e in r.edges]
    out = HeuristicResolver(RepoContext(repo_root=Path(".")), symbols).resolve_edges(edges)
    names = {s.id: s.name for s in symbols}
    got = {(e.kind, names[e.from_id], names[e.to_id]) for e in out if e.to_id}
    assert ("REFERENCES", "Sales", "Documents.AcmeOrder") not in got  # subsystem name is Subsystems.Sales
    assert ("REFERENCES", "Subsystems.Sales", "Documents.AcmeOrder") in got
    assert ("REFERENCES", "Subsystems.Sales", "CommonModules.AcmeUtils") in got
    assert not any(t == "Documents.Missing" for _k, _f, t in got)
    assert ("DEFINED_IN", "Documents.AcmeOrder.ObjectModule", "Documents.AcmeOrder") in got
    assert ("DEFINED_IN", "AcmeUtils", "CommonModules.AcmeUtils") in got
    method = "Documents.AcmeOrder.ObjectModule.Post"
    assert ("REFERENCES", method, "InformationRegisters.Stock") in got   # query text, any case
    assert ("REFERENCES", method, "Documents.AcmeOrder") in got
    assert not any(f == method and t.startswith("Catalogs") for _k, f, t in got)


def test_a_synthetic_dump_goes_through_the_graph_pipeline(tmp_path):
    """Both layouts' symbols, the vendor kinds and the new edge links survive the store."""
    from src.config import Settings
    from src.indexing.graph.pipeline import index_repo_graph, make_graph_store

    repo = tmp_path / "dump"
    files = {
        "Documents/AcmeOrder.xml": DOCUMENT,
        "Subsystems/Sales.xml": SUBSYSTEM,
        "CommonModules/AcmeUtils.xml": COMMON_XML,
        "CommonModules/AcmeUtils/Ext/Module.bsl": "Function Total(A) Export\n\tReturn A;\nEndFunction\n",
        "Documents/AcmeOrder/Ext/ObjectModule.bsl":
            "Procedure Post()\n\tacmeutils.total(1);\n\tQ = \"FROM Document.AcmeOrder\";\nEndProcedure\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    settings = Settings(workspace_dir=tmp_path / "ws")
    index_repo_graph(repo, "dump", settings, src_subdir=None)
    store = make_graph_store(settings.repo_graph_path("dump"))
    try:
        rows = store.query(
            "MATCH (a:Symbol)-[r]->(b:Symbol) RETURN a.name AS a, type(r) AS r, b.name AS b")
        got = {(r["a"], r["r"], r["b"]) for r in rows}
        assert ("Documents.AcmeOrder.ObjectModule.Post", "CALLS", "AcmeUtils.Total") in got
        assert ("Documents.AcmeOrder.ObjectModule.Post", "REFERENCES", "Documents.AcmeOrder") in got
        assert ("Documents.AcmeOrder.ObjectModule", "DEFINED_IN", "Documents.AcmeOrder") in got
        assert ("Subsystems.Sales", "REFERENCES", "CommonModules.AcmeUtils") in got
        # a person's word for the object: its Ukrainian synonym, not in any name
        for word in ("замовлення", "Замовлення acme"):
            rows = store.find_symbols(word, mode="auto", limit=20)
            assert [r["name"] for r in rows][:1] == ["Documents.AcmeOrder"], word
            assert rank.score(rows[0], word) > 0
        found = store.find_symbols("AcmeOrder", mode="contains", limit=20)
        assert {"Documents.AcmeOrder", "Documents.AcmeOrder.ObjectModule.Post"} <= {
            r["name"] for r in found}
    finally:
        store.close()


def test_cyrillic_camel_case_names_have_tokens():
    assert name_tokens("Мод.РасчетРаскроя_v2") == ["мод", "расчет", "раскроя", "v", "2"]
    assert name_tokens("OrderService.create_order") == ["order", "service", "create"]
