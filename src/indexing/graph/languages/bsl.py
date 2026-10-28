"""1C:Enterprise (BSL) extractor — `.bsl` modules and OneScript `.os` files.

BSL has no tree-sitter grammar in the language pack, so this adapter reads the
text line by line. That is enough for what a code-intelligence graph needs from
it: the methods of a module, who calls them, and which object a module belongs
to.

Covers:
    Procedure / Function (Процедура / Функция) with an optional Async (Асинх)
    prefix, an Export (Экспорт) flag, the compiler directives above the header
    (`&AtServer`, `&НаКлиенте`, `&Перед("Name")` ...) and the `//` comment block
    above them as the docstring. Keywords are case-insensitive and may be
    written in Russian or English.

Module identity (the container every method name is qualified with) comes from
where the file sits in a configuration dump:

    Designer dump   <Type>/<Object>/Ext/<Kind>.bsl                 Catalogs.Items.ObjectModule
                    <Type>/<Object>/Forms/<Form>/Ext/Form/Module.bsl   Catalogs.Items.Forms.<Form>
                    <Type>/<Object>/Commands/<Cmd>/Ext/CommandModule.bsl
                    CommonModules/<Name>/Ext/Module.bsl            <Name>
    EDT layout      src/<Type>/<Object>/<Kind>.bsl, .../Forms/<Form>/Module.bsl,
                    src/CommonModules/<Name>/Module.bsl
    anything else   the file stem (a OneScript module is called by its file name)

A method symbol is named `<container>.<Method>`, the way a Python method is
named `<Class>.<method>`, so `find` matches the method, the object and the
`CommonModule.Method` spelling a caller writes.

Symbol model:
    file_module  — one per file, named after the container
    function     — a procedure or function of a common (or plain) module
    method       — a procedure or function of an object, manager, form ... module

Edge model:
    DEFINED_IN   — method → file_module; file_module → the metadata object the
                   module belongs to (`Catalogs.Items`, see `onec_metadata.py`)
    REFERENCES   — method → each data object it names, in code or in query text
                   (`FROM AccumulationRegister.Stock`, `Catalogs.Items.FindByCode(`)
    READS,       — method → the data object a statement reads or writes, with
    WRITES         the statement's line: query tables, manager reads, register
                   movements, record sets (see `bsl_dataflow.py`)
    CALLS        — `Name(` inside the same module (resolved here), `Module.Name(`
                   and `Manager.Object.Name(` (left for the resolver, which
                   matches BSL names case-insensitively)

What is NOT covered:
    - calls through a variable (`Query.Execute()`), global-context functions
      and the platform's own methods: they have no definition in the tree
    - `Perem` (module variables), `#Region`s as containers
    - a method header split by `#If ... #Else` into two alternative headers
      (both are read; the second gets a `@line` suffix on its id)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo
from src.indexing.graph.languages.bsl_dataflow import data_flow_edges
from src.indexing.graph.languages.onec import (
    data_object,
    module_identity,
    owner_object,
)

logger = logging.getLogger(__name__)

#: 1C modules reach several MB; past this the file is skipped and named in
#: `parse_errors` rather than read into memory line by line.
MAX_BSL_BYTES = 32 * 1024 * 1024
_MAX_DOC_CHARS = 1500

_HEADER = re.compile(
    r"^\s*(?:(?P<async>Асинх|Async)\s+)?(?P<kw>Процедура|Функция|Procedure|Function)\s+"
    r"(?P<name>\w+)\s*(?P<rest>\(.*)?$",
    re.IGNORECASE,
)
_END = re.compile(r"^\s*(?:КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE)
_EXPORT = re.compile(r"\)\s*(?:Экспорт|Export)\b", re.IGNORECASE)
_NEW = re.compile(r"\b(?:Новый|New)\s+[\w.]+", re.IGNORECASE)
_CHAIN = re.compile(r"(?<![\w.])(\w+(?:\.\w+)*)\s*\(")
_DATA_REF = re.compile(r"(?<!\w)([^\W\d]\w*)(?=\.([^\W\d]\w*))")
_STRING_START = re.compile(r'"|//')

#: Words that can stand in front of a parenthesis without being a call.
_KEYWORDS = frozenset(w.lower() for w in (
    "Если", "If", "ИначеЕсли", "ElsIf", "Пока", "While", "Для", "For", "Каждого", "Each",
    "Не", "Not", "И", "And", "Или", "Or", "Возврат", "Return", "Новый", "New", "Тогда", "Then",
    "Выполнить", "Execute", "Вычислить", "Eval", "Из", "In", "По", "To", "Цикл", "Do",
    "Исключение", "Except", "Попытка", "Try", "ВызватьИсключение", "Raise", "Перейти", "Goto",
    "Процедура", "Procedure", "Функция", "Function", "Знач", "Val", "Перем", "Var",
))
_SELF = frozenset(w.lower() for w in ("ЭтотОбъект", "ThisObject", "ЭтаФорма", "ThisForm"))

def _strip(line: str, in_string: bool) -> tuple[str, bool]:
    """``line`` without its comment and with each string literal emptied to ``""``.

    ``in_string`` says the previous line ended inside a string literal (BSL
    strings continue on lines that start with ``|``, and a line there that
    starts with ``//`` is commented out, quotes and all); the new state is
    returned.
    """
    if not in_string and '"' not in line and "//" not in line:
        return line, False
    out: list[str] = []
    pos = 0
    if in_string:
        stripped = line.lstrip()
        if stripped.startswith("//"):
            return "", True  # a commented-out line inside a multi-line string
        pos = len(line) - len(stripped) + (1 if stripped.startswith("|") else 0)
    while pos < len(line):
        if in_string:
            end = line.find('"', pos)
            if end < 0:
                return "".join(out), True
            if line.startswith('""', end):
                pos = end + 2  # an escaped quote stays inside the string
                continue
            pos = end + 1
            in_string = False
            out.append('""')
            continue
        m = _STRING_START.search(line, pos)
        if m is None:
            out.append(line[pos:])
            break
        out.append(line[pos:m.start()])
        if m.group() == "//":
            break
        in_string = True
        pos = m.end()
    return "".join(out), in_string


class BslExtractor(SymbolExtractor):
    language = "bsl"
    extensions = (".bsl", ".os")

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        rel = self._relpath(file_path)
        if source is None:
            try:
                if file_path.stat().st_size > MAX_BSL_BYTES:
                    return ExtractionResult(parse_errors=[f"too_large file={rel}"])
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])
        elif len(source) > MAX_BSL_BYTES:
            return ExtractionResult(parse_errors=[f"too_large file={rel}"])

        text = source.decode("utf-8-sig", errors="replace")
        lines = text.replace("\r\n", "\n").split("\n")  # a lone CR is not a line break
        container = module_identity(rel)
        kind = "method" if "." in container else "function"

        file_id = f"{rel}::{container}@file"
        result = ExtractionResult()
        result.symbols.append(SymbolInfo(
            id=file_id, name=container, kind="file_module", file=rel,
            start_line=1, end_line=len(lines), language="bsl", module=container,
        ))

        owner = owner_object(rel)
        if owner:  # the module belongs to a metadata object (linked by the resolver)
            result.edges.append(EdgeInfo(from_id=file_id, to_id=None, kind="DEFINED_IN",
                                         confidence="unresolved", raw_target=owner))

        methods = self._methods(lines, rel, container, kind, file_id, result)
        local = {m["name"].lower(): m["id"] for m in methods}
        for m in methods:
            self._calls(m, lines, local, container, result)
            self._data_refs(m, lines, result)
            result.edges.extend(data_flow_edges(
                m["id"], [(i, lines[i], code) for i, code in m["body"]]))
        return result

    # ─── methods ────────────────────────────────────────────────────

    def _methods(
        self, lines: list[str], rel: str, container: str, kind: str, file_id: str,
        result: ExtractionResult,
    ) -> list[dict]:
        methods: list[dict] = []
        seen: set[str] = set()
        in_string = False
        doc: list[str] = []
        directives: list[str] = []
        current: dict | None = None
        header: dict | None = None  # a header whose parenthesis is still open

        for i, raw in enumerate(lines):
            code, in_string = _strip(raw, in_string)
            stripped = code.strip()
            if header is not None:
                header["text"] += " " + stripped
                header["open"] += code.count("(") - code.count(")")
                if header["open"] <= 0:
                    self._finish_header(header, container, kind, rel, seen, methods, result,
                                        file_id)
                    current, header = header, None
                continue
            if current is not None:
                current["body"].append((i, code))
                if _END.match(code):
                    current["end"] = i + 1
                    current = None
                continue
            if raw.lstrip().startswith("//"):
                doc.append(raw.lstrip()[2:].strip())
                continue
            if not stripped:
                doc = []
                directives = []
                continue
            if stripped.startswith("&"):
                directives.append(stripped)
                continue
            m = _HEADER.match(code)
            if m is None:
                if not stripped.startswith("#"):
                    doc, directives = [], []
                continue
            header = {
                "name": m["name"], "kw": m["kw"], "async": m["async"], "line": i + 1,
                "text": stripped, "open": code.count("(") - code.count(")"), "body": [],
                "doc": doc, "directives": directives, "end": None,
            }
            doc, directives = [], []
            if header["open"] <= 0:
                self._finish_header(header, container, kind, rel, seen, methods, result, file_id)
                current, header = header, None
        for m in methods:
            if m["end"] is None:
                m["end"] = len(lines)
        return methods

    def _finish_header(
        self, h: dict, container: str, kind: str, rel: str, seen: set[str],
        methods: list[dict], result: ExtractionResult, file_id: str,
    ) -> None:
        qualified = f"{container}.{h['name']}"
        sym_id = f"{rel}::{qualified}"
        if sym_id in seen:
            sym_id = f"{sym_id}@{h['line']}"
        seen.add(sym_id)
        signature = re.sub(r"\s+", " ", h["text"]).strip()
        if h["directives"]:
            signature = " ".join(h["directives"]) + " " + signature
        doc = "\n".join(h["doc"]).strip()[:_MAX_DOC_CHARS] or None
        h["id"] = sym_id
        h["qualified"] = qualified
        methods.append(h)
        sym = SymbolInfo(
            id=sym_id, name=qualified, kind=kind, file=rel, start_line=h["line"],
            end_line=None, language="bsl", signature=signature, docstring=doc,
            is_exported=bool(_EXPORT.search(h["text"])), module=container,
        )
        result.symbols.append(sym)
        h["symbol"] = sym
        result.edges.append(EdgeInfo(from_id=sym_id, to_id=file_id, kind="DEFINED_IN",
                                     confidence="strong"))

    # ─── calls ──────────────────────────────────────────────────────

    def _calls(
        self, m: dict, lines: list[str], local: dict[str, str], container: str,
        result: ExtractionResult,
    ) -> None:
        sym: SymbolInfo = m["symbol"]
        sym.end_line = m["end"]
        emitted: set[tuple[str, str]] = set()
        for _i, code in m["body"]:
            if "(" not in code or _END.match(code):
                continue
            code = _NEW.sub(" ", code)
            for found in _CHAIN.finditer(code):
                chain = found.group(1).split(".")
                if len(chain) == 1 and chain[0].lower() in _KEYWORDS:
                    continue
                to_id, raw = self._target(chain, local, container)
                if to_id is None and raw is None:
                    continue
                key = (to_id or "", raw or "")
                if key in emitted:
                    continue
                emitted.add(key)
                if to_id is not None:
                    result.edges.append(EdgeInfo(from_id=m["id"], to_id=to_id, kind="CALLS",
                                                 confidence="strong", raw_target=raw))
                else:
                    result.edges.append(EdgeInfo(from_id=m["id"], to_id=None, kind="CALLS",
                                                 confidence="unresolved", raw_target=raw))

    @staticmethod
    def _data_refs(m: dict, lines: list[str], result: ExtractionResult) -> None:
        """REFERENCES edges to the data objects a method names: `FROM
        AccumulationRegister.Stock`, `Catalogs.Items.FindByCode(` — in query text
        (strings included) and in code alike. Comment lines are skipped."""
        seen: set[str] = set()
        for i, _code in m["body"]:
            raw = lines[i]
            if "." not in raw or raw.lstrip().startswith("//"):
                continue
            for found in _DATA_REF.finditer(raw):
                target = data_object(found.group(1), found.group(2))
                if target and target not in seen:
                    seen.add(target)
                    result.edges.append(EdgeInfo(from_id=m["id"], to_id=None, kind="REFERENCES",
                                                 confidence="unresolved", raw_target=target))

    @staticmethod
    def _target(
        chain: list[str], local: dict[str, str], container: str,
    ) -> tuple[str | None, str | None]:
        """(resolved id, raw target) of a call written as ``chain``; (None, None)
        when it cannot name anything in the tree."""
        if len(chain) == 1:
            sid = local.get(chain[0].lower())
            return (sid, chain[0]) if sid else (None, None)
        if len(chain) == 2:
            if chain[0].lower() in _SELF:
                sid = local.get(chain[1].lower())
                return (sid, chain[1]) if sid else (None, None)
            return None, f"{chain[0]}.{chain[1]}"
        obj = data_object(chain[0], chain[1])
        if obj and len(chain) == 3:
            return None, f"{obj}.ManagerModule.{chain[2]}"
        return None, None

    def _relpath(self, file_path: Path) -> str:
        if self.repo_root is not None:
            try:
                return file_path.resolve().relative_to(self.repo_root).as_posix()
            except (ValueError, OSError):
                pass
        return file_path.as_posix()
