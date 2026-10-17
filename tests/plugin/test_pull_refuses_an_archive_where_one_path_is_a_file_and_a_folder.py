"""`celmis-pack pull` says so, before it writes anything, when an archive holds a
path that is both a file and a folder (`a.md` and `a.md/b.md`).

The contract (bin/celmis-pack `extract`, src/knowledge/ingest.py): a disk cannot
hold both, so the server refuses such a pack on upload; a pack that got through
anyway (an older server, a hand-made archive) must not leave a half-written tree
behind — the tool stops with exit code 4 ("refused") and one line, and neither
the target folder nor a staging folder exists afterwards.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import zipfile

import pytest

from tests.plugin import contract as C

CLI = C.PLUGIN / "bin" / "celmis-pack"


def load_cli():
    loader = importlib.machinery.SourceFileLoader("celmis_pack_extract_clash", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()


def archive(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "# text\n")
    return buf.getvalue()


@pytest.mark.parametrize("names", [
    ("notes/a.md", "notes/a.md/b.md"),
    ("notes/a.md/b.md", "notes/a.md"),
    ("Notes/Guide.md", "notes/guide.md/part.md"),
    ("a.md", "a.md/deep/b.md"),
])
def test_a_file_that_is_also_a_folder_stops_the_extraction_before_anything_is_written(tmp_path, names):
    target = tmp_path / "area" / "out"
    (tmp_path / "area").mkdir()
    with pytest.raises(cli.Stop) as caught:
        cli.extract(archive(*names), target)
    assert caught.value.code == cli.EXIT_REFUSED
    assert "both a file and a folder" in caught.value.message
    assert "\n" not in caught.value.message
    assert not target.exists()
    assert list((tmp_path / "area").iterdir()) == []                   # no staging folder either


def test_names_that_only_look_alike_are_extracted(tmp_path):
    target = tmp_path / "out"
    shas = cli.extract(archive("a.md", "a/b.md", "a.md.txt"), target)
    assert sorted(shas) == ["a.md", "a.md.txt", "a/b.md"]
    assert (target / "a" / "b.md").is_file() and (target / "a.md").is_file()
