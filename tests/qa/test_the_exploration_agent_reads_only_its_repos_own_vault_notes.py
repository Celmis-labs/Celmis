"""`read_vault_note` is a tool the MODEL calls; it must not read past its repo's notes.

THE DEFECT. The tool appended `.md` to the model's `note_path` and passed it to
`VaultReader.read`, which joined it to `vault/projects/<repo>` unchecked:
`../other-repo/secret_note` read another repository's note, an absolute path
read any markdown file on the server, and `../../../ws/repos/acme-app/.env`
became `.env.md`. The exploration agent reads repository text that an attacker
can write, so what the model asks for is attacker-influenced.

THE CONTRACT (details in tests/vault/test_the_vault_reader_stays_inside_its_repos_folder.py).
Every one of those paths answers `{"error": "not_found: <path>"}`, the same
answer as a note that does not exist, with nothing from the file in it; a note
of the repo is returned (redacted) as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.qa.exploration_agent import ExplorationAgent

REPO = "acme-app"
OTHER = "other-tenant-repo"
OWN = "OWN_NOTE_MARKER_61c"
FOREIGN = "FOREIGN_NOTE_MARKER_d02"
OUTSIDE = "OUTSIDE_MARKER_77e"
CLONE_DOC = "CLONE_DOC_MARKER_4b9"
FORBIDDEN = (FOREIGN, OUTSIDE, CLONE_DOC)


def _note(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: module\npath: src\n---\n{marker}\n", encoding="utf-8")


@pytest.fixture()
def agent(tmp_path: Path):
    settings = Settings(workspace_dir=tmp_path / "ws", vault_dir=tmp_path / "vault")
    _note(settings.repo_vault_path(REPO) / "modules" / "legit.md", OWN)
    _note(settings.repo_vault_path(OTHER) / "secret_note.md", FOREIGN)
    _note(tmp_path / "outside.md", OUTSIDE)
    clone = settings.repos_dir / REPO
    _note(clone / "docs" / "credentials.md", CLONE_DOC)
    _note(clone / "README.md", CLONE_DOC)
    (clone / ".env.md").write_text(f"X={CLONE_DOC}\n", encoding="utf-8")
    ea = ExplorationAgent(store=None, repo=REPO, repo_path=clone, gemini=object(),
                          code_reader=object(), settings=settings)
    return ea, tmp_path, settings


def test_a_note_of_the_repo_is_read_with_or_without_the_suffix(agent):
    ea, _root, _settings = agent
    for given in ("modules/legit", "modules/legit.md"):
        out = ea._tool_read_vault_note({"note_path": given})
        assert "error" not in out and OWN in out["body"], given
        assert out["meta"]["type"] == "module"


def _forms(root: Path, settings: Settings) -> list[str]:
    depth = "../" * 12
    return [
        f"../{OTHER}/secret_note",                                         # another repo's vault
        f"modules/../../{OTHER}/secret_note",
        f"../../../ws/repos/{REPO}/docs/credentials",                      # markdown inside the clone
        f"../../../ws/repos/{REPO}/README",
        f"../../../ws/repos/{REPO}/.env",                                  # becomes .env.md
        f"{depth}{(root / 'outside').as_posix().lstrip('/')}",             # far out of the tree
        (settings.repo_vault_path(OTHER) / "secret_note").as_posix(),      # absolute
        (root / "outside").as_posix(),
        f"..\\{OTHER}\\secret_note",                                       # backslashes
        "modules\\legit",
        "modules/legit\x00",                                               # NUL
    ]


def test_every_way_out_of_the_repos_vault_reads_like_a_note_that_is_not_there(agent):
    ea, root, settings = agent
    missing = ea._tool_read_vault_note({"note_path": "modules/nothing"})
    assert list(missing) == ["error"] and missing["error"] == "not_found: modules/nothing.md"
    for given in _forms(root, settings):
        out = ea._tool_read_vault_note({"note_path": given})
        assert list(out) == ["error"], (given, out)
        assert out["error"].startswith("not_found: "), (given, out)
        assert not any(marker in str(out) for marker in FORBIDDEN), given


def test_a_secret_named_note_of_the_repo_is_not_read_either(agent):
    ea, _root, settings = agent
    own = settings.repo_vault_path(REPO)
    _note(own / ".env.md", OWN)
    _note(own / "secrets" / "db.md", OWN)
    for given in (".env", "secrets/db"):
        out = ea._tool_read_vault_note({"note_path": given})
        assert list(out) == ["error"] and OWN not in str(out), given
