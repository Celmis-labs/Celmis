"""Code from an archive: what the extractor accepts and what it refuses.

An archive is hostile input. Each refusal below is a way a file ends up outside
the repository directory, or a way a small upload fills the disk; the
acceptance tests cover the shapes real archives have (a single top folder,
UTF-8 names with and without the zip flag, Cyrillic everywhere).
"""

from __future__ import annotations

import io
import os
import tarfile
import unicodedata
import zipfile
from pathlib import Path

import pytest

from src.repos import upload
from src.repos.upload import ArchiveError, archive_kind, extract_archive


def _zip(path: Path, files: dict[str, bytes | str], *, utf8_flag: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if not utf8_flag:
                info.flag_bits &= ~0x800
            zf.writestr(info, data)
    return path


def _tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for info, data in members:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


# ─── file names ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a.zip", "A.ZIP", "src.tar.gz", "src.TGZ"])
def test_zip_and_tar_gz_are_accepted(name):
    assert archive_kind(name) in ("zip", "tar")


@pytest.mark.parametrize("name", ["a.rar", "a.7z", "a.tar", "a.tar.zst", "a.tar.bz2", "zip", ""])
def test_everything_else_is_refused_with_the_allowed_list(name):
    with pytest.raises(ArchiveError) as exc:
        archive_kind(name)
    assert ".zip" in str(exc.value) and ".tar.gz" in str(exc.value) and ".tgz" in str(exc.value)


# ─── acceptance ──────────────────────────────────────────────────────


def test_a_single_top_folder_is_stripped(tmp_path):
    z = _zip(tmp_path / "a.zip", {"proj/src/main.py": "print(1)", "proj/README.md": "hi"})
    root = extract_archive(z, "a.zip", _staging(tmp_path))
    assert (root / "src" / "main.py").read_text() == "print(1)"
    assert not (root / "proj").exists()


def test_several_top_entries_are_kept_as_they_are(tmp_path):
    z = _zip(tmp_path / "a.zip", {"a/x.py": "1", "b/y.py": "2"})
    root = extract_archive(z, "a.zip", _staging(tmp_path))
    assert (root / "a" / "x.py").exists() and (root / "b" / "y.py").exists()


def test_the_macos_resource_folder_does_not_defeat_the_strip(tmp_path):
    z = _zip(tmp_path / "a.zip", {"proj/x.py": "1", "__MACOSX/proj/._x.py": "junk"})
    root = extract_archive(z, "a.zip", _staging(tmp_path))
    assert (root / "x.py").exists() and not (root.parent / "__MACOSX").exists()


def test_a_tar_gz_unpacks(tmp_path):
    t = _tar(tmp_path / "a.tar.gz", [_file("proj/m.py", b"print(2)")])
    root = extract_archive(t, "a.tar.gz", _staging(tmp_path))
    assert (root / "m.py").read_bytes() == b"print(2)"


def test_cyrillic_names_with_the_utf8_flag_come_out_in_nfc(tmp_path):
    nfd = unicodedata.normalize("NFD", "Документ-Й/Модуль.bsl")
    assert nfd != unicodedata.normalize("NFC", nfd)
    z = _zip(tmp_path / "a.zip", {nfd: "Процедура Тест()\nКонецПроцедуры", "Другий-Й/a.txt": "1"})
    root = extract_archive(z, "a.zip", _staging(tmp_path))
    names = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    assert unicodedata.normalize("NFC", "Документ-Й/Модуль.bsl") in names
    assert all(n == unicodedata.normalize("NFC", n) for n in names)
    assert "Процедура" in (root / unicodedata.normalize("NFC", "Документ-Й/Модуль.bsl")).read_text(encoding="utf-8")


def test_utf8_names_written_without_the_flag_are_read_as_utf8(tmp_path):
    path = tmp_path / "a.zip"
    raw = "Каталог/Файл.xml".encode()
    # A writer that stores UTF-8 bytes but does not set bit 11: build the
    # archive with an ASCII stand-in of the same byte length, then swap the
    # bytes in both the local header and the central directory.
    stand_in = (b"d" * (len(raw) - 4)) + b".xml"
    stand_in = stand_in[:7] + b"/" + stand_in[8:]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(stand_in.decode(), "<a/>")
    path.write_bytes(path.read_bytes().replace(stand_in, raw))
    with zipfile.ZipFile(path) as zf:
        assert not zf.infolist()[0].flag_bits & 0x800
    root = extract_archive(path, "a.zip", _staging(tmp_path))
    assert (root / "Файл.xml").read_text() == "<a/>"  # the one top folder is stripped


def test_a_legacy_cp437_name_is_kept_readable(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("caf\u00e9/\u00fc.txt")  # not valid UTF-8 once re-read
        info.flag_bits = 0
        zf.writestr(info, "x")
    root = extract_archive(path, "a.zip", _staging(tmp_path))
    assert [p.name for p in root.rglob("*") if p.is_file()]


def test_an_archive_with_no_files_is_refused(tmp_path):
    z = _zip(tmp_path / "a.zip", {"empty/": ""})
    with pytest.raises(ArchiveError, match="no files"):
        extract_archive(z, "a.zip", _staging(tmp_path))


def test_a_file_that_is_not_an_archive_is_refused(tmp_path):
    bad = tmp_path / "a.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(ArchiveError, match="not a valid zip"):
        extract_archive(bad, "a.zip", _staging(tmp_path))
    bad_t = tmp_path / "a.tar.gz"
    bad_t.write_bytes(b"not a tar")
    other = tmp_path / "staging2"
    other.mkdir()
    with pytest.raises(ArchiveError, match="not a valid"):
        extract_archive(bad_t, "a.tar.gz", other)


# ─── traversal ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "../evil.py", "a/../../evil.py", "/etc/evil.py", "C:/evil.py", "a\\..\\..\\evil.py",
])
def test_zip_slip_is_refused_and_nothing_is_written_outside(tmp_path, name):
    z = _zip(tmp_path / "a.zip", {"ok.py": "1", name: "pwned"})
    staging = _staging(tmp_path)
    with pytest.raises(ArchiveError):
        extract_archive(z, "a.zip", staging)
    assert not (tmp_path / "evil.py").exists() and not Path("/etc/evil.py").exists()


@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/tmp/evil-abs.py"])
def test_tar_traversal_and_absolute_paths_are_refused(tmp_path, name):
    t = _tar(tmp_path / "a.tar.gz", [_file("ok.py"), _file(name, b"pwned")])
    with pytest.raises(ArchiveError):
        extract_archive(t, "a.tar.gz", _staging(tmp_path))
    assert not (tmp_path / "evil.py").exists() and not Path("/tmp/evil-abs.py").exists()


# ─── links and special files ─────────────────────────────────────────


def test_a_zip_symlink_is_refused(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ok.py", "1")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(ArchiveError, match="link or special"):
        extract_archive(path, "a.zip", _staging(tmp_path))


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_tar_links_and_devices_are_refused(tmp_path, kind):
    info = tarfile.TarInfo("thing")
    info.type = kind
    info.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
    t = _tar(tmp_path / "a.tar.gz", [_file("ok.py"), (info, None)])
    with pytest.raises(ArchiveError, match="link|special"):
        extract_archive(t, "a.tar.gz", _staging(tmp_path))


# ─── limits ──────────────────────────────────────────────────────────


def test_the_file_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "MAX_FILES", 3)
    z = _zip(tmp_path / "a.zip", {f"f{i}.txt": "x" for i in range(5)})
    with pytest.raises(ArchiveError, match="more than 3 files"):
        extract_archive(z, "a.zip", _staging(tmp_path))


def test_the_unpacked_size_is_capped_on_the_bytes_actually_written(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "MAX_UNPACKED_BYTES", 1000)
    z = _zip(tmp_path / "a.zip", {"a.txt": "x" * 600, "b.txt": "y" * 600})
    with pytest.raises(ArchiveError, match="unpacks to more than"):
        extract_archive(z, "a.zip", _staging(tmp_path))


def test_one_file_over_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "MAX_FILE_BYTES", 100)
    t = _tar(tmp_path / "a.tar.gz", [_file("big.bin", b"z" * 500)])
    with pytest.raises(ArchiveError, match="larger than"):
        extract_archive(t, "a.tar.gz", _staging(tmp_path))


def test_the_real_limits_fit_a_large_archive():
    assert upload.MAX_UNPACKED_BYTES >= 2 * 1024**3
    assert upload.MAX_FILES >= 100_000
    assert upload.MAX_FILE_BYTES >= 64 * 1024 * 1024
    assert upload.max_archive_bytes() == 250 * 1024 * 1024


# ─── streaming and the swap ──────────────────────────────────────────


def test_streaming_stops_at_the_upload_limit_and_leaves_no_file(tmp_path):
    chunks = iter([b"a" * 600, b"b" * 600, b""])
    with pytest.raises(ArchiveError, match="larger than"):
        upload.stream_to_temp(lambda: next(chunks), tmp_path / "tmp", limit=1000)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_streaming_hashes_what_it_stored(tmp_path):
    import hashlib

    data = [b"hello ", b"world", b""]
    it = iter(data)
    stored = upload.stream_to_temp(lambda: next(it), tmp_path / "tmp")
    assert stored.sha256 == hashlib.sha256(b"hello world").hexdigest() and stored.size == 11
    assert stored.path.read_bytes() == b"hello world"


def test_a_failed_reupload_keeps_the_working_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    from src.config import get_settings

    get_settings.cache_clear()
    try:
        good = _zip(tmp_path / "v1.zip", {"p/a.py": "v1"})
        stored = upload.StoredArchive(good, "0" * 64, good.stat().st_size)
        final = upload.install_archive(stored, "v1.zip", "legacy-code")
        assert (final / "a.py").read_text() == "v1"

        bad = _zip(tmp_path / "v2.zip", {"p/a.py": "v2", "../evil": "x"})
        with pytest.raises(ArchiveError):
            upload.install_archive(upload.StoredArchive(bad, "1" * 64, 1), "v2.zip", "legacy-code")
        assert (final / "a.py").read_text() == "v1"

        ok = _zip(tmp_path / "v3.zip", {"p/a.py": "v3"})
        upload.install_archive(upload.StoredArchive(ok, "2" * 64, 1), "v3.zip", "legacy-code")
        assert (final / "a.py").read_text() == "v3"
        assert upload.read_upload_meta("legacy-code")["sha256"] == "2" * 64
        leftovers = [p.name for p in final.parent.iterdir()]
        assert leftovers == ["legacy-code"], leftovers
        assert os.path.isdir(get_settings().workspace_dir / "tmp")
    finally:
        get_settings.cache_clear()
