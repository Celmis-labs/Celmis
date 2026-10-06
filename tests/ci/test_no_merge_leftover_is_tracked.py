"""A merge-conflict leftover (`*.orig`, `*.rej`) is not part of the repository.

`src/db/models.py.orig` once rode in on a merge commit: a stale second copy of
the ORM models in the public tree and in any sdist, there for a reader or a
glob over `src/` to trip on. `.gitignore` now refuses them and this test fails
a branch that tracks one anyway.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _tracked() -> list[str]:
    try:
        done = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                              timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return done.stdout.splitlines()


def test_no_orig_or_rej_file_is_tracked() -> None:
    leftovers = [p for p in _tracked() if p.endswith((".orig", ".rej"))]
    assert not leftovers, leftovers


def test_gitignore_refuses_them() -> None:
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"*.orig", "*.rej"} <= lines
