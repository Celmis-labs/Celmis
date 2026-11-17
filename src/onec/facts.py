"""What a check of an external 1C file found, as one structured object.

The report people read (`review_doc`) and the text a model may be asked to
word (`review_llm`) are both built from this and nothing else. It holds no
sentences: names, numbers, line numbers and code excerpts, every one read from
the file, the configuration or the static checks, so no later step can invent
a fact it was not handed.

Built from what `src.onec` already produces (the unpacked file, the procedure
diff against the configuration, the static checks) plus the counts and the code
excerpts a human reviewer would put in the report: the module sizes, the last
change marker with its date, the procedures one side lacks, the lines where a
missing object is used, the nearest line of the configuration to show beside it.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime

from src.onec.bsl_text import Procedure, procedures, split_lines
from src.onec.checks import CheckResult, Finding
from src.onec.compare import (
    DEFAULT_MARKER,
    Comparison,
    Marker,
    ModuleDiff,
    all_markers_of,
)
from src.onec.config import Config
from src.onec.external import ExternalFile

#: Lines of code shown on either side of one blocker.
SNIPPET_MAX = 6
MAX_BLOCKERS = 12
MAX_SIMILAR = 8
MAX_NAMES = 12
_VAR = re.compile(r"^\s*(?:Перем|Var)\s+[^\W\d]\w*", re.IGNORECASE)
_COMMENT_VERSION = re.compile(r"<(?:Comment|comment)>([^<]*)</(?:Comment|comment)>")
_AFTER_AS = re.compile(r"(?:КАК|AS)\s+([^\W\d]\w*)", re.IGNORECASE)


@dataclass
class Snippet:
    module: str          # the module as a person names it (a form's name, "object module")
    path: str
    start: int           # first line shown
    lines: list[str]     # the text of the lines, without numbers


@dataclass
class Blocker:
    id: str
    code: str            # see checks.Finding.code
    subject: str
    path: str
    module: str
    line: int
    procedure: str = ""
    plural: str = ""     # the type directory of a missing object
    name: str = ""       # a missing object's name as the file writes it
    where: str = ""      # query | string | code
    message: str = ""    # the check's own (English) finding, the fallback text
    data: dict = field(default_factory=dict)
    file_snippet: Snippet | None = None
    config_snippet: Snippet | None = None
    #: a task marker in the comment above the configuration's version of the line
    config_marker: str = ""
    similar: list[str] = field(default_factory=list)     # configuration objects of a close name
    other_lines: list[int] = field(default_factory=list)  # more lines of the file using it
    fields: list[str] = field(default_factory=list)       # the object's fields the file reads
    count: int = 1       # places of the file with this problem


@dataclass
class ModuleFacts:
    label: str                 # form name or "object module"
    owner: str                 # "object" or the form's name
    file_path: str
    config_path: str | None
    file_lines: int = 0
    config_lines: int = 0
    file_bytes: int = 0
    config_bytes: int = 0
    file_desc_bytes: int | None = None
    config_desc_bytes: int | None = None
    file_last_marker: str = ""   # id of the newest dated marker in the module's code
    file_last_marker_numeric: bool = False
    file_last_marker_date: datetime | None = None
    config_last_marker: str = ""
    config_last_marker_numeric: bool = False
    config_last_marker_date: datetime | None = None
    unique_file: int = 0       # lines the file has that the configuration lacks
    unique_config: int = 0
    hunks: int = 0
    same_procedures: int = 0
    changed_procedures: int = 0
    identical: bool = False
    missing: list[str] = field(default_factory=list)     # headers the file lacks
    extra: list[str] = field(default_factory=list)       # headers only the file has
    missing_vars: list[str] = field(default_factory=list)
    extra_vars: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class MarkerFact:
    id: str
    numeric: bool
    first: datetime | None
    last: datetime | None
    where: str = ""


@dataclass
class Facts:
    # the file
    file_name: str
    size: int
    sha256: str
    kind: str                  # Reports | DataProcessors | Form | Container
    object_name: str
    synonym: str
    comment: str
    created: datetime | None
    saved: datetime | None
    entries: int
    forms: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    module_count: int = 0
    # the configuration
    has_config: bool = False
    no_config_reason: str = ""
    config_label: str = ""
    config_ref: str = ""        # branch@sha
    config_age: str = ""        # "12 days" style text the caller formed
    config_files_date: datetime | None = None
    config_changed: datetime | None = None
    counterpart: str = ""
    counterpart_path: str = ""
    config_comment: str = ""
    config_last_marker: str = ""
    config_last_marker_numeric: bool = False
    config_last_marker_date: datetime | None = None
    same_comment: bool = False
    hidden: int = 0
    # the comparison
    verdict: str = ""
    verdict_why: list[str] = field(default_factory=list)
    modules: list[ModuleFacts] = field(default_factory=list)
    forms_missing: list[str] = field(default_factory=list)
    forms_extra: list[str] = field(default_factory=list)
    templates_missing: list[str] = field(default_factory=list)
    templates_extra: list[str] = field(default_factory=list)
    handler_changes: list[str] = field(default_factory=list)
    markers_missing: list[MarkerFact] = field(default_factory=list)
    markers_extra: list[MarkerFact] = field(default_factory=list)
    markers_shared: int = 0
    # the static checks
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)   # path, line, message
    inherited: list[dict] = field(default_factory=list)  # the configuration has the same
    errors: int = 0
    skipped: list[str] = field(default_factory=list)
    refs_ok: dict[str, int] = field(default_factory=dict)       # type directory → count
    refs_missing: dict[str, int] = field(default_factory=dict)
    calls_total: int = 0
    calls_bad: int = 0
    handlers_total: int = 0
    handlers_missing: int = 0
    handlers_inherited: int = 0
    balance: list[dict] = field(default_factory=list)  # {module, items: {name: [open, close]}}
    areas_used: list[str] = field(default_factory=list)
    areas_missing: list[str] = field(default_factory=list)
    fields_checked: int = 0
    fields_missing: int = 0
    # the verdict of the whole check
    level: str = "unknown"      # bad | warn | good | unknown
    generated: datetime | None = None


def to_plain(value):  # noqa: ANN001, ANN201
    """A fact object as JSON-ready plain data (datetimes become ISO text)."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: to_plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


# ─── helpers ───────────────────────────────────────────────────────────

def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).lower()


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _owner_label(ext: ExternalFile, path: str) -> tuple[str, str]:
    """(owner, label) of a module of the file: ("object", "object module") or the form."""
    for m in ext.modules:
        if m.path == path:
            return m.owner, ("object module" if m.owner == "object" else m.owner)
    return "", path


def _procedure_at(text: str, line: int) -> Procedure | None:
    for p in procedures(text):
        if p.start <= line <= p.end:
            return p
    return None


def _last_marker(text: str, path: str, pattern: re.Pattern[str]) -> Marker | None:
    marks = all_markers_of(text, path, pattern)
    dated = [m for m in marks.values() if m.dates]
    return max(dated, key=lambda m: m.latest) if dated else None


def _marker_fact(m: Marker) -> MarkerFact:
    return MarkerFact(m.id, m.numeric, min(m.dates) if m.dates else None,
                      m.latest, m.where[0] if m.where else "")


def _top_vars(text: str) -> list[str]:
    inside: set[int] = set()
    for p in procedures(text):
        inside.update(range(max(0, p.start - 1 - len(p.directives)), p.end))
    out = []
    for i, ln in enumerate(_lines(text)):
        if i not in inside and _VAR.match(ln):
            out.append(ln.strip())
    return out


def _header(text: str, proc: Procedure) -> str:
    return _lines(text)[proc.start - 1].strip()


def _config_module(cfg: Config, kind: str, name: str, owner: str):  # noqa: ANN202
    """(path, text, description) of the configuration's module for ``owner``."""
    if owner == "object":
        got = cfg.module(kind, name, "ObjectModule")
        return (got[0], got[1], None) if got else None
    got = cfg.form(kind, name, owner)
    return got if got and got[1] is not None else None


def _snippet(label: str, path: str, text: str, start: int, count: int) -> Snippet:
    lines = _lines(text)
    start = max(1, min(start, len(lines)))
    chunk = [ln.rstrip() for ln in lines[start - 1:start - 1 + max(1, min(count, SNIPPET_MAX))]]
    return Snippet(label, path, start, chunk)


def _statement_len(lines: list[str], at: int) -> int:
    """1 or 2: a query line continues on the next one that starts with `|`."""
    return 2 if at < len(lines) and lines[at].lstrip().startswith("|") else 1


def _nearest(cfg_text: str, file_line: str, near: Procedure | None) -> int:
    """1-based line of the configuration's text most like ``file_line``; 0 when none is close."""
    cl = _lines(cfg_text)
    lo, hi = (near.start - 1, near.end) if near else (0, len(cl))
    pool = [(i, _norm(ln)) for i, ln in enumerate(cl[lo:hi], lo + 1) if ln.strip()]
    if not pool:
        return 0
    target = _norm(file_line)
    if not target:
        return 0
    best, best_i = 0.0, 0
    for i, n in pool:
        sm = difflib.SequenceMatcher(None, target, n, autojunk=False)
        if sm.real_quick_ratio() < best or sm.quick_ratio() < best:
            continue
        r = sm.ratio()
        if r > best:
            best, best_i = r, i
    return best_i if best >= 0.6 else 0


def _similar(cfg: Config, plural: str, name: str) -> list[str]:
    names = cfg.names(plural)
    stem = name.lower()[:max(6, (len(name) + 1) // 2)]
    hits = [real for key, real in names.items() if stem and key.startswith(stem)]
    if len(hits) < 2:
        hits += [real for key, real in names.items()
                 if stem and stem in key and real not in hits]
    hits.sort(key=lambda r: -difflib.SequenceMatcher(None, name.lower(), r.lower()).ratio())
    return sorted((unicodedata.normalize("NFC", h) for h in hits[:MAX_SIMILAR]), key=str.lower)


# ─── the facts ─────────────────────────────────────────────────────────

def build_facts(  # noqa: PLR0912, PLR0915
    ext: ExternalFile, data: bytes, cfg: Config | None, comparison: Comparison | None,
    checks: CheckResult, *, config_info: dict | None = None,
    marker_pattern: str = DEFAULT_MARKER, now: datetime | None = None,
) -> Facts:
    info = config_info or {}
    pattern = re.compile(marker_pattern)
    syn = (ext.synonyms.get("ru") or ext.synonyms.get("uk") or ext.synonyms.get("en")
           or next(iter(ext.synonyms.values()), ""))
    facts = Facts(
        file_name=ext.file_name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        kind=ext.kind, object_name=ext.name, synonym=syn, comment=ext.comment,
        created=ext.created, saved=ext.saved, entries=ext.entries,
        forms=[f.name for f in ext.forms], templates=[t.name for t in ext.templates],
        notes=list(ext.notes), module_count=sum(1 for m in ext.modules if m.text is not None),
        generated=now or datetime.now(),
    )
    facts.has_config = comparison is not None and comparison.counterpart is not None
    facts.config_label = info.get("label") or (cfg.label if cfg else "")
    facts.config_ref = info.get("ref", "")
    facts.config_age = info.get("age", "")
    facts.config_files_date = info.get("files_date")
    facts.no_config_reason = info.get("reason", "")
    if comparison is not None and comparison.counterpart is None:
        facts.no_config_reason = facts.no_config_reason or comparison.reason

    cfg_texts: dict[str, tuple[str, str, str | None] | None] = {}
    kind = name = ""
    if comparison is not None and comparison.counterpart:
        kind, _, name = comparison.counterpart.partition(".")
        facts.counterpart = comparison.counterpart
        facts.counterpart_path = comparison.counterpart_path
        facts.config_changed = comparison.config_changed
        facts.hidden = comparison.hidden
        facts.verdict, facts.verdict_why = comparison.verdict, list(comparison.verdict_why)
        facts.forms_missing = list(comparison.forms_missing)
        facts.forms_extra = list(comparison.forms_extra)
        facts.templates_missing = list(comparison.templates_missing)
        facts.templates_extra = list(comparison.templates_extra)
        facts.handler_changes = list(comparison.handler_changes)
        facts.markers_missing = [_marker_fact(m) if m.dates else MarkerFact(
            m.id, m.numeric, None, None, m.where[0] if m.where else "")
            for m in comparison.missing_markers]
        facts.markers_extra = [MarkerFact(m.id, m.numeric, min(m.dates) if m.dates else None,
                                          m.latest, m.where[0] if m.where else "")
                               for m in comparison.extra_markers]
        facts.markers_shared = len(comparison.file_markers) - len(comparison.extra_markers)
        xml = cfg.object_xml(kind, name) if cfg else None
        m = _COMMENT_VERSION.search(xml or "")
        facts.config_comment = (m.group(1).strip() if m else "")
        facts.same_comment = bool(facts.config_comment and facts.config_comment == ext.comment)
        newest = [mk for mk in comparison.config_markers.values() if mk.dates]
        if newest:
            top = max(newest, key=lambda mk: mk.latest)
            facts.config_last_marker = top.id
            facts.config_last_marker_numeric = top.numeric
            facts.config_last_marker_date = top.latest
        for md in comparison.modules:
            owner, _lbl = _owner_label(ext, md.file_path)
            cfg_texts[md.file_path] = (_config_module(cfg, kind, name, owner)
                                       if cfg and owner else None)
            facts.modules.append(_module_facts(ext, md, owner, cfg_texts[md.file_path], pattern))

    _fill_checks(facts, ext, cfg, checks, cfg_texts, kind, name)
    facts.level = _level(facts)
    return facts


def _module_facts(ext: ExternalFile, md: ModuleDiff, owner: str,
                  config: tuple[str, str, str | None] | None,
                  pattern: re.Pattern[str]) -> ModuleFacts:
    text = ext.texts.get(md.file_path, "")
    label = "object module" if owner == "object" else (owner or md.label)
    mf = ModuleFacts(label=label, owner=owner, file_path=md.file_path,
                     config_path=md.config_path, note=md.note,
                     file_lines=len(text.splitlines()),
                     file_bytes=len(text.encode("utf-8")))
    form = next((f for f in ext.forms if f.name == owner), None)
    if form is not None and form.description_path in ext.texts:
        mf.file_desc_bytes = len(ext.texts[form.description_path].encode("utf-8"))
    fm = _last_marker(text, md.file_path, pattern) if text else None
    if fm:
        mf.file_last_marker, mf.file_last_marker_numeric = fm.id, fm.numeric
        mf.file_last_marker_date = fm.latest
    if config is not None:
        _path, ctext, desc = config
        mf.config_lines = len(ctext.splitlines())
        mf.config_bytes = len(ctext.encode("utf-8"))
        mf.config_desc_bytes = len(desc.encode("utf-8")) if desc is not None else None
        cm = _last_marker(ctext, _path, pattern)
        if cm:
            mf.config_last_marker, mf.config_last_marker_numeric = cm.id, cm.numeric
            mf.config_last_marker_date = cm.latest
        mf.unique_file = sum(c.plus for c in md.changes)
        mf.unique_config = sum(c.minus for c in md.changes)
        mf.hunks = sum(c.hunks for c in md.changes)
        mf.same_procedures = md.same
        mf.changed_procedures = sum(1 for c in md.changes if c.kind == "changed")
        mf.identical = not md.changes and not md.note
        cfg_procs = {p.name.lower(): p for p in procedures(ctext)}
        file_procs = {p.name.lower(): p for p in procedures(text)}
        for c in md.changes:
            key = c.name.lower()
            if c.kind == "removed" and key in cfg_procs:
                mf.missing.append(_header(ctext, cfg_procs[key]))
            elif c.kind == "added" and key in file_procs:
                mf.extra.append(_header(text, file_procs[key]))
        a, b = _top_vars(text), _top_vars(ctext)
        an, bn = {_norm(x) for x in a}, {_norm(x) for x in b}
        mf.extra_vars = [x for x in a if _norm(x) not in bn]
        mf.missing_vars = [x for x in b if _norm(x) not in an]
    return mf


def _fill_checks(facts: Facts, ext: ExternalFile, cfg: Config | None, checks: CheckResult,
                 cfg_texts: dict, kind: str, name: str) -> None:
    st = checks.stats
    facts.errors = checks.errors
    facts.skipped = list(dict.fromkeys(checks.skipped))
    for (plural, _low), real in st.refs.items():
        bucket = facts.refs_ok if real is not None else facts.refs_missing
        bucket[plural] = bucket.get(plural, 0) + 1
    facts.calls_total, facts.calls_bad = st.calls_total, st.calls_bad
    facts.handlers_total = st.handlers_total
    facts.handlers_missing = sum(1 for f in checks.findings
                                 if f.code == "handler" and not f.pre_existing)
    facts.handlers_inherited = sum(1 for f in checks.findings
                                   if f.code == "handler" and f.pre_existing)
    facts.fields_checked, facts.fields_missing = st.fields_checked, st.fields_missing
    facts.areas_used = sorted(st.areas_used, key=str.lower)
    facts.areas_missing = sorted(st.areas_missing, key=str.lower)
    for path, items in st.balance.items():
        _owner, label = _owner_label(ext, path)
        facts.balance.append({"module": label,
                              "items": {k: [v[0], v[1]] for k, v in items.items()}})

    groups: dict[tuple[str, str], Blocker] = {}
    order: list[Blocker] = []
    for f in sorted(checks.findings, key=lambda f: (f.path, f.line)):
        if f.level == "warning":
            if len(facts.warnings) < 40:
                facts.warnings.append({"path": f.path, "line": f.line, "message": f.message,
                                       "module": _owner_label(ext, f.path)[1]})
            continue
        if f.pre_existing:
            facts.inherited.append({"path": f.path, "line": f.line, "message": f.message,
                                    "module": _owner_label(ext, f.path)[1]})
            continue
        key = (f.code, f.subject.lower()) if f.code in ("missing_object", "missing_method") \
            else (f.code, f"{f.subject.lower()}@{f.path}:{f.line}")
        got = groups.get(key)
        if got is not None:
            got.count += 1
            got.other_lines.append(f.line)
            continue
        b = _blocker(f, ext, cfg, cfg_texts, kind, name, len(order) + 1)
        groups[key] = b
        order.append(b)
    for b in order:
        _complete(b, ext)
    facts.blockers = order[:MAX_BLOCKERS]


def _blocker(f: Finding, ext: ExternalFile, cfg: Config | None, cfg_texts: dict,
             kind: str, name: str, n: int) -> Blocker:
    owner, label = _owner_label(ext, f.path)
    text = ext.texts.get(f.path, "")
    proc = _procedure_at(text, f.line) if text else None
    b = Blocker(id=f"b{n}", code=f.code, subject=f.subject, path=f.path, module=label,
                line=f.line, procedure=proc.name if proc else "", message=f.message,
                data=dict(f.data), plural=f.data.get("plural", ""), name=f.data.get("name", ""),
                where=f.data.get("where", ""))
    if text and f.line >= 1:
        flines = _lines(text)
        count = _statement_len(flines, f.line) if f.code == "missing_object" else 1
        b.file_snippet = _snippet(label, f.path, text, f.line, count)
        cfg_mod = cfg_texts.get(f.path)
        if cfg_mod is not None and cfg_mod[1] is not None:
            ctext = cfg_mod[1]
            cl = _lines(ctext)
            cproc = next((p for p in procedures(ctext)
                          if proc and p.name.lower() == proc.name.lower()), None)
            at = _nearest(ctext, flines[f.line - 1], cproc) if f.line <= len(flines) else 0
            if at:
                start = at
                above = []
                i = at - 2
                while i >= 0 and cl[i].lstrip().startswith("//") and len(above) < 2:
                    above.append(i + 1)
                    i -= 1
                if above:
                    start = min(above)
                    mk = all_markers_of("\n".join(cl[start - 1:at - 1]), "", re.compile(DEFAULT_MARKER))
                    if mk:
                        b.config_marker = next(iter(mk))
                b.config_snippet = _snippet(label, cfg_mod[0], ctext, start,
                                            (at - start) + len(b.file_snippet.lines))
    if f.code == "missing_object" and cfg is not None and b.plural and b.name:
        b.similar = _similar(cfg, b.plural, b.name)
    return b


def _complete(b: Blocker, ext: ExternalFile) -> None:
    """The other lines of the file that use a missing object, and the fields read."""
    if b.code != "missing_object" or not b.name:
        b.other_lines = sorted(set(b.other_lines))
        return
    text = ext.texts.get(b.path, "")
    if not text:
        return
    alias = ""
    if b.file_snippet:
        for ln in b.file_snippet.lines:
            m = _AFTER_AS.search(ln)
            if m:
                alias = m.group(1)
                break
    lows = [b.name.lower()]
    flds: dict[str, None] = {}
    lines = _lines(text)
    shown = set(range(b.file_snippet.start, b.file_snippet.start + len(b.file_snippet.lines))) \
        if b.file_snippet else set()
    found: set[int] = set(b.other_lines)
    alias_rx = re.compile(rf"(?<![\w.]){re.escape(alias)}\.([^\W\d]\w*)", re.IGNORECASE) \
        if alias else None
    for i, ln in enumerate(lines, 1):
        code, _c = split_lines(ln)[0] if ln.strip() else ("", "")
        low = code.lower()
        hit = any(w in low for w in lows)
        if alias_rx:
            for m in alias_rx.finditer(code):
                hit = True
                flds.setdefault(m.group(1), None)
        if hit and i not in shown:
            found.add(i)
    b.other_lines = sorted(found - shown)
    b.fields = list(flds)[:MAX_NAMES]


def _level(f: Facts) -> str:
    if f.blockers:
        return "bad"
    if not f.has_config:
        return "unknown"
    if f.markers_missing or f.verdict.startswith(("older", "diverged")) \
            or f.forms_missing or f.templates_missing or any(m.missing for m in f.modules):
        return "warn"
    return "good"


def numbers_in(value) -> set[int]:  # noqa: ANN001
    """Every number written anywhere in the facts: what a model's prose may repeat."""
    out: set[int] = set()
    for m in re.finditer(r"\d+", repr(to_plain(value))):
        try:
            out.add(int(m.group(0)))
        except ValueError:
            continue
    return out


__all__ = ["Blocker", "Facts", "MarkerFact", "ModuleFacts", "Snippet", "build_facts",
           "numbers_in", "to_plain"]
