"""1C object names on a card, and staleness against a real git history."""

from __future__ import annotations

import pytest

from src.kb.objects import ChangeIndex, canonical, canonical_all, staleness
from tests.kb.conftest import make_config_repo


@pytest.mark.parametrize("written, expected", [
    ("Документ.ЗаказКлиента", "Documents.ЗаказКлиента"),
    ("Document.ЗаказКлиента", "Documents.ЗаказКлиента"),
    ("documents.ЗаказКлиента", "Documents.ЗаказКлиента"),
    ("РегистрНакопления.ТоварыВРезерве", "AccumulationRegisters.ТоварыВРезерве"),
    ("ОбщийМодуль.Утилиты", "CommonModules.Утилиты"),
    ("Справочник.Номенклатура.Форма.ФормаЭлемента", "Catalogs.Номенклатура"),
    ("ЗаказКлиента", None),
    ("Модуль.Щось", None),
])
def test_every_spelling_of_an_object_names_the_graph_node(written, expected):
    assert canonical(written) == expected


def test_repeats_collapse_and_non_objects_are_set_aside():
    good, bad = canonical_all(["Документ.А", "Documents.А", "просто текст"])
    assert good == ["Documents.А"] and bad == ["просто текст"]


def test_a_card_whose_object_changed_is_stale_and_one_whose_did_not_is_fresh(tmp_path):
    first, second = make_config_repo(tmp_path)
    index = ChangeIndex(tmp_path)
    stale = staleness(index, ["Documents.Заказ"], first[:10])
    assert stale.state == "stale" and stale.changed == ["Documents.Заказ"] and stale.head == second
    assert staleness(index, ["Catalogs.Товары"], first).state == "fresh"
    assert staleness(index, ["Documents.Заказ"], second).state == "fresh"


@pytest.mark.parametrize("objects, sha, reason", [
    ([], "abcdef1", "names no 1C objects"),
    (["Documents.Заказ"], "", "no source commit"),
    (["Documents.Заказ"], "abcdef1234", "not in the clone"),
])
def test_what_cannot_be_checked_is_unknown_with_the_reason(tmp_path, objects, sha, reason):
    make_config_repo(tmp_path)
    st = staleness(ChangeIndex(tmp_path), objects, sha)
    assert st.state == "unknown" and reason in st.reason


def test_an_uploaded_configuration_without_history_is_unknown(tmp_path):
    (tmp_path / "Documents").mkdir()
    st = staleness(ChangeIndex(tmp_path), ["Documents.Заказ"], "abcdef1")
    assert st.state == "unknown" and "no git history" in st.reason
    assert staleness(None, ["Documents.Заказ"], "abcdef1").reason.startswith("no configuration")
