"""A damaged zip, or a pack that cannot be written to a disk, is refused as an
upload problem — never as a crash.

The contract (src/knowledge/ingest.py, docs/CODEBASE.md "Uploading"):

  * whatever `zipfile` raises while an archive is opened, listed or read
    (`ValueError` for a skewed offset, `OSError`, `struct.error`, a bad stream,
    an unknown method) leaves ingest as `IngestError("bad_archive")` with a
    fixed message that repeats nothing from the archive;
  * a path that is both a file and a folder (`a.md` and `a.md/b.md`) is refused
    as `duplicate_path` before anything is read, because a consumer that writes
    the pack to a disk cannot create both.
"""

from __future__ import annotations

import contextlib
import io
import random
import struct
import zipfile

import pytest

from src.knowledge import ingest
from src.knowledge.ingest import IngestError, ingest_files, ingest_zip

SKILL = "---\nname: demo\ndescription: demo skill\n---\n# demo\n\nhello world\n"


def make_zip(files: dict[str, str], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def skewed_central_directory() -> bytes:
    """A valid archive whose end record points six bytes past the central directory."""
    data = bytearray(make_zip({"demo/SKILL.md": SKILL, "README.md": "# hi\n"}))
    end = data.rfind(b"PK\x05\x06")
    (offset,) = struct.unpack_from("<I", data, end + 16)
    struct.pack_into("<I", data, end + 16, offset + 6)
    return bytes(data)


def skewed_local_header() -> bytes:
    """A valid archive whose first entry points before the start of the file."""
    data = bytearray(make_zip({"demo/SKILL.md": SKILL, "README.md": "# hi\n"}))
    at = data.find(b"PK\x01\x02")
    struct.pack_into("<I", data, at + 42, 0xFFFFFFF0)
    return bytes(data)


def test_a_skewed_central_directory_is_a_bad_archive():
    with pytest.raises(IngestError) as caught:
        ingest_zip(skewed_central_directory())
    assert caught.value.code == "bad_archive"


def test_an_entry_that_points_outside_the_file_is_a_bad_archive():
    with pytest.raises(IngestError) as caught:
        ingest_zip(skewed_local_header())
    assert caught.value.code == "bad_archive"


def test_the_refusal_of_a_damaged_archive_repeats_nothing_from_it():
    with pytest.raises(IngestError) as caught:
        ingest_zip(skewed_central_directory())
    assert "demo" not in caught.value.message and "SKILL" not in caught.value.message


def test_a_short_header_or_a_failed_seek_in_an_entry_is_a_bad_archive(monkeypatch):
    data = make_zip({"SKILL.md": SKILL})
    for exc in (struct.error("unpack requires a buffer"), ValueError("negative seek"),
                OSError("disk"), OverflowError("size")):
        def broken(self, *_a, _exc=exc, **_k):
            raise _exc

        monkeypatch.setattr(zipfile.ZipFile, "open", broken)
        with pytest.raises(IngestError) as caught:
            ingest_zip(data)
        assert caught.value.code == "bad_archive", exc


def test_a_thousand_random_corruptions_end_in_a_pack_or_a_refusal_never_a_crash():
    seeds = [
        make_zip({"p/SKILL.md": SKILL * 5, "p/notes/a.md": "# a\n\ntext text\n" * 20,
                  "p/x.json": '{"a": 1}', "p/img.png": "\x89PNG" * 10}, compression=method)
        for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED)
    ]
    rng = random.Random(7)
    for _ in range(1000):
        data = bytearray(rng.choice(seeds))
        for _step in range(rng.randint(1, 6)):
            at = rng.randrange(len(data))
            roll = rng.random()
            if roll < 0.6:
                data[at] = rng.randrange(256)
            elif roll < 0.8:
                data[at] ^= 1 << rng.randrange(8)
            elif roll < 0.9:
                del data[at:at + rng.randint(1, 8)]
            else:
                data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        with contextlib.suppress(IngestError):
            ingest_zip(bytes(data))


FILE_AND_FOLDER = {"notes/a.md": "# a\n", "notes/a.md/b.md": "# b\n"}


def test_a_zip_with_a_path_that_is_a_file_and_a_folder_is_refused():
    with pytest.raises(IngestError) as caught:
        ingest_zip(make_zip({"README.md": "# r\n", **FILE_AND_FOLDER}))
    assert caught.value.code == "duplicate_path"
    assert "both a file and a folder" in caught.value.message


def test_the_clash_is_found_in_either_order_and_at_the_pack_root():
    for files in ({"a.md": "x", "a.md/b.md": "y"}, {"a.md/b.md": "y", "a.md": "x"}):
        with pytest.raises(IngestError) as caught:
            ingest_files([(p, t.encode()) for p, t in files.items()])
        assert caught.value.code == "duplicate_path"


def test_the_clash_is_found_whatever_the_case_of_the_letters():
    with pytest.raises(IngestError) as caught:
        ingest_files([("Docs/Guide.md", b"x"), ("docs/guide.MD/part.md", b"y")])
    assert caught.value.code == "duplicate_path"


def test_the_clash_is_found_after_the_shared_top_folder_is_dropped():
    with pytest.raises(IngestError) as caught:
        ingest_zip(make_zip({"pack/a.md": "x", "pack/a.md/b.md": "y"}))
    assert caught.value.code == "duplicate_path"


def test_a_file_and_a_folder_with_similar_names_are_not_a_clash():
    result = ingest_files([("a.md", b"x"), ("a/b.md", b"y"), ("a.md.txt", b"z")])
    assert sorted(f.path for f in result.files) == ["a.md", "a.md.txt", "a/b.md"]


def test_the_refusal_names_the_file_that_is_also_a_folder():
    with pytest.raises(IngestError) as caught:
        ingest.refuse_file_and_folder(["x/a.md", "x/a.md/b.md"])
    assert "x/a.md" in caught.value.message
    ingest.refuse_file_and_folder(["x/a.md", "x/b.md"])
