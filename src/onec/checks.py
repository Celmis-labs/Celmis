"""Static checks of an external file's code against a configuration.

Nothing is run: every finding comes from reading the text. An `error` is
something the platform would refuse or fail on (a method that is not there or
not exported, a wrong number of arguments, a metadata object or an enum value
the configuration lacks, a handler with no procedure, an unbalanced block); a
`warning` is likely wrong but may be fine (a query field the object's metadata
does not list, a template area the file's templates do not name).

Names are matched without regard to case, as the platform does.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from src.indexing.graph.languages.onec import MANAGER_BY_NAME, TYPE_BY_NAME
from src.onec.bsl_text import (
    Procedure,
    _closing,
    procedures,
    split_lines,
    split_top,
    string_literals,
    strip_module,
)
from src.onec.config import Config
from src.onec.external import ExternalFile, handlers_of

MAX_FINDINGS = 400


@dataclass
class Finding:
    level: str   # "error" | "warning"
    path: str
    line: int
    message: str
    pre_existing: bool = False  # the configuration's counterpart has the same problem
    #: What kind of finding this is, for a report that words it itself:
    #: missing_object | missing_method | not_exported | arity | missing_enum_value |
    #: missing_predefined | missing_template | unbalanced | no_end | handler | other.
    code: str = "other"
    subject: str = ""            # the name the finding is about (an object, a method)
    data: dict = field(default_factory=dict)


@dataclass
class Stats:
    """What the checks looked at, in numbers: a clean line of a report says "75
    objects exist", not just "nothing found"."""

    #: (type directory, lower-case name) → the configuration's spelling, or None when absent
    refs: dict[tuple[str, str], str | None] = field(default_factory=dict)
    ref_names: dict[tuple[str, str], str] = field(default_factory=dict)  # as written in code
    calls_total: int = 0
    calls_bad: int = 0
    handlers_total: int = 0
    handlers_missing: int = 0
    #: module path → {"Процедура": (opened, closed), …} for procedures, functions and blocks
    balance: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)
    areas_used: set[str] = field(default_factory=set)
    areas_missing: set[str] = field(default_factory=set)
    fields_checked: int = 0
    fields_missing: int = 0

    def ref(self, plural: str, written: str, real: str | None) -> None:
        key = (plural, written.lower())
        self.ref_names.setdefault(key, written)
        if real is not None or key not in self.refs:
            self.refs[key] = real


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    checked: list[str] = field(default_factory=list)   # what was looked at
    skipped: list[str] = field(default_factory=list)   # what could not be
    truncated: int = 0

    def add(self, f: Finding) -> None:
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(f)
        else:
            self.truncated += 1

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "warning")


def _lower(words: str) -> frozenset[str]:
    return frozenset(w.lower() for w in words.split())


#: Methods every manager of a kind has without a manager module.
_MANAGER_METHODS = _lower("""
НайтиПоКоду НайтиПоНаименованию НайтиПоРеквизиту НайтиПоНомеру Выбрать ВыбратьИерархически
СоздатьЭлемент СоздатьГруппу СоздатьДокумент СоздатьНаборЗаписей СоздатьМенеджерЗаписи
СоздатьСчет СоздатьВидРасчета СоздатьБизнесПроцесс СоздатьЗадачу СоздатьУзел Создать
ПолучитьФорму ПолучитьФормуВыбора ПолучитьФормуСписка ПолучитьФормуНовогоОбъекта ПолучитьМакет
ПустаяСсылка ПолучитьСсылку ПолучитьИмяПредопределенного СрезПоследних СрезПервых Получить
ПолучитьПоследнее ПолучитьПервое Остатки Обороты ОстаткиИОбороты Движения Установить
СоздатьМенеджерЗначения Индекс ЭтотУзел ЗарегистрироватьИзменения УдалитьРегистрациюИзменений
ВыбратьИзменения ЗаписатьИзменения ПрочитатьИзменения ЗаписатьОбновленияРегистрации
ЗаписатьДанные ПрочитатьДанные Сохранить Загрузить Печать ПолучитьОбластьПредставления
ФормаСписка ФормаВыбора Количество
FindByCode FindByDescription FindByAttribute FindByNumber Select SelectHierarchically
CreateItem CreateFolder CreateDocument CreateRecordSet CreateRecordManager CreateAccount
CreateCalculationType CreateBusinessProcess CreateTask CreateNode Create GetForm GetChoiceForm
GetListForm GetTemplate EmptyRef GetRef GetPredefinedItemName SliceLast SliceFirst Get GetLast
GetFirst Balance Turnovers BalanceAndTurnovers Set CreateValueManager IndexOf ThisNode
RecordChanges DeleteChangeRecords SelectChanges Count
""")
#: Fields every object of a kind has without listing them.
_STANDARD_FIELDS = _lower("""
Ссылка Наименование Код ПометкаУдаления Предопределенный ИмяПредопределенныхДанных Родитель
Владелец ЭтоГруппа Номер Дата Проведен Период Регистратор НомерСтроки Активность ВидДвижения
Представление ВерсияДанных Счет ВидСубконто Субконто СчетДт СчетКт Тип ТипЗначения Порядок
Стартован Завершен ВедущаяЗадача БизнесПроцесс ТочкаМаршрута Выполнена Исполнитель Значение
ПериодРегистрации ПериодДействия ПериодДействияБазовый БазовыйПериодНачало БазовыйПериодКонец
Сторно ВидРасчета Ссылка ТекущаяСсылка Узел НомерОтправленного НомерПринятого ЭтотУзел
Ref Description Code DeletionMark Predefined PredefinedDataName Parent Owner IsFolder Number
Date Posted Period Recorder LineNumber Active RecordType Presentation DataVersion Account
Value Order Started Completed Executed Performer
""")
#: Tables a query can read from a register or another object besides the object itself.
_VIRTUAL_TABLES = _lower("""
Остатки Обороты ОстаткиИОбороты СрезПоследних СрезПервых ДвиженияССубконто Субконто
ОборотыДтКт ДанныеГрафика ФактическийПериодДействия БазаПериодДействия База Изменения
ВидыСубконто ТочкиМаршрута Представления
Balance Turnovers BalanceAndTurnovers SliceLast SliceFirst RecordsWithExtDimensions ExtDimensions
DrCrTurnovers ScheduleData ActualActionPeriod Changes
""")
_BLOCKS = (
    ("Если", _lower("Если If"), _lower("КонецЕсли EndIf")),
    ("Цикл", _lower("Для For Пока While"), _lower("КонецЦикла EndDo")),
    ("Попытка", _lower("Попытка Try"), _lower("КонецПопытки EndTry")),
)
_WORD = re.compile(r"(?<![\w.~&#])([^\W\d]\w*)(?![\w])")
_PREPROCESSOR = (
    ("#Область", _lower("#Область #Region"), _lower("#КонецОбласти #EndRegion")),
    ("#Если", _lower("#Если #If"), _lower("#КонецЕсли #EndIf")),
)
_CALL = re.compile(r"(?<![\w.])([^\W\d]\w*)\s*\.\s*([^\W\d]\w*)\s*\(")
_MANAGER_REF = re.compile(
    r"(?<![\w.])([^\W\d]\w*)\s*\.\s*([^\W\d]\w*)(?:\s*\.\s*([^\W\d]\w*)\s*(\()?)?")
_LOCAL_CALL = re.compile(r"(?<![\w.~&])([^\W\d]\w*)\s*\(")
#: A metadata path as a query or a type string writes it.
_TYPE_SUFFIX = ("Ссылка", "Объект", "Менеджер", "Выборка", "Список", "НаборЗаписей",
                "МенеджерЗаписи", "Запись", "Ref", "Object", "Manager", "Selection", "List",
                "RecordSet", "RecordManager", "Record")
_TYPE_PATH = re.compile(r"\s*[^\W\d]\w*(?:\.[^\W\d]\w*)+\s*")
_PATH_PARTS = _lower("Форма Form Макет Template Команда Command ТабличнаяЧасть TabularSection "
                     "Реквизит Attribute")
_QUERY = re.compile(r"\b(?:ВЫБРАТЬ|SELECT)\b", re.IGNORECASE)
_TABLE = re.compile(r"(?<![\w.&])([^\W\d]\w*)\.([^\W\d]\w*)(?:\.([^\W\d]\w*))?")
_ALIAS = re.compile(
    r"(?<![\w.&])([^\W\d]\w*)\.([^\W\d]\w*)(?:\.([^\W\d]\w*))?\s*(?:\([^()]*(?:\([^()]*\)[^()]*)*\))?"
    r"\s+(?:КАК|AS)\s+([^\W\d]\w*)", re.IGNORECASE)
_FIELD = re.compile(r"(?<![\w.&])([^\W\d]\w*)\.([^\W\d]\w*)")
_PREDEF = re.compile(
    r"(?:ЗНАЧЕНИЕ|VALUE|ПредопределенноеЗначение|PredefinedValue)\s*\(\s*\"?"
    r"([^\W\d]\w*)\.([^\W\d]\w*)\.([^\W\d]\w*)", re.IGNORECASE)
_GET_TEMPLATE = re.compile(
    r"(?:(?<![\w.])([^\W\d]\w*)\s*\.\s*([^\W\d]\w*)\s*\.\s*)?"
    r"(?:ПолучитьМакет|GetTemplate)\s*\(\s*\"([^\"]+)\"", re.IGNORECASE)
_GET_AREA = re.compile(r"\.\s*(?:ПолучитьОбласть|GetArea)\s*\(\s*\"([^\"]+)\"", re.IGNORECASE)
_PARAM = re.compile(r"\.\s*(?:Параметры|Parameters)\s*\.\s*([^\W\d]\w*)(?!\s*\()", re.IGNORECASE)
_SKIP_ENUM = _lower("ПустаяСсылка EmptyRef")


class _Offsets:
    """Offset in a module's text joined by newlines → 1-based line."""

    def __init__(self, lines: list[str]) -> None:
        self.starts, pos = [], 0
        for ln in lines:
            self.starts.append(pos)
            pos += len(ln) + 1

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def _args(code: str, open_at: int) -> int | None:
    """How many arguments the call whose parenthesis opens at ``open_at`` passes."""
    close = _closing(code, open_at)
    if close < 0:
        return None
    inner = code[open_at + 1:close]
    return len(split_top(inner)) if inner.strip() else 0


def _arity(proc: Procedure, n: int) -> str:
    if n > proc.max_args:
        return f"passes {n} argument(s), {proc.name} takes at most {proc.max_args}"
    if n < proc.min_args:
        return f"passes {n} argument(s), {proc.name} needs at least {proc.min_args}"
    return ""


class Checker:
    def __init__(self, ext: ExternalFile, cfg: Config | None) -> None:
        self.ext, self.cfg = ext, cfg
        self.result = CheckResult()
        self._modules: dict[tuple[str, str, str], dict[str, Procedure] | None] = {}

    # ── the configuration's modules ─────────────────────────────────

    def _procs(self, kind: str, name: str, module: str) -> dict[str, Procedure] | None:
        key = (kind, name.lower(), module)
        if key not in self._modules:
            got = self.cfg.module(kind, name, module) if self.cfg else None
            self._modules[key] = ({p.name.lower(): p for p in procedures(got[1])}
                                  if got else None)
        return self._modules[key]

    # ── the checks ──────────────────────────────────────────────────

    def run(self) -> CheckResult:
        r = self.result
        modules = [m for m in self.ext.modules if m.text is not None]
        if not modules:
            r.skipped.append("there is no code to check")
        for m in modules:
            self._module(m.path, m.text or "")
        self._handlers()
        if self.cfg is None:
            r.skipped.append("checks against a configuration: no configuration was given")
        r.checked = [
            "balance of Если/Для/Пока/Попытка blocks and #Область/#Если in every procedure",
            "calls of the module's own procedures: the number of arguments",
            "form event handlers: a procedure for every bound handler",
            "templates, areas and parameters the code names",
        ]
        if self.cfg is not None:
            r.checked += [
                "metadata objects named in code, queries and type strings",
                "common module calls: the method exists, is exported, the number of arguments",
                "manager calls: a standard method or an exported one of the manager module",
                "enum values in code, ЗНАЧЕНИЕ() and ПредопределенноеЗначение()",
                "query fields against the object's attributes (warnings)",
            ]
        return r

    def _module(self, path: str, text: str) -> None:
        procs = procedures(text)
        local = {p.name.lower(): p for p in procs}
        stripped = strip_module(text)
        self._balance(path, text, procs, stripped)
        joined = "\n".join(stripped)
        at = _Offsets(stripped)
        self._local_calls(path, joined, at, local)
        if self.cfg is not None:
            self._calls(path, joined, at)
            self._manager_refs(path, joined, at)
        literals = string_literals(text)
        if self.cfg is not None:
            self._strings(path, literals)
        kept = "\n".join(code for code, _c in split_lines(text))
        self._templates(path, kept, _Offsets(kept.split("\n")))

    def _balance(self, path: str, text: str, procs: list[Procedure],
                 stripped: list[str]) -> None:
        raw = text.replace("\r\n", "\n").split("\n")
        for p in procs:
            last = stripped[p.end - 1].strip() if p.end - 1 < len(stripped) else ""
            is_fn = p.kind.lower() in ("функция", "function")
            bal = self.result.stats.balance.setdefault(path, {})
            kind_name = "Функция" if is_fn else "Процедура"
            o, c = bal.get(kind_name, (0, 0))
            if not re.match(r"(?:КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b",
                            last, re.IGNORECASE):
                bal[kind_name] = (o + 1, c)
                self.result.add(Finding("error", path, p.start,
                                        f"{p.name}: no КонецПроцедуры/КонецФункции",
                                        code="no_end", subject=p.name))
                continue
            bal[kind_name] = (o + 1, c + 1)
            counts = {name: 0 for name, _o, _c in _BLOCKS}
            opened = {name: 0 for name, _o, _c in _BLOCKS}
            closed = {name: 0 for name, _o, _c in _BLOCKS}
            for i in range(p.start, p.end - 1):
                code = stripped[i]
                if code.lstrip().startswith("#"):
                    continue
                for w in _WORD.findall(code):
                    lw = w.lower()
                    for name, opens, closes in _BLOCKS:
                        if lw in opens:
                            counts[name] += 1
                            opened[name] += 1
                        elif lw in closes:
                            counts[name] -= 1
                            closed[name] += 1
            for name in counts:
                o, c = bal.get(name, (0, 0))
                bal[name] = (o + opened[name], c + closed[name])
            for name, n in counts.items():
                if n:
                    self.result.add(Finding(
                        "error", path, p.start,
                        f"{p.name}: {abs(n)} {name} block(s) "
                        + ("not closed" if n > 0 else "closed without being opened"),
                        code="unbalanced", subject=p.name, data={"block": name, "n": n}))
        depth = {name: 0 for name, _o, _c in _PREPROCESSOR}
        for ln in raw:
            word = ln.strip().split(" ")[0].split("\t")[0].lower()
            for name, opens, closes in _PREPROCESSOR:
                if word in opens:
                    depth[name] += 1
                elif word in closes:
                    depth[name] -= 1
        for name, n in depth.items():
            if n:
                self.result.add(Finding("error", path, 1, f"module: {abs(n)} {name} "
                                        + ("not closed" if n > 0 else "closed too often"),
                                        code="unbalanced", subject=name,
                                        data={"block": name, "n": n}))

    def _local_calls(self, path: str, joined: str, at: _Offsets,
                     local: dict[str, Procedure]) -> None:
        for m in _LOCAL_CALL.finditer(joined):
            proc = local.get(m.group(1).lower())
            if proc is None:
                continue
            line = at.line(m.start())
            if line == proc.start:
                continue  # the header itself
            n = _args(joined, m.end() - 1)
            problem = _arity(proc, n) if n is not None else ""
            if problem:
                self.result.add(Finding("error", path, line, f"call of {proc.name}: {problem}"))

    def _calls(self, path: str, joined: str, at: _Offsets) -> None:
        cfg = self.cfg
        for m in _CALL.finditer(joined):
            module, method = m.group(1), m.group(2)
            real = cfg.find("CommonModules", module)
            if real is None:
                continue
            procs = self._procs("CommonModules", real, "Module")
            line = at.line(m.start())
            if procs is None:
                self.result.skipped.append(f"common module {real}: its code could not be read")
                continue
            st = self.result.stats
            st.calls_total += 1
            proc = procs.get(method.lower())
            if proc is None:
                st.calls_bad += 1
                self.result.add(Finding("error", path, line,
                                        f"{real}.{method}: no such method in common module "
                                        f"{real}", code="missing_method",
                                        subject=f"{real}.{method}", data={"module": real}))
                continue
            if not proc.exported:
                st.calls_bad += 1
                self.result.add(Finding("error", path, line,
                                        f"{real}.{proc.name}: the method is not exported",
                                        code="not_exported", subject=f"{real}.{proc.name}"))
                continue
            n = _args(joined, m.end() - 1)
            problem = _arity(proc, n) if n is not None else ""
            if problem:
                st.calls_bad += 1
                self.result.add(Finding("error", path, line, f"{real}.{proc.name}: {problem}; "
                                        f"signature {proc.signature()}", code="arity",
                                        subject=f"{real}.{proc.name}",
                                        data={"passed": n, "signature": proc.signature()}))

    def _manager_refs(self, path: str, joined: str, at: _Offsets) -> None:
        cfg = self.cfg
        for m in _MANAGER_REF.finditer(joined):
            plural = MANAGER_BY_NAME.get(m.group(1).lower())
            if plural is None:
                continue
            name, member, call = m.group(2), m.group(3), m.group(4)
            line = at.line(m.start())
            real = cfg.find(plural, name)
            if plural != "CommonModules":
                self.result.stats.ref(plural, name, real)
            if real is None:
                if plural == "CommonModules":
                    continue
                self.result.add(Finding("error", path, line,
                                        f"{m.group(1)}.{name}: the configuration has no "
                                        f"{plural[:-1] if plural.endswith('s') else plural} "
                                        f"named {name}", code="missing_object",
                                        subject=f"{m.group(1)}.{name}",
                                        data={"plural": plural, "name": name, "where": "code"}))
                continue
            if member is None:
                continue
            if plural == "Enums" and not call:
                if member.lower() in _SKIP_ENUM:
                    continue
                values = cfg.enum_values(real)
                if values is not None and member.lower() not in values:
                    self.result.add(Finding("error", path, line,
                                            f"Перечисления.{real}.{member}: the enum has no "
                                            "such value", code="missing_enum_value",
                                            subject=f"{real}.{member}",
                                            data={"enum": real, "value": member}))
                continue
            if not call or member.lower() in _MANAGER_METHODS:
                continue
            procs = self._procs(plural, real, "ManagerModule")
            proc = procs.get(member.lower()) if procs else None
            if proc is None:
                self.result.add(Finding("warning", path, line,
                                        f"{m.group(1)}.{real}.{member}: not a standard manager "
                                        "method, and the manager module does not define it"))
            elif not proc.exported:
                self.result.add(Finding("error", path, line,
                                        f"{m.group(1)}.{real}.{proc.name}: the manager module's "
                                        "method is not exported"))
            else:
                n = _args(joined, m.end() - 1)
                problem = _arity(proc, n) if n is not None else ""
                if problem:
                    self.result.add(Finding("error", path, line,
                                            f"{m.group(1)}.{real}.{proc.name}: {problem}"))

    def _object_word(self, word: str) -> str | None:
        """The type directory a query or type word names: `Справочник`,
        `СправочникСсылка`, `CatalogRef` → `Catalogs`."""
        plural = TYPE_BY_NAME.get(word.lower())
        if plural:
            return plural
        for suffix in _TYPE_SUFFIX:
            if word.endswith(suffix) and len(word) > len(suffix):
                plural = TYPE_BY_NAME.get(word[: -len(suffix)].lower())
                if plural:
                    return plural
        return None

    def _is_metadata_path(self, value: str) -> bool:
        """A string that names metadata, not a form's data path: a type name
        (`СправочникСсылка.X`) or a path into an object (`Справочник.X.Форма.Y`);
        `Отчет.КомпоновщикНастроек` is the form's main attribute, not the metadata."""
        parts = value.strip().split(".")
        head = parts[0]
        if any(head.endswith(sfx) and len(head) > len(sfx) for sfx in _TYPE_SUFFIX):
            return self._object_word(head) is not None
        return len(parts) >= 4 and parts[2].lower() in _PATH_PARTS

    def _strings(self, path: str, literals: list[tuple[int, str]]) -> None:
        cfg = self.cfg
        for line, value in literals:
            is_query = bool(_QUERY.search(value))
            is_path = bool(_TYPE_PATH.fullmatch(value)) and self._is_metadata_path(value)
            upper = value.upper()
            if not (is_query or is_path) and "ЗНАЧЕНИЕ(" not in upper and "VALUE(" not in upper:
                continue
            self._predefined(path, line, value)
            if not (is_query or is_path):
                continue
            aliases: dict[str, tuple[str, str, str | None]] = {}
            alias_words = ({a.lower() for a in re.findall(r"(?:КАК|AS)\s+([^\W\d]\w*)", value,
                                                          re.IGNORECASE)}
                           if is_query else set())
            for m in _TABLE.finditer(value):
                plural = self._object_word(m.group(1))
                if plural is None or plural in ("CommonModules",):
                    continue
                real = cfg.find(plural, m.group(2))
                at = line + value[:m.start()].count("\n")
                if real is None and m.group(1).lower() in alias_words:
                    continue  # `… КАК Документ` makes `Документ.Номер` a field
                self.result.stats.ref(plural, m.group(2), real)
                if real is None:
                    self.result.add(Finding("error", path, at,
                                            f"{'query' if is_query else 'string'}: "
                                            f"{m.group(1)}.{m.group(2)}: the configuration has no "
                                            f"such object", code="missing_object",
                                            subject=f"{m.group(1)}.{m.group(2)}",
                                            data={"plural": plural, "name": m.group(2),
                                                  "where": "query" if is_query else "string",
                                                  "sub": m.group(3) or ""}))
                    continue
                sub = m.group(3)
                in_value = bool(re.search(r"(?:ЗНАЧЕНИЕ|VALUE)\s*\(\s*$", value[:m.start()],
                                          re.IGNORECASE))
                if (is_query and sub and plural != "Enums" and not in_value
                        and sub.lower() not in _VIRTUAL_TABLES
                        and sub.lower() not in cfg.children(plural, real)
                        and sub.lower() not in _STANDARD_FIELDS
                        and cfg.children(plural, real)):
                    self.result.add(Finding("warning", path, at,
                                            f"query: {m.group(1)}.{real}.{sub}: neither a "
                                            "tabular section nor a virtual table of the object"))
            if not is_query:
                continue
            for m in _ALIAS.finditer(value):
                plural = self._object_word(m.group(1))
                if plural and cfg.find(plural, m.group(2)):
                    aliases[m.group(4).lower()] = (plural, cfg.find(plural, m.group(2)) or "",
                                                   m.group(3))
            for m in _FIELD.finditer(value):
                target = aliases.get(m.group(1).lower())
                if target is None:
                    continue
                plural, real, sub = target
                if sub and sub.lower() in _VIRTUAL_TABLES:
                    continue  # virtual tables add fields (…Остаток, …Оборот) of their own
                known = cfg.children(plural, real)
                fld = m.group(2).lower()
                if known:
                    self.result.stats.fields_checked += 1
                if known and fld not in known and fld not in _STANDARD_FIELDS:
                    self.result.stats.fields_missing += 1
                    self.result.add(Finding("warning", path,
                                            line + value[:m.start()].count("\n"),
                                            f"query: {m.group(1)}.{m.group(2)}: {plural}.{real} "
                                            "lists no such attribute", code="missing_field",
                                            subject=f"{real}.{m.group(2)}"))

    def _predefined(self, path: str, line: int, value: str) -> None:
        """`ЗНАЧЕНИЕ(Перечисление.X.Значение)`, `ЗНАЧЕНИЕ(Справочник.X.Предопределенный)`
        and the same in `ПредопределенноеЗначение("…")`."""
        cfg = self.cfg
        for m in _PREDEF.finditer(value):
            plural = self._object_word(m.group(1))
            item = m.group(3)
            if plural is None or item.lower() in _SKIP_ENUM:
                continue
            real = cfg.find(plural, m.group(2))
            if real is None:
                continue  # the table check reports a missing object
            known = cfg.enum_values(real) if plural == "Enums" else cfg.predefined(plural, real)
            if known is not None and item.lower() not in known:
                what = "the enum has no such value" if plural == "Enums" else (
                    "no predefined item of that name")
                self.result.add(Finding("error", path, line + value[:m.start()].count("\n"),
                                        f"{m.group(1)}.{m.group(2)}.{item}: {what}",
                                        code=("missing_enum_value" if plural == "Enums"
                                              else "missing_predefined"),
                                        subject=f"{real}.{item}",
                                        data={"enum": real, "value": item}))

    def _templates(self, path: str, kept: str, at: _Offsets) -> None:
        own = {t.name.lower() for t in self.ext.templates}
        areas = {a.lower() for t in self.ext.templates for a in t.areas}
        params = {p.lower() for t in self.ext.templates for p in t.params}
        sheets = any(t.kind == "spreadsheet" for t in self.ext.templates)
        for m in _GET_TEMPLATE.finditer(kept):
            plural = MANAGER_BY_NAME.get((m.group(1) or "").lower())
            name = m.group(3)
            line = at.line(m.start())
            if plural and self.cfg is not None:
                real = self.cfg.find(plural, m.group(2) or "")
                if real and name.lower() not in self.cfg.templates(plural, real):
                    self.result.add(Finding("error", path, line,
                                            f"{plural}.{real} has no template {name}",
                                            code="missing_template", subject=name))
            elif not plural and name.lower() not in own:
                self.result.add(Finding("error", path, line,
                                        f"ПолучитьМакет(\"{name}\"): the file has no template "
                                        f"named {name}", code="missing_template", subject=name))
        if not sheets:
            return
        for m in _GET_AREA.finditer(kept):
            for part in m.group(1).split("|"):
                if part:
                    self.result.stats.areas_used.add(part)
                if part and part.lower() not in areas:
                    self.result.stats.areas_missing.add(part)
                    self.result.add(Finding("warning", path, at.line(m.start()),
                                            f"ПолучитьОбласть(\"{m.group(1)}\"): no template of "
                                            f"the file has an area {part}"))
        if not params:
            return
        for m in _PARAM.finditer(kept):
            name = m.group(1)
            if name.lower() in ("заполнить", "fill") or name.lower() in params:
                continue
            self.result.add(Finding("warning", path, at.line(m.start()),
                                    f".Параметры.{name}: no template of the file has a "
                                    "parameter of that name"))

    def _handlers(self) -> None:
        kind, name = self.ext.kind, self.ext.name
        for form in self.ext.forms:
            if not form.handlers:
                continue
            module = form.module
            if module is None or module.text is None:
                self.result.skipped.append(f"form {form.name}: no module text to look for "
                                           "its handlers in")
                continue
            have = {p.name.lower() for p in procedures(module.text)}
            missing = [h for h in form.handlers if h.lower() not in have]
            self.result.stats.handlers_total += len(form.handlers)
            if not missing:
                continue
            theirs_missing: set[str] = set()
            if self.cfg is not None and kind in ("Reports", "DataProcessors"):
                real = self.cfg.find(kind, name)
                got = self.cfg.form(kind, real, form.name) if real else None
                if got and got[1] is not None:
                    theirs = {p.name.lower() for p in procedures(got[1])}
                    bound = {h.lower() for h in handlers_of(got[2])}
                    theirs_missing = {h for h in bound if h not in theirs}
            for h in missing:
                pre = h.lower() in theirs_missing
                self.result.stats.handlers_missing += 1
                self.result.add(Finding(
                    "error", module.path, 1,
                    f"form {form.name}: handler {h} is bound to an event but the module has no "
                    "such procedure" + (" (the configuration's form has the same problem)"
                                        if pre else ""), pre_existing=pre,
                    code="handler", subject=h, data={"form": form.name}))


def check(ext: ExternalFile, cfg: Config | None) -> CheckResult:
    return Checker(ext, cfg).run()
