"""A data question follows the data: who else writes what a method touches, who reads what it writes.

THE CONTRACT. For a question the model calls ``data`` the walk from the matched
methods also crosses READS/WRITES edges (`onec_context.data_flow_edges`): to the
methods that write the registers a method reads or writes, and to those that
read what it writes. Nothing calls them, so an explanation never gets there.
A file the access decision hides is never reached that way either.

Fixtures are synthetic: an `acme` configuration whose cutting document posts
into a stock register that a report reads and a receipt also posts into.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.access.resolver import RepoAccessDecision, _RuleView
from src.config import Settings
from src.indexing.graph.pipeline import index_repo_graph
from src.qa.multi_repo_retriever import MultiRepoRetriever

SLUG = "acme-erp"
NS = 'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"'
READER = "CommonModules/ОтчетыАкме/Ext/Module.bsl"


def _xml(tag: str, name: str, synonym: str = "") -> str:
    syn = (f"<Synonym><v8:item><v8:lang>uk</v8:lang><v8:content>{synonym}</v8:content>"
           f"</v8:item></Synonym>") if synonym else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><MetaDataObject {NS}><{tag} uuid="1">'
            f"<Properties><Name>{name}</Name>{syn}</Properties><ChildObjects/></{tag}>"
            f"</MetaDataObject>")


_POSTING = """\
Процедура ОбработкаПроведения(Отказ, РежимПроведения)
    Движения.ОстаткиАкме.Записывать = Истина;
    Движения.ОстаткиАкме.Записать();
КонецПроцедуры
"""

_REPORT = """\
Функция ОстаткиНаДату() Экспорт
    Запрос = Новый Запрос;
    Запрос.Текст = "ВЫБРАТЬ Т.Количество ИЗ РегистрНакопления.ОстаткиАкме.Остатки КАК Т";
    Возврат Запрос.Выполнить();
КонецФункции
"""

FILES = {
    "Documents/РаскройАкме.xml": _xml("Document", "РаскройАкме", "Порізка аркушів"),
    "Documents/РаскройАкме/Ext/ObjectModule.bsl": _POSTING,
    "Documents/ПриходАкме.xml": _xml("Document", "ПриходАкме", "Надходження товару"),
    "Documents/ПриходАкме/Ext/ObjectModule.bsl": _POSTING,
    "AccumulationRegisters/ОстаткиАкме.xml": _xml("AccumulationRegister", "ОстаткиАкме"),
    READER: _REPORT,
}
OTHER_WRITER = "Documents.ПриходАкме.ObjectModule.ОбработкаПроведения"
THE_READER = "ОтчетыАкме.ОстаткиНаДату"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repos" / SLUG
    for rel, text in FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = Settings(workspace_dir=tmp_path)
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    return cfg


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text


def _names(settings: Settings, monkeypatch, intent: str, access=None) -> set[str]:
    reply = _Reply(f'{{"terms": ["порізка"], "intent": "{intent}"}}')
    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator",
                        staticmethod(lambda u, w: lambda **kw: reply))
    matches, _ = MultiRepoRetriever(settings=settings, vault_ret=None)._graph_text_matches(
        "що змінює порізка у залишках", [SLUG], access or {}, "u", "w")
    return {h.name for _, h in matches}


def test_a_data_question_reaches_the_other_writer_and_the_reader_of_the_register(
        settings, monkeypatch):
    names = _names(settings, monkeypatch, "data")
    assert "Documents.РаскройАкме.ObjectModule.ОбработкаПроведения" in names
    assert {OTHER_WRITER, THE_READER} <= names


def test_an_explanation_does_not_cross_the_data_edges(settings, monkeypatch):
    names = _names(settings, monkeypatch, "explain")
    assert "Documents.РаскройАкме.ObjectModule.ОбработкаПроведения" in names
    assert not {OTHER_WRITER, THE_READER} & names


def test_a_hidden_reader_is_never_reached_through_the_data(settings, monkeypatch):
    deny = ("CommonModules/ОтчетыАкме/**",)
    dec = RepoAccessDecision(
        repo_slug=SLUG, visibility="code", open_default=False,
        rules=(_RuleView("code", (), deny, ()),), deny_globs=deny)
    names = _names(settings, monkeypatch, "data", {SLUG: dec})
    assert OTHER_WRITER in names and THE_READER not in names
