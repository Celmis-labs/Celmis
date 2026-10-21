"""An uploaded pack — a zip, a list of files or a folder — turned into validated items.

Everything the router and the CLI accept goes through here, and nothing is
written to disk: the archive is read in memory, entry by entry, with a hard cap
on the bytes ACTUALLY decompressed (the size a zip declares is only a hint).
The upload is refused as a whole at the first problem — a pack that is half
there is worse than one that is not.

What is refused (:class:`IngestError`, with a stable ``code``):

    archive_too_large  more than 5 MB compressed
    bad_archive        not a zip, or a damaged one
    unsafe_path        absolute, ``..``, backslash, NUL/control character, drive letter
    symlink            a symbolic link (or another special file) among the files
    encrypted          a password-protected entry
    too_many_files     more than 400 files kept (or an absurd number of entries)
    file_too_large     one file over 512 KB
    total_too_large    more than 8 MB of text in all
    path_too_long      a path over 300 characters
    too_deep           more than 10 levels
    duplicate_path     the same path twice (case-insensitively), or a path that is
                       both a file and a folder (``a.md`` and ``a.md/b.md``)
    secret_file        a file that is a secret by name or by shape (key, .env, ...)
    binary_file        a kept file that is not UTF-8 text
    secrets_found      a secret literal in a kept file — reported as
                       ``path:line label``, NEVER the matched text. An obvious
                       placeholder is not one, and a line that carries the
                       marker ``celmis:allow-secret`` is accepted and reported
                       back in ``IngestResult.allowed`` (``src/security/upload_scan.py``)
    bad_front_matter   SKILL.md without a usable name/description, bad YAML ...
    nested_skill       a SKILL.md inside another skill's directory
    duplicate_skill    two skills with the same name in one version
    no_files           nothing left to keep

What is ignored, and reported as such rather than refused: ``__MACOSX/``,
``.DS_Store``, ``.git/``, any path segment that starts with a dot, and files
whose extension is not a text format (images, binaries, archives).

A single top-level folder shared by every kept file is stripped
(``my-skill/SKILL.md`` -> ``SKILL.md``), so "zip the folder" and "pick the
folder in the browser" give the same pack.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from src.knowledge.search import terms_for

# ─── Limits ──────────────────────────────────────────────────────────

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024         # compressed; the request body cap
MAX_FILES = 400                             # files KEPT
MAX_ENTRIES = 20_000                        # entries looked at, kept or not
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024           # uncompressed, all kept files
MAX_PATH_CHARS = 300
MAX_DEPTH = 10
MAX_FRONT_MATTER_CHARS = 8 * 1024
MAX_SKILL_DESCRIPTION = 1536
MAX_DESCRIPTION = 300
MAX_NAME = 200
MAX_REPOS = 50
MAX_TAGS = 30
MAX_HITS = 50                               # secret findings reported
MAX_IGNORED_REPORTED = 200

#: Extensions of text formats a pack may hold.
TEXT_EXTENSIONS: frozenset[str] = frozenset("""
.md .markdown .txt .rst .yaml .yml .json .toml .ini .cfg .conf .sh .bash .zsh .ps1
.py .js .mjs .cjs .ts .tsx .jsx .go .rs .java .kt .kts .rb .php .cs .sql .http .rest
.graphql .gql .proto .xml .html .css .scss .tf .hcl .env.example .properties .gradle .csv
""".split())  # noqa: SIM905 — a word list reads better than a tuple

SKILL_FILE = "SKILL.md"
KINDS = ("api", "checklist", "note", "doc")
_SEGMENT_KIND = {
    "api": "api", "apis": "api",
    "checklist": "checklist", "checklists": "checklist",
    "note": "note", "notes": "note", "context": "note",
}
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_MARKDOWN = (".md", ".markdown")


class IngestError(Exception):
    """The upload is refused. ``code`` is stable, ``message`` is for people and
    never holds a secret's value, ``hits`` is ``[{"path", "line", "label"}]``."""

    def __init__(self, code: str, message: str, hits: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hits = list(hits or [])


@dataclass(frozen=True)
class IngestedFile:
    path: str
    kind: str                   # skill | file | api | checklist | note | doc
    skill: str | None
    name: str
    description: str
    content: str
    sha: str
    size_bytes: int
    repos: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    terms: dict[str, int] = field(default_factory=dict)


@dataclass
class IngestResult:
    files: list[IngestedFile]
    ignored: list[str]
    skills: list[str]
    #: How many paths were ignored in all (``ignored`` is capped).
    ignored_total: int = 0
    #: Lines accepted only because they carry the ``celmis:allow-secret`` marker:
    #: ``[{"path", "line", "rule"}]`` - where and which rule, never the text.
    allowed: list[dict[str, Any]] = field(default_factory=list)


def version_sha(files: Sequence[Any]) -> str:
    """sha256 over the sorted ``path NUL sha NEWLINE`` lines of a version."""
    digest = hashlib.sha256()
    for path, sha in sorted((f.path, f.sha) for f in files):
        digest.update(f"{path}\0{sha}\n".encode())
    return digest.hexdigest()


# ─── Entries: one shape for a zip, a list of files and a folder ──────


class _Entry(NamedTuple):
    path: str                          # as given
    declared: int                      # size the source claims, or -1
    symlink: bool
    special: bool
    encrypted: bool
    is_dir: bool
    read: Callable[[int], bytes]       # read(n) -> at most n bytes


def _clean_path(raw: str) -> str:
    """The normalised relative path, or :class:`IngestError` ``unsafe_path``."""
    shown = raw[:MAX_PATH_CHARS]

    def refuse(why: str) -> IngestError:
        return IngestError("unsafe_path", f"{shown!r}: {why}")

    if "\\" in raw:
        raise refuse("backslashes are not allowed in a path")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise refuse("control characters are not allowed in a path")
    if raw.startswith("/"):
        raise refuse("absolute paths are not allowed")
    if _DRIVE.match(raw):
        raise refuse("drive letters are not allowed")
    path = unicodedata.normalize("NFC", raw)
    if path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    segments = path.split("/") if path else []
    if not segments:
        raise refuse("empty path")
    for seg in segments:
        if seg == "..":
            raise refuse("`..` is not allowed in a path")
        if seg in ("", "."):
            raise refuse("empty or `.` segments are not allowed in a path")
    return path


def _is_hidden(path: str) -> bool:
    return any(seg.startswith(".") or seg == "__MACOSX" for seg in path.split("/"))


def _extension_allowed(path: str) -> bool:
    low = path.lower()
    return low.endswith(".env.example") or PurePosixPath(low).suffix in TEXT_EXTENSIONS


# ─── The pipeline ────────────────────────────────────────────────────


#: What ``zipfile`` raises on a malformed archive: skewed offsets give ``ValueError``
#: (negative seek) or ``OSError``, a short header ``struct.error``, a bad stream
#: ``zlib.error`` or ``EOFError``, an unknown method ``NotImplementedError``.
_ZIP_ERRORS = (zipfile.BadZipFile, ValueError, OSError, struct.error, EOFError, zlib.error,
               NotImplementedError, OverflowError)


def ingest_zip(data: bytes) -> IngestResult:
    """Validate a zip archive (bytes) and return its kept files."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise IngestError(
            "archive_too_large", f"The archive is over {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise IngestError("bad_archive", "The archive is empty.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except _ZIP_ERRORS as exc:
        raise IngestError("bad_archive", "That is not a readable zip archive.") from exc
    with zf:
        try:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise IngestError("too_many_files", f"The archive has over {MAX_ENTRIES} entries.")
            entries = [_zip_entry(zf, i) for i in infos]
        except _ZIP_ERRORS as exc:
            raise IngestError("bad_archive", "The archive is damaged.") from exc
        return _ingest(entries)


def _zip_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> _Entry:
    mode = (info.external_attr >> 16) & 0o177777
    kind = stat.S_IFMT(mode) if mode else 0

    def read(n: int, _info: zipfile.ZipInfo = info) -> bytes:
        try:
            with zf.open(_info) as fh:
                return fh.read(n)
        except _ZIP_ERRORS as exc:
            raise IngestError("bad_archive", "The archive is damaged.") from exc
        except RuntimeError as exc:               # a member that needs a password
            raise IngestError("encrypted", "The archive cannot be read (is it encrypted?).") from exc

    return _Entry(
        path=info.filename,
        declared=info.file_size,
        symlink=kind == stat.S_IFLNK,
        special=kind in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK),
        encrypted=bool(info.flag_bits & 0x1),
        is_dir=info.filename.endswith("/") or info.is_dir(),
        read=read,
    )


def ingest_files(files: Sequence[tuple[str, bytes]]) -> IngestResult:
    """Validate a folder upload: ``(relative path, bytes)`` pairs."""
    if len(files) > MAX_ENTRIES:
        raise IngestError("too_many_files", f"More than {MAX_ENTRIES} files were sent.")
    entries = [
        _Entry(path, len(data), False, False, False, False,
               lambda n, _d=data: _d[:n])
        for path, data in files
    ]
    return _ingest(entries)


def ingest_directory(root: str | Path) -> IngestResult:
    """Validate a folder on disk (the CLI). Symbolic links are refused, hidden
    directories are not entered, nothing outside ``root`` is read."""
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        raise IngestError("bad_archive", "That is not a directory.")
    entries: list[_Entry] = []
    try:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            rel_dir = Path(dirpath).relative_to(base).as_posix()
            keep: list[str] = []
            for d in sorted(dirnames):
                rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                if _is_hidden(rel):
                    # Not entered; the folder is reported once, as ignored.
                    entries.append(_Entry(rel, 0, False, False, False, False, lambda n: b""))
                    continue
                if (Path(dirpath) / d).is_symlink():
                    raise IngestError(
                        "symlink", f"{rel[:MAX_PATH_CHARS]!r}: symbolic links are not allowed.")
                keep.append(d)
            dirnames[:] = keep
            for fname in sorted(filenames):
                rel = fname if rel_dir == "." else f"{rel_dir}/{fname}"
                full = Path(dirpath) / fname
                st = full.lstat()
                entries.append(_Entry(
                    rel, st.st_size, stat.S_ISLNK(st.st_mode),
                    not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)), False, False,
                    lambda n, _p=full: _read_file(_p, n)))
                if len(entries) > MAX_ENTRIES:
                    raise IngestError(
                        "too_many_files", f"The folder has over {MAX_ENTRIES} files.")
    except OSError as exc:
        raise IngestError("bad_archive", "The folder could not be read.") from exc
    return _ingest(entries)


def _read_file(path: Path, n: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def _ingest(entries: Sequence[_Entry]) -> IngestResult:
    ignored: list[str] = []
    ignored_total = 0
    candidates: list[tuple[str, _Entry]] = []
    secret_paths: list[dict[str, Any]] = []
    seen: dict[str, str] = {}

    from src.security.secret_files import classify, is_git_internal

    for entry in entries:
        path = _clean_path(entry.path)
        if entry.is_dir:
            continue
        # A `.git/` folder (its config can carry a remote URL with a token, its objects every
        # secret ever committed) is not an upload: it is ignored and listed, never stored and
        # never read, whatever it holds and however it is spelled (`.GIT`, `.git ` ...).
        if is_git_internal(path):
            ignored_total += 1
            if len(ignored) < MAX_IGNORED_REPORTED:
                ignored.append(path[:MAX_PATH_CHARS])
            continue
        # A secret by name is refused even where a dot would hide it (`.env`,
        # `.ssh/id_rsa`): a "no" is more useful than a file that quietly vanishes.
        # Only macOS's own debris is waved through.
        if not path.startswith("__MACOSX/") and not path.rsplit("/", 1)[-1].startswith("._") \
                and classify(path) == "deny":
            secret_paths.append({"path": path[:MAX_PATH_CHARS], "line": 0, "label": "secret file"})
            continue
        if _is_hidden(path):
            ignored_total += 1
            if len(ignored) < MAX_IGNORED_REPORTED:
                ignored.append(path[:MAX_PATH_CHARS])
            continue
        if entry.symlink or entry.special:
            raise IngestError(
                "symlink", f"{path[:MAX_PATH_CHARS]!r}: links and special files are not allowed.")
        if entry.encrypted:
            raise IngestError(
                "encrypted", f"{path[:MAX_PATH_CHARS]!r}: encrypted entries are not allowed.")
        if not _extension_allowed(path):
            ignored_total += 1
            if len(ignored) < MAX_IGNORED_REPORTED:
                ignored.append(path[:MAX_PATH_CHARS])
            continue
        folded = path.casefold()
        if folded in seen:
            raise IngestError("duplicate_path", f"{path[:MAX_PATH_CHARS]!r} appears twice.")
        seen[folded] = path
        candidates.append((path, entry))
    if secret_paths:
        raise IngestError(
            "secret_file",
            "The upload contains files that look like secrets (keys, .env files, credentials). "
            "Remove them and upload again.",
            secret_paths[:MAX_HITS])
    if not candidates:
        raise IngestError("no_files", "No usable text files were found in the upload.")
    if len(candidates) > MAX_FILES:
        raise IngestError("too_many_files", f"More than {MAX_FILES} files; split the pack.")
    for path, entry in candidates:                      # the single file is the clearer reason
        if entry.declared > MAX_FILE_BYTES:
            raise IngestError("file_too_large",
                              f"{path[:80]!r} is over {MAX_FILE_BYTES // 1024} KB.")
    if sum(max(e.declared, 0) for _p, e in candidates) > MAX_TOTAL_BYTES:
        raise IngestError(
            "total_too_large", f"The files add up to over {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")

    # One shared top-level folder is the folder that was zipped, not part of the pack.
    top_dir = _common_top_dir([p for p, _e in candidates])
    if top_dir:
        candidates = [(p[len(top_dir) + 1:], e) for p, e in candidates]
    for path, _entry in candidates:
        if len(path) > MAX_PATH_CHARS:
            raise IngestError("path_too_long", f"{path[:80]!r}…: over {MAX_PATH_CHARS} characters.")
        if path.count("/") + 1 > MAX_DEPTH:
            raise IngestError("too_deep", f"{path[:80]!r}: nested over {MAX_DEPTH} levels.")
    refuse_file_and_folder([p for p, _e in candidates])

    loaded = _read_all(candidates)
    allowed = _refuse_secrets(loaded)
    files, skills = _describe(loaded, top_dir)
    return IngestResult(files=files, ignored=sorted(ignored), skills=skills,
                        ignored_total=ignored_total, allowed=allowed)


def refuse_file_and_folder(paths: Sequence[str]) -> None:
    """:class:`IngestError` when a path is both a file and a folder (``a.md`` and
    ``a.md/b.md``): such a pack cannot be written to a disk. Case-insensitive,
    like the duplicate check, because a laptop's disk is."""
    files = {p.casefold(): p for p in paths}
    for path in paths:
        parts = path.casefold().split("/")
        for depth in range(1, len(parts)):
            clash = files.get("/".join(parts[:depth]))
            if clash is not None:
                raise IngestError(
                    "duplicate_path",
                    f"{clash[:80]!r} is both a file and a folder ({path[:80]!r} lies inside it).")


def _common_top_dir(paths: Sequence[str]) -> str:
    """The single folder every path lives in, or "" — never when a SKILL.md
    already sits at the root (then the root is the pack)."""
    if not paths or SKILL_FILE in paths:
        return ""
    firsts = {p.split("/", 1)[0] for p in paths}
    if len(firsts) == 1 and all("/" in p for p in paths):
        return next(iter(firsts))
    return ""


def _read_all(candidates: Sequence[tuple[str, _Entry]]) -> list[tuple[str, str]]:
    """Read and decode every kept file under the real byte caps."""
    from src.security.secret_files import classify

    out: list[tuple[str, str]] = []
    total = 0
    sniffed: list[dict[str, Any]] = []
    for path, entry in candidates:
        if entry.declared > MAX_FILE_BYTES:
            raise IngestError("file_too_large",
                              f"{path!r} is over {MAX_FILE_BYTES // 1024} KB.")
        try:
            data = entry.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise IngestError("bad_archive", f"{path!r} could not be read.") from exc
        if len(data) > MAX_FILE_BYTES:
            raise IngestError("file_too_large",
                              f"{path!r} is over {MAX_FILE_BYTES // 1024} KB.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise IngestError(
                "total_too_large",
                f"The files add up to over {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
        # The whole file (at most MAX_FILE_BYTES), not its first 8 KiB: a key pasted at the end
        # of a long note is as much a key.
        if classify(path, data) == "deny":
            sniffed.append({"path": path, "line": 0, "label": "secret file"})
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IngestError("binary_file", f"{path!r} is not UTF-8 text.") from exc
        if "\x00" in text:
            raise IngestError("binary_file", f"{path!r} is not UTF-8 text.")
        out.append((path, text[1:] if text.startswith("﻿") else text))
    if sniffed:
        raise IngestError(
            "secret_file",
            "The upload contains files that look like secrets (keys, service accounts, "
            "Secret manifests). Remove them and upload again.",
            sniffed[:MAX_HITS])
    return out


def _refuse_secrets(loaded: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    """Refuse the upload if any file holds a secret literal. The hits say where
    (``path``, ``line``) and what kind (``label``) - never the text. A line with
    a run of 2048+ characters (minified JSON) is read in overlapping windows, so
    what sits inside the run is found too. An obvious placeholder is not a secret
    and a line marked ``celmis:allow-secret`` is accepted (``upload_scan``); the
    marked lines come back as ``[{"path", "line", "rule"}]`` (never the text)."""
    from src.security.upload_scan import scan_upload

    hits: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    for path, text in loaded:
        scan = scan_upload(text, source_hint=path)
        for line, label in scan.refused:
            hits.append({"path": path, "line": int(line), "label": str(label)})
            if len(hits) >= MAX_HITS:
                break
        allowed.extend({"path": path, "line": int(line), "rule": str(label)}
                       for line, label in scan.allowed)
        if len(hits) >= MAX_HITS:
            break
    if hits:
        raise IngestError(
            "secrets_found",
            "The upload contains what look like secrets (the lines are listed). "
            "Replace them with placeholders such as <token> or ${API_TOKEN} and upload again.",
            hits)
    return allowed


# ─── Front matter ────────────────────────────────────────────────────

_FM_OPEN = re.compile(r"\A---[ \t]*\r?\n")
_FM_CLOSE = re.compile(r"^(?:---|\.\.\.)[ \t]*\r?$", re.MULTILINE)
_FM_KEYLINE = re.compile(r"^\s*[A-Za-z_][\w. -]*:(?:\s|$)")


def _no_alias_loader():
    """A safe YAML loader that refuses aliases: an alias is how a few hundred
    bytes of YAML expand into gigabytes."""
    import yaml

    class Loader(yaml.SafeLoader):
        def compose_node(self, parent, index):  # noqa: ANN001, ANN201
            if self.check_event(yaml.AliasEvent):
                raise yaml.constructor.ConstructorError(
                    None, None, "aliases are not allowed", self.peek_event().start_mark)
            return super().compose_node(parent, index)

    return Loader


_FM_PLAIN_VALUE = re.compile(r"^(?P<key>[A-Za-z_][\w.-]*):[ \t]+(?P<value>[^\s\"'\[{|>&*!%@`#].*?)[ \t]*\r?$")


def _quote_plain_values(block: str) -> str:
    """``block`` with each top-level one-line plain value that holds ``: `` quoted,
    so that it reads as the text it is. Anything else is left as it was."""
    out = []
    for line in block.split("\n"):
        m = _FM_PLAIN_VALUE.match(line)
        if m and ": " in m.group("value"):
            value = m.group("value").replace("\\", "\\\\").replace('"', '\\"')
            line = f'{m.group("key")}: "{value}"'
        out.append(line)
    return "\n".join(out)


def _front_matter(path: str, text: str, *, required: bool) -> tuple[dict[str, Any], str]:
    """``(meta, body)``. ``required`` (SKILL.md) makes anything unreadable an
    error; otherwise a leading ``---`` that is not YAML is just a rule line."""
    opened = _FM_OPEN.match(text)
    if not opened:
        return {}, text
    rest = text[opened.end():]
    closed = _FM_CLOSE.search(rest)
    if not closed:
        if required:
            raise IngestError("bad_front_matter", f"{path}: the front matter is never closed (`---`).")
        return {}, text
    block, body = rest[:closed.start()], rest[closed.end():]
    first = next((ln for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
    if not required and not _FM_KEYLINE.match(first):
        return {}, text
    if len(block) > MAX_FRONT_MATTER_CHARS:
        raise IngestError(
            "bad_front_matter",
            f"{path}: the front matter is over {MAX_FRONT_MATTER_CHARS // 1024} KB.")
    import yaml

    try:
        try:
            meta = yaml.load(block, Loader=_no_alias_loader())  # noqa: S506 — SafeLoader, no aliases
        except yaml.YAMLError:
            # Claude Code reads `description: Does a thing: with a colon` as one
            # line of text; strict YAML does not. Read it the way the agent does.
            lenient = _quote_plain_values(block)
            if lenient == block:
                raise
            meta = yaml.load(lenient, Loader=_no_alias_loader())  # noqa: S506 — SafeLoader, no aliases
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is None:
            raise IngestError("bad_front_matter", f"{path}: the front matter is not valid YAML.") from exc
        line = block.splitlines()[mark.line] if mark.line < len(block.splitlines()) else ""
        key = _FM_KEYLINE.match(line)
        named = f", `{key.group(0).strip().rstrip(':')}`" if key else ""
        raise IngestError(
            "bad_front_matter",
            f"{path}: the front matter is not valid YAML (line {mark.line + 2}{named}); "
            "if the value holds `: ` or `#`, put it in quotes.") from exc
    except (RecursionError, ValueError, TypeError) as exc:
        raise IngestError("bad_front_matter", f"{path}: the front matter is not valid YAML.") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        if required:
            raise IngestError("bad_front_matter", f"{path}: the front matter must be `key: value` lines.")
        return {}, text
    return {str(k): v for k, v in meta.items()}, body.lstrip("\r\n")


def _scalar(path: str, meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    if value is None:
        return None
    if isinstance(value, bool | int | float) or hasattr(value, "isoformat"):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    raise IngestError("bad_front_matter", f"{path}: `{key}` must be text.")


def _string_list(path: str, meta: dict[str, Any], key: str, *, limit: int, each: int) -> list[str]:
    value = meta.get(key)
    if value is None:
        return []
    items = [value] if isinstance(value, str | int | float) else value
    if not isinstance(items, list):
        raise IngestError("bad_front_matter", f"{path}: `{key}` must be a list of text.")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str | int | float) or isinstance(item, bool):
            raise IngestError("bad_front_matter", f"{path}: `{key}` must be a list of text.")
        text = str(item).strip()
        if not text:
            continue
        if len(text) > each or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise IngestError("bad_front_matter", f"{path}: an entry of `{key}` is too long or has control characters.")
        if text not in out:
            out.append(text)
    if len(out) > limit:
        raise IngestError("bad_front_matter", f"{path}: `{key}` has more than {limit} entries.")
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", unicodedata.normalize("NFKD", text).lower())
    return slug.strip("-._")[:64].strip("-._")


def _first_line(body: str) -> str:
    """The first line of the first paragraph that is not a heading, a fence, a
    rule or a table, with list and quote markers dropped."""
    fenced = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line or line.startswith(("#", "---", "***", "<!--", "|", "<")):
            continue
        line = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)+", "", line).strip()
        if line:
            return line[:MAX_DESCRIPTION]
    return ""


# ─── Describing the files ────────────────────────────────────────────


def _describe(loaded: Sequence[tuple[str, str]], top_dir: str) -> tuple[list[IngestedFile], list[str]]:
    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    for path, text in sorted(loaded):
        if path.lower().endswith(_MARKDOWN):
            parsed[path] = _front_matter(path, text, required=path.rsplit("/", 1)[-1] == SKILL_FILE)
        else:
            parsed[path] = ({}, text)

    # Skills: every SKILL.md names a directory; its other files belong to it.
    skill_dirs: dict[str, str] = {}              # directory ("" = root) -> skill name
    by_name: dict[str, str] = {}
    for path in sorted(parsed):
        if path.rsplit("/", 1)[-1] != SKILL_FILE:
            continue
        meta, _body = parsed[path]
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        name = _scalar(path, meta, "name")
        if not name:
            fallback = directory.rsplit("/", 1)[-1] if directory else top_dir
            name = slugify(fallback) if fallback else ""
            if not name:
                raise IngestError(
                    "bad_front_matter",
                    f"{path}: `name` is required (none could be taken from the folder name).")
        if not _SKILL_NAME.match(name):
            raise IngestError(
                "bad_front_matter",
                f"{path}: `name` must be lowercase letters, digits, `.`, `_` or `-` (up to 64).")
        description = _scalar(path, meta, "description") or ""
        if not description:
            raise IngestError("bad_front_matter", f"{path}: `description` is required.")
        if len(description) > MAX_SKILL_DESCRIPTION:
            raise IngestError(
                "bad_front_matter",
                f"{path}: `description` is over {MAX_SKILL_DESCRIPTION} characters.")
        if name in by_name:
            raise IngestError(
                "duplicate_skill", f"Two skills are named {name!r}: {by_name[name]} and {path}.")
        by_name[name] = path
        skill_dirs[directory] = name
    for inner in sorted(skill_dirs):
        for outer in sorted(skill_dirs):
            if inner != outer and (outer == "" or inner.startswith(outer + "/")):
                inner_path = f"{inner}/{SKILL_FILE}" if inner else SKILL_FILE
                outer_path = f"{outer}/{SKILL_FILE}" if outer else SKILL_FILE
                raise IngestError(
                    "nested_skill",
                    f"{inner_path} sits inside the skill of {outer_path}; a skill cannot contain another.")

    texts = dict(loaded)
    files: list[IngestedFile] = []
    for path in sorted(parsed):
        meta, body = parsed[path]
        text = texts[path]
        base = path.rsplit("/", 1)[-1]
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        skill = next((n for d, n in skill_dirs.items()
                      if path.startswith(d + "/") or d == ""), None) if skill_dirs else None
        if base == SKILL_FILE:
            kind, skill = "skill", skill_dirs[directory]
            name = skill
            description = _squash(_scalar(path, meta, "description") or "")
        else:
            declared = (_scalar(path, meta, "kind") or "").lower()
            if skill is not None:
                kind = "file"
            elif declared:
                if declared not in KINDS:
                    raise IngestError(
                        "bad_front_matter",
                        f"{path}: `kind` must be one of {', '.join(KINDS)}.")
                kind = declared
            else:
                kind = _SEGMENT_KIND.get(path.split("/", 1)[0].lower(), "doc") if "/" in path else "doc"
            stem = base.rsplit(".", 1)[0] if "." in base else base
            name = (_scalar(path, meta, "name") or stem)[:MAX_NAME]
            description = _squash(
                _scalar(path, meta, "description") or _first_line(body)
                if path.lower().endswith(_MARKDOWN) else "")[:MAX_DESCRIPTION]
        content_bytes = text.encode("utf-8")
        files.append(IngestedFile(
            path=path, kind=kind, skill=skill, name=name, description=description,
            content=text, sha=hashlib.sha256(content_bytes).hexdigest(),
            size_bytes=len(content_bytes),
            repos=_string_list(path, meta, "repos", limit=MAX_REPOS, each=300),
            tags=_string_list(path, meta, "tags", limit=MAX_TAGS, each=64),
            terms=terms_for(text),
        ))
    return files, sorted(skill_dirs.values())


__all__ = [
    "KINDS",
    "MAX_ARCHIVE_BYTES",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "TEXT_EXTENSIONS",
    "IngestError",
    "IngestResult",
    "IngestedFile",
    "ingest_directory",
    "ingest_files",
    "ingest_zip",
    "refuse_file_and_folder",
    "slugify",
    "version_sha",
]
