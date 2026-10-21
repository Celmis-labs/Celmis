"""A note name the operating system refuses is a note that is not there.

THE DEFECT. `VaultReader.read` and the docs API asked `is_file()` of a path built
from the caller's name. A name longer than a file name can be (300 characters), or
a path nested deeper than the system allows, makes `stat` raise
`OSError: [Errno 63] File name too long: '<the vault's absolute path>'`. The
exploration agent put that text in the tool result the MODEL reads, and the docs
API answered 500.

THE CONTRACT. Whatever the name, `read` answers None, `note_file` answers None and
the docs route answers the 404 of a note that does not exist, with no path in it.
A note that is there is still read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api.routers import docs
from src.config import Settings
from src.vault.reader import VaultReader, note_file

REPO = "acme-app"
OWN = "OWN_NOTE_MARKER_9d1"

LONG_NAMES = {
    "a name of 300 characters": "modules/" + "a" * 300 + ".md",
    "a name of 5000 characters": "a" * 5000 + ".md",
    "a folder of 300 characters": "a" * 300 + "/x.md",
    "a path 600 folders deep": "a/" * 600 + "x.md",
}


@pytest.fixture()
def world(tmp_path: Path):
    settings = Settings(workspace_dir=tmp_path / "ws", vault_dir=tmp_path / "vault")
    own = settings.repo_vault_path(REPO)
    (own / "modules").mkdir(parents=True)
    (own / "modules" / "legit.md").write_text(f"---\ntype: module\n---\n{OWN}\n", encoding="utf-8")
    return settings, own


@pytest.mark.parametrize("rel", LONG_NAMES.values(), ids=LONG_NAMES.keys())
def test_the_reader_answers_none_for_a_name_the_system_refuses(world, rel):
    settings, _own = world
    assert VaultReader(settings).read(REPO, rel) is None


@pytest.mark.parametrize("rel", LONG_NAMES.values(), ids=LONG_NAMES.keys())
def test_note_file_answers_none_for_a_name_the_system_refuses(world, rel):
    _settings, own = world
    assert note_file(own, rel) is None


@pytest.mark.parametrize("rel", LONG_NAMES.values(), ids=LONG_NAMES.keys())
def test_the_docs_route_answers_the_404_of_a_missing_note_without_a_path(world, rel):
    _settings, own = world
    with pytest.raises(HTTPException) as caught:
        docs._resolve(own, rel)
    assert caught.value.status_code == 404
    assert caught.value.detail == "Note not found"
    assert str(own) not in str(caught.value.detail)


def test_a_note_that_is_there_is_still_read(world):
    settings, own = world
    assert OWN in VaultReader(settings).read(REPO, "modules/legit.md").content
    assert note_file(own, "modules/legit.md") == (own / "modules" / "legit.md").resolve()
    assert docs._resolve(own, "modules/legit.md") == (own / "modules" / "legit.md").resolve()
