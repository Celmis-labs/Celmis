"""A script the docs run as `scripts/name.py` must carry the executable bit.

`scripts/dev_mcp_journey.py` and `scripts/dev_mcp_e2e.py` had a shebang and were
run directly in the docs, but were committed as 100644, so the documented
command answered "permission denied". The mode is part of the file in git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tracked_scripts() -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/*.py"],
        cwd=ROOT, capture_output=True, text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        rows.append((meta.split()[0], path))
    return rows


def test_every_tracked_script_with_a_shebang_is_executable():
    rows = _tracked_scripts()
    if not rows:  # an exported tree without .git
        return
    loose = [path for mode, path in rows
             if (ROOT / path).read_text(encoding="utf-8").startswith("#!")
             and mode != "100755"]
    assert not loose, f"shebang but not executable in git: {loose}"
