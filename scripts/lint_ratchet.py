"""Lint that cannot get worse, on a codebase that has never had CI.

A repository with 602 outstanding findings has two bad options: turn the
linter on and paint the build red on day one, so everybody learns to ignore
it — or turn it off and learn nothing.

This is the third one. The count is recorded in `.ruff-baseline`; the build
fails when it GROWS and tells you to lower the file when it shrinks. New code
is held to the standard immediately, and the existing debt is paid down
whenever somebody is already in the file.

Nothing here is clever, and that is deliberate: a per-rule matrix would be
more precise and would need maintaining, and the thing being bought is that
the number stops rising.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".ruff-baseline"
TARGETS = ["src", "tests", "scripts"]


def current() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *TARGETS, "--output-format", "concise"],
        capture_output=True, text=True, cwd=ROOT,
    ).stdout
    # Every finding is one line "path:line:col: CODE message"; the trailing
    # summary lines are not.
    return sum(1 for line in out.splitlines() if ": " in line and line[0] not in " [F")


def main() -> int:
    now = current()
    was = int(BASELINE.read_text().strip()) if BASELINE.exists() else now

    if now > was:
        print(f"::error::ruff findings rose from {was} to {now}. "
              f"Run `ruff check {' '.join(TARGETS)}` and fix what you added.")
        return 1
    if now < was:
        print(f"ruff findings fell from {was} to {now} — "
              f"write {now} into .ruff-baseline so it cannot come back.")
    else:
        print(f"ruff findings unchanged at {now}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
