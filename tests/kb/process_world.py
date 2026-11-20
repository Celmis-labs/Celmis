"""A small 1C configuration with a real graph, and an engine that records what it was given."""

from __future__ import annotations

from pathlib import Path

from tests.kb.conftest import git

NS = 'xmlns="http://v8.1c.ru/8.3/MDClasses"'


def _xml(tag: str, name: str) -> str:
    return (f'<?xml version="1.0"?><MetaDataObject {NS}><{tag} uuid="1"><Properties>'
            f"<Name>{name}</Name></Properties></{tag}></MetaDataObject>")


POSTING = """Процедура ОбработкаПроведения(Отказ, РежимПроведения)
\tДвижения.ОстаткиАкме.Записывать = Истина;
\tДвижения.ОстаткиАкме.Добавить();
\tРезервАкме.Зарезервировать(Ссылка);
КонецПроцедуры
"""

COMMON = """Процедура Зарезервировать(Заказ) Экспорт
\tТекст = "ВЫБРАТЬ * ИЗ Справочник.ТоварыАкме";
КонецПроцедуры

Процедура СоздатьЗаказ() Экспорт
\tДок = Документы.ЗаказАкме.СоздатьДокумент();
\tДок.Записать(РежимЗаписиДокумента.Проведение);
КонецПроцедуры

Процедура ПрочитатьЗаказы() Экспорт
\tТекст = "ВЫБРАТЬ * ИЗ Документ.ЗаказАкме";
КонецПроцедуры
"""

FILES = {
    "Documents/ЗаказАкме.xml": _xml("Document", "ЗаказАкме"),
    "AccumulationRegisters/ОстаткиАкме.xml": _xml("AccumulationRegister", "ОстаткиАкме"),
    "Catalogs/ТоварыАкме.xml": _xml("Catalog", "ТоварыАкме"),
    "CommonModules/РезервАкме.xml": _xml("CommonModule", "РезервАкме"),
    "Documents/ЗаказАкме/Ext/ObjectModule.bsl": POSTING,
    "CommonModules/РезервАкме/Ext/Module.bsl": COMMON,
}

OBJECT_MODULE = "Documents/ЗаказАкме/Ext/ObjectModule.bsl"
COMMON_MODULE = "CommonModules/РезервАкме/Ext/Module.bsl"


def make_acme_repo(root: Path, slug: str) -> str:
    """Write the dump under ``root``, commit it, index its graph; the commit's sha."""
    from src.config import get_settings
    from src.indexing.graph.pipeline import index_repo_graph

    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    index_repo_graph(root, slug, get_settings(), src_subdir=None)
    return git(root, "rev-parse", "HEAD")


class FakeEngine:
    """Records every call; says what it was told to (or fails for one process)."""

    name = "fake"

    def __init__(self, text: str = "# Процес\n\nОпис процесу.\n", fail_on: str = "") -> None:
        self.text = text
        self.fail_on = fail_on
        self.calls: list[dict] = []

    def generate(self, **kw):  # noqa: ANN003, ANN201
        from src.generation.engines import DocResult

        self.calls.append(kw)
        if self.fail_on and kw["metadata_context"]["process"]["id"] == self.fail_on:
            raise RuntimeError("the model refused")
        return DocResult(text=self.text)
