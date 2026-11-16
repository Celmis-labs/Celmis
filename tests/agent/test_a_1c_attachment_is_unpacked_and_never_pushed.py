"""A 1C file attached to an agent session is unpacked beside itself, checked
against the session's repository, and stays out of the agent's push.

The embedded session has no shell, so the container is opened on the server:
`<file>.d/` gets the code under dump paths and a `REPORT.md` the agent reads
first. A single-repo session keeps `_attachments/` inside the clone, and
`commit_and_push` used to `git add -A` it — the user's file and its unpacked
code went to the remote with the agent's edits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.agent import workspace as ws_mod
from src.agent.workspace import commit_and_push
from src.onec.report import EXTERNAL_MARK
from src.onec.unpack import describe_attachment, unpack
from tests.agent.test_workspace_push import _git, _workspace, remote_and_clone  # noqa: F401
from tests.onec import acme


def test_the_file_is_unpacked_and_compared_with_the_repository(tmp_path: Path):
    repo = acme.write_dump(tmp_path / "repo")
    att = repo / "_attachments"
    att.mkdir()
    target = att / f"{acme.REPORT}.erf"
    target.write_bytes(acme.report_file())

    report_rel = unpack(target, [repo])

    assert report_rel == f"_attachments/{target.name}.d/REPORT.md"
    out = att / f"{target.name}.d"
    report = (out / "REPORT.md").read_text(encoding="utf-8")
    assert report.startswith(EXTERNAL_MARK)
    assert "verdict: older than the configuration" in report
    assert "ФорматСуммы: passes 3 argument(s)" in report
    code = out / f"Reports/{acme.REPORT}/Ext/ObjectModule.bsl"
    assert code.read_text(encoding="utf-8") == acme.FILE_OBJECT_MODULE
    assert "read _attachments/x.d/REPORT.md first" in describe_attachment(
        "_attachments/x", "_attachments/x.d/REPORT.md")


def test_without_a_dump_the_file_is_still_described(tmp_path: Path):
    target = tmp_path / "a.erf"
    target.write_bytes(acme.report_file())
    report = (tmp_path / unpack(target, [tmp_path / "plain-repo"]).split("/", 1)[1]).read_text(
        encoding="utf-8")
    assert "nothing was compared" in report
    assert "Местная: passes 2 argument(s)" in report  # the file's own checks still run


def test_anything_else_is_left_alone(tmp_path: Path):
    target = tmp_path / "notes.bin"
    target.write_bytes(b"\x00\x01 not a container")
    assert unpack(target, []) is None
    assert not (tmp_path / "notes.bin.d").exists()


def test_a_staged_1c_file_is_unpacked_when_the_session_adopts_it(remote_and_clone, monkeypatch,  # noqa: F811
                                                                 tmp_path: Path):
    remote, clone = remote_and_clone
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / f"{acme.REPORT}.erf").write_bytes(acme.report_file())
    (staged / "log.txt").write_text("a log line\n")
    monkeypatch.setattr(ws_mod, "staging_root", lambda session_id: staged)

    moved = ws_mod.adopt_staged("abcdef12-rest", _workspace(clone, remote, "abcdef12-rest"))

    assert "_attachments/log.txt" in moved
    erf = next(m for m in moved if ".erf" in m)
    assert f"read _attachments/{acme.REPORT}.erf.d/REPORT.md first" in erf
    assert (clone / "_attachments" / f"{acme.REPORT}.erf.d" / "REPORT.md").is_file()


def test_attachments_never_reach_the_remote(remote_and_clone):  # noqa: F811
    remote, clone = remote_and_clone
    att = clone / "_attachments"
    (att / "x.erf.d").mkdir(parents=True)
    (att / "x.erf").write_bytes(acme.report_file())
    (att / "x.erf.d" / "REPORT.md").write_text("report\n")
    (clone / "fix.bsl").write_text("Процедура А()\nКонецПроцедуры\n")

    pushed = commit_and_push(_workspace(clone, remote, "abcdef12-rest-of-uuid"))

    assert pushed is not None
    files = _git("ls-tree", "-r", "--name-only", pushed["branch"], cwd=remote).splitlines()
    assert "fix.bsl" in files
    assert not [f for f in files if f.startswith("_attachments")], files


def test_attachments_alone_are_nothing_to_push(remote_and_clone):  # noqa: F811
    remote, clone = remote_and_clone
    (clone / "_attachments").mkdir()
    (clone / "_attachments" / "x.erf").write_bytes(acme.report_file())
    assert commit_and_push(_workspace(clone, remote, "abcdef12-rest-of-uuid")) is None
    heads = subprocess.run(["git", "ls-remote", "--heads", str(remote)], capture_output=True,
                           text=True, check=True).stdout
    assert "celmis-agent/" not in heads
