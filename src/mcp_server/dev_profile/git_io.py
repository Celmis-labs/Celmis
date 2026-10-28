"""Reading code from the clone, at the revision the index was built from.

Every read goes through `git show <sha>:<path>` and `git grep <sha>`, never the
working tree: the clone can be ahead of the index (or have uncommitted edits
from some other tool), and a line number is only worth printing when it is a
line number of the revision named on the idx line. Committed files only, so an
untracked `.env` lying around in the clone cannot be read either.

An uploaded archive has no git. Its one revision is the archive's sha-256, and
its unpacked files are that revision for as long as they are still the archive
the sha names (`_unpacked_at`); a newer upload leaves the older revision with
nothing on disk, like a commit missing from a clone.

Searching that tree with `git grep --no-index` walks every path through the
secret-file pathspecs before the first line is read, which on a large archive
outlasts the call. So a search index is kept beside the archive (never inside
the tree): a git dir of intent-to-add entries, made once per archive sha, that
lets `git grep` run in work-tree mode. Without it (it could not be built) the
search falls back to `--no-index`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.mcp_server.dev_profile.paths import git_excludes, is_unsafe_rel_path

logger = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
#: A UTF-8 locale: under `C` git grep folds case and finds word edges byte by
#: byte, so `-i` and `-w` fail for Cyrillic (and any other non-ASCII text).
#: `C.UTF-8` is built into glibc 2.35+ (the Debian trixie image); where it is
#: missing git quietly falls back to `C`, the old behaviour.
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C.UTF-8"}
_INDEX_DIR = "grep-index.git"
_INDEX_REV = "grep-index.sha"
_INDEX_BUILD_SECONDS = 120.0
#: Bounds of the time one `git grep` may take, whatever the caller still has.
MIN_GREP_SECONDS = 3.0
MAX_GREP_SECONDS = 20.0
_MAX_FILE_BYTES = 2_000_000


def clone_path(slug: str) -> Path | None:
    from src.config import InvalidRepoSlug, get_settings

    try:
        path = get_settings().repo_path(slug)
    except InvalidRepoSlug:
        return None
    return path if (path / ".git").exists() else None


def _unpacked_at(slug: str, sha: str) -> Path | None:
    """The files of the uploaded archive ``slug`` when ``sha`` is its revision."""
    from src.config import InvalidRepoSlug, get_settings
    from src.repos.upload import upload_revision

    if not sha or upload_revision(slug) != sha:
        return None
    try:
        path = get_settings().repo_path(slug)
    except InvalidRepoSlug:
        return None
    return path if path.is_dir() else None


def _read_unpacked(tree: Path, rel_path: str) -> list[str] | None:
    """`show_file` in an unpacked archive: a regular file inside ``tree`` only."""
    try:
        target = (tree / rel_path).resolve(strict=True)
        if not target.is_relative_to(tree.resolve()) or not target.is_file():
            return None
        if target.stat().st_size > _MAX_FILE_BYTES:
            return None
        data = target.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace").split("\n")


def _run(path: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
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
    if _unpacked_at(slug, sha) is not None:
        return True
    path = clone_path(slug)
    if path is None or not _SHA.match(sha or ""):
        return False
    proc = _run(path, ["cat-file", "-e", f"{sha}^{{commit}}"], 10)
    return bool(proc and proc.returncode == 0)


def show_file(slug: str, sha: str, rel_path: str) -> list[str] | None:
    """The lines of `rel_path` at `sha`, or None (missing, binary, too big)."""
    tree = _unpacked_at(slug, sha)
    if tree is not None:
        return None if is_unsafe_rel_path(rel_path) else _read_unpacked(tree, rel_path)
    path = clone_path(slug)
    if path is None or not _SHA.match(sha or "") or is_unsafe_rel_path(rel_path):
        return None
    proc = _run(path, ["show", f"{sha}:{rel_path}"], 10)
    if proc is None or proc.returncode != 0 or len(proc.stdout) > _MAX_FILE_BYTES:
        return None
    if b"\0" in proc.stdout[:8000]:
        return None
    return proc.stdout.decode("utf-8", errors="replace").split("\n")


def _index_ready(gitdir: Path, sha: str) -> bool:
    try:
        return (gitdir / _INDEX_REV).read_text(encoding="ascii").strip() == sha
    except OSError:
        return False


def _build_index(tree: Path, gitdir: Path, sha: str) -> bool:
    """Make ``gitdir`` hold intent-to-add entries for ``tree`` (``git add -A``
    keeps the archive's own `.gitignore`s, nested ones included) and record
    ``sha``. Built next to its place and renamed in, so a reader never sees a
    half-made index."""
    gitdir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{_INDEX_DIR}.tmp-", dir=gitdir.parent))
    try:
        env = {**os.environ, **_ENV, "GIT_DIR": str(tmp), "GIT_WORK_TREE": str(tree)}
        for args, limit in ((["init", "-q"], 30.0),
                            (["add", "-A", "--intent-to-add"], _INDEX_BUILD_SECONDS)):
            proc = subprocess.run(  # noqa: S603
                ["git", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", *args],
                cwd=tree, capture_output=True, timeout=limit, check=False, env=env)
            if proc.returncode != 0:
                logger.warning("dev_grep_index_failed step=%s rc=%s", args[0], proc.returncode)
                return False
        (tmp / _INDEX_REV).write_text(sha, encoding="ascii")
        old = gitdir.with_name(f".{gitdir.name}.old-{os.getpid()}")
        if gitdir.exists():
            os.replace(gitdir, old)
        os.replace(tmp, gitdir)
        shutil.rmtree(old, ignore_errors=True)
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("dev_grep_index_failed err=%s", exc)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def upload_index(slug: str, tree: Path, sha: str) -> Path | None:
    """The search index of the uploaded archive ``sha``, built when it is
    missing or belongs to another archive. None when it cannot be built.

    Safe under concurrent calls (threads and processes): the builder holds a
    file lock, and whoever waits for it finds the index ready.
    """
    from src.config import InvalidRepoSlug, get_settings

    try:
        data = get_settings().repo_data_path(slug)
    except InvalidRepoSlug:
        return None
    gitdir = data / _INDEX_DIR
    if _index_ready(gitdir, sha):
        return gitdir
    try:
        import fcntl

        data.mkdir(parents=True, exist_ok=True)
        with open(data / ".grep-index.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _index_ready(gitdir, sha):
                return gitdir
            return gitdir if _build_index(tree, gitdir, sha) else None
    except (OSError, ImportError) as exc:
        logger.warning("dev_grep_index_failed err=%s", exc)
        return None


def build_upload_index(slug: str) -> bool:
    """Build the search index right after an archive is unpacked (best effort)."""
    from src.repos.upload import upload_revision

    sha = upload_revision(slug)
    tree = _unpacked_at(slug, sha or "")
    return bool(sha and tree and upload_index(slug, tree, sha))


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
    word: bool = False, timed_out: list | None = None,
    deadline: float | None = None,
) -> list[GrepHit]:
    """`git grep` at `sha`, secret files excluded by git itself. In an uploaded
    archive it searches the unpacked files, leaving out what the archive's own
    `.gitignore` names, as a clone would not have it.

    ``deadline`` (a `time.monotonic()` value) is the end of the caller's call:
    git gets what is left of it, within MIN/MAX_GREP_SECONDS; without it,
    ``timeout``.

    Output is read as a stream and the process is stopped at `max_hits`, so a
    pattern that matches a million lines costs a few thousand.
    """
    tree = _unpacked_at(slug, sha)
    path = tree or clone_path(slug)
    if path is None or not pattern or (tree is None and not _SHA.match(sha or "")):
        return []
    spec: list[str] = []
    if path_glob:
        if is_unsafe_rel_path(path_glob.replace("*", "x")):
            return []
        spec.append(f":(glob){path_glob}")
    else:
        spec.append(".")
    spec.extend(git_excludes())
    gitdir = upload_index(slug, tree, sha) if tree is not None else None
    if deadline is not None:
        import time

        timeout = min(MAX_GREP_SECONDS, max(MIN_GREP_SECONDS, deadline - time.monotonic()))
    # An index makes the archive an ordinary work tree for git; the archive's
    # symlinks (extraction refuses them) would be skipped there, not followed.
    base = ["--git-dir", str(gitdir), "--work-tree", str(path)] if gitdir else []
    args = ["grep", *(["--no-index", "--exclude-standard"] if tree and not gitdir else []),
            "-n", "-I", "--no-color", "--no-recurse-submodules",
            "-E" if regex else "-F", f"--max-count={1 if first_only else per_file}"]
    if ignore_case:
        args.append("-i")
    if word:
        args.append("-w")
    args += ["-e", pattern, *([] if tree else [sha]), "--", *spec]
    try:
        proc = subprocess.Popen(  # noqa: S603
            ["git", "-c", "core.quotepath=off", *base, "-C", str(path), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, **_ENV},
        )
    except OSError:
        return []
    hits: list[GrepHit] = []
    prefix = "" if tree else f"{sha}:"
    import threading

    def _expire() -> None:
        if timed_out is not None:
            timed_out.append(slug)  # git was killed: the hits may be partial
        proc.kill()

    timer = threading.Timer(timeout, _expire)
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
