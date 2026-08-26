"""What commit is this process actually running?

Deploys are rsync + `compose up --build` on the server, so there is no image
tag or registry digest to read back, and `.git` is excluded from the sync.
Without a stamp, "did my push reach production?" can only be answered by
watching log timestamps and hoping — which is exactly how a stale container
goes unnoticed after a failed build.

The deploy workflow appends CELMIS_GIT_SHA/CELMIS_DEPLOYED_AT to the server's
.env; locally both are empty and this degrades to a git call, then to nothing.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    """``{git_sha, git_sha_short, deployed_at, source}`` — never raises."""
    sha = (os.environ.get("CELMIS_GIT_SHA") or "").strip()
    source = "env"
    if not sha:
        # Local runs: the checkout is right there.
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if proc.returncode == 0:
                sha = proc.stdout.strip()
                source = "git"
        except Exception:  # noqa: BLE001 — a missing git is not an error here
            pass
    return {
        "git_sha": sha or None,
        "git_sha_short": sha[:7] if sha else None,
        "deployed_at": (os.environ.get("CELMIS_DEPLOYED_AT") or "").strip() or None,
        "source": source if sha else "unknown",
    }


@lru_cache(maxsize=1)
def toolchain_info() -> dict[str, Any]:
    """Versions of the pinned binaries this container actually has.

    Both are installed in Docker layers that nothing invalidates on their own,
    so they age silently across rebuilds while every deploy still reports
    success. Reading them back is the only way to notice — and it matters
    twice over: the model list the UI offers is only as current as the Claude
    CLI, and a stale scanner reports a stale vulnerability database.
    """
    return {
        # From the SDK, not from a binary on PATH.
        #
        # This ran `claude --version` and reported 2.1.228 — the npm package
        # the image used to install — while every agent session actually
        # executed 2.1.233, because claude_agent_sdk bundles its own CLI and
        # `_find_cli` returns that one BEFORE looking at PATH. The diagnostic
        # named a version that never ran, which is worse than naming none: it
        # is the number somebody would check first when a session misbehaves.
        "claude_code": _bundled_cli_version(),
        # "osv-scanner version 2.5.0 ..." → "2.5.0"
        "osv_scanner": _version(["osv-scanner", "--version"], take=-1),
    }


def _bundled_cli_version() -> str | None:
    """The CLI version the SDK will actually spawn, read from the SDK itself."""
    try:
        from claude_agent_sdk import _cli_version
        return getattr(_cli_version, "__cli_version__", None)
    except Exception:  # noqa: BLE001 — a diagnostic must never raise
        return None


def _version(cmd: list[str], *, take: int) -> str | None:
    """First line of `--version`, reduced to the number. None if unavailable."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=15, check=False)
        if proc.returncode != 0:
            return None
        parts = proc.stdout.strip().splitlines()[0].split()
        return parts[take] if parts else None
    except Exception:  # noqa: BLE001 — a missing binary is information, not an error
        return None


__all__ = ["build_info", "toolchain_info"]
