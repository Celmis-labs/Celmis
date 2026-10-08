"""Code from an archive — safe extraction for the ``upload`` provider.

A repository can be added without any git host: a superadmin uploads a
``.zip`` / ``.tar.gz`` / ``.tgz`` and it is unpacked into the same
``settings.repo_path(slug)`` a clone would occupy. Everything downstream (graph
index, vault, Q&A, MCP) then sees an ordinary directory.

An archive is hostile input, so the extractor is deliberately narrower than
the formats it reads:

  * regular files and directories only — a symlink, hardlink, device, fifo or
    socket entry is refused, not skipped (a skipped link would hide that the
    archive tried it);
  * an absolute path, a drive letter or a ``..`` segment is refused (zip-slip
    / tar path traversal), and every target is proven to sit under the
    staging directory before it is opened;
  * the total unpacked size, the number of files and the size of one file are
    capped, and the caps are enforced on the BYTES ACTUALLY WRITTEN — a
    header that claims 1 KB and inflates to 10 GB is stopped at the cap;
  * unpacking happens in a staging directory next to the target and is swapped
    in with a rename, so a failed or half-finished upload never replaces a
    working copy.

Member names are decoded the way the writers spell them: the zip UTF-8 flag,
else UTF-8 anyway when the bytes are valid UTF-8, else the legacy cp437 reading
zipfile already gives. All names are normalised to NFC so one file has one
name whichever platform packed it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

#: The provider value stored on the RepoConfig of an uploaded repository.
UPLOAD_PROVIDER = "upload"

#: Accepted file names, longest suffix first. Anything else (``.rar``, ``.7z``,
#: ``.tar``, ``.tar.zst`` …) is refused with the list below.
ALLOWED_SUFFIXES: tuple[str, ...] = (".tar.gz", ".tgz", ".zip")

DEFAULT_MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 150_000
MAX_FILE_BYTES = 128 * 1024 * 1024

_CHUNK = 1024 * 1024
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")
_IGNORED_TOP = frozenset({"__MACOSX"})


class ArchiveError(ValueError):
    """The archive is not acceptable. The message is meant for the uploader."""


def max_archive_bytes() -> int:
    """The upload size limit (``CELMIS_MAX_UPLOAD_BYTES``, default 250 MB)."""
    try:
        return max(1, int(os.environ.get("CELMIS_MAX_UPLOAD_BYTES", DEFAULT_MAX_ARCHIVE_BYTES)))
    except ValueError:
        return DEFAULT_MAX_ARCHIVE_BYTES


def allowed_extensions_text() -> str:
    return ", ".join(ALLOWED_SUFFIXES)


def archive_kind(filename: str) -> str:
    """``zip`` or ``tar`` for an accepted file name; ArchiveError otherwise."""
    name = (filename or "").strip().lower()
    for suffix in ALLOWED_SUFFIXES:
        if name.endswith(suffix):
            return "zip" if suffix == ".zip" else "tar"
    raise ArchiveError(
        f"Unsupported archive type. Allowed extensions: {allowed_extensions_text()}."
    )


# ─── names ───────────────────────────────────────────────────────────


def _clean_member_name(raw: str) -> list[str]:
    """The path segments of a member name, or ArchiveError if it escapes."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/")
    if "\x00" in name:
        raise ArchiveError("The archive has an entry with a NUL byte in its name.")
    if name.startswith("/") or _WIN_DRIVE.match(name):
        raise ArchiveError(f"The archive has an absolute path ({name[:80]!r}).")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ArchiveError(f"The archive has a path that leaves its folder ({name[:80]!r}).")
    return parts


def _zip_name(info: zipfile.ZipInfo) -> str:
    """The member name, decoded the way its writer meant it.

    zipfile decodes with UTF-8 only when the language-encoding flag (bit 11)
    is set and with cp437 otherwise. Several writers store UTF-8 without the
    flag, so a cp437 reading that round-trips to valid UTF-8 is taken as such;
    a genuinely legacy name stays as zipfile gave it.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _tar_name(raw: str) -> str:
    # tarfile maps undecodable bytes to lone surrogates; keep the name usable.
    try:
        return raw.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeError:
        return raw.encode("utf-8", "replace").decode("utf-8", "replace")


# ─── extraction ──────────────────────────────────────────────────────


@dataclass
class _Budget:
    files: int = 0
    total: int = 0

    def add_file(self) -> None:
        self.files += 1
        if self.files > MAX_FILES:
            raise ArchiveError(f"The archive has more than {MAX_FILES} files.")


def _target(root: Path, parts: list[str]) -> Path:
    target = root.joinpath(*parts)
    if not os.path.abspath(target).startswith(os.path.abspath(root) + os.sep):
        raise ArchiveError("The archive has a path that leaves its folder.")
    return target


def _copy_limited(src: BinaryIO, dest: Path, budget: _Budget, display: str) -> None:
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                budget.total += len(chunk)
                if written > MAX_FILE_BYTES:
                    raise ArchiveError(
                        f"A file in the archive is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB "
                        f"({display[:80]!r}).")
                if budget.total > MAX_UNPACKED_BYTES:
                    raise ArchiveError(
                        f"The archive unpacks to more than {MAX_UNPACKED_BYTES // (1024 ** 3)} GB.")
                out.write(chunk)
    except OSError as exc:
        raise ArchiveError(f"Cannot unpack {display[:80]!r}: {exc.strerror or 'I/O error'}.") from None


def _make_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"Cannot create a folder from the archive: {exc.strerror or 'I/O error'}.") from None


def _unzip(archive: Path, staging: Path) -> None:
    budget = _Budget()
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise ArchiveError("The file is not a valid zip archive.") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES * 2:
            raise ArchiveError(f"The archive has more than {MAX_FILES} files.")
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode not in (0o100000, 0o040000):
                raise ArchiveError(
                    f"The archive contains a link or special file ({info.filename[:80]!r}); "
                    "only regular files and folders are accepted.")
            if info.flag_bits & 0x1:
                raise ArchiveError("Encrypted archives are not supported.")
            parts = _clean_member_name(_zip_name(info))
            if not parts:
                continue
            target = _target(staging, parts)
            if info.is_dir():
                _make_dir(target)
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ArchiveError(
                    f"A file in the archive is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB "
                    f"({info.filename[:80]!r}).")
            budget.add_file()
            _make_dir(target.parent)
            try:
                with zf.open(info) as src:
                    _copy_limited(src, target, budget, info.filename)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
                raise ArchiveError(f"Cannot read {info.filename[:80]!r}: {type(exc).__name__}.") from None


def _untar(archive: Path, staging: Path) -> None:
    budget = _Budget()
    try:
        tf = tarfile.open(archive, mode="r:gz")  # noqa: SIM115 — closed by the with below
    except (tarfile.TarError, OSError, EOFError):
        raise ArchiveError("The file is not a valid .tar.gz archive.") from None
    with tf:
        try:
            for member in tf:
                name = _tar_name(member.name)
                if member.issym() or member.islnk():
                    raise ArchiveError(
                        f"The archive contains a link ({name[:80]!r}); "
                        "only regular files and folders are accepted.")
                if not (member.isfile() or member.isdir()):
                    raise ArchiveError(
                        f"The archive contains a special file ({name[:80]!r}); "
                        "only regular files and folders are accepted.")
                parts = _clean_member_name(name)
                if not parts:
                    continue
                target = _target(staging, parts)
                if member.isdir():
                    _make_dir(target)
                    continue
                if member.size > MAX_FILE_BYTES:
                    raise ArchiveError(
                        f"A file in the archive is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB "
                        f"({name[:80]!r}).")
                budget.add_file()
                _make_dir(target.parent)
                src = tf.extractfile(member)
                if src is None:
                    raise ArchiveError(f"Cannot read {name[:80]!r}.")
                with src:
                    _copy_limited(src, target, budget, name)
        except (tarfile.TarError, EOFError, OSError) as exc:
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError("The .tar.gz archive is damaged or truncated.") from None


def _strip_single_top(staging: Path) -> Path:
    """The directory holding the project: the archive's one top-level folder,
    when it has exactly one, else the staging directory itself."""
    for name in _IGNORED_TOP:
        junk = staging / name
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
    entries = [e for e in staging.iterdir()]
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return staging


def extract_archive(archive: Path, filename: str, staging: Path) -> Path:
    """Unpack ``archive`` into the empty ``staging`` directory; returns the
    directory that should become the repository root."""
    kind = archive_kind(filename)
    if kind == "zip":
        _unzip(archive, staging)
    else:
        _untar(archive, staging)
    root = _strip_single_top(staging)
    if not any(p.is_file() for p in root.rglob("*")):
        raise ArchiveError("The archive contains no files.")
    return root


# ─── upload: stream, extract, swap ───────────────────────────────────


@dataclass(frozen=True)
class StoredArchive:
    path: Path
    sha256: str
    size: int


def stream_to_temp(read_chunk, tmp_dir: Path, limit: int | None = None) -> StoredArchive:
    """Write an upload to a temp file in ``tmp_dir``, hashing as it goes.

    ``read_chunk`` is a callable returning the next ``bytes`` chunk (empty at
    the end). Raises ArchiveError past ``limit`` bytes (default: the upload
    limit) and removes the partial file.
    """
    limit = limit or max_archive_bytes()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=tmp_dir)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ArchiveError(
                        f"The archive is larger than {limit // (1024 * 1024)} MB.")
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    if size == 0:
        Path(name).unlink(missing_ok=True)
        raise ArchiveError("The uploaded file is empty.")
    return StoredArchive(Path(name), digest.hexdigest(), size)


def install_archive(stored: StoredArchive, filename: str, slug: str, *,
                    uploaded_by: str = "") -> Path:
    """Unpack ``stored`` and swap it in as the repository ``slug``.

    The previous copy (a re-upload) is replaced only after the new one has
    unpacked completely; on any failure it stays where it was.
    """
    from src.config import get_settings

    settings = get_settings()
    final = settings.repo_path(slug)
    work = settings.workspace_dir / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"unpack-{slug[:40]}-", dir=work))
    try:
        root = extract_archive(stored.path, filename, staging)
        final.parent.mkdir(parents=True, exist_ok=True)
        old: Path | None = None
        if final.exists():
            old = final.with_name(f".{final.name}.old-{os.getpid()}")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            os.replace(final, old)
        try:
            os.replace(root, final)
        except OSError:
            if old is not None:
                os.replace(old, final)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    meta = {
        "sha256": stored.sha256, "size": stored.size, "filename": Path(filename).name[:200],
        "uploaded_at": datetime.now(UTC).isoformat(), "uploaded_by": uploaded_by,
    }
    data_dir = settings.repo_data_path(slug)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "upload.json").write_text(json.dumps(meta), encoding="utf-8")
    return final


def read_upload_meta(slug: str) -> dict | None:
    """What was last uploaded under ``slug`` (sha256, size, …), if anything."""
    from src.config import get_settings

    try:
        path = get_settings().repo_data_path(slug) / "upload.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no record is a normal answer
        return None


def write_display_name(slug: str, name: str) -> None:
    """Record the human name given at upload next to the archive facts."""
    from src.config import get_settings

    meta = read_upload_meta(slug) or {}
    meta["name"] = name[:200]
    path = get_settings().repo_data_path(slug) / "upload.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")


def is_upload_provider(provider: str | None) -> bool:
    return (provider or "") == UPLOAD_PROVIDER


__all__ = [
    "ALLOWED_SUFFIXES",
    "ArchiveError",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_UNPACKED_BYTES",
    "StoredArchive",
    "UPLOAD_PROVIDER",
    "allowed_extensions_text",
    "archive_kind",
    "extract_archive",
    "install_archive",
    "is_upload_provider",
    "max_archive_bytes",
    "read_upload_meta",
    "stream_to_temp",
    "write_display_name",
]
