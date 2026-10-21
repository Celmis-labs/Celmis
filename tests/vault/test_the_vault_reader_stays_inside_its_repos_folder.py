"""A note path is untrusted text; the vault reader opens only this repo's own notes.

THE DEFECT. `VaultReader.read` joined the path to `vault/projects/<repo>` and
opened the result. The Q&A exploration agent's `read_vault_note` tool hands it a
path the MODEL chose, so `../other-repo/secret_note` read another repository's
note (another tenant's, on a shared server), an absolute path read any `.md`
file the server could open, and `../../../ws/repos/acme-app/.env` plus the `.md`
the tool appends read `.env.md`, or any `*.md` in a clone. A prompt injected into a repository the agent explores is enough to
steer the model there.

THE CONTRACT. The reader returns the note of THIS repo's vault folder or None:

  * an absolute path, a `..` segment (even one that stays inside), a NUL byte
    and a backslash are refused before anything is opened;
  * the resolved path must be inside the repo's vault folder, so a link planted
    in the vault (to a file or to a folder) is not a way out;
  * the secret-file floor applies to the note's own name (`.env.md`,
    `.env.production.md`, anything under `secrets/`), also through a link inside the
    vault that points at one;
  * every refusal is the None of a note that is not there, so the answer is not
    an oracle for what exists; `list_notes` leaves the same notes out;
  * the docs API (`GET /api/docs/{slug}/note`) asks the same question.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api.routers import docs
from src.config import Settings
from src.vault.reader import VaultReader, confined_note

REPO = "acme-app"
OTHER = "other-tenant-repo"
OWN = "OWN_NOTE_MARKER_61c"
FOREIGN = "FOREIGN_NOTE_MARKER_d02"
OUTSIDE = "OUTSIDE_MARKER_77e"
CLONE_DOC = "CLONE_DOC_MARKER_4b9"


def _note(path: Path, marker: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: module\npath: src\n---\n{marker}\n", encoding="utf-8")
    return path


@pytest.fixture()
def world(tmp_path: Path):
    settings = Settings(workspace_dir=tmp_path / "ws", vault_dir=tmp_path / "vault")
    own = settings.repo_vault_path(REPO)
    other = settings.repo_vault_path(OTHER)
    _note(own / "modules" / "legit.md", OWN)
    _note(own / "_overview.md", OWN)
    _note(other / "secret_note.md", FOREIGN)
    _note(tmp_path / "outside.md", OUTSIDE)
    # A markdown file of a clone, which the tool's appended ".md" can reach by ".." alone.
    clone = settings.repos_dir / REPO
    _note(clone / "docs" / "credentials.md", CLONE_DOC)
    _note(clone / "README.md", CLONE_DOC)
    (clone / ".env.md").write_text(f"X={CLONE_DOC}\n", encoding="utf-8")
    return settings, own, other, tmp_path


def _markers(note) -> str:
    return "" if note is None else note.content


def test_a_note_of_the_repo_is_read(world):
    settings, _own, _other, _root = world
    reader = VaultReader(settings)
    assert OWN in _markers(reader.read(REPO, "modules/legit.md"))
    assert OWN in _markers(reader.read(REPO, "_overview.md"))
    note = reader.read(REPO, "modules/legit.md")
    assert note.relative_path == "modules/legit.md" and note.type == "module"


def _refused_paths(settings: Settings, tmp_path: Path) -> list[str]:
    depth = "../" * 12
    return [
        f"../{OTHER}/secret_note.md",                                  # another repo's vault
        f"modules/../../{OTHER}/secret_note.md",
        f"../../../ws/repos/{REPO}/docs/credentials.md",               # a clone's markdown
        f"../../../ws/repos/{REPO}/README.md",
        f"../../../ws/repos/{REPO}/.env.md",
        f"{depth}{(tmp_path / 'outside.md').as_posix().lstrip('/')}",  # far out of the tree
        (settings.repo_vault_path(OTHER) / "secret_note.md").as_posix(),   # absolute
        (tmp_path / "outside.md").as_posix(),
        "/etc/hosts",
        f"..\\{OTHER}\\secret_note.md",                                # backslash spelling
        "modules\\legit.md",
        "modules/legit.md\x00.png",                                    # NUL
        "modules/../modules/legit.md",                                 # a `..` that stays inside
        "./../" + f"{OTHER}/secret_note.md",
        "",
    ]


def test_every_traversal_form_is_refused_exactly_like_a_missing_note(world):
    settings, _own, _other, tmp_path = world
    reader = VaultReader(settings)
    for rel in _refused_paths(settings, tmp_path):
        assert reader.read(REPO, rel) is None, repr(rel)
    assert reader.read(REPO, "modules/nothing.md") is None, "and a note that is not there is None too"


@pytest.mark.parametrize("rel", [
    ".env.md", "modules/.env.md", "secrets/db.md", "modules/secrets/db.md",
    "id_rsa.md", "modules/.env.production.md",
])
def test_a_secret_name_is_refused_even_when_the_note_exists(world, rel):
    """The floor every other reader applies. The note is really there: only its NAME is judged."""
    settings, own, _other, _root = world
    _note(own / rel, OWN)
    assert (own / rel).is_file()
    assert VaultReader(settings).read(REPO, rel) is None


def test_a_link_in_the_vault_to_a_note_of_another_repo_or_to_a_file_outside_is_not_followed(world):
    settings, own, other, tmp_path = world
    try:
        (own / "modules" / "to_foreign.md").symlink_to(other / "secret_note.md")
        (own / "modules" / "to_outside.md").symlink_to(tmp_path / "outside.md")
        (own / "linked_dir").symlink_to(other, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")
    reader = VaultReader(settings)
    for rel in ("modules/to_foreign.md", "modules/to_outside.md", "linked_dir/secret_note.md"):
        assert reader.read(REPO, rel) is None, rel


def test_a_link_inside_the_vault_to_a_secret_named_note_is_refused_by_its_target(world):
    settings, own, _other, _root = world
    _note(own / "modules" / "real.md", OWN)
    try:
        (own / "modules" / "innocent.md").symlink_to(own / "modules" / "real.md")
        (own / "modules" / "to_env.md").symlink_to(_note(own / "modules" / "secrets" / "x.md", OWN))
    except OSError:  # pragma: no cover
        pytest.skip("symlinks unavailable")
    reader = VaultReader(settings)
    assert reader.read(REPO, "modules/innocent.md") is not None, "a link that stays inside is fine"
    assert reader.read(REPO, "modules/to_env.md") is None


def test_listing_the_notes_leaves_out_what_reading_refuses(world):
    settings, own, other, tmp_path = world
    _note(own / "modules" / "secrets" / "x.md", OWN)
    _note(own / ".env.md", OWN)
    try:
        (own / "modules" / "to_foreign.md").symlink_to(other / "secret_note.md")
        (own / "modules" / "to_outside.md").symlink_to(tmp_path / "outside.md")
    except OSError:  # pragma: no cover
        pytest.skip("symlinks unavailable")
    listed = {n.relative_path for n in VaultReader(settings).list_notes(REPO)}
    assert listed == {"modules/legit.md", "_overview.md"}
    assert not any(FOREIGN in n.content or OUTSIDE in n.content for n in VaultReader(settings).list_notes(REPO))


def test_a_repo_slug_that_is_not_a_folder_name_reads_nothing(world):
    settings, *_ = world
    reader = VaultReader(settings)
    with pytest.raises(ValueError):                 # the slug alphabet is checked by Settings itself
        reader.read(f"../{OTHER}", "secret_note.md")


def test_confined_note_returns_the_real_path_of_a_note_inside_the_folder(world):
    settings, own, _other, _root = world
    assert confined_note(own, "modules/legit.md") == (own / "modules" / "legit.md").resolve()
    assert confined_note(own, "modules/../x.md") is None
    assert confined_note(own, ".") is None


# ─── the docs API asks the same question ─────────────────────────────


@pytest.mark.parametrize("rel", [
    f"../{OTHER}/secret_note.md", "/etc/hosts", "modules/../../" + f"{OTHER}/secret_note.md",
    ".env.md", "secrets/db.md", "modules/nothing.md", "modules",
])
def test_the_docs_note_route_answers_404_for_each_of_them(world, rel):
    settings, own, _other, _root = world
    _note(own / "secrets" / "db.md", OWN)
    _note(own / ".env.md", OWN)
    with pytest.raises(HTTPException) as caught:
        docs._resolve(own, rel)
    assert caught.value.status_code == 404 and caught.value.detail == "Note not found"


def test_the_docs_note_route_still_opens_a_note_of_the_repo(world):
    _settings, own, _other, _root = world
    assert docs._resolve(own, "modules/legit.md") == (own / "modules" / "legit.md").resolve()


# ─── the reverse index walks the same folder ─────────────────────────


def test_the_reverse_index_does_not_index_a_note_the_reader_would_refuse(world, monkeypatch):
    from src.vault import reverse_index

    settings, own, other, _root = world
    _note(own / "modules" / "legit.md", OWN)
    (other / "secret_note.md").write_text(
        "---\ntype: module\npath: elsewhere/leak.py\n---\nbody\n", encoding="utf-8")
    try:
        (own / "modules" / "to_foreign.md").symlink_to(other / "secret_note.md")
    except OSError:  # pragma: no cover
        pytest.skip("symlinks unavailable")
    _note(own / "modules" / ".env.md", OWN)
    monkeypatch.setattr("src.config.get_settings", lambda: settings)

    index = reverse_index.build_reverse_index(REPO, force=True)

    assert "elsewhere/leak.py" not in index, "a note of another repo, reached through a link"
    assert index == {"src": ["_overview.md", "modules/legit.md"]}, "the secret-named note is not indexed either"
