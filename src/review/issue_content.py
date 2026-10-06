"""Reading the target branch for the issues backlog.

`issue_resolver` asks one question — "does this code still exist on the
branch the PR merged into?" — and needs four things to answer it: the branch's
head sha, a file at that sha, the history of a path, and what one commit did to
a path. `ContentSource` answers them from the provider's API first and from the
repository's local clone second.

Everything that goes wrong becomes `ContentUnavailable`, and the resolver
reads that as "unreadable", never as "fixed": a token without repository read
scope, a rate limit, a file over the size cap, a branch the clone does not
have. An issue nobody could check stays open.

The clone fallback reads only what the clone already holds (the refs the index
refresh keeps current, `refs/remotes/origin/<branch>`); it does not fetch. The
clone's directory is read-only except `.git` and its credentials belong to the
index refresh, so a second fetch path here would be a second place to leak or
break them. Every git call is a list of arguments (no shell), the branch and
the path are validated before they reach it, and paths are passed after `--` so
a file called `-rf` is a file.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from datetime import datetime

from src.review.providers.base import (
    MAX_FILE_BYTES,
    FileChange,
    PathCommit,
    PullRequestProvider,
    PullRequestProviderError,
    committed_since,
)

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 20

_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
#: A branch name git itself would accept, without anything an option parser
#: or a revision expression could read as more than a name.
_SAFE_REF = re.compile(r"^[^\s\x00-\x1f~^:?*\[\\-][^\s\x00-\x1f~^:?*\[\\]*$")


class ContentUnavailable(Exception):
    """The branch could not be read. The issue is unreadable, not fixed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def content_hash(text: str) -> str:
    """A stable hash of a file's text, line endings folded: a checkout that
    turns LF into CRLF is not a change."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


# ─── Which pull request a commit came from ──────────────────────────

_PR_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "bitbucket": (
        re.compile(r"\(pull request #(\d+)\)", re.I),
        re.compile(r"pull request #(\d+)", re.I),
    ),
    "github": (
        re.compile(r"Merge pull request #(\d+)\b", re.I),
        re.compile(r"\(#(\d+)\)\s*$"),
    ),
    "gitlab": (
        re.compile(r"See merge request [^\s!]*!(\d+)", re.I),
        re.compile(r"\(!(\d+)\)\s*$"),
    ),
}


def pr_number_from_commit_subject(subject: str | None, provider: str = "") -> int | None:
    """The PR a merge or squash commit names in its subject, or None.

    The provider's own convention is tried first ("(pull request #N)" on
    Bitbucket, "(#N)" and "Merge pull request #N" on GitHub, "See merge
    request !N" on GitLab), then the others: a repository moved between hosts
    keeps the old subjects. A number is never guessed from other digits.
    """
    text = (subject or "").strip()
    if not text:
        return None
    order = [provider.lower(), *(p for p in _PR_PATTERNS if p != provider.lower())]
    for name in order:
        for pattern in _PR_PATTERNS.get(name, ()):
            m = pattern.search(text)
            if m:
                return int(m.group(1))
    return None


def pr_url_for(provider: str, repo: str, number: int, *, base_url: str | None = None) -> str | None:
    """The web address of a pull request, built from what the ledger holds."""
    name = provider.lower()
    if name == "github":
        return f"https://github.com/{repo}/pull/{number}"
    if name == "gitlab":
        base = (base_url or "https://gitlab.com").rstrip("/")
        return f"{base}/{repo}/-/merge_requests/{number}"
    if name == "bitbucket":
        return f"https://bitbucket.org/{repo}/pull-requests/{number}"
    return None


# ─── The local clone ────────────────────────────────────────────────


def _clone_dir(slug: str):
    from src.config import get_settings

    path = get_settings().repo_path(slug)
    return path if (path / ".git").exists() else None


def _git(path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "core.quotepath=false", *args],
        capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
    )


def _valid_ref(ref: str) -> bool:
    return bool(ref) and ".." not in ref and bool(_SAFE_REF.match(ref))


class _Clone:
    """The repository's local clone, read-only, by sha."""

    def __init__(self, slug: str) -> None:
        self.path = _clone_dir(slug)

    @property
    def present(self) -> bool:
        return self.path is not None

    def head(self, branch: str) -> str | None:
        if self.path is None or not _valid_ref(branch):
            return None
        for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
            proc = _git(self.path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            sha = proc.stdout.decode("utf-8", "replace").strip()
            if proc.returncode == 0 and _SHA.match(sha):
                return sha
        return None

    def has(self, sha: str) -> bool:
        if self.path is None or not _SHA.match(sha or ""):
            return False
        return _git(self.path, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0

    def read(self, sha: str, path: str) -> str | None:
        if self.path is None or not _SHA.match(sha) or not path:
            raise ContentUnavailable("the local clone cannot be read")
        proc = _git(self.path, "show", f"{sha}:{path}")
        if proc.returncode == 0:
            if len(proc.stdout) > MAX_FILE_BYTES:
                raise ContentUnavailable(f"{path} is too large to read")
            return proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").lower()
        if "does not exist" in err or "exists on disk, but not in" in err:
            return None
        raise ContentUnavailable("the local clone could not read the file")

    def history(self, sha: str, path: str, *, limit: int) -> list[PathCommit]:
        if self.path is None or not _SHA.match(sha):
            return []
        proc = _git(self.path, "log", "--format=%H%x1f%s%x1f%cI", "-n", str(limit),
                    sha, "--", path)
        if proc.returncode != 0:
            return []
        out = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3 and parts[0]:
                out.append(PathCommit(sha=parts[0], subject=parts[1][:300], date=parts[2]))
        return out

    def change(self, sha: str, path: str) -> FileChange | None:
        if self.path is None or not _SHA.match(sha):
            return None
        proc = _git(self.path, "show", "-M", "--name-status", "--format=", sha)
        if proc.returncode != 0:
            return None
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            cols = line.split("\t")
            code = cols[0][:1]
            if code == "R" and len(cols) >= 3 and cols[1] == path:
                return FileChange("renamed", cols[2], previous_path=cols[1])
            if code == "D" and len(cols) >= 2 and cols[1] == path:
                return FileChange("deleted", path)
            if code in "AM" and len(cols) >= 2 and cols[1] == path:
                return FileChange("added" if code == "A" else "modified", path)
        return None


# ─── The source the resolver reads ──────────────────────────────────


class ContentSource:
    """The target branch of one repository, from the API or the clone.

    `provider` is a `PullRequestProvider` (or anything with its four reading
    methods); `repo` the full name the review uses; `local_slug` the slug the
    clone is stored under (None: no fallback). One instance serves one pass:
    reads are remembered, so a file asked about by three issues is fetched
    once.
    """

    def __init__(self, provider: PullRequestProvider, repo: str, *,
                 local_slug: str | None = None, pr_provider: str = "") -> None:
        self.provider = provider
        self.repo = repo
        self.pr_provider = pr_provider or getattr(provider, "name", "")
        self._clone = _Clone(local_slug) if local_slug else None
        self._reads: dict[tuple[str, str], str | None] = {}
        self.requests = 0
        #: The head was read from the local clone (the API could not be
        #: asked): the clone does not fetch, so it may be behind the branch.
        self.head_from_clone = False

    # -- head -------------------------------------------------------

    def head_sha(self, branch: str) -> str:
        """The branch head, or `ContentUnavailable` naming why not."""
        self.requests += 1
        try:
            return self.provider.branch_head_sha(self.repo, branch)
        except Exception as exc:  # noqa: BLE001 — any failure is "could not ask"
            api_error = _sentence(exc)
        if self._clone is not None and self._clone.present:
            sha = self._clone.head(branch)
            if sha:
                logger.info("issue_content_from_clone repo=%s branch=%s", self.repo, branch)
                self.head_from_clone = True
                return sha
            raise ContentUnavailable(
                f"{api_error}; the local clone has no branch {branch!r}")
        raise ContentUnavailable(api_error)

    # -- files ------------------------------------------------------

    def read(self, ref: str, path: str) -> str | None:
        """The file's text at `ref` (a sha), None if it does not exist there."""
        key = (ref, path)
        if key in self._reads:
            return self._reads[key]
        self.requests += 1
        try:
            text = self.provider.read_file_at(self.repo, ref, path)
        except Exception as exc:  # noqa: BLE001
            api_error = _sentence(exc)
            if self._clone is not None and self._clone.has(ref):
                text = self._clone.read(ref, path)
            else:
                raise ContentUnavailable(api_error) from exc
        self._reads[key] = text
        return text

    def history(self, ref: str, path: str, *, since: datetime | None = None,
                limit: int = 10) -> list[PathCommit]:
        """Commits that touched `path`, newest first. [] when none could be
        listed — attribution is a nicety, never a reason to resolve."""
        self.requests += 1
        try:
            return self.provider.commits_touching(
                self.repo, ref, path, since=since, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.info("issue_history_unavailable repo=%s err=%s", self.repo, _sentence(exc))
            if self._clone is not None and self._clone.has(ref):
                return [c for c in self._clone.history(ref, path, limit=limit)
                        if committed_since(c.date, since)]
            return []

    def change(self, sha: str, path: str) -> FileChange | None:
        self.requests += 1
        try:
            return self.provider.file_change_in_commit(self.repo, sha, path)
        except Exception as exc:  # noqa: BLE001
            logger.info("issue_commit_files_unavailable repo=%s err=%s",
                        self.repo, _sentence(exc))
            if self._clone is not None and self._clone.has(sha):
                return self._clone.change(sha, path)
            return None

    def commit_url(self, sha: str) -> str | None:
        try:
            return self.provider.commit_url(self.repo, sha)
        except Exception:  # noqa: BLE001
            return None


def _sentence(exc: BaseException) -> str:
    """What went wrong, short, with nothing that could carry a secret."""
    if isinstance(exc, PullRequestProviderError):
        return str(exc)[:200]
    return type(exc).__name__


__all__ = [
    "ContentSource", "ContentUnavailable", "content_hash",
    "pr_number_from_commit_subject", "pr_url_for",
]
