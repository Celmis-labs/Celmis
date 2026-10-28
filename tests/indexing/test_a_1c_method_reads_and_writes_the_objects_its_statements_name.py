"""A 1C method's reads and writes are edges to the objects, one per statement.

THE CONTRACT. `BslExtractor` emits READS and WRITES edges from a method to a
metadata object, each with the line of its statement: READS for a table a query
names after `FROM` / `JOIN` (any case, Russian or English, a virtual table or a
tabular section being the object itself) and for a manager read in code; WRITES
for register movements, a record set / object that was made by a manager and
then written, and a constant that is set. Only text that is code counts: a
commented-out query, a string that merely says "from Catalog.X" and a
`ЗНАЧЕНИЕ(Catalog.X.Item)` make no edge. The resolver links an edge to the
object's node when the dump has the object and drops it when it does not (as it
does a REFERENCES), and the store keeps the line and answers `data_edges` /
`data_users`.

Fixtures are synthetic: an `acme` configuration, Russian names.
"""

from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.indexing.graph.configs import RepoContext
from src.indexing.graph.languages.bsl import BslExtractor
from src.indexing.graph.languages.onec_metadata import OneCMetadataExtractor
from src.indexing.graph.pipeline import index_repo_graph, make_graph_store
from src.indexing.graph.resolver import HeuristicResolver

MODULE = "Documents/РаскройАкме/Ext/ObjectModule.bsl"
NS = 'xmlns="http://v8.1c.ru/8.3/MDClasses"'


def _edges(text: str, rel: str = MODULE) -> list[tuple[str, str, int]]:
    """(kind, raw target, line) of the READS / WRITES edges of a module."""
    res = BslExtractor().extract(Path(rel), source=text.encode("utf-8"))
    assert all(e.to_id is None and e.confidence == "unresolved" for e in res.edges
               if e.kind in ("READS", "WRITES"))
    return sorted((e.kind, e.raw_target, e.line) for e in res.edges if e.kind in ("READS", "WRITES"))


def _method(body: str) -> str:
    return f"Процедура Выполнить() Экспорт\n{body}\nКонецПроцедуры\n"


POSTING = """Процедура ОбработкаПроведения(Отказ, РежимПроведения)
\tДвижения.ОстаткиАкме.Записывать = Истина;
\tДвижения.ОстаткиАкме.Добавить();
\tДвижения.ЦеныАкме.Записать();
КонецПроцедуры
"""


def test_a_posting_procedure_writes_each_register_it_makes_movements_in():
    got = _edges(POSTING)
    assert got == [("WRITES", "Registers.ОстаткиАкме", 2), ("WRITES", "Registers.ОстаткиАкме", 3),
                   ("WRITES", "Registers.ЦеныАкме", 4)]


def test_a_variable_holding_the_movements_writes_the_register_when_it_is_changed():
    got = _edges(_method("\tНабор = Движения.ОстаткиАкме;\n\tНабор.Очистить();\n"
                         "\tЕсли Набор.Записывать Тогда КонецЕсли;\n\tНабор.Записывать = Истина;\n"
                         "\tНабор = Неопределено;\n\tНабор.Добавить();"))
    assert got == [("WRITES", "Registers.ОстаткиАкме", 3), ("WRITES", "Registers.ОстаткиАкме", 5)]


def test_a_comparison_of_the_write_flag_is_not_a_write():
    assert _edges(_method("\tЕсли Движения.ОстаткиАкме.Записывать = Истина Тогда\n\tКонецЕсли;")) == []


def test_a_query_reads_a_register_virtual_table_and_a_catalog_at_their_lines():
    got = _edges(_method('\tЗапрос.Текст = "ВЫБРАТЬ Т.Товар ИЗ РегистрНакопления.ОстаткиАкме.Остатки(&Дата) КАК Т";\n'
                         '\tДругой.Текст = "ВЫБРАТЬ * ИЗ Справочник.ТоварыАкме";'))
    assert got == [("READS", "AccumulationRegisters.ОстаткиАкме", 2),
                   ("READS", "Catalogs.ТоварыАкме", 3)]


def test_a_multiline_query_with_a_join_reads_both_tables_on_their_own_lines():
    got = _edges(_method(
        '\tЗапрос.Текст =\n\t"ВЫБРАТЬ\n\t|\tДок.Ссылка\n\t|ИЗ\n\t|\tДокумент.РаскройАкме.Материалы КАК Мат\n'
        '\t|\t// ЛЕВОЕ СОЕДИНЕНИЕ Справочник.Закомментировано КАК К\n'
        '\t|ЛЕВОЕ СОЕДИНЕНИЕ\n\t|\tРегистрСведений.ЦеныАкме.СрезПоследних КАК Ц\n\t|ПО Мат.Товар = Ц.Товар";'))
    assert got == [("READS", "Documents.РаскройАкме", 6), ("READS", "InformationRegisters.ЦеныАкме", 9)]


def test_a_from_list_with_commas_reads_every_table_and_skips_the_parameters_commas():
    got = _edges(_method('\tЗ = "ВЫБРАТЬ 1 ИЗ РегистрНакопления.ОстаткиАкме.Остатки(&Д, Склад = &С) КАК О,'
                         ' Справочник.СкладыАкме КАК С ГДЕ О.Склад = С.Ссылка";'))
    assert got == [("READS", "AccumulationRegisters.ОстаткиАкме", 2), ("READS", "Catalogs.СкладыАкме", 2)]


def test_a_query_with_english_keywords_and_a_lowercase_type_reads_the_table():
    got = _edges(_method('\tQ = "select * from AccumulationRegister.StockAcme.Balance as b '
                         'left join informationregister.RatesAcme.SliceLast as r on 1 = 1";'))
    assert got == [("READS", "AccumulationRegisters.StockAcme", 2),
                   ("READS", "InformationRegisters.RatesAcme", 2)]


def test_a_cyrillic_table_name_at_the_start_of_a_word_is_found_in_every_spelling():
    for word, plural in (("РегистрНакопления", "AccumulationRegisters"), ("регистрнакопления", "AccumulationRegisters"),
                         ("Документ", "Documents"), ("ПЛАНСЧЕТОВ", "ChartsOfAccounts"),
                         ("Перечисление", "Enums"), ("ЖурналДокументов", "DocumentJournals")):
        got = _edges(_method(f'\tТекст = "выбрать * из {word}.ИмяАкме";'))
        assert got == [("READS", f"{plural}.ИмяАкме", 2)], word


def test_a_commented_out_query_is_not_counted():
    got = _edges(_method('\t// Запрос.Текст = "ВЫБРАТЬ * ИЗ Справочник.ТоварыАкме";\n'
                         '\tX = 1; // "ВЫБРАТЬ * ИЗ Справочник.ТоварыАкме"\n\t// Справочники.ТоварыАкме.НайтиПоКоду(1);'))
    assert got == []


def test_a_string_that_merely_says_from_is_not_a_query():
    got = _edges(_method('\tСообщить("Данные загружены ИЗ Справочник.ТоварыАкме");\n'
                         '\tСообщить("Взять ИЗ папки");\n\tЗ = "ВЫБРАТЬ ЗНАЧЕНИЕ(Справочник.ТоварыАкме.Основной)";'))
    assert got == []


def test_a_value_in_a_query_is_not_a_table_but_the_from_after_it_is():
    got = _edges(_method('\tЗ = "ВЫБРАТЬ * ИЗ Документ.РаскройАкме ГДЕ Вид = ЗНАЧЕНИЕ(Перечисление.ВидыАкме.Один)";'))
    assert got == [("READS", "Documents.РаскройАкме", 2)]


def test_a_query_built_from_pieces_reads_the_table_of_the_piece_that_starts_with_from():
    got = _edges(_method('\tТекст = "ВЫБРАТЬ * ";\n\tТекст = Текст + " ИЗ Справочник.ТоварыАкме";'))
    assert got == [("READS", "Catalogs.ТоварыАкме", 3)]


def test_a_type_that_is_not_a_data_object_is_not_a_table():
    got = _edges(_method('\tЗ = "ВЫБРАТЬ * ИЗ Т.Поле, ОбщийМодуль.Утилиты, ВременнаяТаблица.Что";'))
    assert got == []


def test_manager_reads_in_code_read_the_object():
    got = _edges(_method("\tА = Справочники.ТоварыАкме.НайтиПоКоду(\"1\");\n"
                         "\tБ = РегистрыСведений.ЦеныАкме.ПолучитьПоследнее(Отбор);\n"
                         "\tВ = Константы.ВалютаАкме.Получить();\n\tГ = Catalogs.ТоварыАкме.Select();\n"
                         "\tД = Справочник.ТоварыАкме.Выбрать();\n\tЕ = Документы.РаскройАкме.ПустаяСсылка();"))
    assert got == [("READS", "Catalogs.ТоварыАкме", 2), ("READS", "Catalogs.ТоварыАкме", 5),
                   ("READS", "Constants.ВалютаАкме", 4), ("READS", "InformationRegisters.ЦеныАкме", 3)]


def test_a_record_set_made_by_a_manager_is_a_write_when_the_variable_is_written():
    got = _edges(_method("\tНабор = РегистрыСведений.ЦеныАкме.СоздатьНаборЗаписей();\n\tНабор.Добавить();\n"
                         "\tНабор.Записать();\n\tДругой = РегистрыНакопления.ОстаткиАкме.СоздатьНаборЗаписей();\n"
                         "\tДругой.Прочитать();"))
    assert got == [("WRITES", "InformationRegisters.ЦеныАкме", 4)]


def test_a_document_item_or_group_is_a_write_when_it_is_written():
    got = _edges(_method("\tДок = Документы.РаскройАкме.СоздатьДокумент();\n\tДок.Записать(РежимЗаписиДокумента.Проведение);\n"
                         "\tЭл = Справочники.ТоварыАкме.СоздатьЭлемент();\n\tГр = Справочники.ТоварыАкме.СоздатьГруппу();\n"
                         "\tGr = Catalogs.ГруппыАкме.CreateFolder();\n\tGr.Write();\n"
                         "\tСправочники.ПартнерыАкме.СоздатьЭлемент().Записать();"))
    assert got == [("WRITES", "Catalogs.ГруппыАкме", 7), ("WRITES", "Catalogs.ПартнерыАкме", 8),
                   ("WRITES", "Documents.РаскройАкме", 3)]


def test_a_constant_that_is_set_is_a_write_and_a_catalog_manager_set_is_not():
    got = _edges(_method("\tКонстанты.ВалютаАкме.Установить(1);\n\tСправочники.ТоварыАкме.Установить(1);"))
    assert got == [("WRITES", "Constants.ВалютаАкме", 2)]


def test_a_variable_written_in_another_method_is_not_a_write_of_this_one():
    text = ("Процедура Один()\n\tНабор = РегистрыСведений.ЦеныАкме.СоздатьНаборЗаписей();\nКонецПроцедуры\n"
            "Процедура Два(Набор)\n\tНабор.Записать();\nКонецПроцедуры\n")
    assert _edges(text) == []


# ─── resolution and the store ───────────────────────────────────────

def _xml(plural_tag: str, name: str) -> str:
    return (f'<?xml version="1.0"?><MetaDataObject {NS}><{plural_tag} uuid="1"><Properties>'
            f"<Name>{name}</Name></Properties></{plural_tag}></MetaDataObject>")


DUMP = {
    "Documents/РаскройАкме.xml": _xml("Document", "РаскройАкме"),
    "AccumulationRegisters/ОстаткиАкме.xml": _xml("AccumulationRegister", "ОстаткиАкме"),
    "InformationRegisters/ЦеныАкме.xml": _xml("InformationRegister", "ЦеныАкме"),
    "Catalogs/ТоварыАкме.xml": _xml("Catalog", "ТоварыАкме"),
    MODULE: POSTING.replace("КонецПроцедуры\n", '\t'
                            'Запрос.Текст = "ВЫБРАТЬ * ИЗ Справочник.ТоварыАкме";\n'
                            '\tМусор = Движения.НетТакогоАкме.Добавить();\n'
                            '\tЗапрос.Текст = "ВЫБРАТЬ * ИЗ Справочник.НетТакогоАкме";\nКонецПроцедуры\n'),
}


def _resolved(files: dict[str, str]):  # noqa: ANN202
    bsl, meta = BslExtractor(), OneCMetadataExtractor()
    symbols, edges = [], []
    for rel, text in files.items():
        ext = bsl if rel.endswith(".bsl") else meta
        res = ext.extract(Path(rel), source=text.encode("utf-8"))
        symbols += res.symbols
        edges += res.edges
    out = HeuristicResolver(RepoContext(repo_root=Path(".")), symbols).resolve_edges(edges)
    names = {s.id: s.name for s in symbols}
    return out, names


def test_an_edge_resolves_to_the_metadata_object_node_and_one_to_a_missing_object_is_dropped():
    out, names = _resolved(DUMP)
    got = sorted((e.kind, names[e.to_id], e.line) for e in out
                 if e.kind in ("READS", "WRITES") and e.to_id)
    assert got == [("READS", "Catalogs.ТоварыАкме", 5), ("WRITES", "AccumulationRegisters.ОстаткиАкме", 2),
                   ("WRITES", "AccumulationRegisters.ОстаткиАкме", 3), ("WRITES", "InformationRegisters.ЦеныАкме", 4)]
    assert all(e.to_id for e in out if e.kind in ("READS", "WRITES"))  # nothing unresolved is kept
    assert all(e.confidence == "strong" for e in out if e.kind == "WRITES")


def test_movements_in_a_name_two_kinds_of_register_share_resolve_weakly_to_the_first_kind():
    files = {**DUMP, "InformationRegisters/ОстаткиАкме.xml": _xml("InformationRegister", "ОстаткиАкме")}
    out, names = _resolved(files)
    kinds = {names[e.to_id]: e.confidence for e in out if e.kind == "WRITES" and e.to_id}
    assert kinds["AccumulationRegisters.ОстаткиАкме"] == "weak"


def test_the_store_keeps_the_lines_and_answers_what_a_method_touches_and_who_touches_an_object(tmp_path):
    repo = tmp_path / "dump"
    for rel, text in DUMP.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    settings = Settings(workspace_dir=tmp_path / "ws")
    index_repo_graph(repo, "dump", settings, src_subdir=None)
    store = make_graph_store(settings.repo_graph_path("dump"))
    try:
        post = store.find_named("Documents.РаскройАкме.ObjectModule.ОбработкаПроведения", limit=5)
        assert len(post) == 1
        rows = store.data_edges([post[0]["id"]])
        assert [(r["kind"], r["to_name"], r["to_kind"], r["line"]) for r in rows] == [
            ("WRITES", "AccumulationRegisters.ОстаткиАкме", "vendor.onec.accumulationregister", 2),
            ("WRITES", "AccumulationRegisters.ОстаткиАкме", "vendor.onec.accumulationregister", 3),
            ("WRITES", "InformationRegisters.ЦеныАкме", "vendor.onec.informationregister", 4),
            ("READS", "Catalogs.ТоварыАкме", "vendor.onec.catalog", 5)]
        assert {r["from_id"] for r in rows} == {post[0]["id"]}
        reg = store.find_named("AccumulationRegisters.ОстаткиАкме", limit=5)
        users = store.data_users([reg[0]["id"]])
        assert [(r["kind"], r["from_name"], r["line"]) for r in users] == [
            ("WRITES", "Documents.РаскройАкме.ObjectModule.ОбработкаПроведения", 2),
            ("WRITES", "Documents.РаскройАкме.ObjectModule.ОбработкаПроведения", 3)]
        assert store.data_edges([]) == [] and store.data_users([]) == []
        assert len(store.data_edges([post[0]["id"]], limit=2)) == 2
        counts = {r["k"]: r["n"] for r in store.query(
            "MATCH ()-[r]->() RETURN type(r) AS k, count(r) AS n")}
        assert counts["WRITES"] == 3 and counts["READS"] == 1
    finally:
        store.close()
