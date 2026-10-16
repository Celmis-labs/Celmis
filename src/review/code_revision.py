"""Which revision of the repository stands for "the code as of the pull request".

Why this exists:
    Three review features read repository code from the local clone of the code
    graph: `code_context`, `verifier_evidence` (what the veto greps) and
    `structural_constants` (the undefined class constant check). They all want
    the pull request's own code, i.e. the head commit. For an OPEN pull request
    the head is often not in the clone, even after `head_fetch` tried (a fork,
    a deleted branch, no credential, no network). Each consumer used to fall
    back to the clone's HEAD, which is the default branch - NEWER than the pull
    request. That is wrong in a quiet way: if the pull
    request used a mistyped class constant and the default branch (merged
    later) defined a constant of that name, the veto would read "file.php:21
    defines const X, so the constant exists" and drop a true finding, while the
    structural check would find the name defined and stay silent.

Resolution order, one rule for every consumer (`resolve_code_revision`):
    1. "head"           - the head sha is in the clone. Exact.
    2. "base_plus_diff" - the head sha is missing but the pull request's base
                          sha (`PullRequest.base_sha`: GitHub `base.sha`, GitLab
                          `diff_refs.base_sha`, Bitbucket `destination.commit`)
                          is. A normal pull request is "base + its diff": a file
                          the diff does not touch is identical at base and at
                          head, and a file it touches is rebuilt by applying the
                          diff's hunks to the base text (`overlay_file`). The
                          consumer reads the base revision and overlays the diff
                          for touched paths.
    3. "clone_head"     - the clone's HEAD, as the last resort. It is explicitly
                          labelled "newer than the pull request": whatever it
                          shows may have appeared after the pull request was
                          written, so it is NOT the pull request's code
                          (`is_pr_code` is False) and the veto must not drop a
                          finding on the strength of it.
    4. "none"           - no clone or nothing readable.

Honest limits of "base + diff":
    Bitbucket's destination commit is the target branch's tip when the pull
    request was last read, GitHub's base sha likewise, while the diff is taken
    against the merge base. If the target moved since the branch point, a
    touched file at base can differ from the diff's own context. Then the hunks
    do not apply (context or removed line mismatch), `overlay_file` returns
    None, and the consumer treats that file as unreadable instead of guessing.
    Files the diff does not touch are read from base as they are.

Everything here is local git and string work; nothing raises into the review.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SHA = re.compile(r"[0-9A-Fa-f]{7,64}")
_GIT_TIMEOUT = 20

KIND_HEAD = "head"
KIND_BASE_PLUS_DIFF = "base_plus_diff"
KIND_CLONE_HEAD = "clone_head"
KIND_NONE = "none"

#: More touched files than this and rebuilding them one by one for a search is
#: not worth it; the consumer treats the base revision as incomplete.
MAX_OVERLAY_FILES = 200


@dataclass(frozen=True)
class CodeRevision:
    """Where "the code as of the pull request" is read from."""

    kind: str
    #: The git revision to read (`head_sha`, `base_sha` or "HEAD"); "" for none.
    rev: str = ""

    @property
    def is_pr_code(self) -> bool:
        """True when what is read is the pull request's own code (exactly, or
        base plus its diff). False for the newer clone HEAD."""
        return self.kind in (KIND_HEAD, KIND_BASE_PLUS_DIFF)

    @property
    def needs_overlay(self) -> bool:
        return self.kind == KIND_BASE_PLUS_DIFF

    @property
    def label(self) -> str:
        """Short provenance text for prompts, findings and the stage record."""
        if self.kind == KIND_HEAD:
            return f"clone @ {self.rev[:10]}"
        if self.kind == KIND_BASE_PLUS_DIFF:
            return f"clone @ {self.rev[:10]} (PR base) + this PR's diff"
        if self.kind == KIND_CLONE_HEAD:
            return ("clone HEAD - NEWER than the pull request, not its code "
                    "(the PR head and base commits are not in the clone)")
        return "no local code"


def has_commit(root: Path, sha: str) -> bool:
    if not sha or not _SHA.fullmatch(sha):
        return False
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, timeout=_GIT_TIMEOUT, check=False,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def resolve_code_revision(pr: Any, root: Path | None) -> CodeRevision:
    """The revision to read, per the module doc. Never raises."""
    if root is None:
        return CodeRevision(KIND_NONE)
    try:
        head = str(getattr(pr, "head_sha", "") or "")
        if has_commit(root, head):
            return CodeRevision(KIND_HEAD, head)
        base = str(getattr(pr, "base_sha", "") or "")
        if has_commit(root, base):
            return CodeRevision(KIND_BASE_PLUS_DIFF, base)
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_revision_failed err=%s", exc)
    return CodeRevision(KIND_CLONE_HEAD, "HEAD")


# ─── Overlaying the diff on the base ─────────────────────────────────


def diff_hunks(pr: Any) -> list[Any]:
    """The hunks that describe the whole pull request (not an increment)."""
    hunks = getattr(pr, "anchor_hunks", None)
    if hunks is None:
        hunks = getattr(pr, "hunks", None)
    return [h for h in (hunks or []) if not getattr(h, "is_binary", False)]


def hunks_by_new_path(pr: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for h in diff_hunks(pr):
        out.setdefault(getattr(h, "file_path", "") or "", []).append(h)
    return out


def touched_base_paths(pr: Any) -> set[str]:
    """Paths whose text at base differs from head: new paths and renamed-from."""
    paths: set[str] = set()
    for h in diff_hunks(pr):
        for p in (getattr(h, "file_path", ""), getattr(h, "old_file_path", "")):
            if p:
                paths.add(p)
    return paths


def overlay_file(base_text: str | None, hunks: list[Any]) -> str | None:
    """The head text of a file: `base_text` with `hunks` applied.

    `base_text` is None for a file that is new in the pull request. Returns
    None when a hunk does not fit the base (a context or removed line differs,
    or the hunk points past the end): the caller must treat the file as not
    readable rather than guess. A file the pull request deletes yields None
    through the caller (it checks `is_deleted_file`), not here.
    """
    lines = (base_text or "").split("\n") if base_text else []
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline is not a line
    out: list[str] = []
    pos = 0  # next base line index not yet copied
    for h in sorted(hunks, key=lambda x: int(getattr(x, "old_start", 0) or 0)):
        old_start = int(getattr(h, "old_start", 0) or 0)
        # `-5,0` means "insert after line 5"; otherwise the hunk starts AT old_start.
        target = old_start if int(getattr(h, "old_count", 0) or 0) == 0 else old_start - 1
        if target < pos or target > len(lines):
            return None
        out.extend(lines[pos:target])
        pos = target
        body = (getattr(h, "content", "") or "").split("\n")
        if body and body[-1] == "":
            body.pop()  # the newline that ends the hunk is not a context line
        for raw in body:
            if raw.startswith("@@") or raw.startswith("\\"):
                continue
            if raw.startswith("+"):
                out.append(raw[1:])
                continue
            tag, text = (raw[:1], raw[1:]) if raw[:1] in (" ", "-") else (" ", raw)
            if pos >= len(lines) or lines[pos].rstrip() != text.rstrip():
                return None
            if tag == " ":
                out.append(lines[pos])
            pos += 1
    out.extend(lines[pos:])
    return "\n".join(out) + "\n"
