"""The report on an external 1C file, as blocks.

Built from `facts.Facts` only: every table, number, name and code excerpt comes
from the facts. The sentences around them are templates in Ukrainian and in
English, so the report reads like one a person wrote even when no model is
involved. A model may be asked for the prose (`review_llm`): its text goes into
fixed places (the conclusion, a blocker's explanation and expected effect, the
recommendation, a note per section) and nowhere else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from src.docs.blocks import (
    Block,
    Bullets,
    Code,
    Heading,
    Para,
    Table,
    Title,
)
from src.onec.facts import Blocker, Facts, MarkerFact, ModuleFacts

LANGS = ("uk", "en")
DEFAULT_LANG = "uk"
_NBSP = " "


@dataclass
class Prose:
    """The words a model may supply. Every field is optional."""

    conclusion: str = ""
    blockers: dict[str, dict[str, str]] = field(default_factory=dict)  # id → explanation/effect
    recommendation: str = ""
    notes: dict[str, str] = field(default_factory=dict)  # sources|version|checks|limits


# ─── the vocabulary ────────────────────────────────────────────────────

#: type directory → (nominative, genitive); the report names an object by its kind
_KINDS_UK = {
    "Catalogs": ("довідник", "довідника"), "Documents": ("документ", "документа"),
    "AccumulationRegisters": ("регістр накопичення", "регістра накопичення"),
    "InformationRegisters": ("регістр відомостей", "регістра відомостей"),
    "AccountingRegisters": ("регістр бухгалтерії", "регістра бухгалтерії"),
    "CalculationRegisters": ("регістр розрахунку", "регістра розрахунку"),
    "Enums": ("перелічення", "перелічення"), "Constants": ("константа", "константи"),
    "ChartsOfAccounts": ("план рахунків", "плану рахунків"),
    "ChartsOfCharacteristicTypes": ("план видів характеристик", "плану видів характеристик"),
    "ChartsOfCalculationTypes": ("план видів розрахунку", "плану видів розрахунку"),
    "ExchangePlans": ("план обміну", "плану обміну"),
    "BusinessProcesses": ("бізнес-процес", "бізнес-процесу"), "Tasks": ("завдання", "завдання"),
    "Reports": ("звіт", "звіту"), "DataProcessors": ("обробка", "обробки"),
    "DocumentJournals": ("журнал документів", "журналу документів"),
    "CommonModules": ("загальний модуль", "загального модуля"),
    "SessionParameters": ("параметр сеансу", "параметра сеансу"),
}
_KINDS_EN = {
    "Catalogs": "catalog", "Documents": "document",
    "AccumulationRegisters": "accumulation register",
    "InformationRegisters": "information register", "AccountingRegisters": "accounting register",
    "CalculationRegisters": "calculation register", "Enums": "enumeration",
    "Constants": "constant", "ChartsOfAccounts": "chart of accounts",
    "ChartsOfCharacteristicTypes": "chart of characteristic types",
    "ChartsOfCalculationTypes": "chart of calculation types", "ExchangePlans": "exchange plan",
    "BusinessProcesses": "business process", "Tasks": "task", "Reports": "report",
    "DataProcessors": "data processor", "DocumentJournals": "document journal",
    "CommonModules": "common module", "SessionParameters": "session parameter",
}
_KINDS_PLURAL_UK = {
    "Catalogs": "довідники", "Documents": "документи", "AccumulationRegisters":
    "регістри накопичення", "InformationRegisters": "регістри відомостей",
    "AccountingRegisters": "регістри бухгалтерії", "CalculationRegisters": "регістри розрахунку",
    "Enums": "перелічення", "Constants": "константи", "ChartsOfAccounts": "плани рахунків",
    "ChartsOfCharacteristicTypes": "плани видів характеристик",
    "ChartsOfCalculationTypes": "плани видів розрахунку", "ExchangePlans": "плани обміну",
    "BusinessProcesses": "бізнес-процеси", "Tasks": "завдання", "Reports": "звіти",
    "DataProcessors": "обробки", "DocumentJournals": "журнали документів",
    "SessionParameters": "параметри сеансу",
}
_BLOCK_NAMES_UK = {"Если": "умова", "Цикл": "цикл", "Попытка": "спроба"}


def _plural_uk(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class _T:
    """Words and number formats of one language."""

    def __init__(self, lang: str) -> None:
        self.uk = lang != "en"

    def n(self, value: int) -> str:
        s = f"{int(value):,}"
        return s.replace(",", _NBSP) if self.uk else s

    def date(self, d: datetime | None) -> str:
        return f"{d:%d.%m.%Y}" if d else ("невідомо" if self.uk else "unknown")

    def dt(self, d: datetime | None) -> str:
        return f"{d:%d.%m.%Y %H:%M:%S}" if d else ("невідомо" if self.uk else "unknown")

    def count(self, n: int, uk: tuple[str, str, str], en: tuple[str, str]) -> str:
        if self.uk:
            return f"{self.n(n)} {_plural_uk(n, *uk)}"
        return f"{self.n(n)} {en[0] if n == 1 else en[1]}"

    def kind(self, plural: str, genitive: bool = False) -> str:
        if self.uk:
            pair = _KINDS_UK.get(plural)
            if pair:
                return pair[1 if genitive else 0]
            return "об'єкта" if genitive else "об'єкт"
        return _KINDS_EN.get(plural, "object")

    def kinds(self, plural: str) -> str:
        if self.uk:
            return _KINDS_PLURAL_UK.get(plural, plural)
        return _KINDS_EN.get(plural, plural) + "s"

    def bytes_(self, size: int) -> str:
        if self.uk:
            return f"{self.n(size)} {_plural_uk(size, 'байт', 'байти', 'байтів')}"
        return f"{self.n(size)} bytes"


def _span(a: int, b: int) -> str:
    return str(a) if a == b else f"{a}–{b}"


def _ranges(lines: list[int]) -> str:
    """716, 717, 718, 1200 → `716–718, 1200`."""
    out: list[str] = []
    xs = sorted(set(lines))
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(_span(xs[i], xs[j]))
        i = j + 1
    return ", ".join(out)


def _marker_name(t: _T, m: str, numeric: bool) -> str:
    return (f"задача {m}" if t.uk else f"task {m}") if numeric else m


def _marker_dates(t: _T, mf: MarkerFact) -> str:
    if mf.last is None:
        return ""
    if mf.first is None or mf.first == mf.last:
        return t.date(mf.last)
    if (mf.first.month, mf.first.year) == (mf.last.month, mf.last.year):
        return f"{mf.first:%d}–{mf.last:%d.%m.%Y}"
    return f"{t.date(mf.first)}–{t.date(mf.last)}"


def _numbered(snippet) -> list[str]:  # noqa: ANN001
    width = len(str(snippet.start + len(snippet.lines) - 1))
    return [f"{snippet.start + i:>{width}}  {ln.replace(chr(9), '  ')}"
            for i, ln in enumerate(snippet.lines)]


def _short_hash(h: str) -> str:
    return f"{h[:8]}…{h[-8:]}"


def _years(t: _T, marks: list[MarkerFact]) -> str:
    ys = sorted({m.last.year for m in marks if m.last})
    if not ys:
        return ""
    if len(ys) == 1:
        return f"{ys[0]} {'року' if t.uk else ''}".strip()
    return f"{ys[0]}–{ys[-1]} {'років' if t.uk else ''}".strip()


# ─── the document ──────────────────────────────────────────────────────

def build_document(facts: Facts, *, lang: str = DEFAULT_LANG, prose: Prose | None = None,
                   model_note: str = "") -> list[Block]:
    """The report's blocks. ``prose`` fills the places a model may word."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return _Builder(facts, lang, prose or Prose(), model_note).build()


def template_drafts(facts: Facts, *, lang: str = DEFAULT_LANG) -> dict:
    """The template's own wording of every place a model may word: what a model
    is asked to improve, and what stays when its version is dropped."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    b = _Builder(facts, lang, Prose(), "")
    b.build()
    return b.drafts


class _Builder:
    def __init__(self, f: Facts, lang: str, prose: Prose, model_note: str) -> None:
        self.f, self.lang, self.p, self.model_note = f, lang, prose, model_note
        self.t = _T(lang)
        self.uk = self.t.uk
        self.out: list[Block] = []
        self.section = 0
        self.drafts: dict = {"blockers": {}}

    # small helpers ---------------------------------------------------

    def tr(self, uk: str, en: str) -> str:
        return uk if self.uk else en

    def heading(self, text: str, level: int = 2, numbered: bool = True) -> None:
        if numbered and level == 2:
            self.section += 1
            text = f"{self.section}. {text}"
        self.out.append(Heading(level, text))

    def note(self, key: str) -> None:
        text = (self.p.notes.get(key) or "").strip()
        if text:
            self.out.append(Para(text, "note"))

    @property
    def object_word(self) -> tuple[str, str, str]:
        """(nominative, genitive, accusative-ish "the") of what the file is."""
        k = self.f.kind
        if self.uk:
            return {"Reports": ("звіт", "звіту", "звіт"),
                    "DataProcessors": ("обробка", "обробки", "обробку"),
                    "Form": ("форма", "форми", "форму")}.get(k, ("файл", "файлу", "файл"))
        word = {"Reports": "report", "DataProcessors": "data processor",
                "Form": "form"}.get(k, "file")
        return word, word, word

    # the whole -------------------------------------------------------

    def build(self) -> list[Block]:
        self.title()
        self.conclusion()
        if self.f.has_config or self.f.config_label:
            self.sources()
        else:
            self.sources()
        self.blockers()
        if self.f.has_config:
            self.version()
        self.checked()
        self.limits()
        self.recommendation()
        return self.out

    # title and conclusion --------------------------------------------

    def title(self) -> None:
        f, t = self.f, self.t
        name = f.synonym or f.object_name
        if f.kind == "Reports":
            title = self.tr(f"Перевірка зовнішнього звіту «{name}»",
                            f"Review of the external report “{name}”")
        elif f.kind == "DataProcessors":
            title = self.tr(f"Перевірка зовнішньої обробки «{name}»",
                            f"Review of the external data processor “{name}”")
        elif f.kind == "Form":
            title = self.tr("Перевірка файлу форми (Form.bin)", "Review of an ordinary form file")
        else:
            title = self.tr("Перевірка файлу 1С", "Review of a 1C file")
        when = f"{f.generated:%d.%m.%Y}" if f.generated else ""
        method = self.tr(
            "статичний аналіз (розпаковування контейнера, звірка з конфігурацією). "
            "У 1С файл не запускався.",
            "static analysis (the container is unpacked and compared with the configuration). "
            "The file was not run in 1C.")
        self.out.append(Title(title, [
            (self.tr("Файл", "File"),
             f"{f.file_name}, {t.bytes_(f.size)} · SHA-256 {_short_hash(f.sha256)}"),
            (self.tr("Перевірено", "Checked"), when),
            (self.tr("Метод", "Method"), method),
        ]))

    def _version_sentence(self) -> str:
        f = self.f
        if not f.saved:
            return ""
        _nom, gen, _acc = self.object_word
        return self.tr(f"Це версія {gen} від {self.t.date(f.saved)}.",
                       f"This is the {gen} as saved on {self.t.date(f.saved)}.")

    def _stale_sentence(self, where: str) -> str:
        f, t = self.f, self.t
        marks = f.markers_missing
        if not marks:
            return ""
        years = _years(t, marks)
        if years:
            return self.tr(f"У файлі бракує доробок {years}, які є {where}.",
                           f"The file lacks changes from {years} that {where} has.")
        return self.tr(f"У файлі бракує {t.count(len(marks), ('доробки', 'доробок', 'доробок'), ('change', 'changes'))}, "
                       f"які є {where}.",
                       f"The file lacks {len(marks)} change(s) that {where} has.")

    def _blocker_sentence(self, b: Blocker) -> str:
        t = self.t
        if b.code == "missing_object":
            kind = t.kind(b.plural, True)
            what = self.tr("формування звіту завершиться помилкою", "building the report will fail") \
                if self.f.kind == "Reports" and b.where == "query" \
                else self.tr("виконання цього коду завершиться помилкою",
                             "running this code will fail")
            return self.tr(
                f"Вона звертається до {kind} «{b.name}», а в поточній конфігурації такого "
                f"об'єкта немає, тому {what}.",
                f"It reads the {kind} “{b.name}”, which the current configuration does not "
                f"have, so {what}.")
        return self.tr(f"У коді є помилка, яка зупинить виконання: {self._blocker_title(b)}.",
                       f"The code has an error that will stop it: {self._blocker_title(b)}.")

    def conclusion(self) -> None:
        f, t = self.f, self.t
        where = self.tr("у конфігурації", "the configuration")
        if f.level == "bad":
            lead = self.tr("Висновок: файл використовувати не можна.",
                           "Conclusion: do not use this file.")
            main = f.blockers[0]
            more = ""
            if len(f.blockers) > 1:
                more = self.tr(f" Усього знайдено {t.count(len(f.blockers), ('блокер', 'блокери', 'блокерів'), ('blocker', 'blockers'))}.",
                               f" {len(f.blockers)} blockers in total.")
            body = " ".join(s for s in (self._version_sentence(), self._blocker_sentence(main),
                                        self._stale_sentence(where)) if s) + more
        elif f.level == "warn":
            lead = self.tr("Висновок: файл працюватиме, але його версія застаріла.",
                           "Conclusion: the file should run, but its version is out of date.")
            body = " ".join(s for s in (
                self._version_sentence(),
                self.tr("Помилок, які зупинять виконання, не знайдено.",
                        "No errors that would stop it were found."),
                self._stale_sentence(where) or self.tr(
                    "Код відрізняється від конфігурації (розділ про версію).",
                    "The code differs from the configuration's (see the version section)."),
            ) if s)
        elif f.level == "good":
            lead = self.tr("Висновок: файл можна використовувати.",
                           "Conclusion: the file can be used.")
            same = sum(1 for m in f.modules if m.identical)
            body = self.tr(
                "Помилок, які зупинять виконання, не знайдено" +
                (", а код збігається з конфігурацією." if all(m.identical for m in f.modules)
                 and f.modules else
                 f"; код відрізняється від конфігурації, але маркерів доробок, яких у файлі "
                 f"бракує, немає ({same} із {len(f.modules)} модулів збігаються повністю)."),
                "No errors that would stop it were found" +
                ("; the code matches the configuration." if all(m.identical for m in f.modules)
                 and f.modules else "; the code differs from the configuration's, but no change "
                 "marker of the configuration is missing from the file."))
        else:
            lead = self.tr("Висновок: порівняти з конфігурацією не вдалося.",
                           "Conclusion: it could not be compared with a configuration.")
            n = f.errors
            body = self.tr(
                f"Статичні перевірки самого файлу знайшли {t.count(n, ('помилку', 'помилки', 'помилок'), ('error', 'errors'))}. "
                "Звірка з метаданими й версією можлива лише з вивантаженням конфігурації: "
                "оберіть її і запустіть перевірку ще раз.",
                f"Static checks of the file alone found {n} error(s). Metadata and version "
                "checks need a configuration dump: pick one and run the check again.")
        self.drafts["conclusion"] = body
        body = (self.p.conclusion or "").strip() or body
        tone = {"bad": "bad", "warn": "warn", "good": "good"}.get(f.level, "neutral")
        self.out.append(Para(f"**{lead}** {body}", "callout", tone))

    # 1. sources -------------------------------------------------------

    def sources(self) -> None:
        f, t = self.f, self.t
        self.heading(self.tr("З чим порівнювали", "What it was compared with"))
        nom, _g, _a = self.object_word
        ext_label = {".erf": self.tr("Файл .erf", "The .erf file")}.get("", "")
        suffix = f.file_name.rsplit(".", 1)[-1].lower() if "." in f.file_name else ""
        file_name = self.tr(f"Файл .{suffix}" if suffix else "Файл",
                            f"The .{suffix} file" if suffix else "The file")
        del ext_label
        parts = []
        if f.forms:
            parts.append(t.count(len(f.forms), ("форма", "форми", "форм"), ("form", "forms")))
        if f.templates:
            parts.append(t.count(len(f.templates), ("макет", "макети", "макетів"),
                                 ("template", "templates")))
        kind_label = {"Reports": self.tr("Зовнішній звіт", "External report"),
                      "DataProcessors": self.tr("Зовнішня обробка", "External data processor"),
                      "Form": self.tr("Звичайна форма", "Ordinary form")
                      }.get(f.kind, self.tr("Контейнер 1С", "1C container"))
        what = kind_label + (": " + self.tr(" та ", " and ").join(parts) if parts else "")
        if f.saved and f.created and f.saved == f.created:
            state = self.tr(f"Усі внутрішні файли датовані {t.dt(f.saved)}",
                            f"All inner files are dated {t.dt(f.saved)}")
        else:
            state = self.tr(f"Збережено {t.dt(f.saved)}, створено {t.dt(f.created)}",
                            f"Saved {t.dt(f.saved)}, created {t.dt(f.created)}")
        rows = [[file_name, what, state]]
        if f.has_config:
            cname = unicodedata.normalize("NFC", f.counterpart.partition(".")[2] or f.counterpart)
            nom = self.object_word[0]
            what_c = self.tr(f"Вбудований {nom} з тим самим іменем" if nom == "звіт"
                             else f"Вбудована {nom} з тим самим іменем" if nom in ("обробка", "форма")
                             else f"Вбудований об'єкт «{cname}»",
                             f"The built-in {nom} of the same name")
            bits = []
            if f.config_files_date:
                bits.append(self.tr(f"Файли від {t.date(f.config_files_date)}",
                                    f"Files from {t.date(f.config_files_date)}"))
            elif f.config_changed:
                bits.append(self.tr(f"Остання зміна об'єкта в історії {t.date(f.config_changed)}",
                                    f"Object last changed {t.date(f.config_changed)}"))
            if f.config_last_marker_date:
                mk = _marker_name(t, f.config_last_marker, f.config_last_marker_numeric)
                bits.append(self.tr(
                    f"останній маркер зміни в коді — {t.date(f.config_last_marker_date)} ({mk})",
                    f"last change marker in the code: {t.date(f.config_last_marker_date)} ({mk})"))
            rows.append([self.tr("Конфігурація", "Configuration") + f" ({f.config_label})",
                         what_c, "; ".join(bits) or "—"])
            if f.config_ref or f.config_age:
                ref = f.config_ref
                age = self.tr(f"індекс давності {f.config_age}", f"index age {f.config_age}") \
                    if f.config_age else ""
                rows.append([self.tr("Репозиторій", "Repository"), ref or "—", age or "—"])
        self.out.append(Table([self.tr("Джерело", "Source"), self.tr("Що це", "What it is"),
                               self.tr("Стан", "State")], rows, [1.2, 1.4, 2.2]))
        if not f.has_config:
            why = f.no_config_reason or self.tr("конфігурацію не обрано", "no configuration chosen")
            self.out.append(Para(self.tr(
                f"Порівняння з конфігурацією не робилося: {why}. Виконано лише перевірки, "
                "яким конфігурація не потрібна.",
                f"No comparison with a configuration was made: {why}. Only the checks that need "
                "no configuration were run."), "body"))
        if f.same_comment:
            self.out.append(Para(self.tr(
                f"Обидва об'єкти мають однаковий коментар версії — «{f.comment}», тому за назвою "
                "версії їх не відрізнити.",
                f"Both objects carry the same version comment, “{f.comment}”, so the version "
                "name does not tell them apart."), "body"))
        self.note("sources")

    # 2. blockers ------------------------------------------------------

    def _blocker_title(self, b: Blocker) -> str:
        t = self.t
        c = b.code
        if c == "missing_object":
            return self.tr(f"{t.kind(b.plural)}, якого немає в конфігурації",
                           f"a {t.kind(b.plural)} the configuration does not have")
        if c == "missing_method":
            return self.tr("виклик методу, якого немає", "a call of a method that does not exist")
        if c == "not_exported":
            return self.tr("виклик неекспортного методу", "a call of a method that is not exported")
        if c == "arity":
            return self.tr("виклик із хибною кількістю аргументів",
                           "a call with the wrong number of arguments")
        if c in ("missing_enum_value", "missing_predefined"):
            return self.tr("значення, якого немає в конфігурації",
                           "a value the configuration does not have")
        if c == "missing_template":
            return self.tr("макет, якого немає", "a template that does not exist")
        if c in ("unbalanced", "no_end"):
            return self.tr("порушена парність блоків", "unbalanced blocks")
        if c == "handler":
            return self.tr("обробник події без процедури", "an event handler without a procedure")
        return b.message

    def _intro(self, b: Blocker) -> str:
        t = self.t
        where = (self.tr(f"процедури «{b.procedure}» ", f"procedure “{b.procedure}” ")
                 if b.procedure else "") + self.tr(f"модуля «{b.module}»", f"in module “{b.module}”")
        c = b.code
        if c == "missing_object":
            kind = t.kind(b.plural, True)
            if b.where == "query":
                return self.tr(f"Запит {where} звертається до {kind} «{b.name}».",
                               f"A query of the {where} reads the {kind} “{b.name}”.")
            return self.tr(f"Код {where}, рядок {b.line}, звертається до {kind} «{b.name}».",
                           f"The code of the {where}, line {b.line}, uses the {kind} “{b.name}”.")
        if c == "missing_method":
            mod = b.data.get("module", "")
            return self.tr(f"Код {where}, рядок {b.line}, викликає {b.subject}, але в загальному "
                           f"модулі «{mod}» такого методу немає.",
                           f"The code of the {where}, line {b.line}, calls {b.subject}, but "
                           f"common module “{mod}” has no such method.")
        if c == "not_exported":
            return self.tr(f"Код {where}, рядок {b.line}, викликає {b.subject}, а цей метод не "
                           "експортний (у заголовку немає слова Экспорт), тому з іншого модуля "
                           "його викликати не можна.",
                           f"The code of the {where}, line {b.line}, calls {b.subject}, which is "
                           "not exported (no Экспорт in its header) and cannot be called from "
                           "another module.")
        if c == "arity":
            return self.tr(f"Код {where}, рядок {b.line}, викликає {b.subject} з "
                           f"{b.data.get('passed')} аргументами; сигнатура методу: "
                           f"`{b.data.get('signature', '')}`.",
                           f"The code of the {where}, line {b.line}, calls {b.subject} with "
                           f"{b.data.get('passed')} argument(s); the method's signature is "
                           f"`{b.data.get('signature', '')}`.")
        if c == "missing_enum_value":
            return self.tr(f"Код {where}, рядок {b.line}, читає значення «{b.data.get('value')}» "
                           f"перелічення «{b.data.get('enum')}», якого в конфігурації немає.",
                           f"The code of the {where}, line {b.line}, reads the value "
                           f"“{b.data.get('value')}” of enumeration “{b.data.get('enum')}”, "
                           "which the configuration lacks.")
        if c == "missing_predefined":
            return self.tr(f"Код {where}, рядок {b.line}, посилається на предопределений елемент "
                           f"«{b.data.get('value')}» об'єкта «{b.data.get('enum')}», якого немає.",
                           f"The code of the {where}, line {b.line}, refers to the predefined "
                           f"item “{b.data.get('value')}” of “{b.data.get('enum')}”, which is "
                           "not there.")
        if c == "missing_template":
            return self.tr(f"Код {where}, рядок {b.line}, отримує макет «{b.subject}», якого "
                           "немає.", f"The code of the {where}, line {b.line}, gets the "
                           f"template “{b.subject}”, which does not exist.")
        if c == "no_end":
            return self.tr(f"Процедура «{b.subject}» модуля «{b.module}» (рядок {b.line}) не "
                           "завершується КонецПроцедуры/КонецФункции.",
                           f"Procedure “{b.subject}” of module “{b.module}” (line {b.line}) has "
                           "no КонецПроцедуры/КонецФункции.")
        if c == "unbalanced":
            n = int(b.data.get("n", 0))
            name = b.data.get("block", "")
            return self.tr(
                f"У {where} блок «{name}» "
                + (f"не закрито ({abs(n)})." if n > 0 else f"закрито без відкриття ({abs(n)})."),
                f"In the {where} the «{name}» block "
                + (f"is not closed ({abs(n)})." if n > 0 else f"is closed without being opened ({abs(n)})."))
        if c == "handler":
            return self.tr(f"Для події форми «{b.data.get('form', b.module)}» призначено обробник "
                           f"«{b.subject}», але в модулі форми такої процедури немає.",
                           f"Form “{b.data.get('form', b.module)}” binds the handler "
                           f"“{b.subject}” to an event, but its module has no such procedure.")
        return b.message

    def _effect(self, b: Blocker) -> str:
        c = b.code
        proc = b.procedure
        run = self.tr(f"під час виконання процедури «{proc}»" if proc else "під час виконання",
                      f"while procedure “{proc}” runs" if proc else "at run time")
        if c == "missing_object" and b.where == "query":
            when = self.tr("під час формування звіту", "while the report is built") \
                if self.f.kind == "Reports" else run
            return self.tr(f"Очікуваний результат у 1С: помилка виконання запиту (таблицю не "
                           f"знайдено) {when}.",
                           f"Expected in 1C: a query error (table not found) {when}.")
        if c in ("missing_object", "missing_enum_value", "missing_predefined", "missing_method",
                 "missing_template", "not_exported"):
            return self.tr(f"Очікуваний результат у 1С: помилка виконання {run} "
                           "(звернення до відсутнього або недоступного об'єкта).",
                           f"Expected in 1C: a run-time error {run} (a missing or unavailable "
                           "object is used).")
        if c == "arity":
            return self.tr(f"Очікуваний результат у 1С: помилка виклику {run}.",
                           f"Expected in 1C: a call error {run}.")
        if c in ("unbalanced", "no_end"):
            return self.tr("Очікуваний результат у 1С: модуль не скомпілюється, форма не відкриється.",
                           "Expected in 1C: the module will not compile and the form will not open.")
        if c == "handler":
            return self.tr("Очікуваний результат у 1С: помилка під час відкриття форми або "
                           "спрацювання події.",
                           "Expected in 1C: an error when the form opens or the event fires.")
        return ""

    def blockers(self) -> None:
        f, t = self.f, self.t
        self.heading(self.tr("Блокери", "Blockers"))
        if not f.blockers:
            self.out.append(Para(self.tr(
                "Помилок, які зупинили б виконання, не знайдено. Що саме перевірялося — у розділі "
                "«Що перевірено і в порядку».",
                "No errors that would stop the file were found. What was checked is listed in the "
                "section “What was checked and is fine”."), "body"))
        for i, b in enumerate(f.blockers, 1):
            self.heading(self.tr(f"Блокер {i}: {self._blocker_title(b)}",
                                 f"Blocker {i}: {self._blocker_title(b)}"), 3)
            extra = self.p.blockers.get(b.id, {})
            self.drafts["blockers"][b.id] = {"explanation": self._intro(b), "effect": self._effect(b)}
            self.out.append(Para((extra.get("explanation") or "").strip() or self._intro(b), "body"))
            if b.file_snippet:
                s = b.file_snippet
                self.out.append(Para(self.tr(
                    f"Файл, модуль {s.module}, рядки {_span(s.start, s.start + len(s.lines) - 1)}:",
                    f"The file, module {s.module}, lines "
                    f"{_span(s.start, s.start + len(s.lines) - 1)}:"), "body"))
                self.out.append(Code(_numbered(s)))
            if b.config_snippet:
                s = b.config_snippet
                same = self.tr("той самий запит" if b.where == "query" else "те саме місце",
                               "the same query" if b.where == "query" else "the same place")
                self.out.append(Para(self.tr(
                    f"Конфігурація, {same}, рядки {_span(s.start, s.start + len(s.lines) - 1)}:",
                    f"The configuration, {same}, lines "
                    f"{_span(s.start, s.start + len(s.lines) - 1)}:"), "body"))
                self.out.append(Code(_numbered(s)))
            if b.code == "missing_object" and f.has_config:
                self.confirm(b)
            if b.other_lines or b.count > 1:
                lines = _ranges(b.other_lines)
                flds = (f" ({', '.join(b.fields)})" if b.fields else "")
                self.out.append(Para(self.tr(
                    f"У файлі цей об'єкт (або його поля) використано також у рядках {lines}{flds}.",
                    f"The file uses this object (or its fields) in more places: lines "
                    f"{lines}{flds}."), "body"))
            effect = (extra.get("effect") or "").strip() or self._effect(b)
            if effect:
                self.out.append(Para(effect, "body"))
        if f.warnings:
            self.heading(self.tr("Попередження", "Warnings"), 3)
            self.out.append(Para(self.tr(
                "Це місця, які схожі на помилку, але можуть бути правильними; їх варто глянути "
                "після виправлення блокерів.",
                "These places look wrong but may be fine; look at them after the blockers are "
                "fixed."), "body"))
            rows = [[f"{w['module']}:{w['line']}", w["message"]] for w in f.warnings[:15]]
            self.out.append(Table([self.tr("Місце", "Place"), self.tr("Що помічено", "What was seen")],
                                  rows, [1.0, 3.5]))
            if len(f.warnings) > 15:
                self.out.append(Para(self.tr(f"…і ще {len(f.warnings) - 15} (показано перші 15).",
                                             f"… and {len(f.warnings) - 15} more (first 15 shown)."), "note"))
        if f.inherited:
            self.out.append(Para(self.tr(
                f"Ще {t.count(len(f.inherited), ('зауваження', 'зауваження', 'зауважень'), ('finding', 'findings'))} "
                "є й у самій конфігурації (наприклад, обробник події без процедури); вони не "
                "залежать від цього файлу й блокерами не вважаються.",
                f"{len(f.inherited)} more finding(s) exist in the configuration itself (for "
                "example an event handler without a procedure); they do not come from this file "
                "and are not counted as blockers."), "note"))

    def confirm(self, b: Blocker) -> None:
        t = self.t
        kinds = t.kinds(b.plural)
        if b.similar:
            self.out.append(Para(self.tr(
                f"Об'єкта «{b.name}» у конфігурації немає. {kinds.capitalize()} зі схожою назвою:",
                f"There is no “{b.name}” in the configuration. {kinds.capitalize()} with a close "
                "name:"), "body"))
            self.out.append(Code(list(b.similar)))
        else:
            self.out.append(Para(self.tr(
                f"Об'єкта «{b.name}» у конфігурації немає, і схожих за назвою {kinds} теж не "
                "знайдено.",
                f"There is no “{b.name}” in the configuration, and no {kinds} with a similar name."),
                "body"))
        if b.config_marker:
            self.out.append(Para(self.tr(
                f"У конфігурації це місце змінено в задачі {b.config_marker}; файл зроблено до "
                "цієї зміни.",
                f"The configuration changed this place in {b.config_marker}; the file predates "
                "that change."), "body"))

    # 3. version -------------------------------------------------------

    def version(self) -> None:
        f = self.f
        v = f.verdict
        if v.startswith("older"):
            head = self.tr("Версія застаріла", "The version is out of date")
        elif v.startswith("newer"):
            head = self.tr("Версія новіша за конфігурацію", "The file is newer than the configuration")
        elif v.startswith("diverged"):
            head = self.tr("Версії розійшлися", "The versions have diverged")
        elif v.startswith("same"):
            head = self.tr("Код збігається з конфігурацією", "The code matches the configuration")
        else:
            head = self.tr("Код відрізняється від конфігурації", "The code differs from the configuration")
        self.heading(head)
        differing = [m for m in f.modules if not m.identical]
        if not differing:
            self.out.append(Para(self.tr(
                "Модулі файлу збігаються з конфігурацією рядок у рядок.",
                "The file's modules match the configuration line by line."), "body"))
        for m in differing:
            if len(differing) > 1:
                self.heading(self.tr(f"Модуль {m.label}", f"Module {m.label}"), 3)
            self.module_table(m)
        self.markers()
        for lst, uk, en in (
            (f.forms_missing, "Форми, які є в конфігурації, але яких немає у файлі",
             "Forms the configuration has and the file lacks"),
            (f.forms_extra, "Форми, яких немає в конфігурації", "Forms only the file has"),
            (f.templates_missing, "Макети, яких бракує у файлі", "Templates the file lacks"),
            (f.templates_extra, "Макети, яких немає в конфігурації", "Templates only the file has"),
        ):
            if lst:
                self.out.append(Para(f"**{self.tr(uk, en)}:** {', '.join(lst)}", "body"))
        if f.handler_changes:
            self.out.append(Para(f"**{self.tr('Відмінності в обробниках подій форм', 'Differences in the forms event handlers')}:**", "body"))
            self.out.append(Bullets([self._handler_change(h) for h in f.handler_changes]))
        self.note("version")

    def _handler_change(self, text: str) -> str:
        """The engine's English line about a handler, in the report's language."""
        m = re.match(r"form (.+?): handler (.+?) is bound in the (configuration|file), "
                     r"not in the (?:file|configuration)", text)
        if not m:
            return text
        form, handler, side = m.groups()
        if side == "configuration":
            return self.tr(f"форма «{form}»: обробник «{handler}» призначено в конфігурації, але не у файлі",
                           f"form “{form}”: the handler “{handler}” is bound in the configuration, not in the file")
        return self.tr(f"форма «{form}»: обробник «{handler}» призначено у файлі, але не в конфігурації",
                       f"form “{form}”: the handler “{handler}” is bound in the file, not in the configuration")

    def module_table(self, m: ModuleFacts) -> None:
        t = self.t
        L = m.label

        def marker(date, ident, numeric) -> str:  # noqa: ANN001
            if not ident:
                return "—"
            return f"{t.date(date)} ({_marker_name(t, ident, numeric)})"

        rows = [
            [self.tr(f"Рядків у модулі {L}", f"Lines in module {L}"),
             t.n(m.file_lines), t.n(m.config_lines)],
            [self.tr("Розмір модуля, байт", "Module size, bytes"),
             t.n(m.file_bytes), t.n(m.config_bytes)],
        ]
        if m.file_desc_bytes is not None and m.config_desc_bytes is not None:
            rows.append([self.tr("Розмір опису форми, байт", "Form description size, bytes"),
                         t.n(m.file_desc_bytes), t.n(m.config_desc_bytes)])
        rows.append([self.tr("Останній маркер зміни в коді", "Last change marker in the code"),
                     marker(m.file_last_marker_date, m.file_last_marker, m.file_last_marker_numeric),
                     marker(m.config_last_marker_date, m.config_last_marker,
                            m.config_last_marker_numeric)])
        rows.append([self.tr("Рядків, яких немає в іншій стороні", "Lines the other side lacks"),
                     t.n(m.unique_file), t.n(m.unique_config)])
        self.out.append(Table([self.tr("Показник", "Measure"), self.tr("Файл", "File"),
                               self.tr("Конфігурація", "Configuration")], rows, [2.6, 1.6, 1.6]))
        added = len(m.extra)
        removed = len(m.missing)
        self.out.append(Para(self.tr(
            f"Порівняння модулів дало {t.count(m.hunks, ('відмінний фрагмент', 'відмінні фрагменти', 'відмінних фрагментів'), ('differing piece', 'differing pieces'))}: "
            f"{t.count(m.changed_procedures, ('процедура змінена', 'процедури змінені', 'процедур змінено'), ('procedure changed', 'procedures changed'))}, "
            f"{added} лише у файлі, {removed} лише в конфігурації.",
            f"Comparing the modules gave {m.hunks} differing piece(s): {m.changed_procedures} "
            f"procedure(s) changed, {added} only in the file, {removed} only in the configuration."),
            "body"))
        if m.missing or m.missing_vars:
            self.out.append(Heading(4, self.tr("Процедури й змінні, яких у файлі немає",
                                               "Procedures and variables the file lacks")))
            self.out.append(Code([*m.missing_vars, *[self._trim(x) for x in m.missing]]))
        if m.extra or m.extra_vars:
            self.out.append(Heading(4, self.tr("Процедури й змінні, яких немає в конфігурації",
                                               "Procedures and variables the configuration lacks")))
            self.out.append(Code([*m.extra_vars, *[self._trim(x) for x in m.extra]]))

    @staticmethod
    def _trim(line: str, width: int = 110) -> str:
        return line if len(line) <= width else line[:width - 1] + "…"

    def markers(self) -> None:
        f, t = self.f, self.t
        if f.markers_missing:
            self.out.append(Heading(4, self.tr(
                "Задачі, маркери яких є в конфігурації, але відсутні у файлі",
                "Tasks marked in the configuration but absent from the file")))
            groups: dict[str, list[MarkerFact]] = {}
            for m in f.markers_missing:
                key = "" if m.numeric else m.id.rsplit("-", 1)[0]
                groups.setdefault(key, []).append(m)
            rows = []
            for key in sorted(groups, key=lambda k: (k == "", k)):
                items = sorted(groups[key], key=lambda m: (len(m.id), m.id))
                cells = []
                for m in items[:40]:
                    num = m.id if m.numeric else m.id.rsplit("-", 1)[1]
                    d = _marker_dates(t, m)
                    cells.append(f"{num} ({d})" if d else num)
                tail = (self.tr(f", … ще {len(items) - 40}", f", … {len(items) - 40} more")
                        if len(items) > 40 else "")
                label = (self.tr("Числові номери", "Bare numbers") if key == ""
                         else self.tr(f"Трекер {key}", f"Tracker {key}"))
                rows.append([label, ", ".join(cells) + tail])
            self.out.append(Table([self.tr("Позначка", "Marker"), self.tr("Задачі", "Tasks")],
                                  rows, [1.0, 4.0]))
            kind_gen = self.object_word[1]
            self.out.append(Para(self.tr(
                f"Якщо підключити цей файл замість {kind_gen} з конфігурації, ці "
                "доробки зникнуть"
                + (" навіть після виправлення блокерів" if f.blockers else "")
                + ". Зміст кожної задачі окремо не аналізувався.",
                "If this file replaces the one in the configuration, these changes will be lost"
                + (" even after the blockers are fixed" if f.blockers else "")
                + ". The content of each task was not analysed on its own."), "body"))
        if f.markers_extra:
            ids = ", ".join(_marker_name(t, m.id, m.numeric) for m in f.markers_extra[:20])
            self.out.append(Para(self.tr(
                f"У файлі є {t.count(len(f.markers_extra), ('маркер', 'маркери', 'маркерів'), ('marker', 'markers'))}, "
                f"яких немає в конфігурації: {ids}. Це доробки, які ще не потрапили в конфігурацію.",
                f"The file has {len(f.markers_extra)} marker(s) the configuration lacks: {ids}. "
                "These are changes that have not reached the configuration."), "body"))

    # 4. checked -------------------------------------------------------

    def checked(self) -> None:
        f, t = self.f, self.t
        self.heading(self.tr("Що перевірено і в порядку", "What was checked and is fine"))
        ok = "OK"
        rows: list[list[str]] = []
        rows.append([self.tr("Цілісність контейнера", "Container integrity"),
                     self.tr(f"{ok} — розпаковано всі {t.n(f.entries)} файлів, вкладені "
                             "контейнери форм читаються",
                             f"{ok} — all {t.n(f.entries)} entries unpacked, the nested form "
                             "containers read")])
        if f.has_config or f.refs_ok or f.refs_missing:
            n_ok, n_bad = sum(f.refs_ok.values()), sum(f.refs_missing.values())
            kinds = ", ".join(sorted({t.kinds(k) for k in {*f.refs_ok, *f.refs_missing}}))
            label = self.tr("Об'єкти метаданих, на які посилається код",
                            "Metadata objects the code refers to") + (f" ({kinds})" if kinds else "")
            if not f.has_config:
                res = self.tr("не перевірено: конфігурацію не обрано", "not checked: no configuration")
            elif n_bad:
                res = self.tr(f"{n_ok} існують, {n_bad} відсутній (розділ «Блокери»)"
                              if n_bad == 1 else f"{n_ok} існують, {n_bad} відсутні (розділ «Блокери»)",
                              f"{n_ok} exist, {n_bad} missing (see “Blockers”)")
            else:
                res = self.tr(f"{ok} — {n_ok}, усі існують", f"{ok} — {n_ok}, all exist")
            rows.append([label, res])
        if f.has_config:
            if f.calls_total == 0:
                res = self.tr("у коді немає викликів методів загальних модулів",
                              "the code has no calls of common-module methods")
            elif f.calls_bad:
                res = self.tr(f"{f.calls_bad} із {f.calls_total} викликів із проблемами "
                              "(розділ «Блокери»)",
                              f"{f.calls_bad} of {f.calls_total} calls have problems (see “Blockers”)")
            else:
                res = self.tr(f"{ok} — {t.count(f.calls_total, ('виклик', 'виклики', 'викликів'), ('call', 'calls'))}, "
                              "усі методи є та експортні",
                              f"{ok} — {f.calls_total} call(s), every method exists and is exported")
            rows.append([self.tr("Виклики експортних методів загальних модулів",
                                 "Calls of common-module methods"), res])
        if f.handlers_total:
            if f.handlers_missing:
                res = self.tr(f"{f.handlers_missing} із {f.handlers_total} без процедури "
                              "(розділ «Блокери»)",
                              f"{f.handlers_missing} of {f.handlers_total} have no procedure "
                              "(see “Blockers”)")
            else:
                res = self.tr(f"{ok} — для кожного з {f.handlers_total} є процедура в модулі"
                              + (f" (ще {f.handlers_inherited} без процедури, але так само і в конфігурації)"
                                 if f.handlers_inherited else ""),
                              f"{ok} — each of the {f.handlers_total} has a procedure in the module"
                              + (f" ({f.handlers_inherited} more lack one, as in the configuration)"
                                 if f.handlers_inherited else ""))
            rows.append([self.tr("Обробники подій форми", "Form event handlers"), res])
        for bal in f.balance:
            items = bal["items"]
            order = ("Процедура", "Функция", "Если", "Цикл", "Попытка")
            parts, good = [], True
            for k in order:
                a, b = items.get(k, (0, 0))
                if a == 0 and b == 0:
                    continue
                parts.append(f"{k} {a}/{b}")
                good = good and a == b
            rows.append([self.tr(f"Парність блоків у модулі {bal['module']}",
                                 f"Balanced blocks in module {bal['module']}"),
                         (ok + " — " if good else self.tr("порушено — ", "broken — "))
                         + (", ".join(parts) or "—")])
        if f.areas_used:
            names = ", ".join(f.areas_used)
            if f.areas_missing:
                res = self.tr(f"не знайдено в макеті: {', '.join(f.areas_missing)}",
                              f"not found in the template: {', '.join(f.areas_missing)}")
            else:
                res = self.tr(f"{ok} — усі є в макеті", f"{ok} — all are in the template")
            rows.append([self.tr(f"Області макета, які отримує код ({names})",
                                 f"Template areas the code asks for ({names})"), res])
        for m in f.modules:
            if m.identical:
                rows.append([self.tr(f"Модуль форми {m.label}" if m.owner != "object"
                                     else "Модуль об'єкта", f"Module {m.label}"),
                             self.tr(f"{ok} — збігається з конфігурацією рядок у рядок",
                                     f"{ok} — matches the configuration line by line")])
        if f.fields_checked:
            if f.fields_missing:
                res = self.tr(f"перевірено {f.fields_checked}, не знайдено {f.fields_missing} "
                              "(розділ «Попередження»)",
                              f"{f.fields_checked} checked, {f.fields_missing} not found "
                              "(see “Warnings”)")
            else:
                res = self.tr(f"{ok} — перевірено {f.fields_checked}, усі існують",
                              f"{ok} — {f.fields_checked} checked, all exist")
            rows.append([self.tr("Реквізити, до яких звертаються запити", "Attributes the queries read"),
                         res])
        self.out.append(Table([self.tr("Перевірка", "Check"), self.tr("Результат", "Result")],
                              rows, [2.4, 3.0]))
        self.note("checks")

    # 5. limits --------------------------------------------------------

    def limits(self) -> None:
        f = self.f
        self.heading(self.tr("Межі перевірки", "Limits of the check"))
        items = [self.tr(
            "Файл не запускався в 1С: помилки даних, прав доступу та швидкодії запитів статичний "
            "аналіз не показує.",
            "The file was not run in 1C: data errors, access rights and query performance are "
            "not visible to a static analysis.")]
        if f.has_config:
            age = ""
            if f.config_age:
                age = self.tr(f" Індекс конфігурації має давність {f.config_age}.",
                              f" The configuration index is {f.config_age} old.")
            items.append(self.tr(
                "Вивантаження конфігурації може бути старішим за робочу базу: зміни, зроблені "
                "після нього, в порівняння не потрапили." + age,
                "The configuration dump may be older than the live database: later changes are "
                "not part of the comparison." + age))
            items.append(self.tr(
                "Існування реквізитів у запитах перевіряється за описом об'єктів; після "
                "виправлення блокерів можуть виявитися інші розбіжності.",
                "Attributes in queries are checked against the objects' descriptions; other "
                "differences may surface once the blockers are fixed."))
        if f.hidden:
            items.append(self.tr(
                f"{f.hidden} файлів конфігурації вам недоступні, тому їх вважали відсутніми.",
                f"{f.hidden} configuration file(s) are hidden from you and were treated as absent."))
        for s in f.skipped:
            items.append(self.tr(f"Пропущено: {s}.", f"Skipped: {s}."))
        known = {"no object module": ("У файлі немає модуля об'єкта: уся логіка — у модулях форм",
                                      "The file has no object module: all of its code is in the forms")}
        for n in f.notes:
            uk, en = known.get(n, (f"Примітка до файлу: {n}", f"About the file: {n}"))
            items.append(self.tr(f"{uk}.", f"{en}."))
        items.append(self.tr(
            "Не перевірено: керовані форми (модуль і опис), схеми компонування даних, логіка "
            "коду (чи правильно він рахує). Звіт без зауважень не доводить, що все працює.",
            "Not checked: managed forms (module and description), data composition schemas, what "
            "the code means. A clean report is not a proof that it works."))
        if self.model_note:
            items.append(self.model_note)
        self.out.append(Bullets(items))
        self.note("limits")

    # 6. recommendation --------------------------------------------------

    def recommendation(self) -> None:
        f = self.f
        self.heading(self.tr("Рекомендація", "Recommendation"))
        nom, gen, acc = self.object_word
        if f.level == "bad":
            text = self.tr(
                f"Не підключати цей файл. Якщо в ньому мала бути нова правка, перенести її на "
                f"актуальний модуль {gen} з конфігурації та зберегти зовнішній {nom} уже з нього.",
                "Do not connect this file. If it was meant to carry a new change, apply that "
                "change to the current module in the configuration and save the external file "
                "again from it.")
        elif f.level == "warn":
            text = self.tr(
                "Файл можна підключати лише свідомо: у ньому бракує доробок конфігурації. "
                "Щоб їх не втратити, оновіть файл з поточного модуля в конфігурації.",
                "Connect the file only knowingly: it lacks changes the configuration has. To keep "
                "them, rebuild the file from the current module in the configuration.")
        elif f.level == "good":
            text = self.tr(
                "Файл можна підключати. Після підключення варто один раз сформувати його на "
                "тестових даних, бо запуску в 1С не було.",
                "The file can be connected. Run it once on test data afterwards: it was not "
                "executed in 1C.")
        else:
            text = self.tr(
                "Оберіть конфігурацію, з якою треба порівняти файл, і запустіть перевірку ще раз: "
                "без неї метадані й версію не перевірити.",
                "Choose the configuration to compare with and run the check again: without it "
                "metadata and version cannot be checked.")
        self.drafts["recommendation"] = text
        self.out.append(Para((self.p.recommendation or "").strip() or text, "body"))


def footer_label(facts: Facts, lang: str = DEFAULT_LANG) -> str:
    t = _T(lang)
    when = f"{facts.generated:%Y-%m-%d}" if facts.generated else ""
    return ("Celmis — " + ("Перевірка файлу 1С" if t.uk else "1C file review") + f"  ·  {when}")


_SAFE_NAME = re.compile(r"[^A-Za-z0-9.\-]+")


def export_name(facts: Facts) -> str:
    base = _SAFE_NAME.sub("_", facts.file_name.rsplit(".", 1)[0]).strip("._") or "file"
    return f"celmis-1c-review-{base}"[:80]


__all__ = ["template_drafts", "DEFAULT_LANG", "LANGS", "Prose", "build_document", "export_name", "footer_label"]
