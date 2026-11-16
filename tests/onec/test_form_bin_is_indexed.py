"""The module of an ordinary form lives inside its binary `Form.bin`; the
indexer reads it out and files its symbols under `<...>/Ext/Form.bin/module`,
the path the dev tools read back, so the line numbers agree."""

from __future__ import annotations

from pathlib import Path

from src.indexing.graph.languages.factory import build_default_registry
from tests.onec.container_writer import form_bin, form_description

REL = "Reports/ОтчетАкме/Forms/Форма/Ext/Form.bin"
MODULE = """\
&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры

Функция Итог(А, Б = 0) Экспорт
\tВозврат А + Б;
КонецФункции
"""


def _extract(data: bytes):
    reg = build_default_registry()
    ex = reg.match(Path(REL), read_for_sniff=False)
    assert ex is not None and ex.language == "bsl"
    return ex.extract(Path(REL), source=data)


def test_the_form_module_symbols_are_filed_under_the_member_path():
    res = _extract(form_bin(form_description("ПриОткрытии"), MODULE))
    by_name = {s.name.rsplit(".", 1)[-1]: s for s in res.symbols}
    assert {"ПриОткрытии", "Итог"} <= set(by_name)
    assert by_name["ПриОткрытии"].file == f"{REL}/module"
    assert (by_name["ПриОткрытии"].start_line, by_name["Итог"].start_line) == (2, 5)


def test_a_form_bin_without_a_module_gives_nothing():
    res = _extract(form_bin(form_description(), None))
    assert not res.symbols


def test_a_broken_form_bin_is_a_parse_error_not_a_crash():
    res = _extract(b"\xff\xff\xff\x7f garbage")
    assert not res.symbols
