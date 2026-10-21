"""The documentation pages read the vault the way the reader does, through every route.

THE DEFECT. `GET /api/docs/{slug}` (the list), `/export`, `/export-all` (the archive of
every repository) walked `vault.rglob("*.md")` and opened what it found. A link planted in
the vault to another tenant's note or to any `.md` on the server was listed, exported and
zipped, and so was a note with a secret name (`.env.md`, anything under `secrets/`).
Reading one note by name (`/note?path=`) refused all of them. A note path longer than a
file name can be made that route answer 500.

THE CONTRACT, for each route, as the global admin (who bypasses the access rules, so the
only thing under test is the vault boundary):

  * the notes of the repository's own folder are listed, exported and archived;
  * a link to another repository's note or to a file outside the vault, a link to a folder
    outside it, and a secret-named note are in none of the answers;
  * `/note` answers 404, with the same body as a missing note, for a path that is too long
    and for each of those names.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.api.rbac_world import A_REPO, world

pytestmark = pytest.mark.asyncio

OWN = "OWN_NOTE_BODY_61c"
FOREIGN = "FOREIGN_NOTE_BODY_d02"
OUTSIDE = "OUTSIDE_NOTE_BODY_77e"
SECRET_NAMED = "SECRET_NAMED_BODY_4b9"
VAULT_IN_TMP = "vault_of_this_test"


def _extra():
    from src.api.routers import docs

    return (docs.router,)


@pytest.fixture(autouse=True)
def _vault_in_tmp(tmp_path, monkeypatch):
    """The world sets WORKSPACE_DIR only; the vault folder must be set here too, or it
    falls back to the default one outside the test's temporary directory."""
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / VAULT_IN_TMP))


def _plant_vault():
    from src.config import get_settings

    settings = get_settings()
    own = settings.repo_vault_path(A_REPO)
    other = settings.repo_vault_path("github_other-tenant-repo")
    assert settings.vault_dir.name == VAULT_IN_TMP, "the vault must be the temporary one"
    outside = settings.vault_dir.parent / "outside_notes"
    for folder in (own / "modules", own / "secrets", other, outside):
        folder.mkdir(parents=True, exist_ok=True)
    (own / "modules" / "api.md").write_text(f"---\ntitle: API\n---\n# API\n{OWN}\n")
    (own / "overview.md").write_text(f"---\ntitle: Overview\n---\n# Overview\n{OWN}\n")
    (own / ".env.md").write_text(f"---\ntitle: Env\n---\n{SECRET_NAMED}\n")
    (own / "secrets" / "db.md").write_text(f"---\ntitle: Db\n---\n{SECRET_NAMED}\n")
    (other / "private.md").write_text(f"---\ntitle: Private\n---\n{FOREIGN}\n")
    (outside / "server.md").write_text(f"---\ntitle: Server\n---\n{OUTSIDE}\n")
    try:
        (own / "modules" / "to_other.md").symlink_to(other / "private.md")
        (own / "modules" / "to_server.md").symlink_to(outside / "server.md")
        (own / "linked_folder").symlink_to(outside, target_is_directory=True)
        (own / "linked_tenant").symlink_to(other, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")


def _zip_text(response) -> str:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return "\n".join(zf.read(n).decode() for n in zf.namelist())


def _nothing_foreign(text: str) -> None:
    for marker in (FOREIGN, OUTSIDE, SECRET_NAMED):
        assert marker not in text, marker


async def test_the_list_names_only_the_notes_of_the_repos_own_folder(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        r = await w.client.get(f"/api/docs/{A_REPO}", headers=w.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    assert {n["path"] for n in r.json()["notes"]} == {"modules/api.md", "overview.md"}
    assert r.json()["omitted"] == 0
    _nothing_foreign(r.text)


async def test_the_export_carries_only_the_notes_of_the_repos_own_folder(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        r = await w.client.get(f"/api/docs/{A_REPO}/export", headers=w.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    assert OWN in r.text
    _nothing_foreign(r.text)


async def test_the_archive_of_every_repository_carries_only_the_notes_of_each_own_folder(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        r = await w.client.get("/api/docs/export-all", headers=w.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert {f"{A_REPO}/modules/api.md", f"{A_REPO}/overview.md"} <= names
    assert not any("to_other" in n or "to_server" in n or ".env" in n or "secrets/" in n
                   or "linked_" in n for n in names), names
    text = _zip_text(r)
    assert OWN in text
    _nothing_foreign(text)


@pytest.mark.parametrize("path", [
    "modules/to_other.md", "modules/to_server.md", "linked_folder/server.md",
    "linked_tenant/private.md", ".env.md", "secrets/db.md", "modules/nothing.md",
])
async def test_a_note_by_name_answers_the_404_of_a_missing_note(tmp_path, monkeypatch, path):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        missing = await w.client.get(f"/api/docs/{A_REPO}/note", params={"path": "modules/absent.md"},
                                     headers=w.h("su", "ws-a"))
        r = await w.client.get(f"/api/docs/{A_REPO}/note", params={"path": path},
                               headers=w.h("su", "ws-a"))
    assert r.status_code == 404 and r.json() == missing.json() == {"detail": "Note not found"}
    _nothing_foreign(r.text)


@pytest.mark.parametrize("path", ["modules/" + "a" * 300 + ".md", "a" * 480 + ".md", "a/" * 240 + "x.md"])
async def test_a_note_path_too_long_for_a_file_name_is_a_404_not_a_500(tmp_path, monkeypatch, path):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        r = await w.client.get(f"/api/docs/{A_REPO}/note", params={"path": path},
                               headers=w.h("su", "ws-a"))
    assert r.status_code == 404 and r.json() == {"detail": "Note not found"}
    assert "Errno" not in r.text and "vault" not in r.text.lower()


async def test_a_note_of_the_repo_is_still_read_by_name(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        _plant_vault()
        r = await w.client.get(f"/api/docs/{A_REPO}/note", params={"path": "modules/api.md"},
                               headers=w.h("su", "ws-a"))
    assert r.status_code == 200 and OWN in r.json()["body"]
