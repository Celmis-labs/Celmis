"""Every walk of a vault asks what a read of one note asks.

THE DEFECT. The docs API (list, export, the archive of every repository) and the
review rule generator walked `vault.rglob("*.md")` and opened each hit. A link
planted in the vault to another repository's note (another tenant's, on a shared
server) or to any `.md` on the server was listed and read, and so was a note with
a secret name (`.env.md`, anything under `secrets/`). Reading ONE note by name
refused all of them; listing did not.

THE CONTRACT. `vault_note_files` lists a note when it is a regular file whose name
the reader would open. A link is not listed even when it stays inside the vault (its
target is listed under its own name, so it would only appear twice); a folder link is
not entered; a secret-named note is not listed; the result is sorted; a folder that is
not there lists nothing. The reverse index and `list_notes` walk the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import Settings
from src.vault.reader import VaultReader, vault_note_files

REPO = "acme-app"
OTHER = "other-tenant-repo"
OWN = "OWN_NOTE_MARKER_61c"
FOREIGN = "FOREIGN_NOTE_MARKER_d02"
OUTSIDE = "OUTSIDE_MARKER_77e"


def _note(path: Path, marker: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: module\npath: src/{path.stem}.py\n---\n{marker}\n", encoding="utf-8")
    return path


@pytest.fixture()
def world(tmp_path: Path):
    settings = Settings(workspace_dir=tmp_path / "ws", vault_dir=tmp_path / "vault")
    own = settings.repo_vault_path(REPO)
    other = settings.repo_vault_path(OTHER)
    _note(own / "modules" / "legit.md", OWN)
    _note(own / "_overview.md", OWN)
    _note(other / "secret_note.md", FOREIGN)
    _note(tmp_path / "outside" / "notes.md", OUTSIDE)
    try:
        (own / "modules" / "to_foreign.md").symlink_to(other / "secret_note.md")
        (own / "modules" / "to_outside.md").symlink_to(tmp_path / "outside" / "notes.md")
        (own / "modules" / "to_legit.md").symlink_to(own / "modules" / "legit.md")
        (own / "folder_link").symlink_to(tmp_path / "outside", target_is_directory=True)
        (own / "foreign_folder").symlink_to(other, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")
    return settings, own, tmp_path


def _names(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in vault_note_files(root)]


def test_only_the_notes_of_the_folder_itself_are_listed_sorted(world):
    _settings, own, _root = world
    assert _names(own) == ["_overview.md", "modules/legit.md"]


def test_no_listed_note_carries_another_repos_or_a_servers_text(world):
    _settings, own, _root = world
    text = "".join(p.read_text(encoding="utf-8") for p in vault_note_files(own))
    assert OWN in text and FOREIGN not in text and OUTSIDE not in text


@pytest.mark.parametrize("rel", [
    ".env.md", "modules/.env.production.md", "secrets/db.md", "modules/secrets/x.md",
    "id_rsa.md", "modules/Credentials.JSON.md",
])
def test_a_secret_named_note_is_not_listed_even_though_it_exists(world, rel):
    _settings, own, _root = world
    _note(own / rel, OWN)
    assert (own / rel).is_file()
    assert rel not in _names(own)


def test_a_link_inside_the_vault_is_not_listed_a_second_time(world):
    _settings, own, _root = world
    assert "modules/to_legit.md" not in _names(own)


def test_a_folder_link_that_stays_inside_the_vault_is_not_entered_so_each_note_is_listed_once(world):
    """A link to a folder of the vault passes the name check (its target is inside), so only the walk
    itself keeps it from listing the same notes again under the link's name."""
    _settings, own, _root = world
    (own / "alias").symlink_to(own / "modules", target_is_directory=True)
    (own / "relative_alias").symlink_to("modules", target_is_directory=True)
    assert (own / "alias" / "legit.md").is_file()          # the link works: a reader that followed it would find the note

    names = _names(own)

    assert names == ["_overview.md", "modules/legit.md"]
    assert {n.relative_path for n in VaultReader(_settings).list_notes(REPO)} == {"_overview.md", "modules/legit.md"}


def test_a_folder_that_is_not_there_lists_nothing(tmp_path):
    assert vault_note_files(tmp_path / "nowhere") == []
    (tmp_path / "file.md").write_text("x", encoding="utf-8")
    assert vault_note_files(tmp_path / "file.md") == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this system")
def test_a_pipe_named_like_a_note_is_not_listed_and_does_not_block(world):
    _settings, own, _root = world
    os.mkfifo(own / "modules" / "pipe.md")
    assert "modules/pipe.md" not in _names(own)


def test_list_notes_and_the_reverse_index_walk_the_same_way(world, monkeypatch):
    from src.vault import reverse_index

    settings, _own, _root = world
    listed = {n.relative_path for n in VaultReader(settings).list_notes(REPO)}
    assert listed == {"_overview.md", "modules/legit.md"}

    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    index = reverse_index.build_reverse_index(REPO, force=True)
    indexed = {rel for rels in index.values() for rel in rels}
    assert indexed == {"modules/legit.md", "_overview.md"}
