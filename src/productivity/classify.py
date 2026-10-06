"""Who is a bot, what kind of PR is this, which PR does a revert undo.

Pure functions, no I/O, so every rule has a test that names it.

THE BOT COMMENT PROBLEM. Celmis posts under a real person's account: the same
Atlassian user that reviews. Authorship cannot tell its comments from his own;
only the marker line can (`markers.is_bot_text` — whole lines, outside code,
not quoted). A human who quote-replies a review comment copies the marker into
their own text; `>`-quoted lines never count (the markers module's rule), so
that reply is still a human's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache

from src.productivity.settings import ProductivitySettings
from src.review import markers

KINDS = ("feature", "bugfix", "hotfix", "revert")

_TICKET = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9])")
_PR_REF = (
    re.compile(r"(?<![\w/])#(\d{1,9})\b"),
    re.compile(r"/pull-?requests?/(\d{1,9})\b"),
    re.compile(r"/pull/(\d{1,9})\b"),
    re.compile(r"/merge_requests/(\d{1,9})\b"),
    re.compile(r"(?<![\w])!(\d{1,9})\b"),
)
#: GitHub's own revert text, `Reverts owner/repo#123`: counts only when `owner/repo` is THIS repository.
_QUALIFIED_REF = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d{1,9})\b")
_REVERT_WORD = re.compile(r"\b(?:revert(?:s|ed|ing)?|rollback|відкат\w*)", re.IGNORECASE | re.UNICODE)
_QUOTED = re.compile(r"[\"“«'‘]([^\"”»'’]{3,200})[\"”»'’]")
_TEXT_CAP = 2000


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE | re.UNICODE)
    except re.error:
        return None


def _matches(patterns: Iterable[str], text: str) -> bool:
    text = (text or "")[:300]
    for pattern in patterns:
        rx = _compiled(pattern)
        if rx is not None and rx.search(text):
            return True
    return False


# ─── bots ─────────────────────────────────────────────────────────────

def _carries(raw: str, marker: str) -> bool:
    """`marker` opens a whole line of `raw`; a `>`-quoted line does not count."""
    marker = marker.strip().casefold()
    if not marker:
        return False
    for line in markers.reveal(raw or "").splitlines():
        line = line.strip().casefold()
        if line.startswith(marker):
            return True
    return False


def is_bot_comment(raw: str, bot_markers: Sequence[str] = ()) -> bool:
    """A comment Celmis (or another configured bot) wrote, judged by its text."""
    if markers.is_bot_text(raw or ""):
        return True
    return any(_carries(raw, m) for m in bot_markers)


def is_ignored_actor(key: str | None, name: str | None, ignored: Iterable[str]) -> bool:
    """`ignored_authors` matches an account key or a display name, case-insensitively."""
    wanted = {str(i).strip().casefold() for i in ignored if str(i).strip()}
    if not wanted:
        return False
    return any(v and str(v).strip().casefold() in wanted for v in (key, name))


# ─── what kind of PR ──────────────────────────────────────────────────

_REVERT_BRANCH = re.compile(r"^revert[/_-]", re.IGNORECASE)
_HOTFIX_TITLE = re.compile(r"^\s*[\[(]?hotfix\b", re.IGNORECASE)


def classify_kind(title: str, source_branch: str | None, settings: ProductivitySettings) -> str:
    """revert | hotfix | bugfix | feature, in that order of precedence."""
    branch = source_branch or ""
    if _matches(settings.revert_patterns, title) or _REVERT_BRANCH.match(branch):
        return "revert"
    if _matches(settings.hotfix_branch_patterns, branch) or _HOTFIX_TITLE.match(title or ""):
        return "hotfix"
    if _matches(settings.bugfix_patterns, title):
        return "bugfix"
    return "feature"


def ticket_key(*texts: str | None) -> str | None:
    """The first Jira-style key (`PROJ-123`) in the texts, in the order given."""
    for text in texts:
        found = _TICKET.search((text or "")[:_TEXT_CAP])
        if found:
            return found.group(0)
    return None


# ─── reverts ──────────────────────────────────────────────────────────

def revert_reference(
    title: str, description: str | None, own_number: int,
    settings: ProductivitySettings | None = None, repo: str | None = None,
) -> tuple[int | None, str | None]:
    """(PR number the revert names outright, text that names it indirectly).

    A number comes from `#123`, a pull-request URL or `!123` in the title or
    description, or from `owner/repo#123` when `owner/repo` is `repo` (GitHub's
    own `Reverts owner/repo#123`). A number that follows the revert word on its
    own line wins over an earlier one (`Fixes #7, reverts #9` names 9); with
    none like that, the first one is taken. Failing any number, the quoted title —
    `Revert "PROJ-456: Fix totals"` — or what follows the revert word is kept
    as a hint for `resolve_hint`, which needs the other PRs to look it up in.
    """
    blob = f"{title or ''}\n{(description or '')[:_TEXT_CAP]}"
    found: list[tuple[int, int]] = []  # (position, number)
    for rx in _PR_REF:
        found += [(m.start(), int(m.group(1))) for m in rx.finditer(blob)]
    if repo:
        found += [(m.start(), int(m.group(2))) for m in _QUALIFIED_REF.finditer(blob)
                  if m.group(1).casefold() == repo.casefold()]
    found = sorted(f for f in found if f[1] != own_number)
    if found:
        named = [f for f in found if _REVERT_WORD.search(blob[max(0, f[0] - 80):f[0]].rsplit("\n", 1)[-1])]
        return (named or found)[0][1], None
    quoted = _QUOTED.search(title or "")
    if quoted:
        return None, quoted.group(1).strip()[:200]
    rest = re.sub(r"^\s*(revert|rollback|відкат\w*)[\s:\-–]*", "", title or "",
                  flags=re.IGNORECASE | re.UNICODE).strip()
    return None, (rest[:200] or None)


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "")).strip().casefold()


def resolve_hint(
    hint: str | None, own_number: int, candidates: Iterable[Mapping[str, object]],
) -> int | None:
    """The PR a revert's hint points at: an older PR with that title, else that ticket.

    `candidates` are dicts with `number`, `title`, `ticket_key`. Only PRs
    numbered below the revert can be what it reverts; of several matches the
    latest one wins (the one most recently re-done).
    """
    if not hint:
        return None
    older = [c for c in candidates if int(c["number"]) < own_number]  # type: ignore[call-overload]
    wanted = _norm(hint)
    same_title = [int(c["number"]) for c in older if _norm(str(c.get("title") or "")) == wanted]  # type: ignore[call-overload]
    if same_title:
        return max(same_title)
    key = ticket_key(hint)
    if key:
        same_ticket = [int(c["number"]) for c in older if c.get("ticket_key") == key]  # type: ignore[call-overload]
        if same_ticket:
            return max(same_ticket)
    return None
