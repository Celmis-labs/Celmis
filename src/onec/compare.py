"""An external file against its counterpart in a configuration.

The counterpart is the object of the same kind and name (`Reports.<Name>`, the
other external kind as a fallback), or the one the caller names. Modules are
compared procedure by procedure, the code outside procedures as one more
piece; trailing whitespace does not count as a change.

Change markers are the task ids people leave in comments (`// ABC-123 …`) and
the dates beside them. Which markers the configuration has and the file does
not is what says the file is older than the configuration; the dates and the
configuration's history say by how much.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime

from src.indexing.graph.languages.onec import TYPE_BY_NAME
from src.onec.bsl_text import Procedure, body_text, procedures, split_lines
from src.onec.config import Config
from src.onec.external import ExternalFile, handlers_of

#: A task id as trackers write them: `ABC-123`.
DEFAULT_MARKER = r"(?<![\w-])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?![\w-])"
_DATE = re.compile(r"(?<!\d)(?:(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})|(\d{4})-(\d{2})-(\d{2}))(?!\d)")
_OUTSIDE = "(module code outside procedures)"


@dataclass
class ProcChange:
    name: str
    kind: str          # "added" | "removed" | "changed"
    file_lines: int    # lines in the file (0 when removed)
    config_lines: int  # lines in the configuration (0 when added)
    plus: int = 0      # lines the file has that the configuration does not
    minus: int = 0     # lines the configuration has that the file does not
    file_start: int = 0
    config_start: int = 0


@dataclass
class ModuleDiff:
    label: str               # "object module", "form Основная"
    file_path: str
    config_path: str | None  # None: no counterpart module
    changes: list[ProcChange] = field(default_factory=list)
    same: int = 0
    note: str = ""


@dataclass
class Marker:
    id: str
    dates: list[datetime]
    where: list[str]  # "path:line"

    @property
    def latest(self) -> datetime | None:
        return max(self.dates) if self.dates else None


@dataclass
class Comparison:
    config_label: str
    counterpart: str | None          # "Reports.Name", None when not found
    counterpart_path: str = ""       # the object's folder in the dump
    reason: str = ""                 # why there is no counterpart
    modules: list[ModuleDiff] = field(default_factory=list)
    forms_missing: list[str] = field(default_factory=list)   # in the configuration, not the file
    forms_extra: list[str] = field(default_factory=list)     # in the file, not the configuration
    templates_missing: list[str] = field(default_factory=list)
    templates_extra: list[str] = field(default_factory=list)
    handler_changes: list[str] = field(default_factory=list)
    file_markers: dict[str, Marker] = field(default_factory=dict)
    config_markers: dict[str, Marker] = field(default_factory=dict)
    config_changed: datetime | None = None
    file_saved: datetime | None = None
    verdict: str = ""
    verdict_why: list[str] = field(default_factory=list)
    hidden: int = 0

    @property
    def missing_markers(self) -> list[Marker]:
        """Markers of the configuration's counterpart the file does not carry."""
        return sorted((m for k, m in self.config_markers.items() if k not in self.file_markers),
                      key=lambda m: (m.latest or datetime.min), reverse=True)

    @property
    def extra_markers(self) -> list[Marker]:
        return sorted((m for k, m in self.file_markers.items() if k not in self.config_markers),
                      key=lambda m: (m.latest or datetime.min), reverse=True)


def _date(m: re.Match) -> datetime | None:
    try:
        if m.group(1):
            year = int(m.group(3))
            year += 2000 if year < 100 else 0
            return datetime(year, int(m.group(2)), int(m.group(1)))
        return datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None


def markers_of(text: str, path: str, pattern: re.Pattern[str],
               into: dict[str, Marker] | None = None) -> dict[str, Marker]:
    """Task ids in the comments of ``text``, each with the dates written beside it."""
    out = into if into is not None else {}
    for i, (_code, comment) in enumerate(split_lines(text)):
        if not comment:
            continue
        ids = pattern.findall(comment)
        if not ids:
            continue
        dates = [d for d in (_date(m) for m in _DATE.finditer(comment))
                 if d is not None and 1990 <= d.year <= 2100]
        for raw in ids:
            key = (raw if isinstance(raw, str) else raw[0]).upper()
            mk = out.setdefault(key, Marker(key, [], []))
            mk.dates.extend(dates)
            if len(mk.where) < 5:
                mk.where.append(f"{path}:{i + 1}")
    return out


def _pieces(text: str) -> dict[str, tuple[Procedure | None, str]]:
    """lower-case name → (procedure, normalized text); the code outside the
    procedures is one more piece."""
    procs = procedures(text)
    lines = text.replace("\r\n", "\n").split("\n")
    inside = set()
    out: dict[str, tuple[Procedure | None, str]] = {}
    for p in procs:
        inside.update(range(max(0, p.start - 1 - len(p.directives)), p.end))
        out.setdefault(p.name.lower(), (p, body_text(text, p)))
    rest = "\n".join(ln.rstrip() for i, ln in enumerate(lines)
                     if i not in inside and ln.strip())
    if rest:
        out[_OUTSIDE.lower()] = (None, rest)
    return out


def diff_modules(label: str, file_path: str, file_text: str,
                 config_path: str | None, config_text: str | None) -> ModuleDiff:
    out = ModuleDiff(label, file_path, config_path)
    if config_text is None:
        out.note = "no counterpart module in the configuration"
        return out
    mine, theirs = _pieces(file_text), _pieces(config_text)
    for key, (proc, body) in mine.items():
        name = proc.name if proc else _OUTSIDE
        other = theirs.get(key)
        n = body.count("\n") + 1
        if other is None:
            out.changes.append(ProcChange(name, "added", n, 0, n, 0,
                                          proc.start if proc else 0))
            continue
        if other[1] == body:
            out.same += 1
            continue
        a, b = other[1].split("\n"), body.split("\n")
        plus = minus = 0
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op != "equal":
                minus += i2 - i1
                plus += j2 - j1
        out.changes.append(ProcChange(name, "changed", len(b), len(a), plus, minus,
                                      proc.start if proc else 0,
                                      other[0].start if other[0] else 0))
    for key, (proc, body) in theirs.items():
        if key not in mine:
            n = body.count("\n") + 1
            out.changes.append(ProcChange(proc.name if proc else _OUTSIDE, "removed", 0, n,
                                          0, n, 0, proc.start if proc else 0))
    return out


def counterpart(ext: ExternalFile, cfg: Config, override: str = "") -> tuple[str, str] | None:
    """(kind, the dump's spelling of the name) of the object to compare with."""
    if override:
        kind, _, name = override.strip().rpartition(".")
        kinds = [kind] if kind else ["Reports", "DataProcessors"]
        for k in kinds:
            plural = TYPE_BY_NAME.get(k.lower(), k)
            real = cfg.find(plural, name)
            if real:
                return plural, real
        return None
    if ext.kind not in ("Reports", "DataProcessors"):
        return None
    for k in (ext.kind, "DataProcessors" if ext.kind == "Reports" else "Reports"):
        real = cfg.find(k, ext.name)
        if real:
            return k, real
    return None


def compare(ext: ExternalFile, cfg: Config, override: str = "",
            marker_pattern: str = DEFAULT_MARKER) -> Comparison:
    pattern = re.compile(marker_pattern)
    out = Comparison(cfg.label, None, file_saved=ext.saved)
    for path, text in ext.texts.items():
        if path.endswith(".bsl"):
            markers_of(text, path, pattern, out.file_markers)
    found = counterpart(ext, cfg, override)
    if found is None:
        out.reason = (f"no object named {override or ext.name} among "
                      + ("the named kind" if override and "." in override
                         else "reports and data processors")
                      + " of the configuration")
        out.verdict = "no counterpart"
        return out
    kind, name = found
    out.counterpart = f"{kind}.{name}"
    folder = cfg._rel(kind, name)
    out.counterpart_path = folder

    obj = next((m for m in ext.modules if m.owner == "object"), None)
    cfg_obj = cfg.module(kind, name, "ObjectModule")
    if cfg_obj:
        markers_of(cfg_obj[1], cfg_obj[0], pattern, out.config_markers)
    if obj is not None and obj.text is not None:
        out.modules.append(diff_modules("object module", obj.path, obj.text,
                                        cfg_obj[0] if cfg_obj else None,
                                        cfg_obj[1] if cfg_obj else None))
    elif cfg_obj:
        out.modules.append(ModuleDiff("object module", obj.path if obj else "", cfg_obj[0],
                                      note="the configuration has an object module, "
                                           "the file has none"))

    cfg_forms = cfg.forms(kind, name)
    file_forms = {f.name.lower(): f for f in ext.forms}
    for key, real in sorted(cfg_forms.items()):
        got = cfg.form(kind, name, real)
        if got is None:
            continue
        rel, text, description = got
        if text:
            markers_of(text, rel, pattern, out.config_markers)
        mine = file_forms.get(key)
        if mine is None:
            out.forms_missing.append(real)
            continue
        if mine.module and mine.module.text is not None:
            out.modules.append(diff_modules(f"form {mine.name}", mine.module.path,
                                            mine.module.text, rel, text))
        if description is not None:
            theirs = {h.lower(): h for h in handlers_of(description)}
            ours = {h.lower(): h for h in mine.handlers}
            for k in sorted(set(ours) - set(theirs)):
                out.handler_changes.append(f"form {mine.name}: handler {ours[k]} is bound "
                                           "in the file, not in the configuration")
            for k in sorted(set(theirs) - set(ours)):
                out.handler_changes.append(f"form {mine.name}: handler {theirs[k]} is bound "
                                           "in the configuration, not in the file")
    out.forms_extra = sorted(f.name for k, f in file_forms.items() if k not in cfg_forms)
    cfg_tpl = cfg.templates(kind, name)
    file_tpl = {t.name.lower(): t.name for t in ext.templates}
    out.templates_missing = sorted(t for t in cfg_tpl if t not in file_tpl)
    out.templates_extra = sorted(v for k, v in file_tpl.items() if k not in cfg_tpl)
    out.config_changed = cfg.last_changed(folder)
    out.hidden = len(cfg.source.hidden)
    _verdict(out)
    return out


def _verdict(c: Comparison) -> None:
    changed = sum(len(m.changes) for m in c.modules)
    missing, extra = c.missing_markers, c.extra_markers
    why = c.verdict_why
    if missing:
        latest = max((m.latest for m in missing if m.latest), default=None)
        why.append(f"the configuration carries {len(missing)} change marker(s) the file "
                   "does not" + (f", the latest dated {latest:%Y-%m-%d}" if latest else ""))
    if extra:
        why.append(f"the file carries {len(extra)} change marker(s) the configuration does not")
    if c.file_saved and c.config_changed:
        if c.config_changed > c.file_saved:
            why.append(f"the counterpart changed in the repository on "
                       f"{c.config_changed:%Y-%m-%d}, after the file was saved "
                       f"({c.file_saved:%Y-%m-%d})")
        else:
            why.append(f"the file was saved ({c.file_saved:%Y-%m-%d}) after the counterpart's "
                       f"last change in the repository ({c.config_changed:%Y-%m-%d})")
    if changed == 0 and not c.forms_missing and not c.forms_extra and not c.handler_changes:
        c.verdict = "same code as the configuration"
    elif missing and extra:
        c.verdict = "diverged: each side has changes the other lacks"
    elif missing:
        c.verdict = "older than the configuration"
    elif extra:
        c.verdict = "newer than the configuration (has changes it lacks)"
    elif c.file_saved and c.config_changed and c.config_changed > c.file_saved:
        c.verdict = "differs; probably older than the configuration (by dates only)"
    elif c.file_saved and c.config_changed:
        c.verdict = "differs; probably newer than the configuration (by dates only)"
    else:
        c.verdict = "differs; no markers or dates to tell which side is newer"
