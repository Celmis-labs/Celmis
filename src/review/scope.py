"""What asked for this review, and which gates that request may skip.

One contract for every way a review starts — the webhook, the poller, a
`@celmis review` comment, the Review button, bulk review, the CLI, the MCP
tool:

    ReviewRequest(trigger="command", force=True)

`orchestrator.review(..., request=...)`, `dispatch.execute_review` (payload
keys `trigger`, `force`, `scope`, `resume`) and `dispatch.enqueue_review_run`
carry it, so a comment command and the UI cannot grow two bypass vocabularies.

Rules (`ReviewRequest.bypasses`):

* An automatic trigger (webhook, poller) meets every gate.
* An explicit one (a human asked: command, manual, bulk, cli, mcp) skips the
  draft, title and cadence gates — somebody wants this PR reviewed now.
* `force` also skips the target-branch gate and the "no new commits" check.
* Never skipped: `gate_enabled` (the repository switched the reviewer off),
  `gate_size` and `gate_hunks` (nothing to read, or too much to read).

The pure title matcher lives here too, next to the gate that uses it, so the
webhook, the poller and the orchestrator answer "is this title ignored?" with
one function.

The other half of this module is the review SCOPE — what part of the PR a run
reads. `decide_scope` answers "the whole PR, or only the commits since the last
reviewed one, or nothing at all" from facts the orchestrator hands it, and
every uncertain case is answered "the whole PR". `restrict_to_pr`,
`removed_old_lines` and `plan_threads` are the pure helpers around an
incremental run: keep only what the PR itself changed, find the earlier
comments the new commits made outdated, and spot a finding that is already on
the PR. No I/O here; the providers fetch the commits and the increment.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

Trigger = Literal["webhook", "poller", "command", "manual", "bulk", "cli", "mcp"]
Scope = Literal["full", "incremental"]

TRIGGERS: Final[tuple[str, ...]] = (
    "webhook", "poller", "command", "manual", "bulk", "cli", "mcp")
#: The triggers where a person asked for this review.
EXPLICIT_TRIGGERS: Final[frozenset[str]] = frozenset(
    {"command", "manual", "bulk", "cli", "mcp"})

#: Gates an explicit trigger skips.
_SOFT_GATES: Final[frozenset[str]] = frozenset(
    {"gate_draft", "gate_title", "gate_cadence"})
#: Gates only `force` skips (on top of the soft ones).
_FORCE_GATES: Final[frozenset[str]] = frozenset({"gate_target_branch"})


@dataclass(frozen=True)
class ReviewRequest:
    trigger: Trigger = "webhook"
    #: Review even when the gates and the same-commit check say no.
    force: bool = False
    #: Pin the scope of the review; None lets the `review_scope` setting decide.
    scope: Scope | None = None
    #: A run that resumes a paused PR (`@celmis start-review`): it covers
    #: every push that was skipped while the PR waited.
    resume: bool = False

    @property
    def explicit(self) -> bool:
        return self.trigger in EXPLICIT_TRIGGERS

    def bypasses(self, gate_key: str) -> bool:
        if gate_key in _SOFT_GATES:
            return self.explicit or self.force
        if gate_key in _FORCE_GATES:
            return self.force
        return False

    def as_payload(self) -> dict:
        """The queue payload keys. Defaults are left out, so a plain webhook
        job's payload is what it was before this contract existed."""
        out: dict = {"trigger": self.trigger}
        if self.force:
            out["force"] = True
        if self.scope:
            out["scope"] = self.scope
        if self.resume:
            out["resume"] = True
        return out

    @classmethod
    def from_payload(cls, payload: dict) -> ReviewRequest:
        """The request a queued job carries. An unknown or missing trigger is
        read from the job's `source` (what older jobs and the UI wrote);
        failing that it is "manual", never an automatic one: a job nobody can
        place was not started by a push."""
        trigger = str(payload.get("trigger") or payload.get("source") or "")
        if trigger not in TRIGGERS:
            trigger = "manual"
        scope = payload.get("scope")
        return cls(
            trigger=trigger,  # type: ignore[arg-type]
            force=bool(payload.get("force", False)),
            scope=scope if scope in ("full", "incremental") else None,
            resume=bool(payload.get("resume", False)),
        )


#: What a caller that passes no request is: nobody asked, so every gate
#: applies — the behaviour `orchestrator.review` had before the contract.
AUTOMATIC = ReviewRequest(trigger="webhook")


def title_keyword_match(title: str | None, keywords: list[str] | None) -> str | None:
    """The first keyword the title contains, or None.

    Case-insensitive substring match on `casefold()`, so Cyrillic and German
    "ß" behave; blank keywords never match (an empty string is a substring of
    everything, and would silence every PR).
    """
    text = (title or "").casefold()
    if not text:
        return None
    for keyword in keywords or []:
        word = str(keyword).strip()
        if word and word.casefold() in text:
            return word
    return None


# ─── Review scope: the whole PR, or only what is new ─────────────────

#: A finding whose fingerprint is already an open comment of ours within this
#: many lines is not posted again.
DEDUPE_LINE_WINDOW: Final = 3

#: The most commits a PR listing may hold before the provider calls it
#: truncated (GitHub stops at 250); a truncated list proves nothing about
#: ancestry, so the scope falls back to the whole PR.
MAX_LISTED_COMMITS: Final = 250


@dataclass(frozen=True)
class CommitInfo:
    """One commit of a PR, as the providers list them."""

    sha: str
    parents: tuple[str, ...] = ()
    message: str = ""
    committed_at: str | None = None


@dataclass(frozen=True)
class ScopeDecision:
    """What a run reads. `mode` is `full`, `incremental` or `skip`; `code` is
    a stable word (tests and the run row), `reason` the stage's sentence."""

    mode: Literal["full", "incremental", "skip"]
    code: str
    reason: str
    base_sha: str | None = None
    new_commits: int = 0


def same_commit(a: str | None, b: str | None) -> bool:
    """Two ids of one commit: Bitbucket's webhook carries 12 characters where
    its API returns 40, so a prefix of at least 7 is one commit."""
    x, y = (a or "").lower(), (b or "").lower()
    short = min(len(x), len(y))
    return short >= 7 and x[:short] == y[:short]


def _short(sha: str | None) -> str:
    return (sha or "")[:7]


def decide_scope(
    setting: str | None,
    request: ReviewRequest,
    *,
    last_reviewed_sha: str | None,
    head_sha: str | None,
    list_commits: Callable[[], Sequence[CommitInfo] | None],
) -> ScopeDecision:
    """The scope of one run.

    Rules, in order (every doubt ends in `full`):

    1. A forced or full-pinned request, or the `review_scope` setting `full`.
    2. No reviewed commit on record: the first review.
    3. The head is the reviewed commit: nothing is new. A person's request
       (a command, the button, the CLI; a resume included) is answered with a
       whole review (they asked; a quiet skip would look broken); everything
       else skips.
    4. The provider cannot list the commits (or the list was cut short).
    5. The reviewed commit is not among the PR's commits (force-push, rebase),
       or the head is not newer than it.
    6. Every new commit is a merge commit: skip, nothing of the PR's own.
    7. Otherwise incremental from the reviewed commit.

    `list_commits` is called only when the answer needs it, so a full review
    costs no extra request.
    """
    if request.force:
        return ScopeDecision("full", "forced", "Full review: the review was forced.")
    if request.scope == "full":
        return ScopeDecision("full", "requested_full", "Full review: the whole PR was asked for.")
    if (setting or "incremental") != "incremental" and request.scope != "incremental":
        return ScopeDecision("full", "setting_full",
                             "Full review: the review scope of this repository is full.")
    if not last_reviewed_sha:
        return ScopeDecision("full", "first_review",
                             "Full review: this pull request has no reviewed commit yet.")
    if same_commit(last_reviewed_sha, head_sha):
        if request.explicit:
            return ScopeDecision(
                "full", "asked_again",
                f"Full review: nothing is new since `{_short(last_reviewed_sha)}`, "
                f"but the review was asked for.")
        return ScopeDecision(
            "skip", "no_new_commits",
            f"Skipped: no new commits since `{_short(last_reviewed_sha)}`.")
    commits = None
    try:
        commits = list_commits()
    except Exception:  # noqa: BLE001 — an incremental problem never fails a review
        commits = None
    if commits is None:
        return ScopeDecision(
            "full", "commits_unavailable",
            "Full review: the provider could not list the commits of this pull request.")
    by_sha = {c.sha: c for c in commits if c.sha}
    last = next((c.sha for c in commits if same_commit(c.sha, last_reviewed_sha)), None)
    head = next((c.sha for c in commits if same_commit(c.sha, head_sha)), None)
    if last is None or head is None:
        return ScopeDecision(
            "full", "history_rewritten",
            f"Full review: `{_short(last_reviewed_sha)}` is no longer part of this pull "
            f"request (history was rewritten).")
    # Everything reachable from the reviewed commit inside the PR's own commits.
    seen: set[str] = set()
    stack = [last]
    while stack:
        sha = stack.pop()
        if sha in seen or sha not in by_sha:
            continue
        seen.add(sha)
        stack.extend(by_sha[sha].parents)
    new = [c for c in commits if c.sha not in seen]
    if not new or head in seen:
        return ScopeDecision(
            "full", "history_rewritten",
            f"Full review: the head is not newer than `{_short(last_reviewed_sha)}`.")
    if all(len(c.parents) > 1 for c in new):
        return ScopeDecision(
            "skip", "only_merge_commits",
            f"Skipped: the {len(new)} new commit{'s are' if len(new) != 1 else ' is a'} "
            f"merge commit{'s' if len(new) != 1 else ''} only, nothing of this "
            f"pull request's own.", base_sha=last, new_commits=len(new))
    return ScopeDecision(
        "incremental", "incremental",
        f"Incremental: {len(new)} new commit{'s' if len(new) != 1 else ''} since "
        f"`{_short(last)}`.", base_sha=last, new_commits=len(new))


def _hunk_new_lines(hunk: Any) -> tuple[set[int], set[int]]:
    """(added new-side line numbers, context+added new-side numbers) of a hunk."""
    added: set[int] = set()
    seen: set[int] = set()
    n = int(hunk.new_start)
    rows = (hunk.content or "").split("\n")
    for raw in rows[1:] if rows and rows[0].startswith("@@") else rows:
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            added.add(n)
        seen.add(n)
        n += 1
    return added, seen


def restrict_to_pr(inc_hunks: Iterable[Any], pr_hunks: Iterable[Any]) -> list[Any]:
    """The incremental hunks that belong to the PR itself.

    A merge commit that pulls the target branch into the PR branch puts the
    target's changes into the increment; they are not what the author wrote
    and must not be reviewed. A hunk stays when at least one of its added
    lines is an added line of the whole PR's diff (same path, same new-side
    number: both diffs end at the same head). A hunk with no added lines (a
    pure removal) stays when the PR's diff touches the same file around it.
    """
    pr_added: dict[str, set[int]] = {}
    pr_span: dict[str, list[tuple[int, int]]] = {}
    for h in pr_hunks:
        added, _ = _hunk_new_lines(h)
        pr_added.setdefault(h.file_path, set()).update(added)
        pr_span.setdefault(h.file_path, []).append(
            (int(h.new_start), int(h.new_start) + max(int(h.new_count), 1) - 1))
    kept: list[Any] = []
    for h in inc_hunks:
        added, _ = _hunk_new_lines(h)
        if added:
            if added & pr_added.get(h.file_path, set()):
                kept.append(h)
            continue
        at = int(h.new_start)
        if any(lo - 1 <= at <= hi + 1 for lo, hi in pr_span.get(h.file_path, [])):
            kept.append(h)
    return kept


def same_added_lines(inc_hunks: Iterable[Any], pr_hunks: Iterable[Any]) -> bool:
    """Do the two hunk lists add exactly the same lines (per file, new side)?"""
    def added(hunks: Iterable[Any]) -> dict[str, set[int]]:
        out: dict[str, set[int]] = {}
        for h in hunks:
            lines, _ = _hunk_new_lines(h)
            if lines:
                out.setdefault(h.file_path, set()).update(lines)
        return out

    mine = added(inc_hunks)
    return bool(mine) and mine == added(pr_hunks)


def drop_diff_sections(raw_diff: str, paths: Iterable[str]) -> str:
    """`raw_diff` without the file sections of `paths` (either side of the
    `diff --git a/X b/Y` header). Used to take a merged-in target branch's
    files out of the text an increment's agents read. Text without git
    headers is returned unchanged."""
    drop = {p for p in paths if p}
    if not drop or not raw_diff:
        return raw_diff
    from src.review.ignore_globs import _DIFF_HEADER

    lines = raw_diff.splitlines(keepends=True)
    if not any(line.startswith("diff --git ") for line in lines):
        return raw_diff
    out: list[str] = []
    keep = True
    for line in lines:
        if line.startswith("diff --git "):
            m = _DIFF_HEADER.match(line.rstrip("\n"))
            keep = not (m and (m.group(1) in drop or m.group(2) in drop))
        if keep:
            out.append(line)
    return "".join(out)


def removed_old_lines(hunks: Iterable[Any]) -> tuple[dict[str, set[int]], set[str]]:
    """What an increment took away, from the OLD side of its hunks.

    Returns ({path: old line numbers removed or replaced}, {paths deleted}).
    A comment on one of those lines (or in a deleted file) sits on code that is
    gone, so the thread is outdated.
    """
    lines: dict[str, set[int]] = {}
    deleted: set[str] = set()
    for h in hunks:
        old = int(h.old_start)
        rows = (h.content or "").split("\n")
        for raw in rows[1:] if rows and rows[0].startswith("@@") else rows:
            if raw.startswith("\\"):
                continue
            if raw.startswith("-"):
                for path in {h.file_path, h.old_file_path}:
                    if path:
                        lines.setdefault(path, set()).add(old)
                old += 1
            elif raw.startswith("+"):
                continue
            else:
                old += 1
        if getattr(h, "is_deleted_file", False):
            deleted.update(p for p in (h.file_path, h.old_file_path) if p)
    return lines, deleted


def map_old_line(hunks: Iterable[Any], path: str, line: int) -> int | None:
    """Where line `line` of the OLD side of `path` is after `hunks` (the
    increment's), or None when the increment removed or replaced it.

    Lines before the first hunk stay where they are; lines after a hunk move by
    what it added minus what it took away.
    """
    mine = sorted(
        (h for h in hunks if path in (h.file_path, getattr(h, "old_file_path", ""))),
        key=lambda h: int(h.old_start))
    delta = 0
    for h in mine:
        old_count = int(getattr(h, "old_count", 1))
        # "@@ -5,0 +6,2 @@" adds after line 5: its first old row is line 6.
        old = int(h.old_start) + (1 if old_count == 0 else 0)
        new = int(h.new_start)
        if line < old:
            break
        rows = (h.content or "").split("\n")
        for raw in rows[1:] if rows and rows[0].startswith("@@") else rows:
            if raw.startswith("\\"):
                continue
            if raw.startswith("-"):
                if old == line:
                    return None
                old += 1
            elif raw.startswith("+"):
                new += 1
            else:
                if old == line:
                    return new
                old += 1
                new += 1
        delta = new - old
    return line + delta


@dataclass(frozen=True)
class ThreadPlan:
    """What to do with the comments of ours that are already on the PR."""

    #: Open threads whose code the new commits removed: resolve them.
    resolve: list[Any] = field(default_factory=list)
    #: Open threads that stay open (code untouched).
    keep_open: list[Any] = field(default_factory=list)
    #: Already resolved by somebody or by an earlier run.
    resolved_before: int = 0


def base_line(th: Any, base_sha: str | None) -> int | None:
    """The thread's line as the last reviewed commit numbers it, or None when
    that is not known.

    The providers hand back different things: GitLab and Bitbucket keep the
    position the comment was created at, GitHub the position at the CURRENT
    head (the original one only once the code is outdated). A line is the
    base commit's own only for a comment posted at that commit (the marker
    carries the commit), on the new side, and not already moved to the head.
    Anything else is left alone rather than compared in a foreign numbering.
    """
    line = getattr(th, "line", None)
    if not isinstance(line, int):
        return None
    if str(getattr(th, "side", "RIGHT") or "RIGHT").upper() == "LEFT":
        return None
    if getattr(th, "line_is_current", False):
        return None
    if not same_commit(getattr(th, "sha", None), base_sha):
        return None
    return line


def plan_threads(
    threads: Iterable[Any], removed: dict[str, set[int]], deleted: set[str],
    *, base_sha: str | None = None,
) -> ThreadPlan:
    """Sort our earlier comment threads: outdated (resolve) or still standing.

    A thread is outdated when its path was deleted by the increment, or when
    its line (in the numbering of `base_sha`, see `base_line`)
    is one of the old-side lines the increment removed. A thread whose line
    cannot be tied to `base_sha` stays open: a comment left standing costs
    one click, a wrongly resolved one hides a finding.
    """
    resolve: list[Any] = []
    keep: list[Any] = []
    done = 0
    for th in threads:
        if getattr(th, "resolved", False):
            done += 1
            continue
        path = getattr(th, "path", "") or ""
        line = base_line(th, base_sha)
        if path in deleted or (line is not None and line in removed.get(path, ())):
            resolve.append(th)
        else:
            keep.append(th)
    return ThreadPlan(resolve=resolve, keep_open=keep, resolved_before=done)


def already_posted(
    finding_fp: str, path: str, line: int, open_threads: Iterable[Any],
    *, window: int = DEDUPE_LINE_WINDOW,
) -> bool:
    """Is this finding already an open comment of ours at about this place?

    Same path, same fingerprint, within `window` lines. An open thread with no
    line counts as being anywhere in its file.
    """
    for th in open_threads:
        if getattr(th, "fingerprint", None) != finding_fp or getattr(th, "path", "") != path:
            continue
        at = getattr(th, "line", None)
        if not isinstance(at, int) or abs(at - line) <= window:
            return True
    return False
