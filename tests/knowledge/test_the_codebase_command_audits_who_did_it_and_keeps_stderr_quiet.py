"""`analyzer codebase import` leaves the same audit rows the web upload does, and a quiet stderr.

The contract (docs/CODEBASE.md, "Command line"):

  * a pack the command CREATES leaves `codebase.pack_created` and every import
    leaves `codebase.version_uploaded`, both with `via: "cli"`; adding a version
    to an existing pack leaves no `pack_created`;
  * the actor is the person named by `--as` (their email and id), or `cli` for
    the operator at the keyboard - never `cli` for a run made as somebody;
  * lines taken on `celmis:allow-secret` are printed by place and rule (never the
    text), counted as `allowed=N` on the result line and as `allowed_count` in
    the audit row;
  * a successful import does not print the audit writer's `audit_written` JSON on
    stderr (the command is one line of output); `-v` brings it back;
  * a person who may neither create packs nor see a pack is refused with ONE
    sentence, whether the slug is taken by a hidden pack or is free.
"""

from __future__ import annotations

import logging

import pytest

from tests.knowledge.test_the_codebase_command_imports_a_folder_and_lists_packs import (  # noqa: F401
    _folder,
    shell,
)

REAL = "aB3dE6gH9jK2mN5pQ8sT"
LINE = "pack not found or not accessible"


@pytest.fixture
def audit(monkeypatch):
    """Every `record_action` of the command, as the dicts it was called with."""
    import src.security.audit as audit_mod

    rows: list[dict] = []
    monkeypatch.setattr(audit_mod, "record_action", lambda **kw: rows.append(kw))
    yield rows


@pytest.fixture(autouse=True)
def _restore_audit_logger_level():
    logger = logging.getLogger("audit")
    before = logger.level
    yield
    logger.setLevel(before)


def _kit(tmp_path, **extra):
    return _folder(tmp_path / "kit", **{"README.md": "# Kit\n", **extra})


def test_the_operator_creating_a_pack_leaves_a_pack_created_and_a_version_row_as_cli(shell, audit, tmp_path):  # noqa: F811
    r = shell.run("import", str(_kit(tmp_path)), "-w", "ws-a", "-p", "kit", "--audience", "workspace")
    assert r.exit_code == 0, r.output
    assert [row["action"] for row in audit] == ["codebase.pack_created", "codebase.version_uploaded"]
    for row in audit:
        assert row["actor"] == "cli" and row["actor_id"] is None
        assert row["workspace_id"] == "wsid-a" and row["detail"]["via"] == "cli"
    created, uploaded = audit
    assert created["detail"] == {"audience": "workspace", "teams": 0, "via": "cli"}
    assert created["target"] == uploaded["target"]
    assert uploaded["detail"] == {"number": 1, "file_count": 1, "allowed_count": 0, "created": True, "via": "cli"}


def test_a_second_import_leaves_only_a_version_row(shell, audit, tmp_path):  # noqa: F811
    kit = _kit(tmp_path)
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "kit").exit_code == 0
    audit.clear()
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "kit").exit_code == 0
    assert [row["action"] for row in audit] == ["codebase.version_uploaded"]
    assert audit[0]["detail"]["created"] is False and audit[0]["detail"]["number"] == 2


def test_an_import_as_a_person_names_that_person_as_the_actor(shell, audit, tmp_path):  # noqa: F811
    r = shell.run("import", str(_kit(tmp_path)), "-w", "ws-a", "-p", "kit", "--as", "editor@acme-corp.io")
    assert r.exit_code == 0, r.output
    assert [row["action"] for row in audit] == ["codebase.pack_created", "codebase.version_uploaded"]
    assert {(row["actor"], row["actor_id"]) for row in audit} == {("editor@acme-corp.io", "u-editor")}


def test_a_refused_import_leaves_no_audit_row(shell, audit, tmp_path):  # noqa: F811
    r = shell.run("import", str(_kit(tmp_path)), "-w", "ws-a", "-p", "kit", "--as", "member@acme-corp.io")
    assert r.exit_code == 1
    assert audit == []


def test_a_marked_line_is_listed_by_place_and_rule_and_counted(shell, audit, tmp_path):  # noqa: F811
    kit = _kit(tmp_path, notes__bad=f"# Bad\n\nDo not do this:\n\npassword = '{REAL}'  # celmis:allow-secret\n")
    (kit / "notes" / "bad").rename(kit / "notes" / "bad.md")
    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit")
    assert r.exit_code == 0, r.output
    assert "allowed by celmis:allow-secret: notes/bad.md:5 " in r.output
    assert "allowed=1" in r.output and REAL not in r.output
    assert audit[-1]["detail"]["allowed_count"] == 1
    assert REAL not in repr(audit)
    clean = shell.run("import", str(_kit(tmp_path / "other")), "-w", "ws-a", "-p", "other")
    assert "allowed=0" in clean.output and "allowed by" not in clean.output


def test_a_successful_import_prints_no_audit_json_unless_asked(shell, tmp_path, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    kit = _kit(tmp_path)
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "kit").exit_code == 0
    assert not [r for r in caplog.records if r.name == "audit"]
    caplog.clear()
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "kit", "-v").exit_code == 0
    assert [r for r in caplog.records if r.name == "audit"]


def test_a_person_who_may_not_create_is_told_the_same_for_a_hidden_slug_and_a_free_one(shell, tmp_path):  # noqa: F811
    kit = _kit(tmp_path)
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "vault", "--audience", "restricted").exit_code == 0
    hidden = shell.run("import", str(kit), "-w", "ws-a", "-p", "vault", "--as", "member@acme-corp.io")
    free = shell.run("import", str(kit), "-w", "ws-a", "-p", "nothing-here", "--as", "member@acme-corp.io")
    assert hidden.exit_code == free.exit_code == 1
    assert LINE in hidden.output and LINE in free.output
    assert hidden.output.replace("vault", "X") == free.output.replace("nothing-here", "X")
    assert "editor role" not in free.output
