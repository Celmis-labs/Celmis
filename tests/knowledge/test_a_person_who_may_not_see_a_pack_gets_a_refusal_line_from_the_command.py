"""`analyzer codebase import --as PERSON` answers a hidden pack with one line.

The contract (docs/CODEBASE.md, "Command line"): a pack the person may not see
answers exactly like one that does not exist — `pack not found or not
accessible`, exit code 1, no traceback — whether it is restricted or granted
only to teams the person is not in; and the traceback Typer would print for a
real crash never lists local variables (parsed files, tokens).
"""

from __future__ import annotations

import pytest

from src.cli import app
from tests.knowledge.test_the_codebase_command_imports_a_folder_and_lists_packs import (  # noqa: F401
    TEAM,
    shell,
)

LINE = "pack not found or not accessible"


@pytest.fixture
def kit(tmp_path):
    folder = tmp_path / "kit"
    folder.mkdir()
    (folder / "README.md").write_text("# kit\nhello world\n")
    return folder


def refused(result) -> bool:
    return (result.exit_code == 1 and isinstance(result.exception, SystemExit)
            and LINE in result.output and "Traceback" not in result.output)


def test_a_restricted_pack_is_refused_to_a_person_who_is_not_on_it(shell, kit):  # noqa: F811
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack",
                     "--audience", "restricted").exit_code == 0
    result = shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack",
                       "--as", "editor@acme-corp.io")
    assert refused(result), result.output


def test_a_teams_pack_is_refused_to_a_person_outside_its_teams(shell, kit):  # noqa: F811
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "teamonly", "--audience", "teams",
                     "--team", f"{TEAM}:read").exit_code == 0
    result = shell.run("import", str(kit), "-w", "ws-a", "-p", "teamonly",
                       "--as", "member@acme-corp.io")
    assert refused(result), result.output


def test_the_refusal_names_neither_the_pack_nor_its_audience(shell, kit):  # noqa: F811
    shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack", "--audience", "restricted")
    hidden = shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack", "--as", "editor@acme-corp.io")
    assert refused(hidden)
    assert "secretpack" not in hidden.output and "restricted" not in hidden.output


def test_a_hidden_pack_gets_no_new_version(shell, kit):  # noqa: F811
    from src.db.models import KnowledgePackVersion

    shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack", "--audience", "restricted")
    before = shell.count(KnowledgePackVersion)
    shell.run("import", str(kit), "-w", "ws-a", "-p", "secretpack", "--as", "editor@acme-corp.io")
    assert shell.count(KnowledgePackVersion) == before


def test_a_traceback_of_the_command_line_never_prints_local_variables():
    assert app.pretty_exceptions_show_locals is False
