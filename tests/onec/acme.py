"""A synthetic `Акме` configuration and an external report built against it.

The report `ОтчетПоЗаказамАкме` exists in the configuration too. The file is an
older copy: it lacks the configuration's change `ACME-205`, binds a handler its
form module does not have, and calls, names and queries things the
configuration would refuse — one of each kind the static checks look for.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from pathlib import Path

from tests.onec.container_writer import external, form_bin, form_description

NS = 'xmlns="http://v8.1c.ru/8.3/MDClasses"'
REPORT = "ОтчетПоЗаказамАкме"
MODULE = "ОбщегоНазначенияАкме"


def _obj(tag: str, name: str, children: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {NS}>\n'
            f'<{tag} uuid="0"><Properties><Name>{name}</Name></Properties>'
            f"<ChildObjects>{children}</ChildObjects></{tag}></MetaDataObject>\n")


def _attr(name: str) -> str:
    return f'<Attribute uuid="1"><Properties><Name>{name}</Name></Properties></Attribute>'


def _tabular(name: str, *attrs: str) -> str:
    # InternalInfo comes before Properties, as a Designer dump writes it.
    return (f'<TabularSection uuid="2"><InternalInfo><xr:GeneratedType name="x" '
            f'category="TabularSection"/></InternalInfo><Properties><Name>{name}</Name>'
            f"</Properties><ChildObjects>{''.join(_attr(a) for a in attrs)}</ChildObjects>"
            "</TabularSection>")


def _value(name: str) -> str:
    return f'<EnumValue uuid="3"><Properties><Name>{name}</Name></Properties></EnumValue>'


COMMON_MODULE = """\
// Общие функции Акме

Функция ФорматСуммы(Сумма, Разделитель = ", ") Экспорт
\tВозврат Строка(Сумма) + Разделитель;
КонецФункции

Процедура Внутренняя()
КонецПроцедуры
"""

CONFIG_OBJECT_MODULE = """\
// ACME-101 01.03.2025 подбор заказов
// ACME-205 12.05.2025 отбор по складу

Процедура Сформировать(Параметры) Экспорт
\tИтог = ОбщегоНазначенияАкме.ФорматСуммы(Параметры.Сумма);
\tОтбор = Параметры.Склад;
КонецПроцедуры

Процедура ТолькоВКонфигурации()
КонецПроцедуры
"""

FILE_OBJECT_MODULE = """\
// ACME-101 01.03.2025 подбор заказов

Процедура Сформировать(Параметры) Экспорт
\tИтог = ОбщегоНазначенияАкме.ФорматСуммы(Параметры.Сумма);
КонецПроцедуры

Процедура Проверки()
\tА = ОбщегоНазначенияАкме.ФорматСуммы(1, 2, 3);
\tОбщегоНазначенияАкме.Внутренняя();
\tОбщегоНазначенияАкме.НетТакого();
\tС = Перечисления.СтатусыЗаказовАкме.Отменен;
\tН = Перечисления.СтатусыЗаказовАкме.Новый;
\tСклад = Справочники.СкладыАкме.НайтиПоКоду("1");
\tТ = Справочники.НоменклатураАкме.НайтиПоКоду("2");
\tЗапрос = Новый Запрос;
\tЗапрос.Текст = "ВЫБРАТЬ
\t|\tЗаказ.Контрагент,
\t|\tЗаказ.Менеджер,
\t|\tЗНАЧЕНИЕ(Справочник.НоменклатураАкме.Товар) КАК Т,
\t|\tЗНАЧЕНИЕ(Справочник.НоменклатураАкме.Услуга) КАК У
\t|ИЗ
\t|\tДокумент.ЗаказКлиентаАкме КАК Заказ
\t|\tЛЕВОЕ СОЕДИНЕНИЕ РегистрНакопления.ТоварыНаСкладахСтарый.Остатки КАК Ост
\t|\tПО ИСТИНА";
\tМакет = ПолучитьМакет("Макет");
\tНет = ПолучитьМакет("НетМакета");
\tОбласть = Макет.ПолучитьОбласть("Шапка");
\tПодвал = Макет.ПолучитьОбласть("Подвал");
\tОбласть.Параметры.НомерЗаказа = 1;
\tОбласть.Параметры.Лишний = 2;
\tМестная(1, 2);
\tЕсли А Тогда
\t\tБ = 1;
КонецПроцедуры

Процедура Местная(Один)
КонецПроцедуры
"""

FORM_MODULE = """\
&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры
"""

CONFIG_FORM_MODULE = FORM_MODULE + """
&НаКлиенте
Процедура ПриЗакрытии()
КонецПроцедуры
"""

SAVED = datetime(2025, 3, 4, 10, 30)


def dump_files(nfd: bool = False) -> dict[str, bytes | str]:
    """rel path → content of the `Акме` Designer dump. ``nfd`` spells the
    directory names as macOS may keep them (`й` decomposed)."""
    def n(name: str) -> str:
        return unicodedata.normalize("NFD", name) if nfd else name

    rep = f"Reports/{n(REPORT)}"
    return {
        "Configuration.xml": f'<?xml version="1.0"?><MetaDataObject {NS}><Configuration/>'
                             "</MetaDataObject>\n",
        f"CommonModules/{n(MODULE)}.xml": _obj("CommonModule", MODULE),
        f"CommonModules/{n(MODULE)}/Ext/Module.bsl": COMMON_MODULE,
        "Catalogs/НоменклатураАкме.xml": _obj("Catalog", "НоменклатураАкме", _attr("Артикул")),
        "Catalogs/НоменклатураАкме/Ext/Predefined.xml":
            '<?xml version="1.0"?><PredefinedData><Item id="1"><Name>Услуга</Name></Item>'
            "</PredefinedData>\n",
        "Documents/ЗаказКлиентаАкме.xml": _obj(
            "Document", "ЗаказКлиентаАкме",
            _attr("Контрагент") + _tabular("Товары", "Номенклатура", "Количество")),
        "Enums/СтатусыЗаказовАкме.xml": _obj("Enum", "СтатусыЗаказовАкме",
                                             _value("Новый") + _value("Закрыт")),
        "AccumulationRegisters/ТоварыНаСкладахАкме.xml": _obj("AccumulationRegister",
                                                             "ТоварыНаСкладахАкме"),
        f"{rep}.xml": _obj("Report", REPORT),
        f"{rep}/Ext/ObjectModule.bsl": CONFIG_OBJECT_MODULE,
        f"{rep}/Forms/ФормаОтчета.xml": _obj("Form", "ФормаОтчета"),
        f"{rep}/Forms/ФормаОтчета/Ext/Form.bin": form_bin(
            form_description("ПриОткрытии", "ПриЗакрытии"), CONFIG_FORM_MODULE),
        f"{rep}/Templates/Макет.xml": _obj("Template", "Макет"),
    }


def write_dump(root: Path, nfd: bool = False) -> Path:
    for rel, content in dump_files(nfd).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


def report_file() -> bytes:
    """The external report: older than the configuration, with one of each problem."""
    return external(
        REPORT, synonym="Отчет по заказам", comment="версия 1.4",
        object_module=FILE_OBJECT_MODULE,
        forms=[("ФормаОтчета", ["ПриОткрытии", "ПриЗакрытии"], FORM_MODULE)],
        templates=[("Макет", ["Шапка", "Строка"], ["НомерЗаказа", "Сумма"])],
        when=SAVED)
