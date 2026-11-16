"""What an external file holds: kind, name, synonym, comment, dates, modules,
forms with their handlers, templates with areas and parameters, and a plain
statement when a module has no code to read."""

from __future__ import annotations

from src.onec.external import inspect_bytes, try_inspect
from tests.onec import acme
from tests.onec.container_writer import REPORT_CLASS, container, external, form_bin, header


def test_an_external_report_is_described():
    ext = inspect_bytes(acme.report_file(), "ОтчетПоЗаказамАкме.erf")
    assert (ext.kind, ext.name, ext.label) == ("Reports", acme.REPORT, "external report")
    assert ext.synonyms == {"ru": "Отчет по заказам"}
    assert ext.comment == "версия 1.4"
    assert ext.saved == acme.SAVED
    paths = [m.path for m in ext.modules]
    assert f"Reports/{acme.REPORT}/Ext/ObjectModule.bsl" in paths
    assert f"Reports/{acme.REPORT}/Forms/ФормаОтчета/Ext/Form/Module.bsl" in paths
    form = ext.forms[0]
    assert (form.name, form.handlers) == ("ФормаОтчета", ["ПриОткрытии", "ПриЗакрытии"])
    tpl = ext.templates[0]
    assert (tpl.name, tpl.kind) == ("Макет", "spreadsheet")
    assert sorted(tpl.areas) == ["Строка", "Шапка"]
    assert sorted(tpl.params) == ["НомерЗаказа", "Сумма"]
    assert ext.texts[f"Reports/{acme.REPORT}/Ext/ObjectModule.bsl"] == acme.FILE_OBJECT_MODULE


def test_a_data_processor_is_told_apart():
    ext = inspect_bytes(external("ОбработкаАкме", kind="processor", object_module=""), "a.epf")
    assert (ext.kind, ext.label) == ("DataProcessors", "external data processor")


def test_a_lone_form_bin():
    ext = inspect_bytes(form_bin('{27,{18,{{1,e1692cc2-605b-4535-84dd-28440238746c,'
                                 '{3,"ПриОткрытии",{1,""}}}}}}',
                                 "Процедура ПриОткрытии()\nКонецПроцедуры\n"), "Form.bin")
    assert (ext.kind, ext.label) == ("Form", "ordinary form (Form.bin)")
    assert ext.forms[0].handlers == ["ПриОткрытии"]
    assert "Процедура ПриОткрытии()" in ext.texts["Form/Module.bsl"]


def test_a_module_compiled_without_source_says_there_is_no_code():
    main = "11111111-1111-4111-8111-111111111111"
    data = container([
        ("root", f"{{2,{main},}}".encode()),
        (main, f"{{1,{REPORT_CLASS},{{3,{header(main, 'ЗакрытыйАкме')}}}}}".encode("utf-8-sig")),
        (f"{main}.0", container([("image", b"\x01\x02compiled")])),
    ], deflate=True)
    ext = inspect_bytes(data, "closed.erf")
    assert ext.name == "ЗакрытыйАкме"
    assert all(m.text is None for m in ext.modules)
    assert any("no code to read" in n for n in ext.notes)


def test_not_a_1c_file():
    ext, why = try_inspect(b"%PDF-1.7 not a container", "x.erf")
    assert ext is None and why
