"""Citation verification for Q&A answers.

The answer prompt asks the model to cite sources as markdown links carrying a
repo-prefixed path and a line anchor, e.g.::

    [Pipeline.run](gitlab_acme-etl/src/pipeline.py#L54)

A hallucinated citation is the fastest way to destroy trust in a code-answering
tool: the prose looks authoritative and the link looks precise, but the line
doesn't exist. So after generation we re-check every citation against the real
files on disk and report what didn't hold up.

We deliberately *report* rather than rewrite the answer: generation is streamed,
so the text has already reached the user by the time we can check it. The UI
surfaces the verdict next to the message instead.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# [label](target#L12) or [label](target#L12-L20) — the anchor is optional.
_CITATION_RE = re.compile(
    r"\[(?P<label>[^\]]{1,200})\]\("
    r"(?P<target>[^)\s#]+)"
    r"(?:#L(?P<line>\d+)(?:-L?(?P<end>\d+))?)?"
    r"\)"
)

# Anything that clearly isn't a repo file reference.
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "/api/")

MAX_CITATIONS = 200  # safety bound on pathological answers

# A fenced block starting within this many characters after a citation is
# treated as that citation's quoted code. Far enough to clear a short sentence
# of lead-in prose, close enough that an unrelated later block is not blamed
# on it.
_SNIPPET_WINDOW = 400

#: The OPENING of a fence. The whole fence used to have to fit inside the
#: window, and that made the check skip exactly what it was written for: a
#: wholly invented function is long, so its closing ``` falls past 400
#: characters and the block was never examined. One of fifteen citations in a
#: single measured session escaped that way. The window now bounds how far
#: from the citation a block may START; the body runs to its real close.
_FENCE_OPEN_RE = re.compile(r"```(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n")

#: How much of a block to read once one is found. Generous — a fabricated
#: function is the case this exists for — but bounded, because a pathological
#: answer must not turn one citation into a megabyte of comparison.
_SNIPPET_MAX_BODY = 20_000

# Fences whose whole purpose is to NOT match the file: a proposed replacement
# and a diff are supposed to differ from what is there.
_NON_QUOTING_FENCES = {"suggestion", "diff", "patch"}

# The model renders quoted source with the file's own line numbers in front —
# "18  share = total_cents // len(parties)". Strip them before comparing, or
# every real quote looks fabricated.
_LEADING_LINENO_RE = re.compile(r"^\s*\d{1,5}[:|]?\s{1,4}")


def _substantive(line: str) -> str:
    """A code line reduced to something worth comparing, or "".

    Drops the noise that matches everything — closing braces, bare keywords,
    ellipses marking an elision — and collapses whitespace so indentation
    differences do not count as a mismatch.
    """
    t = _LEADING_LINENO_RE.sub("", line).strip()
    if t in {"...", "…", "# ...", "// ..."}:
        return ""
    if len(t) < 8:
        return ""
    if t.lstrip("#/*- ").strip() == "":
        return ""
    return " ".join(t.split())


def _snippet_after(text: str, end: int) -> tuple[str, str] | None:
    """The fenced block a citation is quoting, as (language, body).

    The block must START within `_SNIPPET_WINDOW` of the citation — close
    enough that an unrelated later block is not blamed on it — but it may run
    as long as it likes from there. Requiring the whole fence to fit was how
    the longest and most dangerous blocks went unchecked.

    An unterminated fence reads to the end of the answer rather than being
    skipped: a model that opened a code block and never closed it has still
    shown the reader code, and that code still has to be real.
    """
    window = text[end:end + _SNIPPET_WINDOW]
    m = _FENCE_OPEN_RE.search(window)
    if not m:
        return None
    body_start = end + m.end()
    close = text.find("```", body_start)
    body_end = close if close != -1 else len(text)
    return m.group("lang").lower(), text[body_start:body_end][:_SNIPPET_MAX_BODY]


@dataclass
class Citation:
    label: str
    target: str                 # raw link target as written by the model
    repo: str | None = None
    path: str | None = None
    line: int | None = None
    #: ok | missing_file | bad_line | unknown_repo | not_in_context |
    #: fabricated_snippet
    status: str = "ok"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "label": self.label, "target": self.target, "repo": self.repo,
            "path": self.path, "line": self.line, "status": self.status,
            "detail": self.detail,
        }


@dataclass
class CitationReport:
    citations: list[Citation] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.citations)

    @property
    def invalid(self) -> list[Citation]:
        return [c for c in self.citations if not c.ok]

    def as_meta(self) -> dict:
        """Compact payload for the message meta / SSE `done` event."""
        return {
            "citations_total": self.total,
            "citations_invalid": len(self.invalid),
            "citations_bad": [c.as_dict() for c in self.invalid[:10]],
        }


def _split_repo_path(target: str, known_repos: set[str]) -> tuple[str | None, str]:
    """Split ``repo_slug/rel/path.py`` into (repo, path).

    Falls back to (None, target) when the first segment isn't a known repo —
    the model sometimes cites a bare repo-relative path.
    """
    target = target.lstrip("./")
    if "/" not in target:
        return None, target
    head, rest = target.split("/", 1)
    if head in known_repos:
        return head, rest
    return None, target


def verify_citations(
    text: str,
    *,
    files_read: list[str] | None = None,
    repos: list[str] | None = None,
    settings: Settings | None = None,
) -> CitationReport:
    """Check every citation in ``text`` against the files on disk.

    ``files_read`` (repo-prefixed paths the model was actually shown) lets us
    flag citations to files that were never in context — a weaker signal than a
    missing file, but a useful one.
    """
    settings = settings or get_settings()
    report = CitationReport()
    if not text:
        return report

    known_repos = set(repos or [])
    if not known_repos and files_read:
        known_repos = {f.split("/", 1)[0] for f in files_read if "/" in f}
    context_paths = set(files_read or [])

    for m in list(_CITATION_RE.finditer(text))[:MAX_CITATIONS]:
        target = m.group("target")
        if not target or target.startswith(_SKIP_PREFIXES):
            continue
        # Ignore pure anchors / non-file-looking targets.
        if "." not in target.rsplit("/", 1)[-1]:
            continue

        line = int(m.group("line")) if m.group("line") else None
        repo, rel = _split_repo_path(target, known_repos)
        cit = Citation(label=m.group("label"), target=target, repo=repo,
                       path=rel, line=line)

        if repo is None:
            # Try each known repo — the model may have dropped the prefix.
            candidates = [r for r in known_repos]
            resolved = None
            for r in candidates:
                if (settings.repo_path(r) / rel).is_file():
                    resolved = r
                    break
            if resolved is None:
                cit.status = "unknown_repo"
                cit.detail = "no indexed repo contains this path"
                report.citations.append(cit)
                continue
            cit.repo = repo = resolved

        fpath = settings.repo_path(repo) / rel
        if not fpath.is_file():
            cit.status = "missing_file"
            cit.detail = "file does not exist in the repo"
            report.citations.append(cit)
            continue

        if line is not None:
            try:
                # Cheap line count — answers cite source files, not blobs.
                with fpath.open("r", encoding="utf-8", errors="replace") as fh:
                    total = sum(1 for _ in fh)
            except OSError as exc:
                cit.status = "missing_file"
                cit.detail = f"unreadable: {exc}"
                report.citations.append(cit)
                continue
            if line < 1 or line > total:
                cit.status = "bad_line"
                cit.detail = f"line {line} outside file (1..{total})"
                report.citations.append(cit)
                continue

        # ── Does the quoted code actually appear in the cited file? ──
        #
        # The check this module was missing, and the one it most needed. Its
        # own docstring frames the danger as "the prose looks authoritative
        # and the link looks precise, but the line doesn't exist" — that is
        # only half. In production the model wrote a `process_settlement`
        # function that exists in NO repository, rendered it with line numbers
        # 1-3, and hung it under a real path. Every existing check passed: the
        # file exists, line 1 is in range. citations_invalid was 0.
        #
        # Deliberately conservative: flagged only when the block has real code
        # in it and NOT ONE of its substantive lines occurs anywhere in the
        # file. A partial quote, a re-indented quote, an elided quote and a
        # quote from a different part of the file all survive. Only wholesale
        # invention fails.
        snippet = _snippet_after(text, m.end())
        if snippet is not None and cit.status == "ok":
            lang, body = snippet
            if lang not in _NON_QUOTING_FENCES:
                try:
                    haystack = {
                        _substantive(ln)
                        for ln in fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                    }
                except OSError:
                    haystack = set()
                quoted = [_substantive(ln) for ln in body.splitlines()]
                quoted = [q for q in quoted if q]
                if quoted and haystack and not any(q in haystack for q in quoted):
                    cit.status = "fabricated_snippet"
                    cit.detail = (
                        "the quoted code does not occur in this file — "
                        f"{len(quoted)} substantive line(s) checked, none matched"
                    )

        prefixed = f"{repo}/{rel}"
        if context_paths and prefixed not in context_paths and cit.status == "ok":
            cit.status = "not_in_context"
            cit.detail = "file was not part of the retrieved context"

        report.citations.append(cit)

    if report.invalid:
        logger.info(
            "citations_verified total=%d invalid=%d statuses=%s",
            report.total, len(report.invalid),
            sorted({c.status for c in report.invalid}),
        )
    return report


__all__ = ["Citation", "CitationReport", "verify_citations"]
