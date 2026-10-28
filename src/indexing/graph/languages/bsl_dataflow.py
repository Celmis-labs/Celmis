"""Data-flow edges of a 1C:Enterprise method: which registers, catalogs and
documents it reads and which it writes — per statement, not per method.

`bsl.py` hands over a method's lines; the edges are READS and WRITES from the
method to a metadata object (raw target `<Type>.<Object>`, the resolver links it
to the object's node when the dump has one; `Registers.<Name>` for a document's
`Движения.<Name>`, whose register kind only the dump knows). Each edge carries
the line of its statement.

READS
    - a table named after `FROM` / `JOIN` (`ИЗ`, `СОЕДИНЕНИЕ`) in query text, a
      comma-continued `FROM` list included: `РегистрНакопления.X.Остатки(&Date)`,
      `Document.X.Lines`, `Catalog.X` — a virtual table or a tabular section is
      the object itself. A string counts as a query when it has `ВЫБРАТЬ` /
      `SELECT` in it or starts with a clause keyword (the continuation of a
      query built from several strings), so a message that merely says "from
      Catalog.X" is not one. `ЗНАЧЕНИЕ(Catalog.X.Item)` is a value, not a table.
    - a manager call in code: `Справочники.X.НайтиПоКоду(`, `РегистрыСведений.X.
      СрезПоследних(`, `Константы.X.Получить()`, `Движения.X.Прочитать()`.

WRITES
    - `Движения.X.Записать()` / `.Добавить()` / `.Очистить()` / `.Загрузить()` /
      `.Записывать = ...`, also through a variable assigned `Движения.X`;
    - a record set, record manager, document, item or group made by a manager
      (`РегистрыСведений.X.СоздатьНаборЗаписей()`, `Документы.X.СоздатьДокумент()`
      ...) and written with `.Записать(` through the variable it was assigned to
      (or straight away: `...СоздатьЭлемент().Записать()`);
    - `Константы.X.Установить(`.

Text is read the way `bsl.py` reads it: comments are dropped, a string is a query
only as a whole (continuation lines start with `|`, a `//` line inside it is
commented out).

What is NOT covered: a variable that is passed on (`ЗаписатьНабор(Набор)`), an
object got by `Ref.ПолучитьОбъект()`, a statement split over lines before its
variable (`Набор =` alone on a line), a table name built at run time, and a query
text that is assembled from pieces with a table in a different string than its
`FROM` keyword.
"""

from __future__ import annotations

import re
from bisect import bisect_right

from src.indexing.graph.extractor import EdgeInfo
from src.indexing.graph.languages.onec import RECORDS_PREFIX, data_object, manager_object

# `\w` is Unicode for a str pattern, so a word may start with Cyrillic; never
# spell these with [A-Za-z].
_NAME = r"[^\W\d]\w*"
_TABLE = rf"(?P<type>{_NAME})\.(?P<name>{_NAME})(?:\.(?P<sub>{_NAME}))?"
_FROM = re.compile(rf"(?<![\w.])(?:ИЗ|FROM|СОЕДИНЕНИЕ|JOIN)\s+{_TABLE}", re.IGNORECASE)
_NEXT_TABLE = re.compile(rf"\s*,\s*{_TABLE}", re.IGNORECASE)
_ALIAS = re.compile(r"\s*(?:(?:КАК|AS)\s+)?(?:[^\W\d]\w*)?", re.IGNORECASE)
_SELECT = re.compile(r"(?<![\w.])(?:ВЫБРАТЬ|SELECT)(?!\w)", re.IGNORECASE)
_CLAUSE_START = re.compile(r"\s*(?:ИЗ|FROM|СОЕДИНЕНИЕ|JOIN)\s", re.IGNORECASE)
_STRING_START = re.compile(r'"|//')

_MANAGER_CALL = re.compile(rf"(?<!\w)(?P<type>{_NAME})\s*\.\s*(?P<name>{_NAME})\s*\.\s*"
                           rf"(?P<op>{_NAME})\s*\(")
_ASSIGN = re.compile(rf"\s*(?:(?:Перем|Var)\s+)?(?P<var>{_NAME})\s*=\s*", re.IGNORECASE)
_MOVEMENTS = r"(?:Движения|RegisterRecords)"
_MOVEMENT = re.compile(rf"(?<!\w){_MOVEMENTS}\s*\.\s*(?P<name>{_NAME})\s*\.\s*(?P<op>{_NAME})"
                       rf"\s*(?P<tail>\(|=(?!=))", re.IGNORECASE)
_MOVEMENT_ALIAS = re.compile(rf"{_MOVEMENTS}\s*\.\s*(?P<name>{_NAME})\s*(?:;|$)", re.IGNORECASE)
_VAR_CALL = re.compile(rf"(?<![\w.])(?P<var>{_NAME})\s*\.\s*(?P<op>{_NAME})\s*(?P<tail>\(|=(?!=))")
_CONDITION = re.compile(r"\s*(?:Если|ИначеЕсли|Пока|If|ElsIf|While)\b", re.IGNORECASE)
_CHAINED_WRITE = re.compile(r"\s*\.\s*(?:Записать|Write)\s*\(", re.IGNORECASE)

#: Methods of a manager that read: the objects' finders and selections, the
#: registers' slices and virtual tables, a constant's getter.
_READ_OPS = frozenset(w.lower() for w in (
    "НайтиПоКоду", "НайтиПоНаименованию", "НайтиПоРеквизиту", "Выбрать", "ВыбратьИерархически",
    "Получить", "ПолучитьПоследнее", "ПолучитьПервое", "СрезПоследних", "СрезПервых",
    "Остатки", "Обороты", "ОстаткиИОбороты",
    "FindByCode", "FindByDescription", "FindByAttribute", "Select", "SelectHierarchically",
    "Get", "GetLast", "GetFirst", "SliceLast", "SliceFirst",
    "Balance", "Turnovers", "BalanceAndTurnovers",
))
#: Methods of a manager that make something to be written later.
_CREATE_OPS = frozenset(w.lower() for w in (
    "СоздатьНаборЗаписей", "СоздатьМенеджерЗаписи", "СоздатьДокумент", "СоздатьЭлемент",
    "СоздатьГруппу",
    "CreateRecordSet", "CreateRecordManager", "CreateDocument", "CreateItem", "CreateFolder",
))
_SET_CONSTANT = frozenset({"установить", "set"})
#: What `Движения.X` (or a variable holding it) does that changes the register.
_MOVEMENT_WRITES = frozenset(w.lower() for w in (
    "Записать", "Добавить", "Очистить", "Загрузить", "Write", "Add", "Clear", "Load"))
_MOVEMENT_FLAGS = frozenset({"записывать", "write"})  # `.Записывать = Истина`
_MOVEMENT_READS = frozenset(w.lower() for w in ("Прочитать", "Выгрузить", "Read", "Unload"))
_PERSIST = frozenset({"записать", "write"})


def data_flow_edges(from_id: str, body: list[tuple[int, str, str]]) -> list[EdgeInfo]:
    """READS / WRITES edges of one method. ``body`` is its lines as
    (0-based line index, raw text, text without comments and with the strings
    emptied)."""
    found: dict[tuple[str, str, int], EdgeInfo] = {}

    def add(kind: str, target: str, line: int) -> None:
        found.setdefault((kind, target, line), EdgeInfo(
            from_id=from_id, to_id=None, kind=kind, confidence="unresolved",
            raw_target=target, line=line))

    for line_of, text in _literals(body):
        for pos, target in _query_tables(text):
            add("READS", target, line_of(pos))
    _statements(body, add)
    return list(found.values())


# ─── query text ─────────────────────────────────────────────────────


def _literals(body: list[tuple[int, str, str]]):  # noqa: ANN202
    """(line-of-offset function, text) of each string literal of the method. A
    literal's lines are joined by newlines, their `|` margin removed, so a table
    split from its keyword by a line break still follows it."""
    in_string = False
    segments: list[tuple[int, str]] = []
    for i, raw, _code in body:
        pos = 0
        start = 0
        if in_string:
            stripped = raw.lstrip()
            if stripped.startswith("//"):
                continue  # a commented-out line inside a multi-line string
            pos = start = len(raw) - len(stripped) + (1 if stripped.startswith("|") else 0)
        while pos < len(raw):
            if in_string:
                end = raw.find('"', pos)
                if end < 0:
                    segments.append((i + 1, raw[start:]))
                    break
                if raw.startswith('""', end):
                    pos = end + 2  # an escaped quote stays inside the string
                    continue
                segments.append((i + 1, raw[start:end]))
                yield _literal(segments)
                segments = []
                in_string = False
                pos = end + 1
                continue
            m = _STRING_START.search(raw, pos)
            if m is None or m.group() == "//":
                break
            in_string = True
            pos = start = m.end()


def _literal(segments: list[tuple[int, str]]):  # noqa: ANN202
    starts: list[int] = []
    offset = 0
    # `|// text` is a comment of the query language: the line is part of the
    # string for BSL, and a table named in it is not read.
    segments = [(line, seg.split("//", 1)[0]) for line, seg in segments]
    for _line, seg in segments:
        starts.append(offset)
        offset += len(seg) + 1
    lines = [line for line, _seg in segments]
    return (lambda pos: lines[max(0, bisect_right(starts, pos) - 1)],
            "\n".join(seg for _line, seg in segments))


def _query_tables(text: str):  # noqa: ANN202
    """(offset, `<Type>.<Object>`) of each table a query string reads."""
    if not (_SELECT.search(text) or _CLAUSE_START.match(text)):
        return
    for m in _FROM.finditer(text):
        target = data_object(m["type"], m["name"])
        if target is None:
            continue
        yield m.start("type"), target
        # `FROM A AS X, B AS Y`: after a table, its parameters and alias, a comma
        # brings the next table.
        pos = m.end()
        while True:
            pos = _after_parameters(text, pos)
            pos = _ALIAS.match(text, pos).end()
            nxt = _NEXT_TABLE.match(text, pos)
            target = nxt and data_object(nxt["type"], nxt["name"])
            if not target:
                break
            yield nxt.start("type"), target
            pos = nxt.end()


def _after_parameters(text: str, pos: int) -> int:
    """``pos`` moved past a virtual table's `(...)` parameters, if they follow."""
    i = pos
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "(":
        return pos
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == "(") - (text[j] == ")")
        if depth == 0:
            return j + 1
    return pos


# ─── code ───────────────────────────────────────────────────────────


def _statements(body: list[tuple[int, str, str]], add) -> None:  # noqa: ANN001
    """Manager reads and writes, line by line, in order: the variable a write
    goes through is known from the assignment above it."""
    held: dict[str, tuple[str, frozenset[str]]] = {}  # variable → (object, ops that write)
    for i, _raw, code in body:
        if "." not in code and "=" not in code:
            continue
        line = i + 1
        condition = bool(_CONDITION.match(code))
        assign = _ASSIGN.match(code)
        if assign:
            var = assign["var"].lower()
            held.pop(var, None)  # reassigned: no longer what it was
            rest = code[assign.end():]
            alias = _MOVEMENT_ALIAS.match(rest)
            if alias:
                held[var] = (RECORDS_PREFIX + alias["name"], _MOVEMENT_WRITES | _MOVEMENT_FLAGS)
            created = _MANAGER_CALL.match(rest)
            if created and created["op"].lower() in _CREATE_OPS:
                target = manager_object(created["type"], created["name"])
                if target:
                    held[var] = (target, _PERSIST)

        for m in _MANAGER_CALL.finditer(code):
            op = m["op"].lower()
            target = manager_object(m["type"], m["name"])
            if target is None:
                continue
            if op in _READ_OPS:
                add("READS", target, line)
            elif _written_at_once(op, target, code, m.end() - 1):
                add("WRITES", target, line)

        for m in _MOVEMENT.finditer(code):
            op, target = m["op"].lower(), RECORDS_PREFIX + m["name"]
            if m["tail"] == "(" and op in _MOVEMENT_WRITES:
                add("WRITES", target, line)
            elif m["tail"] == "(" and op in _MOVEMENT_READS:
                add("READS", target, line)
            elif m["tail"] != "(" and op in _MOVEMENT_FLAGS and not condition:
                add("WRITES", target, line)

        if held:
            for m in _VAR_CALL.finditer(code):
                entry = held.get(m["var"].lower())
                if entry is None or m["op"].lower() not in entry[1]:
                    continue
                if m["tail"] != "(" and condition:
                    continue  # `Если Набор.Записывать = Истина Тогда` compares
                add("WRITES", entry[0], line)


def _written_at_once(op: str, target: str, code: str, open_at: int) -> bool:
    """The manager call whose parenthesis opens at ``open_at`` writes by itself:
    a constant's `Установить(`, or a `СоздатьЭлемент()` with `.Записать(` chained."""
    if op in _SET_CONSTANT:
        return target.startswith("Constants.")
    if op in _CREATE_OPS:
        return _CHAINED_WRITE.match(code, open_at + _closing(code, open_at)) is not None
    return False


def _closing(code: str, open_at: int) -> int:
    """Offset from ``open_at`` (an opening parenthesis) to the character after
    its closing one; to the end of the line when it does not close there."""
    depth = 0
    for j in range(open_at, len(code)):
        depth += (code[j] == "(") - (code[j] == ")")
        if depth == 0:
            return j + 1 - open_at
    return len(code) - open_at
