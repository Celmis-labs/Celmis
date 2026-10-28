"""`refs` answers what a 1C method reads and writes, and who writes an object.

THE CONTRACT. `refs direction=reads|writes` lists the objects a method's
statements read or write, `readers|writers` the methods that do so to an object,
each at the file and line of the STATEMENT. Both ends must be files the caller
may list: an object whose file is hidden is not named by the method that writes
it, and a method in a hidden file is not a writer of anyone; the hidden name
appears nowhere in the answer.

Fixtures are synthetic: an `acme` configuration, Russian names, indexed by the
real pipeline into a graph of its own.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.config import Settings
from src.indexing.graph.pipeline import index_repo_graph, make_graph_store
from src.mcp_server.dev_profile import common, tools_refs
from tests.mcp_server.dev.conftest import SHOP

NS = 'xmlns="http://v8.1c.ru/8.3/MDClasses"'
CUT = "Documents/РаскройАкме/Ext/ObjectModule.bsl"
INCOME = "Documents/ПриходАкме/Ext/ObjectModule.bsl"


def _xml(tag: str, name: str) -> str:
    return (f'<?xml version="1.0"?><MetaDataObject {NS}><{tag} uuid="1"><Properties>'
            f"<Name>{name}</Name></Properties></{tag}></MetaDataObject>")


DUMP = {
    "Documents/РаскройАкме.xml": _xml("Document", "РаскройАкме"),
    "Documents/ПриходАкме.xml": _xml("Document", "ПриходАкме"),
    "AccumulationRegisters/ОстаткиАкме.xml": _xml("AccumulationRegister", "ОстаткиАкме"),
    "InformationRegisters/ЦеныАкме.xml": _xml("InformationRegister", "ЦеныАкме"),
    CUT: ("Процедура ОбработкаПроведения(Отказ)\n\tДвижения.ОстаткиАкме.Записывать = Истина;\n"
          '\tЗ = "ВЫБРАТЬ * ИЗ РегистрСведений.ЦеныАкме.СрезПоследних";\nКонецПроцедуры\n'),
    INCOME: "Процедура ОбработкаПроведения(Отказ)\n\tДвижения.ОстаткиАкме.Добавить();\nКонецПроцедуры\n",
}
POST = "Documents.РаскройАкме.ObjectModule.ОбработкаПроведения"


@pytest.fixture
def one_c_graph(tmp_path, monkeypatch, as_caller):
    repo = tmp_path / "dump"
    for rel, text in DUMP.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    settings = Settings(workspace_dir=tmp_path / "ws")
    index_repo_graph(repo, "dump", settings, src_subdir=None)
    store = make_graph_store(settings.repo_graph_path("dump"))

    @contextmanager
    def open_store(_slug):
        yield store

    monkeypatch.setattr(common, "open_store", open_store)
    yield
    store.close()


def test_reads_and_writes_of_a_method_are_listed_at_their_statements(one_c_graph):
    out = tools_refs.run(SHOP, POST, direction="writes")
    assert f"{SHOP} {CUT}:2 writes AccumulationRegisters.ОстаткиАкме" in out
    assert "ЦеныАкме" not in out
    out = tools_refs.run(SHOP, POST, direction="reads")
    assert f"{SHOP} {CUT}:3 reads InformationRegisters.ЦеныАкме" in out
    assert "ОстаткиАкме" not in out


def test_readers_and_writers_of_an_object_are_the_methods_with_the_statement_lines(one_c_graph):
    out = tools_refs.run(SHOP, "AccumulationRegisters.ОстаткиАкме", direction="writers")
    assert f"{SHOP} {CUT}:2 writes {POST}" in out
    assert f"{SHOP} {INCOME}:2 writes Documents.ПриходАкме.ObjectModule.ОбработкаПроведения" in out
    assert "readers" not in out and "reads" not in out
    assert f"{INCOME}" not in tools_refs.run(SHOP, "InformationRegisters.ЦеныАкме", direction="readers")


def test_a_method_in_a_hidden_file_is_not_a_writer_and_its_name_is_nowhere(one_c_graph, as_caller):
    as_caller({SHOP: "code"}, deny={SHOP: ("Documents/ПриходАкме/**",)})
    out = tools_refs.run(SHOP, "AccumulationRegisters.ОстаткиАкме", direction="writers")
    assert f"{CUT}:2 writes {POST}" in out
    assert "ПриходАкме" not in out


def test_an_object_in_a_hidden_file_is_not_named_by_the_method_that_reads_it(one_c_graph, as_caller):
    as_caller({SHOP: "code"}, deny={SHOP: ("InformationRegisters/**",)})
    out = tools_refs.run(SHOP, POST, direction="reads")
    assert "ЦеныАкме" not in out and "no reads found" in out
    assert "ЦеныАкме" not in tools_refs.run(SHOP, POST, direction="writes")
    # the object itself is no symbol for this caller: the same answer as for a name that is nowhere
    assert "no symbol" in tools_refs.run(SHOP, "InformationRegisters.ЦеныАкме", direction="readers")
