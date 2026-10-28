"""What the 1C:Enterprise extractors share: the metadata type names and where a
file of a configuration dump belongs.

A configuration dump is laid out by metadata type — Designer: `Catalogs/<Object>.xml`
and `Catalogs/<Object>/Ext/ObjectModule.bsl`; EDT: `src/Catalogs/<Object>/...`.
The BSL extractor (`bsl.py`) and the metadata extractor (`onec_metadata.py`)
must name an object the same way, or a module could not be linked to the object
it belongs to; both take the name from here: `<Type>.<Object>` with the type in
its dump spelling (`Catalogs`, `InformationRegisters`, `CommonModules` ...).
"""

from __future__ import annotations

#: Metadata type directories of a configuration dump (Designer and EDT), and the
#: singular spelling of each (the tag of the object's XML and the English name
#: used in code: `Catalog.Items`).
SINGULAR: dict[str, str] = {
    "AccountingRegisters": "AccountingRegister",
    "AccumulationRegisters": "AccumulationRegister",
    "BusinessProcesses": "BusinessProcess",
    "CalculationRegisters": "CalculationRegister",
    "Catalogs": "Catalog",
    "ChartsOfAccounts": "ChartOfAccounts",
    "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "CommandGroups": "CommandGroup",
    "CommonCommands": "CommonCommand",
    "CommonForms": "CommonForm",
    "CommonModules": "CommonModule",
    "Constants": "Constant",
    "DataProcessors": "DataProcessor",
    "DefinedTypes": "DefinedType",
    "DocumentJournals": "DocumentJournal",
    "DocumentNumerators": "DocumentNumerator",
    "Documents": "Document",
    "Enums": "Enum",
    "EventSubscriptions": "EventSubscription",
    "ExchangePlans": "ExchangePlan",
    "ExternalDataSources": "ExternalDataSource",
    "FilterCriteria": "FilterCriterion",
    "HTTPServices": "HTTPService",
    "InformationRegisters": "InformationRegister",
    "Reports": "Report",
    "ScheduledJobs": "ScheduledJob",
    "SettingsStorages": "SettingsStorage",
    "Sequences": "Sequence",
    "SessionParameters": "SessionParameter",
    "Subsystems": "Subsystem",
    "Tasks": "Task",
    "WSReferences": "WSReference",
    "WebServices": "WebService",
}
TYPES = frozenset(SINGULAR) | {"Configuration"}

#: Russian spellings of the types that code and queries name data objects by
#: (`Справочники.X`, `РегистрНакопления.X`) → the dump's directory name.
_RU: dict[str, str] = {
    "Справочник": "Catalogs", "Справочники": "Catalogs",
    "Документ": "Documents", "Документы": "Documents",
    "Обработка": "DataProcessors", "Обработки": "DataProcessors",
    "Отчет": "Reports", "Отчёт": "Reports", "Отчеты": "Reports", "Отчёты": "Reports",
    "РегистрСведений": "InformationRegisters", "РегистрыСведений": "InformationRegisters",
    "РегистрНакопления": "AccumulationRegisters", "РегистрыНакопления": "AccumulationRegisters",
    "РегистрБухгалтерии": "AccountingRegisters", "РегистрыБухгалтерии": "AccountingRegisters",
    "РегистрРасчета": "CalculationRegisters", "РегистрыРасчета": "CalculationRegisters",
    "БизнесПроцесс": "BusinessProcesses", "БизнесПроцессы": "BusinessProcesses",
    "Задача": "Tasks", "Задачи": "Tasks",
    "ПланОбмена": "ExchangePlans", "ПланыОбмена": "ExchangePlans",
    "ПланСчетов": "ChartsOfAccounts", "ПланыСчетов": "ChartsOfAccounts",
    "ПланВидовХарактеристик": "ChartsOfCharacteristicTypes",
    "ПланыВидовХарактеристик": "ChartsOfCharacteristicTypes",
    "ПланВидовРасчета": "ChartsOfCalculationTypes",
    "ПланыВидовРасчета": "ChartsOfCalculationTypes",
    "Перечисление": "Enums", "Перечисления": "Enums",
    "Константа": "Constants", "Константы": "Constants",
    "ЖурналДокументов": "DocumentJournals", "ЖурналыДокументов": "DocumentJournals",
    "Последовательность": "Sequences", "Последовательности": "Sequences",
    "ОбщийМодуль": "CommonModules", "ОбщиеМодули": "CommonModules",
}
#: Data-object types, i.e. the ones a query or a manager call can name.
_DATA = frozenset(_RU.values()) - {"CommonModules"}


def _build_names() -> dict[str, str]:
    out = {k.lower(): v for k, v in _RU.items()}
    for plural, single in SINGULAR.items():
        out[plural.lower()] = plural
        out[single.lower()] = plural
    return out


#: Any spelling (case-blind) of a metadata type → the dump's directory name.
TYPE_BY_NAME: dict[str, str] = _build_names()


def data_object(type_word: str, name: str) -> str | None:
    """`<Type>.<Object>` for ``Type.Object`` as code writes it (any language,
    singular or plural), when Type names a data-object type; else None."""
    plural = TYPE_BY_NAME.get(type_word.lower())
    return f"{plural}.{name}" if plural in _DATA else None


def _tail(rel: str) -> list[str] | None:
    """The path of ``rel`` from its metadata type directory on, without the
    ``Ext`` / ``Form`` folders Designer puts a module under, and with the file
    extension removed. None when no directory of the path is a metadata type."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    stem = parts[-1].rsplit(".", 1)[0]
    dirs = parts[:-1]
    start = next((i for i, p in enumerate(dirs) if p in TYPES), None)
    if start is None:
        return None
    tail = [*dirs[start:], stem]
    return [p for i, p in enumerate(tail) if not (p in ("Ext", "Form") and i < len(tail) - 1)]


def module_identity(rel: str) -> str:
    """The container name of the module stored at ``rel``.

    `CommonModules/<Name>/Ext/Module.bsl` → `<Name>` (callers write
    `Name.Method(`); `Catalogs/<Obj>/Ext/ObjectModule.bsl` →
    `Catalogs.<Obj>.ObjectModule`; a form or command module is named after the
    form or command; a file outside any metadata folder is its stem (a OneScript
    module is called by its file name).
    """
    tail = _tail(rel)
    if tail is None:
        return rel.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if tail[0] == "CommonModules" and len(tail) >= 2:
        return tail[1]
    if tail[-1] in ("Module", "CommandModule") and ("Forms" in tail or "Commands" in tail):
        tail = tail[:-1]  # the form or command is the module's identity
    return ".".join(tail)


def owner_object(rel: str) -> str | None:
    """`<Type>.<Object>` of the metadata object the module at ``rel`` belongs to
    (a common module is its own object: `CommonModules.<Name>`), else None."""
    tail = _tail(rel)
    if tail is None or len(tail) < 2 or tail[0] == "Configuration":
        return None
    return f"{tail[0]}.{tail[1]}"


def any_object(type_word: str, name: str) -> str | None:
    """Like :func:`data_object`, for every metadata type (a subsystem lists
    common modules, roles and the rest as well)."""
    plural = TYPE_BY_NAME.get(type_word.lower())
    return f"{plural}.{name}" if plural else None
