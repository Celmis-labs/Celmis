"""Reading code from the clone, at the revision the index was built from.

Every read goes through `git show <sha>:<path>` and `git grep <sha>`, never the
working tree: the clone can be ahead of the index (or have uncommitted edits
from some other tool), and a line number is only worth printing when it is a
line number of the revision named on the idx line. Committed files only, so an
untracked `.env` lying around in the clone cannot be read either.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.mcp_server.dev_profile.paths import git_excludes, is_unsafe_rel_path

logger = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
_MAX_FILE_BYTES = 2_000_000


def clone_path(slug: str) -> Path | None:
    from src.config import InvalidRepoSlug, get_settings

    try:
        path = get_settings().repo_path(slug)
    except InvalidRepoSlug:
        return None
    return path if (path / ".git").exists() else None


def _run(path: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    import os

    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", str(path), *args],
            capture_output=True, timeout=timeout, check=False,
            env={**os.environ, **_ENV},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("dev_git_failed args=%s err=%s", args[:2], exc)
        return None


def head_sha(slug: str) -> str | None:
    path = clone_path(slug)
    if path is None:
        return None
    proc = _run(path, ["rev-parse", "HEAD"], 10)
    out = proc.stdout.decode().strip() if proc and proc.returncode == 0 else ""
    return out if _SHA.match(out) else None


def head_branch(slug: str) -> str | None:
    path = clone_path(slug)
    if path is None:
        return None
    proc = _run(path, ["rev-parse", "--abbrev-ref", "HEAD"], 10)
    name = proc.stdout.decode().strip() if proc and proc.returncode == 0 else ""
    return name if name and name != "HEAD" else None


def has_commit(slug: str, sha: str) -> bool:
    path = clone_path(slug)
    if path is None or not _SHA.match(sha or ""):
        return False
    proc = _run(path, ["cat-file", "-e", f"{sha}^{{commit}}"], 10)
    return bool(proc and proc.returncode == 0)


def show_file(slug: str, sha: str, rel_path: str) -> list[str] | None:
    """The lines of `rel_path` at `sha`, or None (missing, binary, too big)."""
    path = clone_path(slug)
    if path is None or not _SHA.match(sha or "") or is_unsafe_rel_path(rel_path):
        return None
    proc = _run(path, ["show", f"{sha}:{rel_path}"], 10)
    if proc is None or proc.returncode != 0 or len(proc.stdout) > _MAX_FILE_BYTES:
        return None
    if b"\0" in proc.stdout[:8000]:
        return None
    return proc.stdout.decode("utf-8", errors="replace").split("\n")


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str


_HIT_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<text>.*)$")


def _match_survives_redaction(
    hit: GrepHit, pattern: str, regex: bool, ignore_case: bool, word: bool,
) -> bool:
    """Is the match still there once the line is redacted?

    The line a caller is shown is redacted, but WHETHER a line matched is not
    redactable: a pattern that is a guess at a masked secret (``password = "Q``)
    answers yes or no for each guess, and a client can walk a literal out one
    character at a time. So a hit counts only when the text that matched is
    still visible after redaction; a hit that exists only inside a masked
    value is dropped, as if the line did not match. Fails closed.
    """
    try:
        from src.mcp_server.dev_profile import emit

        # The same chain the caller's output goes through (central + literal
        # floor): a line only the floor masks must not keep a hit either.
        shown = emit.redact_text(hit.text)
    except Exception:  # noqa: BLE001
        return False
    if shown == hit.text:
        return True
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), flags)
        if word:
            rx = re.compile(rf"\b(?:{rx.pattern})\b", flags)
        return rx.search(shown) is not None
    except re.error:
        return False


def grep(
    slug: str, sha: str, pattern: str, *, regex: bool = False,
    path_glob: str = "", max_hits: int = 60, timeout: float = 5.0,
    ignore_case: bool = False, per_file: int = 8, first_only: bool = False,
    word: bool = False,
) -> list[GrepHit]:
    """`git grep` at `sha`, secret files excluded by git itself.

    Output is read as a stream and the process is stopped at `max_hits`, so a
    pattern that matches a million lines costs a few thousand.
    """
    path = clone_path(slug)
    if path is None or not _SHA.match(sha or "") or not pattern:
        return []
    spec: list[str] = []
    if path_glob:
        if is_unsafe_rel_path(path_glob.replace("*", "x")):
            return []
        spec.append(f":(glob){path_glob}")
    else:
        spec.append(".")
    spec.extend(git_excludes())
    args = ["grep", "-n", "-I", "--no-color", "--no-recurse-submodules",
            "-E" if regex else "-F", f"--max-count={1 if first_only else per_file}"]
    if ignore_case:
        args.append("-i")
    if word:
        args.append("-w")
    args += ["-e", pattern, sha, "--", *spec]
    import os

    try:
        proc = subprocess.Popen(  # noqa: S603
            ["git", "-c", "core.quotepath=off", "-C", str(path), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, **_ENV},
        )
    except OSError:
        return []
    hits: list[GrepHit] = []
    prefix = f"{sha}:"
    import threading

    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None  # noqa: S101
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith(prefix):
                line = line[len(prefix):]
            m = _HIT_LINE.match(line)
            if m is None:
                continue
            hit = GrepHit(m["path"], int(m["line"]), m["text"])
            if not _match_survives_redaction(hit, pattern, regex, ignore_case, word):
                continue
            hits.append(hit)
            if len(hits) >= max_hits:
                break
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    return hits
