"""Review issues — findings followed across the runs of one pull request.

A run's findings are a JSON blob on its SQLite row, and nothing tied push N's
finding to push N+1's, so neither "is this still there?" nor "was it fixed?"
had an answer. This module gives each finding an identity and keeps it in
Postgres (`review_issues`), next to a small record of the PR itself
(`review_pull_requests`).

Identity — `fingerprint()`:
    sha256(rule_id | file_path | normalised title). No line number: a push
    that adds ten lines above a defect moves the line and leaves the defect.
    No run id: that is the whole point. Digits in the title are folded too,
    because models like to write "on line 42" into a title.

Issues are per PR: the unique key is (workspace, repo, PR number,
fingerprint), so the same defect on two PRs is two issues with two fates.

Fixed in a subsequent commit — `plan_sync()`:
    When a reviewed run (complete, or partial) on the same PR at a NEW head
    finishes, an open issue
    whose fingerprint it did not find again is marked `fixed`
    (resolution_source=auto_next_commit, fixed_in_sha=the new head) only if
    its file changed between the two heads. "Changed" is read from the diffs:
    the hash of that file's section of the previous reviewed diff against the
    new one. Anything less certain leaves the issue open:
      - no previous diff recorded (first run after this shipped) — unknown;
      - the same head reviewed again — a model not repeating itself is not a
        fix;
      - the agent that raised it failed (what makes a run partial) or was
        skipped — the finder that would have repeated it did not look. The
        rest of a partial run's agents did look, so their issues are judged:
        a partial run moves the baseline, and refusing to judge in it lost
        every fix made in that commit for good;
      - the file's hunks did not reach the agents (skipped for size, a skip
        list, an ignore glob, or the file left the diff) — nobody looked;
      - the issue's rule was hidden by the deny-list this run — it may have
        been found and dropped;
      - the flagged line (`anchor`, its text on the head that raised it) is
        still in the file's new diff: the commit changed the file elsewhere.
        This is the case that went wrong first — a refund fixed in the same
        file closed an untouched divide-by-zero the model did not repeat.
    A section hash also moves when the base branch moves under a rebase; that
    can call an untouched file changed. It cannot call a changed file
    untouched, which is the error that would hide a live defect.

Closed unmerged — `record_pr_state()`:
    Closing resolves the PR's open issues (resolution_source=pr_closed) and
    reopening the PR reopens exactly those. A review that finishes after the
    close (it was already running) files its new issues as resolved too, so a
    closed PR never carries an open issue nobody will ever resolve.

Category — `categorize()`: see the docstring; derived from the agent and
keywords in rule_id/title, because rule ids are free text from the model.

Every entry point here is best-effort: a review that ran and posted must never
fail because the issue ledger could not be written.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

ISSUE_STATUSES = ("open", "fixed", "dismissed", "resolved")
ISSUE_CATEGORIES = (
    "bug", "security", "performance", "maintainability", "style", "other",
)

# ─── Identity ──────────────────────────────────────────────────────

_DIGITS = re.compile(r"\d+")
_NON_WORD = re.compile(r"[^\w#]+", re.UNICODE)


def normalize_title(title: str) -> str:
    """Lowercase, digits folded to '#', punctuation and spacing collapsed.

    'Null deref on line 42 in `load()`' and 'null deref on line 57 in load()'
    are the same claim, and must hash the same.
    """
    t = (title or "").strip().lower()
    t = _DIGITS.sub("#", t)
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())[:200]


def fingerprint(rule_id: str | None, file_path: str, title: str) -> str:
    basis = f"{(rule_id or '').strip().lower()}|{(file_path or '').strip()}|{normalize_title(title)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ─── Category ──────────────────────────────────────────────────────

_SECURITY_WORDS = (
    "security", "sec.", "cwe", "cve", "ghsa", "injection", "xss", "csrf", "ssrf",
    "secret", "credential", "password", "token", "auth", "permission",
    "privilege", "sanitiz", "escape", "traversal", "deserializ", "crypto",
    "vulnerab",
)
_PERF_WORDS = (
    "perf", "performance", "n+1", "n + 1", "slow", "latency", "quadratic",
    "inefficien", "allocation", "memory leak", "hot path", "blocking call",
    "o(n", "unbounded",
)
_STYLE_WORDS = (
    "style", "naming", "format", "lint", "typo", "spelling", "docstring",
    "whitespace", "indent", "convention",
)
_MAINT_WORDS = (
    "maintainab", "duplicat", "magic", "todo", "typing", "type hint",
    "dead code", "unused", "complexity", "readab", "refactor", "structur",
    "coupling", "no-coverage", "test coverage",
)
_BUG_WORDS = (
    "bug", "null", "none", "race", "deadlock", "exception", "crash",
    "off-by-one", "incorrect", "wrong", "overflow", "undefined", "leak",
    "breaking", "regression", "contract",
)


def categorize(agent: str | None, rule_id: str | None, title: str | None) -> str:
    """bug | security | performance | maintainability | style | other.

    The mapping, in priority order (first match wins):
      1. security — agent `security` or `cve`, or a security word in
         rule_id/title (cwe, injection, secret, auth, …);
      2. performance — agent `performance`, or a performance word (n+1,
         slow, quadratic, …);
      3. style — a style word (naming, format, typo, docstring, …);
      4. maintainability — agent `structural`, or a maintainability word
         (duplication, magic number, dead code, complexity, …);
      5. bug — agent `defect`, `contract`, `business_logic` or
         `breaking_change`, or a bug word (null, race, exception, wrong, …) —
         a change that does not do what its pull request says is a bug to
         the person tracking it;
      6. other — everything else, `compliance` included.
    Keywords outrank the agent for 2–4 because the defect agent finds slow
    code and naming problems too, and the rule id says which it was.
    """
    a = (agent or "").strip().lower()
    text = f"{rule_id or ''} {title or ''}".lower()

    def has(words: Iterable[str]) -> bool:
        return any(w in text for w in words)

    if a in ("security", "cve") or has(_SECURITY_WORDS):
        return "security"
    if a == "performance" or has(_PERF_WORDS):
        return "performance"
    if has(_STYLE_WORDS):
        return "style"
    if a == "structural" or has(_MAINT_WORDS):
        return "maintainability"
    if a in ("defect", "contract", "business_logic", "breaking_change") or has(_BUG_WORDS):
        return "bug"
    return "other"


# ─── Diff sections ─────────────────────────────────────────────────

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def file_section_hashes(raw_diff: str | None) -> dict[str, str]:
    """{path: sha256 of that file's section} for a git-format unified diff.

    The `index abc..def` line is left out of the hash: it names blob ids and
    would differ for the same content on another base. Both sides of a rename
    get the section's hash, so an issue on either path can be compared.
    """
    if not raw_diff:
        return {}
    out: dict[str, str] = {}
    current: list[str] = []
    paths: tuple[str, ...] = ()

    def flush() -> None:
        if paths and current:
            h = hashlib.sha256("".join(current).encode("utf-8")).hexdigest()[:32]
            for p in paths:
                out[p] = h

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            m = _DIFF_HEADER.match(line.rstrip("\n"))
            paths = tuple(dict.fromkeys((m.group(1), m.group(2)))) if m else ()
            current = []
            continue
        if line.startswith("index "):
            continue
        current.append(line)
    flush()
    return out


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _new_side_lines(raw_diff: str | None) -> dict[str, dict[int, tuple[str, bool]]]:
    """{path: {new-file line number: (text, added?)}} for every added or
    context line — the right-hand side of each hunk, as the reviewed head has
    it. Both sides of a rename get the lines, like `file_section_hashes`.

    `---`/`+++` are file headers only before a section's first hunk: inside a
    hunk "+++ x" is an added line whose content is "++ x" (TOML front matter,
    `++i;`), and skipping it would shift every later number.
    """
    out: dict[str, dict[int, tuple[str, bool]]] = {}
    if not raw_diff:
        return out
    lines: dict[int, tuple[str, bool]] = {}
    paths: tuple[str, ...] = ()
    n = 0

    def flush() -> None:
        for p in paths:
            out.setdefault(p, {}).update(lines)

    for raw in raw_diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            m = _DIFF_HEADER.match(raw)
            paths = tuple(dict.fromkeys((m.group(1), m.group(2)))) if m else ()
            lines = {}
            n = 0
            continue
        h = _HUNK_HEADER.match(raw)
        if h:
            n = int(h.group(1))
            continue
        if not n:
            continue  # section headers (index, ---, +++, mode lines)
        if raw.startswith("+"):
            lines[n] = (raw[1:], True)
            n += 1
        elif raw.startswith(" "):
            lines[n] = (raw[1:], False)
            n += 1
        # "-" lines and "\\ No newline at end of file" are not on the new side.
    flush()
    return out


def _norm(text: str) -> str:
    return " ".join(text.split())


#: An anchor must say something: "}" or "return None" is on every other line.
ANCHOR_MIN_CHARS = 8


def _distinctive(text: str) -> bool:
    return sum(ch.isalnum() for ch in text) >= ANCHOR_MIN_CHARS


def anchor_at(raw_diff: str | None, path: str, line: int | None) -> str | None:
    """The text of `path`:`line` on the reviewed head — only when the PR ADDED
    that line, it says something, and it occurs once in the file's diff.

    A context line can leave the next diff without leaving the file, so its
    absence would prove nothing; a short or repeated line would be "still
    there" forever. Those findings keep the plain file rule.
    """
    if not path or not isinstance(line, int):
        return None
    side = _new_side_lines(raw_diff).get(path, {})
    got = side.get(line)
    if got is None or not got[1]:
        return None
    text = _norm(got[0])
    if not text or not _distinctive(text):
        return None
    if sum(1 for t, _ in side.values() if _norm(t) == text) != 1:
        return None
    return text


def anchors_present(raw_diff: str | None) -> dict[str, set[str]]:
    """{path: normalised texts of every new-side line} — what an anchor is
    looked up in."""
    return {p: {_norm(t) for t, _ in ls.values() if _norm(t)}
            for p, ls in _new_side_lines(raw_diff).items()}


NEAR_WINDOW = 2


def near_lines(raw_diff: str | None, path: str, line: int | None,
               *, _cache: dict | None = None) -> frozenset[str]:
    """Normalised texts of `path` lines `line`±NEAR_WINDOW on the new side."""
    if not path or not isinstance(line, int):
        return frozenset()
    side = (_cache if _cache is not None else _new_side_lines(raw_diff)).get(path, {})
    return frozenset(
        t for n in range(line - NEAR_WINDOW, line + NEAR_WINDOW + 1)
        if (t := _norm(side.get(n, ("", False))[0]))
    )


# ─── Planning (pure) ───────────────────────────────────────────────


@dataclass
class FoundIssue:
    """One finding of the run, reduced to what an issue row needs."""

    fingerprint: str
    file_path: str
    line: int | None
    agent: str | None
    rule_id: str | None
    category: str
    severity: str
    title: str
    body: str
    suggestion: str | None
    #: Normalised texts of the head's lines around `line` (see `near_lines`):
    #: what lets a re-worded finding re-find the issue it already is.
    near: frozenset[str] = frozenset()
    #: Normalised text of the flagged line itself ("" when not in the diff).
    at_line: str = ""


@dataclass
class ExistingIssue:
    id: str
    fingerprint: str
    file_path: str
    agent: str | None
    status: str
    resolution_source: str | None
    rule_id: str | None = None
    anchor: str | None = None
    category: str | None = None
    title: str = ""


@dataclass
class SyncPlan:
    create: list[FoundIssue] = field(default_factory=list)
    #: (existing id, the finding that re-found it, reopen?, re-worded?)
    refound: list[tuple[str, FoundIssue, bool, bool]] = field(default_factory=list)
    #: ids of open issues the new head fixed
    fixed: list[str] = field(default_factory=list)


_SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def found_issues(findings: Iterable[Any]) -> list[FoundIssue]:
    """The run's findings as issues, one per fingerprint (worst severity wins)."""
    from src.review.models import severity_value

    by_fp: dict[str, FoundIssue] = {}
    for f in findings:
        get = (f.get if isinstance(f, dict) else lambda k, d=None, _f=f: getattr(_f, k, d))
        file_path = str(get("file_path", "") or "")
        title = str(get("title", "") or "")
        rule_id = get("rule_id") or None
        agent = get("agent") or None
        sev = severity_value(get("severity", "warning")) or "warning"
        fp = fingerprint(rule_id, file_path, title)
        line = get("line")
        cand = FoundIssue(
            fingerprint=fp, file_path=file_path,
            line=int(line) if isinstance(line, int) else None,
            agent=agent, rule_id=rule_id,
            category=categorize(agent, rule_id, title),
            severity=sev, title=title[:500], body=str(get("body", "") or ""),
            suggestion=get("suggestion") or None,
        )
        prev = by_fp.get(fp)
        if prev is None or _SEV_RANK.get(sev, 0) > _SEV_RANK.get(prev.severity, 0):
            by_fp[fp] = cand
    return list(by_fp.values())


#: Agents that pin every finding to line 1 of a file (a manifest, the first
#: changed file): position says nothing about which defect it is.
_FIXED_LINE_AGENTS = frozenset({"cve", "compliance", "breaking_change"})


def _reworded(f: FoundIssue, existing: list[ExistingIssue],
              claimed: set[str], exact: set[str]) -> ExistingIssue | None:
    """An open issue this finding is, re-worded by the model, or None.

    Titles come from a model, so the same defect arrives as "Division by zero
    on empty totals list" one push and "Potential ZeroDivisionError on empty
    list" the next — two fingerprints, two rows. What must agree instead:
    same file and agent, and the issue's anchor — a distinctive line the PR
    added, see `anchor_at` — within NEAR_WINDOW lines of where this finding
    points when the rule id or the normalised title agrees; when neither does
    (rule ids are model text and drift), the same category and the anchor ON
    the flagged line itself.
    Fixed-line agents never match this way. One finding claims at most one
    issue, an issue is claimed at most once per run, and an issue its own
    fingerprint already found this run is not up for grabs.
    """
    if not f.near or (f.agent or "") in _FIXED_LINE_AGENTS:
        return None
    for e in existing:
        if (e.id in claimed or e.fingerprint in exact
                or e.status != "open" or not e.anchor
                or e.file_path != f.file_path
                or (e.agent or "") != (f.agent or "")):
            continue
        same_rule = bool(e.rule_id and f.rule_id and e.rule_id == f.rule_id)
        same_title = bool(e.title) and normalize_title(e.title) == normalize_title(f.title)
        if e.category and e.category != f.category and not (same_rule or same_title):
            continue
        if not (same_rule or same_title):
            # Rule ids are free text from the model too ("defect.zero_div",
            # then "defect.division"), so differing ones prove nothing — but
            # without a shared rule or title only the exact flagged line
            # will do, not a neighbour of it.
            if e.anchor == f.at_line:
                return e
            continue
        if e.anchor in f.near:
            return e
    return None


def plan_sync(
    existing: list[ExistingIssue],
    found: list[FoundIssue],
    *,
    run_reviewed: bool,
    head_sha: str | None,
    prev_head_sha: str | None,
    prev_file_hashes: dict[str, str] | None,
    new_file_hashes: dict[str, str] | None,
    agents_not_run: Iterable[str] = (),
    reviewed_files: Iterable[str] | None = None,
    hidden_rules: Iterable[str] = (),
    pr_open: bool = True,
    new_anchors: dict[str, set[str]] | None = None,
) -> SyncPlan:
    """What to do with this PR's issue rows after one run. No I/O.

    See the module docstring for when an unrepeated issue counts as fixed.

    `reviewed_files` is the set of paths whose hunks the run's agents were
    actually given. `raw_diff` (and so `new_file_hashes`) still carries a file
    that was skipped for size, a lock/binary/generated list or an ignore glob,
    so a changed hash there says nothing about whether anyone looked. None —
    not known — judges nothing. `hidden_rules` are rule ids whose findings the
    prefilter's deny-list dropped this run: such a finding WAS found again and
    then hidden, which is not a fix.

    `run_reviewed` is False for a run in which no stage answered; a partial
    run IS reviewed — its failed agents arrive in `agents_not_run`, and only
    their issues are left unjudged. `pr_open` is False once the PR was closed
    unmerged: an issue that close resolved is then not reopened by a review
    that was still running when it closed.

    `new_anchors` ({path: texts of the new head's diff lines}, see
    `anchors_present`) keeps open an issue whose flagged line is still there
    unchanged: the file moving elsewhere is not the defect going away.
    """
    plan = SyncPlan()
    by_fp = {e.fingerprint: e for e in existing}
    found_fps = set()
    claimed: set[str] = set()
    exact = {f.fingerprint for f in found if f.fingerprint in by_fp}
    for f in found:
        found_fps.add(f.fingerprint)
        e = by_fp.get(f.fingerprint)
        reworded = e is None
        if e is None:
            e = _reworded(f, existing, claimed, exact)
            if e is None:
                plan.create.append(f)
                continue
            # The same defect under a new title: it re-finds that issue, and
            # the issue's fingerprint counts as found for the fix check below.
            found_fps.add(e.fingerprint)
        claimed.add(e.id)
        # A fix the machine inferred and the next run contradicted is a
        # regression — the issue comes back. A person's decision (dismissed,
        # resolved, or fixed by hand) is not overruled by a model.
        # The close of a PR is a machine decision too: refound on a PR that is
        # open again, it is live.
        reopen = (
            (e.status == "fixed" and e.resolution_source == "auto_next_commit")
            or (pr_open and e.status == "resolved"
                and e.resolution_source == "pr_closed")
        )
        plan.refound.append((e.id, f, reopen, reworded))

    can_judge = (
        run_reviewed
        and bool(head_sha) and bool(prev_head_sha)
        and head_sha != prev_head_sha
        and prev_file_hashes is not None
        and bool(new_file_hashes)
        and reviewed_files is not None
    )
    if not can_judge:
        return plan
    skipped_agents = {a.strip().lower() for a in agents_not_run if a}
    looked_at = {p for p in (reviewed_files or ()) if p}
    hidden = {r.strip() for r in hidden_rules if r and r.strip()}
    for e in existing:
        if e.status != "open" or e.fingerprint in found_fps:
            continue
        if (e.agent or "").strip().lower() in skipped_agents:
            continue
        if e.file_path not in looked_at:
            # Skipped for size, by a glob or a skip list, or gone from the
            # diff altogether: nobody read the file, so nothing was fixed.
            continue
        if e.rule_id and e.rule_id.strip() in hidden:
            continue
        before = (prev_file_hashes or {}).get(e.file_path)
        after = (new_file_hashes or {}).get(e.file_path)
        if before is None and after is None:
            # The file is in neither diff we can read: nothing says it changed.
            continue
        if before == after:
            continue
        if e.anchor and new_anchors is not None and e.anchor in new_anchors.get(e.file_path, set()):
            # The commit changed this file somewhere else; the line the
            # finding pointed at is still exactly as it was.
            continue
        plan.fixed.append(e.id)
    return plan


# ─── Persistence (best-effort) ─────────────────────────────────────

_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def _engine():
    """One process-wide sync engine, built lazily (see llm/budget.py)."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from sqlalchemy import create_engine

            from src.db.session import get_database_url

            url = get_database_url().replace(
                "postgresql+asyncpg://", "postgresql+psycopg://",
            )
            _ENGINE = create_engine(
                url, pool_pre_ping=True, pool_size=2, max_overflow=3,
                pool_recycle=1800,
            )
    return _ENGINE


def _now() -> datetime:
    return datetime.now(UTC)


def _pr_row(s, workspace_id: str, provider: str, repo: str, number: int):
    """The PR's row, created if missing, and LOCKED for this transaction.

    Two writers can finish on one PR at once (a UI-triggered run next to a
    webhook one; the queue's dedup key has no head sha). Read-then-insert let
    the second commit hit the unique key and lose its whole sync — no issues,
    no new baseline — silently. So the row is first inserted with ON CONFLICT
    DO NOTHING and then read FOR UPDATE: the second writer waits for the first
    to commit and then sees its baseline and its issues. SQLite (tests) has no
    row locks and serialises writers anyway; FOR UPDATE is dropped there.
    """
    from sqlalchemy import select

    from src.db.models import ReviewPullRequest

    dialect = s.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # pragma: no cover - no third backend is deployed
        _insert = None
    now = _now()
    if _insert is not None:
        s.execute(_insert(ReviewPullRequest).values(
            id=_new_id(), workspace_id=workspace_id, provider=provider,
            repo=repo, number=number, state="open", reviews_count=0,
            title="", opened_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=[
            "workspace_id", "provider", "repo", "number",
        ]))
    q = select(ReviewPullRequest).where(
        ReviewPullRequest.workspace_id == workspace_id,
        ReviewPullRequest.provider == provider,
        ReviewPullRequest.repo == repo,
        ReviewPullRequest.number == number,
    )
    if dialect == "postgresql":
        q = q.with_for_update()
    row = s.execute(q).scalar_one_or_none()
    if row is None:
        row = ReviewPullRequest(
            workspace_id=workspace_id, provider=provider, repo=repo,
            number=number, state="open", reviews_count=0, opened_at=now,
        )
        s.add(row)
        s.flush()
    return row


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def record_review_run(
    result: Any,
    *,
    run_id: str,
    workspace_id: str,
    status: str,
    engine=None,
) -> bool | None:
    """After a run finished: upsert the PR row and this PR's issues.

    `status` is the run row's final status (complete | partial | skipped |
    failed). Never raises. True when written, False when the write failed,
    None when there was nothing to write (no batch or no PR).
    """
    try:
        batch = getattr(result, "batch", None)
        pr = getattr(batch, "pull_request", None)
        if batch is None or pr is None or not getattr(pr, "number", None):
            return None
        _record(batch, pr, run_id=run_id, workspace_id=workspace_id or "default",
                status=status, engine=engine or _engine())
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_issues_sync_failed run=%s err=%s", run_id, exc)
        return False


def _reviewed_files(pr) -> set[str]:
    """Paths whose hunks reached the review: both sides of a rename.

    `pr.hunks` is what is left after the provider's skip lists, the size
    limit per file and the repo's ignore globs, so a file in `skipped_files`
    is not in it.
    """
    out: set[str] = set()
    for h in getattr(pr, "hunks", None) or []:
        for p in (getattr(h, "file_path", None), getattr(h, "old_file_path", None)):
            if p:
                out.add(str(p))
    return out


def _stage_status(batch, fallback: str) -> str:
    try:
        return str(batch.run_status.value)
    except Exception:  # noqa: BLE001
        return fallback


def _record(batch, pr, *, run_id: str, workspace_id: str, status: str, engine) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ReviewIssue

    now = _now()
    provider = str(pr.provider or "")
    repo = str(pr.repo or "")
    number = int(pr.number)
    try:
        repo_slug = pr.local_slug
    except Exception:  # noqa: BLE001
        repo_slug = repo
    reviewed = status in ("complete", "partial")

    with Session(engine) as s:
        row = _pr_row(s, workspace_id, provider, repo, number)
        prev_head = row.head_sha
        prev_hashes = row.file_hashes if isinstance(row.file_hashes, dict) else None

        row.repo_slug = repo_slug
        row.title = str(getattr(pr, "title", "") or row.title or "")[:500]
        row.author = getattr(pr, "author", None) or row.author
        row.url = getattr(pr, "url", None) or row.url
        row.head_ref = getattr(pr, "head_ref", None) or row.head_ref
        row.base_ref = getattr(pr, "base_ref", None) or row.base_ref
        pr_state = str(getattr(pr, "state", "") or "").lower()
        if pr_state in ("merged", "closed") and row.state == "open":
            row.state = pr_state
            row.closed_at = row.closed_at or now
        row.reviews_count = int(row.reviews_count or 0) + 1
        row.last_review_status = status
        row.last_run_id = run_id
        row.updated_at = now

        if not reviewed:
            # A skipped or failed run read nothing, so it moves neither the
            # baseline the next fix check compares against nor any issue.
            s.commit()
            return

        raw_diff = getattr(pr, "raw_diff", "") or ""
        new_hashes = file_section_hashes(raw_diff)
        head_sha = getattr(pr, "head_sha", None) or None
        # The ROW's state, not the run's snapshot: a close webhook that landed
        # while this review ran is newer than the PR this run fetched.
        pr_closed_unmerged = row.state == "closed"

        existing_rows = s.execute(select(ReviewIssue).where(
            ReviewIssue.workspace_id == workspace_id,
            ReviewIssue.repo_slug == repo_slug,
            ReviewIssue.pr_number == number,
        )).scalars().all()
        by_id = {r.id: r for r in existing_rows}
        found = found_issues(batch.findings)
        side = _new_side_lines(raw_diff)
        for f in found:
            f.near = near_lines(raw_diff, f.file_path, f.line, _cache=side)
            if isinstance(f.line, int):
                f.at_line = _norm(side.get(f.file_path, {}).get(f.line, ("", False))[0])
        plan = plan_sync(
            [ExistingIssue(
                id=r.id, fingerprint=r.fingerprint, file_path=r.file_path,
                agent=r.agent, status=r.status,
                resolution_source=r.resolution_source, rule_id=r.rule_id,
                anchor=r.anchor, category=r.category, title=r.title or "",
            ) for r in existing_rows],
            found,
            # The stages, not the delivery: a review whose every agent
            # answered and whose comments failed to post still looked. A
            # partial one looked too, minus its failed agents (passed below).
            run_reviewed=_stage_status(batch, status) in ("complete", "partial"),
            head_sha=head_sha, prev_head_sha=prev_head,
            prev_file_hashes=prev_hashes, new_file_hashes=new_hashes,
            agents_not_run=[
                *(getattr(batch, "agents_failed", None) or []),
                *(getattr(batch, "agents_skipped", None) or []),
            ],
            reviewed_files=_reviewed_files(pr),
            hidden_rules=list((getattr(batch, "dropped_by_rule", None) or {}).keys()),
            pr_open=not pr_closed_unmerged,
            new_anchors=anchors_present(raw_diff) if raw_diff else None,
        )

        for f in plan.create:
            s.add(ReviewIssue(
                workspace_id=workspace_id, repo_slug=repo_slug,
                fingerprint=f.fingerprint, file_path=f.file_path, line=f.line,
                anchor=anchor_at(raw_diff, f.file_path, f.line),
                agent=f.agent, rule_id=f.rule_id, category=f.category,
                severity=f.severity, title=f.title, body=f.body,
                suggestion=f.suggestion,
                # Closed while this run was in flight: record what it found,
                # resolved the way the close resolved the rest.
                status="resolved" if pr_closed_unmerged else "open",
                resolution_source="pr_closed" if pr_closed_unmerged else None,
                closed_at=now if pr_closed_unmerged else None,
                pr_provider=provider, pr_repo=repo, pr_number=number,
                pr_url=getattr(pr, "url", None) or None,
                first_run_id=run_id, last_run_id=run_id,
                first_seen_sha=head_sha, last_seen_sha=head_sha,
                occurrences=1, first_seen_at=now, last_seen_at=now,
            ))
        for issue_id, f, reopen, reworded in plan.refound:
            r = by_id[issue_id]
            if reworded:
                # Re-key to the wording the review now shows, so a dismiss on
                # this finding (apply_feedback matches by fingerprint) lands.
                # Unique-safe: no row held this fingerprint (else it would
                # have matched exactly), and found_issues has one per print.
                r.fingerprint = f.fingerprint
                r.rule_id = f.rule_id
            r.line = f.line
            r.anchor = anchor_at(raw_diff, f.file_path, f.line) or r.anchor
            r.severity = f.severity
            r.title = f.title
            r.body = f.body
            r.suggestion = f.suggestion
            r.category = f.category
            r.last_run_id = run_id
            r.last_seen_sha = head_sha
            r.last_seen_at = now
            r.occurrences = int(r.occurrences or 0) + 1
            if reopen:
                r.status = "open"
                r.resolution_source = None
                r.fixed_in_sha = None
                r.closed_at = None
        for issue_id in plan.fixed:
            r = by_id[issue_id]
            r.status = "fixed"
            r.resolution_source = "auto_next_commit"
            r.fixed_in_sha = head_sha
            r.closed_at = now

        row.head_sha = head_sha
        row.file_hashes = new_hashes or None
        s.commit()
        logger.info(
            "review_issues_synced run=%s pr=%s#%d new=%d refound=%d fixed=%d",
            run_id, repo, number, len(plan.create), len(plan.refound),
            len(plan.fixed),
        )


def record_failed_review(
    *, workspace_id: str, provider: str, repo: str, number: int, run_id: str,
    engine=None,
) -> None:
    """A run that raised before it had a batch still counts as a review of
    the PR, and as a failed one. Never raises."""
    record_unreviewed_run(workspace_id=workspace_id, provider=provider, repo=repo,
                          number=number, run_id=run_id, status="failed",
                          engine=engine)


def record_unreviewed_run(
    *, workspace_id: str, provider: str, repo: str, number: int, run_id: str,
    status: str, title: str | None = None, author: str | None = None,
    url: str | None = None, head_ref: str | None = None,
    base_ref: str | None = None, engine=None,
) -> bool:
    """A run that ended without a batch — failed, or skipped before the
    pipeline started (auto-review off, a draft at the webhook) — still counts
    as a review of the PR, so the pull-requests page can show it with its
    reason. Moves no baseline and no issue. Never raises."""
    try:
        from sqlalchemy.orm import Session

        now = _now()
        with Session(engine or _engine()) as s:
            row = _pr_row(s, workspace_id, provider, repo, int(number))
            if title:
                row.title = str(title)[:500]
            row.author = author or row.author
            row.url = url or row.url
            row.head_ref = head_ref or row.head_ref
            row.base_ref = base_ref or row.base_ref
            row.reviews_count = int(row.reviews_count or 0) + 1
            row.last_review_status = status
            row.last_run_id = run_id
            row.updated_at = now
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_pr_unreviewed_record_failed run=%s status=%s err=%s",
                       run_id, status, exc)
        return False


def record_pr_state(
    *, workspace_id: str, provider: str, repo: str, number: int, state: str,
    title: str | None = None, author: str | None = None, url: str | None = None,
    head_sha: str | None = None, engine=None,
) -> bool:
    """A provider said the PR was merged or closed (or reopened). Never raises.

    Closing UNMERGED resolves the PR's open issues (resolution_source
    pr_closed): the code they were raised on will never land. Reopening puts
    those back to open. A merge leaves them open on purpose — "found by
    Celmis, merged anyway" is exactly the number the analytics page reports.

    The PR row is created when missing, with reviews_count=0: a review that
    is still running when the close lands must find the closed state. The
    Pull requests list shows only rows with a review (reviews_count > 0), so
    a PR Celmis never reviewed does not appear there.
    """
    if state not in ("open", "merged", "closed"):
        return False
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from src.db.models import ReviewIssue

        now = _now()
        with Session(engine or _engine()) as s:
            row = _pr_row(s, workspace_id, provider, repo, int(number))
            row.state = state
            row.closed_at = now if state != "open" else None
            if title:
                row.title = title[:500]
            if author:
                row.author = author
            if url:
                row.url = url
            row.updated_at = now
            of_pr = (
                ReviewIssue.workspace_id == workspace_id,
                ReviewIssue.pr_provider == provider,
                ReviewIssue.pr_repo == repo,
                ReviewIssue.pr_number == int(number),
            )
            if state == "closed":
                for issue in s.execute(select(ReviewIssue).where(
                    *of_pr, ReviewIssue.status == "open",
                )).scalars():
                    issue.status = "resolved"
                    issue.resolution_source = "pr_closed"
                    issue.closed_at = now
            elif state == "open":
                # Reopened: what the close resolved is live again. Only what
                # the CLOSE resolved — a person's resolution stands.
                for issue in s.execute(select(ReviewIssue).where(
                    *of_pr, ReviewIssue.status == "resolved",
                    ReviewIssue.resolution_source == "pr_closed",
                )).scalars():
                    issue.status = "open"
                    issue.resolution_source = None
                    issue.closed_at = None
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_pr_state_record_failed repo=%s pr=%s err=%s",
                       repo, number, exc)
        return False


def apply_feedback(
    *, workspace_id: str, run_id: str, state: str | None,
    file_path: str | None, title: str | None, rule_id: str | None,
    pr: tuple[str, str, int] | None = None,
    engine=None,
) -> int:
    """Map a finding's accept/dismiss onto its issue. Returns rows changed.

    dismissed → the issue is dismissed (resolution_source=feedback);
    None (the feedback was cleared) or accepted (the verdict was flipped —
    the review page's only way to undo a dismissal) → an issue dismissed BY
    feedback reopens; on an open issue accepted changes nothing, because
    "this is real" is what open already says.

    `pr` is the run's (provider, repo, number). With it the issue is found by
    its PR and fingerprint, so feedback given on ANY run of the PR reaches it
    — an issue keeps only its first and its latest run id, and feedback on a
    run in between matched nothing. Without it (a run row from before the PR
    columns) the run ids are all there is.
    Never raises.
    """
    if not file_path or title is None:
        return 0
    try:
        from sqlalchemy import or_, select
        from sqlalchemy.orm import Session

        from src.db.models import ReviewIssue

        fp = fingerprint(rule_id, file_path, title)
        changed = 0
        with Session(engine or _engine()) as s:
            if pr is not None:
                provider, repo, number = pr
                scope = (ReviewIssue.pr_provider == provider,
                         ReviewIssue.pr_repo == repo,
                         ReviewIssue.pr_number == int(number))
            else:
                scope = (or_(ReviewIssue.last_run_id == run_id,
                             ReviewIssue.first_run_id == run_id),)
            rows = s.execute(select(ReviewIssue).where(
                ReviewIssue.workspace_id == workspace_id,
                ReviewIssue.fingerprint == fp,
                *scope,
            )).scalars().all()
            for r in rows:
                if state == "dismissed" and r.status == "open":
                    r.status = "dismissed"
                    r.resolution_source = "feedback"
                    r.closed_at = _now()
                    changed += 1
                elif state in (None, "accepted") and r.status == "dismissed" \
                        and r.resolution_source == "feedback":
                    r.status = "open"
                    r.resolution_source = None
                    r.closed_at = None
                    changed += 1
            s.commit()
        return changed
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_issue_feedback_failed run=%s err=%s", run_id, exc)
        return 0
