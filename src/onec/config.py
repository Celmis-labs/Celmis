"""A configuration dump to check an external file against.

Read through a :class:`Source`: a directory on disk (a checkout, an unpacked
archive, an agent session's clone) or a git revision (the indexed commit of a
repository, read with `git cat-file`). Either can hide paths the caller may not
read: a hidden file is treated as absent and counted, so a report can say how
much it could not see instead of calling a hidden object missing.

Both dump layouts are understood: Designer (`Catalogs/X.xml`,
`Catalogs/X/Ext/ObjectModule.bsl`, an ordinary form's `Ext/Form.bin`) and EDT
(`src/Catalogs/X/X.mdo`, `src/Catalogs/X/ObjectModule.bsl`). 1C names are
case-blind; the dump's own spelling is looked up once per type directory.
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.onec.container import form_module, read_container, text_of

_MAX_READ = 32 * 1024 * 1024


class Source:
    """Files of one revision of a repository."""

    hidden: set[str]

    def list_dir(self, rel: str) -> list[str] | None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, rel: str) -> bytes | None:  # pragma: no cover - interface
        raise NotImplementedError

    def last_changed(self, rel: str) -> datetime | None:
        return None


def _everything(_rel: str) -> bool:
    return True


class DirSource(Source):
    def __init__(self, root: Path, readable: Callable[[str], bool] | None = None,
                 listable: Callable[[str], bool] | None = None) -> None:
        self.root = Path(root).resolve()
        self.readable = readable or _everything
        self.listable = listable or _everything
        self.hidden = set()

    def _path(self, rel: str) -> Path | None:
        try:
            p = (self.root / rel).resolve()
        except (OSError, ValueError):
            return None
        return p if p == self.root or p.is_relative_to(self.root) else None

    def list_dir(self, rel: str) -> list[str] | None:
        p = self._path(rel)
        if p is None or not p.is_dir():
            return None
        try:
            names = sorted(c.name for c in p.iterdir() if not c.is_symlink())
        except OSError:
            return None
        return [n for n in names if self.listable(f"{rel}/{n}" if rel else n)]

    def read(self, rel: str) -> bytes | None:
        p = self._path(rel)
        if p is None or not p.is_file() or p.is_symlink():
            return None
        if not self.readable(rel):
            self.hidden.add(rel)
            return None
        try:
            if p.stat().st_size > _MAX_READ:
                return None
            return p.read_bytes()
        except OSError:
            return None

    def last_changed(self, rel: str) -> datetime | None:
        if not (self.root / ".git").exists():
            return None
        return _git_date(self.root, "HEAD", rel)


class GitSource(Source):
    """The files of ``sha`` in the clone at ``clone``."""

    def __init__(self, clone: Path, sha: str,
                 readable: Callable[[str], bool] | None = None,
                 listable: Callable[[str], bool] | None = None) -> None:
        self.clone, self.sha = Path(clone), sha
        self.readable = readable or _everything
        self.listable = listable or _everything
        self.hidden = set()

    def _git(self, *args: str, timeout: float = 20) -> bytes | None:
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", "-c", "core.quotepath=off", "-C", str(self.clone), *args],
                capture_output=True, timeout=timeout, check=False,
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "PATH": _path()})
        except (subprocess.TimeoutExpired, OSError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def list_dir(self, rel: str) -> list[str] | None:
        out = self._git("ls-tree", "--name-only", "-z", f"{self.sha}:{rel}" if rel else self.sha)
        if out is None:
            return None
        names = sorted(n for n in out.decode("utf-8", errors="replace").split("\0") if n)
        return [n for n in names if self.listable(f"{rel}/{n}" if rel else n)]

    def read(self, rel: str) -> bytes | None:
        if not self.readable(rel):
            if self.list_dir_has(rel):
                self.hidden.add(rel)
            return None
        size = self._git("cat-file", "-s", f"{self.sha}:{rel}")
        try:
            if size is None or int(size.strip()) > _MAX_READ:
                return None
        except ValueError:
            return None
        return self._git("cat-file", "blob", f"{self.sha}:{rel}", timeout=30)

    def list_dir_has(self, rel: str) -> bool:
        parent, _, name = rel.rpartition("/")
        return name in (self.list_dir(parent) or ())

    def last_changed(self, rel: str) -> datetime | None:
        return _git_date(self.clone, self.sha, rel)


def _path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


def _git_date(repo: Path, rev: str, rel: str) -> datetime | None:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "log", "-1", "--format=%cI", rev, "--", rel],
            capture_output=True, text=True, timeout=20, check=False,
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "PATH": _path()})
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = proc.stdout.strip()
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None) if raw else None
    except ValueError:
        return None


_CHILD = re.compile(
    r"<(Attribute|TabularSection|Dimension|Resource|AddressingAttribute|AccountingFlag|"
    r"ExtDimensionAccountingFlag|EnumValue|Form|Template|Command)\b[^>]*>\s*"
    r"(?:<InternalInfo>(?:(?!</InternalInfo>).)*</InternalInfo>\s*)?"
    r"(?:<Properties>\s*)?<Name>([^<]+)</Name>", re.S)
_EDT_CHILD = re.compile(
    r"<(attributes|tabularSections|dimensions|resources|addressingAttributes|enumValues|"
    r"forms|templates|commands)\b[^>]*>\s*(?:<[^>]+>[^<]*</[^>]+>\s*)*?<name>([^<]+)</name>")
_ENUM_VALUE = re.compile(r"<EnumValue\b[^>]*>.*?<Name>([^<]+)</Name>", re.S)
_EDT_ENUM_VALUE = re.compile(r"<enumValues\b[^>]*>.*?<name>([^<]+)</name>", re.S)


def _key(name: str) -> str:
    """How two spellings of a 1C name compare: without case, and in one Unicode
    form (a dump copied on macOS can keep `й` as `и` plus a combining mark)."""
    return unicodedata.normalize("NFC", name).lower()


class Config:
    """A configuration dump inside ``source``, or `ok = False` when there is none."""

    def __init__(self, source: Source, label: str = "") -> None:
        self.source = source
        self.label = label
        self.base, self.edt = self._detect()
        self.ok = self.base is not None
        self._names: dict[str, dict[str, str]] = {}

    def _detect(self) -> tuple[str | None, bool]:
        top = self.source.list_dir("") or []
        if "Configuration.xml" in top:
            return "", False
        if "src" in top and "Configuration" in (self.source.list_dir("src") or []):
            return "src", True
        for name in top[:200]:
            inner = self.source.list_dir(name) or []
            if "Configuration.xml" in inner:
                return name, False
        return None, False

    def _rel(self, *parts: str) -> str:
        return "/".join(p for p in (self.base, *parts) if p)

    # ── objects ─────────────────────────────────────────────────────

    def names(self, kind: str) -> dict[str, str]:
        """lower-case name → the dump's spelling, for one type directory."""
        if kind not in self._names:
            out: dict[str, str] = {}
            for entry in self.source.list_dir(self._rel(kind)) or []:
                name = entry[:-4] if entry.endswith(".xml") else entry
                if name and "." not in name:
                    out.setdefault(_key(name), name)
            self._names[kind] = out
        return self._names[kind]

    def find(self, kind: str, name: str) -> str | None:
        """The dump's spelling of ``kind``/``name``, or None when it is not there."""
        return self.names(kind).get(_key(name))

    def read_text(self, rel: str) -> str | None:
        data = self.source.read(rel)
        return text_of(data) if data is not None else None

    def object_xml(self, kind: str, name: str) -> str | None:
        real = self.find(kind, name)
        if real is None:
            return None
        if self.edt:
            return self.read_text(self._rel(kind, real, f"{real}.mdo"))
        return self.read_text(self._rel(kind, f"{real}.xml"))

    def children(self, kind: str, name: str) -> set[str]:
        """Lower-case names of the object's attributes, tabular sections,
        dimensions, resources, values, forms, templates and commands."""
        xml = self.object_xml(kind, name) or ""
        pattern = _EDT_CHILD if self.edt else _CHILD
        return {_key(m.group(2).strip()) for m in pattern.finditer(xml)}

    def enum_values(self, name: str) -> set[str] | None:
        xml = self.object_xml("Enums", name)
        if xml is None:
            return None
        pattern = _EDT_ENUM_VALUE if self.edt else _ENUM_VALUE
        return {_key(v.strip()) for v in pattern.findall(xml)}

    def predefined(self, kind: str, name: str) -> set[str] | None:
        """Names of the object's predefined items (`Ext/Predefined.xml`), or
        None when the dump has no such file for it."""
        real = self.find(kind, name)
        if real is None:
            return None
        rel = (self._rel(kind, real, "Predefined.xml") if self.edt
               else self._rel(kind, real, "Ext", "Predefined.xml"))
        xml = self.read_text(rel)
        if xml is None:
            return None
        return {_key(n.strip()) for n in re.findall(r"<Name>([^<]+)</Name>", xml)}

    # ── modules ─────────────────────────────────────────────────────

    def module_path(self, kind: str, name: str, module: str) -> str | None:
        """`ObjectModule`, `ManagerModule`, `RecordSetModule` … of an object;
        `Module` of a common module."""
        real = self.find(kind, name)
        if real is None:
            return None
        if self.edt:
            return self._rel(kind, real, f"{module}.bsl")
        return self._rel(kind, real, "Ext", f"{module}.bsl")

    def module(self, kind: str, name: str, module: str) -> tuple[str, str] | None:
        """(path, text) of a module, or None."""
        rel = self.module_path(kind, name, module)
        text = self.read_text(rel) if rel else None
        return (rel, text) if rel and text is not None else None

    def forms(self, kind: str, name: str) -> dict[str, str]:
        real = self.find(kind, name)
        if real is None:
            return {}
        out: dict[str, str] = {}
        for entry in self.source.list_dir(self._rel(kind, real, "Forms")) or []:
            form = entry[:-4] if entry.endswith(".xml") else entry
            if "." not in form:
                out.setdefault(_key(form), form)
        return out

    def form(self, kind: str, name: str, form: str) -> tuple[str, str | None, str | None] | None:
        """(path of the module, module text, form description) of a form.

        An ordinary form's module and description come out of its `Form.bin`
        (the path is the one the indexer files the module under); a managed
        form's module is `Ext/Form/Module.bsl` and its description is not read.
        """
        real_obj = self.find(kind, name)
        real = self.forms(kind, name).get(_key(form))
        if real_obj is None or real is None:
            return None
        if self.edt:
            rel = self._rel(kind, real_obj, "Forms", real, "Module.bsl")
            return rel, self.read_text(rel), None
        folder = self._rel(kind, real_obj, "Forms", real, "Ext")
        data = self.source.read(f"{folder}/Form.bin")
        if data is not None:
            description = None
            try:
                box = read_container(data)
                desc = box.get("form")
                description = text_of(desc.data) if desc else None
            except ValueError:
                pass
            return f"{folder}/Form.bin/module", form_module(data), description
        rel = f"{folder}/Form/Module.bsl"
        return rel, self.read_text(rel), None

    def templates(self, kind: str, name: str) -> set[str]:
        real = self.find(kind, name)
        if real is None:
            return set()
        out = set()
        for entry in self.source.list_dir(self._rel(kind, real, "Templates")) or []:
            out.add(_key(entry[:-4] if entry.endswith(".xml") else entry))
        return out

    def last_changed(self, rel: str) -> datetime | None:
        return self.source.last_changed(rel.split("/Form.bin/")[0] + "/Form.bin"
                                        if "/Form.bin/" in rel else rel)
