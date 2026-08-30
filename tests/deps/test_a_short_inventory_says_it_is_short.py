"""Four caps bound a dependency audit, and every one used to stop in silence.

    locks.py    _MAX_LOCKFILES = 20            found[:20]
    locks.py    _MAX_ENTRIES_PER_LOCK = 4000   entries[:4000]
    scanner.py  _MAX_MANIFESTS_PER_REPO = 40   break
    auditor.py  _MAX_TRANSITIVE_PER_REPO = 600 break

A monorepo with 25 lock files produced an SBOM missing five, and nothing in
the log, the run summary, the SBOM or the evidence pack said so. The caps
themselves are reasonable — an audit has to end. Not saying they bit is what
turns a bounded read into a false statement about what is installed.

`document.py` already writes the rule down: "count of what was dropped is
printed rather than silently truncated". These are the paths that were not
keeping it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_more_lock_files_than_the_cap_is_reported(tmp_path: Path) -> None:
    from src.deps.locks import _MAX_LOCKFILES, lock_files

    over = _MAX_LOCKFILES + 5
    for i in range(over):
        _write(tmp_path, f"pkg{i:03d}/package-lock.json",
               json.dumps({"lockfileVersion": 3, "packages": {}}))

    notes: list[dict] = []
    kept = lock_files(tmp_path, notes)

    assert len(kept) == _MAX_LOCKFILES
    assert notes, "twenty-five lock files were read as twenty and nobody said so"
    note = next(n for n in notes if n["what"] == "lock files")
    assert note["found"] == over
    assert note["kept"] == _MAX_LOCKFILES
    assert note["dropped"] == 5


def test_a_repository_within_the_cap_reports_nothing(tmp_path: Path) -> None:
    """The note has to mean something. A warning on every run is noise."""
    from src.deps.locks import lock_files

    for i in range(3):
        _write(tmp_path, f"pkg{i}/package-lock.json",
               json.dumps({"lockfileVersion": 3, "packages": {}}))

    notes: list[dict] = []
    assert len(lock_files(tmp_path, notes)) == 3
    assert notes == []


def test_more_manifests_than_the_cap_is_reported(tmp_path: Path) -> None:
    from src.deps.scanner import _MAX_MANIFESTS_PER_REPO, scan_repo

    for i in range(_MAX_MANIFESTS_PER_REPO + 6):
        _write(tmp_path, f"svc{i:03d}/requirements.txt", f"pkg{i}==1.0.0\n")

    notes: list[dict] = []
    scan_repo(tmp_path, notes)

    assert notes, "the walk stopped early and the inventory did not say so"
    note = next(n for n in notes if n["what"] == "manifests")
    assert note["kept"] == _MAX_MANIFESTS_PER_REPO
    assert "stopped at" in note["detail"]


def test_a_repository_within_the_manifest_cap_reports_nothing(tmp_path: Path) -> None:
    from src.deps.scanner import scan_repo

    _write(tmp_path, "requirements.txt", "requests==2.31.0\n")
    notes: list[dict] = []
    scan_repo(tmp_path, notes)
    assert notes == []


def test_the_caps_still_bound_the_work(tmp_path: Path) -> None:
    """Reporting is not the same as removing. An audit has to end.

    If somebody 'fixes' the warning by lifting the limit, a repository with
    fifty thousand lock files becomes an audit that never finishes.
    """
    from src.deps.locks import _MAX_LOCKFILES, lock_files

    for i in range(_MAX_LOCKFILES + 3):
        _write(tmp_path, f"p{i:03d}/package-lock.json", "{}")
    assert len(lock_files(tmp_path)) == _MAX_LOCKFILES


def test_the_notes_are_json(tmp_path: Path) -> None:
    """They travel in the run summary, which is stored as JSON."""
    from src.deps.locks import _MAX_LOCKFILES, lock_files

    for i in range(_MAX_LOCKFILES + 1):
        _write(tmp_path, f"p{i:03d}/package-lock.json", "{}")
    notes: list[dict] = []
    lock_files(tmp_path, notes)
    json.dumps(notes)          # raises if anything in here is not serialisable


def test_the_auditor_puts_them_in_the_run_summary() -> None:
    """The path from a cap to the artefact, read with ast rather than grepped.

    The comment explaining why this exists names `truncated`, so a substring
    check would pass on the prose with the line deleted — the failure this
    repository keeps rediscovering.
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "src" / "deps" / "auditor.py").read_text("utf-8"))
    summary_keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            summary_keys |= {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    assert "truncated" in summary_keys, (
        "the run summary no longer carries what the caps dropped, so the SBOM "
        "and the evidence pack cannot say the inventory was short"
    )


@pytest.mark.parametrize("module,name", [
    ("src.deps.locks", "_MAX_LOCKFILES"),
    ("src.deps.locks", "_MAX_ENTRIES_PER_LOCK"),
    ("src.deps.scanner", "_MAX_MANIFESTS_PER_REPO"),
    ("src.deps.auditor", "_MAX_TRANSITIVE_PER_REPO"),
])
def test_every_cap_is_still_where_this_test_thinks(module: str, name: str) -> None:
    """If a cap moves or is renamed, this file stops describing the code."""
    import importlib

    assert isinstance(getattr(importlib.import_module(module), name), int)
