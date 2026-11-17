"""A session on a repository added as an archive: a copy, a local git, no push.

An archive upload has no remote, so there is nothing to clone. The session
copies the unpacked tree into its workspace, baselines it with one local
commit, and never pushes or opens a PR for it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.agent import workspace as ws_mod
from src.agent.runner import _open_pr
from src.agent.workspace import (
    commit_and_push,
    prepare_multi_workspace,
    prepare_workspace,
    read_only_changes,
)
from src.config import get_settings
from tests.agent.test_workspace_push import _git, remote_and_clone  # noqa: F401
from tests.onec import acme

SHA = "ab12" * 16


@pytest.fixture
def uploaded(tmp_path: Path, monkeypatch):
    """Registers `install_upload(slug, files)`; workspaces live under tmp_path."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()

    def install(slug: str, files: dict[str, bytes | str]) -> Path:
        settings = get_settings()
        root = settings.repo_path(slug)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        meta = settings.repo_data_path(slug) / "upload.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"sha256": SHA}), encoding="utf-8")
        return root

    yield install
    get_settings.cache_clear()


def test_an_upload_is_copied_with_a_baseline_commit(uploaded):
    source = uploaded("erp", {"a.txt": "one\n", "sub/b.txt": "two\n"})
    (source / ".git").mkdir()  # never carried over
    (source / ".git" / "HEAD").write_text("x")

    ws = prepare_workspace("sess0001-aaaa", "upload:erp", "w1")

    assert ws.read_only and ws.repos[0].read_only
    assert ws.repo_slug == "erp"
    assert (ws.repo_dir / "sub" / "b.txt").read_text() == "two\n"
    log = _git("log", "--format=%s", cwd=ws.repo_dir)
    assert log == f"uploaded archive {SHA[:12]}"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=ws.repo_dir) == "celmis-agent/sess0001"
    # The agent's edit shows up against the baseline; the shared tree is untouched.
    (ws.repo_dir / "a.txt").write_text("changed\n")
    assert "a.txt" in _git("status", "--porcelain", cwd=ws.repo_dir)
    assert (source / "a.txt").read_text() == "one\n"
    assert read_only_changes(ws) == ["erp"]


def test_a_missing_or_oversized_upload_gives_a_clear_error(uploaded, monkeypatch):
    with pytest.raises(RuntimeError, match="no unpacked files"):
        prepare_workspace("sess0002-aaaa", "upload:ghost", "w1")
    uploaded("big", {"x.bin": "0123456789"})
    monkeypatch.setenv("CELMIS_AGENT_UPLOAD_COPY_MAX_BYTES", "5")
    with pytest.raises(RuntimeError, match="larger than"):
        prepare_workspace("sess0003-aaaa", "upload:big", "w1")


def test_a_multi_session_mixes_git_and_upload(uploaded, remote_and_clone, monkeypatch):  # noqa: F811
    remote, _clone = remote_and_clone
    uploaded("erp", {"a.txt": "one\n"})

    ws = prepare_multi_workspace(
        "sess0004-aaaa", [("erp", "upload:erp", None), ("svc", str(remote), None)], "w1")

    by_slug = {r.slug: r for r in ws.repos}
    assert by_slug["erp"].read_only and not by_slug["svc"].read_only
    assert (by_slug["erp"].path / "a.txt").is_file()
    assert (by_slug["svc"].path / "README.md").is_file()
    assert ws.read_only  # the first repo is the upload


def test_push_skips_the_upload_and_still_pushes_the_rest(uploaded, remote_and_clone):  # noqa: F811
    remote, _clone = remote_and_clone
    uploaded("erp", {"a.txt": "one\n"})
    ws = prepare_multi_workspace(
        "sess0005-aaaa", [("erp", "upload:erp", None), ("svc", str(remote), None)], "w1")
    (ws.repos[0].path / "a.txt").write_text("edited\n")
    (ws.repos[1].path / "new.txt").write_text("new\n")

    pushed = commit_and_push(ws)

    assert pushed is not None
    assert [p["repo_slug"] for p in pushed["pushes"]] == ["svc"]
    assert pushed["read_only_skipped"] == ["erp"]
    # No PR is attempted for the upload even when it is the "first" repo.
    assert _open_pr(type("Row", (), {"workspace_id": "w1", "title": "t", "prompt": "p"})(),
                    ws, {"repo_slug": "erp", "branch": pushed["branch"]}) is None


def test_an_upload_alone_has_nothing_to_push(uploaded):
    uploaded("erp", {"a.txt": "one\n"})
    ws = prepare_workspace("sess0006-aaaa", "upload:erp", "w1")
    (ws.repo_dir / "a.txt").write_text("edited\n")

    assert commit_and_push(ws) is None
    # Edits stay in the workspace, uncommitted.
    assert (ws.repo_dir / "a.txt").read_text() == "edited\n"
    assert len(_git("log", "--format=%H", cwd=ws.repo_dir).splitlines()) == 1


def test_a_1c_file_is_compared_with_an_uploaded_configuration_dump(uploaded, tmp_path: Path):
    uploaded("erp", dict(acme.dump_files()))
    ws = prepare_workspace("sess0007-aaaa", "upload:erp", "w1")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / f"{acme.REPORT}.erf").write_bytes(acme.report_file())
    ws_mod_staging = ws_mod.staging_root
    ws_mod.staging_root = lambda session_id: staged
    try:
        moved = ws_mod.adopt_staged("sess0007-aaaa", ws)
    finally:
        ws_mod.staging_root = ws_mod_staging

    erf = next(m for m in moved if ".erf" in m)
    assert "REPORT.md first" in erf
    report = ws.repo_dir / "_attachments" / f"{acme.REPORT}.erf.d" / "REPORT.md"
    assert "verdict: older than the configuration" in report.read_text(encoding="utf-8")
    # The attachment is input, not an edit to push.
    assert subprocess.run(["git", "status", "--porcelain", "--", ".", ":(exclude)_attachments"],
                          cwd=ws.repo_dir, capture_output=True, text=True).stdout == ""
