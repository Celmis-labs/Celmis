"""What people said about findings, kept so the next review can learn from it.

One append-only table (`finding_signals`) and one lookup table
(`posted_finding_comments`). Every writer in the product ends up here:

    record_ui          the thumbs on the reviews page (PUT /api/feedback)
    record_verdict     a reply, a command, a reaction, a resolved thread
    record_outcome     the issues ledger saying "implemented" / "ignored"
    record_posted      a posted inline comment, so a reply can be traced back

A finding is identified by `issues.fingerprint` (rule | file | normalised
title): PR-independent, so a dismissal on one pull request is recognised on
the next. A signal carries a snapshot of the finding (title, file, a short
body) because the feedback table keeps none and a run can be pruned.

Rules every writer follows:

  * never raises — learning is best-effort and must not fail a review, a
    webhook or a verdict;
  * idempotent — the unique key (workspace, PR, fingerprint, signal, source,
    actor) makes a repeated delivery a no-op;
  * one actor counts once per (PR, fingerprint): a person changing their mind
    replaces their earlier verdict instead of adding to it;
  * a reviewer the settings exclude (`learning_excluded_reviewers`) teaches
    nothing — the signal is dropped with a log line;
  * every read and write carries the workspace id (the repo slug is not
    unique across tenants).

The issues ledger tells this module what became of a suggestion through
`outcome_hooks` (`on_issue_outcome`, registered at import): implemented counts
as accepted, an ignored suggestion is a weak signal (0.3) that feeds only the
rules job, and a resolved thread (0.4) is upgraded or dropped at the next
outcome.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

SIGNALS: tuple[str, ...] = ("dismissed", "accepted", "implemented", "ignored", "resolved")
SOURCES: tuple[str, ...] = (
    "ui", "reply", "command", "reaction", "resolve", "auto_next_commit", "auto_merge",
)
#: Sources the code itself writes (no person behind them).
AUTO_SOURCES: tuple[str, ...] = ("auto_next_commit", "auto_merge")
#: How much one signal counts. A resolved thread says little, an ignored
#: suggestion less (somebody may simply not have had time).
WEIGHTS: dict[str, float] = {
    "dismissed": 1.0, "accepted": 1.0, "implemented": 1.0,
    "ignored": 0.3, "resolved": 0.4,
}
#: A signal that says "this finding was right".
POSITIVE = frozenset({"accepted", "implemented"})
#: Reasons a person gives for a dismissal that are not about the finding being
#: wrong: the same finding twice says nothing against it.
NEUTRAL_REASONS = frozenset({"duplicate"})

BODY_MAX = 1000
TITLE_MAX = 300
REASON_MAX = 300
ACTOR_MAX = 200
#: Most reviews' worth of findings one posted-comment batch records.
MAX_POSTED_PER_RUN = 200

_SLUG = re.compile(r"[^a-z0-9]+")


# ─── Identity and snapshots ──────────────────────────────────────────


@dataclass(frozen=True)
class PRRef:
    """The pull request a signal was given on."""

    provider: str
    repo: str
    number: int


@dataclass(frozen=True)
class FindingSnapshot:
    """What is kept of a finding: enough to recognise it again and to show it."""

    title: str
    file_path: str = ""
    body: str = ""
    rule_id: str | None = None
    agent: str | None = None
    severity: str | None = None
    category: str | None = None
    fingerprint: str = ""
    line: int | None = None

    def complete(self) -> FindingSnapshot:
        """The same snapshot with its fingerprint and category filled in."""
        from src.review.issues import categorize, fingerprint

        fp = self.fingerprint or fingerprint(self.rule_id, self.file_path, self.title)
        cat = self.category or categorize(self.agent, self.rule_id, self.title)
        return FindingSnapshot(
            title=_clip(self.title, TITLE_MAX), file_path=self.file_path or "",
            body=_clip(self.body, BODY_MAX), rule_id=self.rule_id or None,
            agent=self.agent or None, severity=self.severity or None,
            category=cat, fingerprint=fp, line=self.line)


def _clip(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def snapshot_of(finding: Any) -> FindingSnapshot:
    """A snapshot from a `Finding` object or the dict a run stores."""

    def get(name: str, default: Any = "") -> Any:
        if isinstance(finding, dict):
            return finding.get(name, default)
        return getattr(finding, name, default)

    severity = get("severity", "")
    severity = getattr(severity, "value", severity)
    line = get("line", None)
    return FindingSnapshot(
        title=str(get("title") or ""), file_path=str(get("file_path") or ""),
        body=str(get("body") or ""), rule_id=str(get("rule_id") or "") or None,
        agent=str(get("agent") or "") or None,
        severity=str(severity or "").lower() or None,
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
    ).complete()


def feedback_key(file_path: str, line: int, title: str, rule_id: str | None = None) -> str:
    """Stable identity of a finding across re-runs, as the reviews page mints it
    for a verdict (`api.routers.feedback.finding_key` delegates here)."""
    import hashlib

    basis = f"{rule_id or ''}|{file_path}|{line}|{title.strip()[:120]}"
    return hashlib.sha256(basis.encode()).hexdigest()[:20]


def local_slug(provider: str, repo: str) -> str:
    """The slug a repository is registered under (what `PullRequest.local_slug`
    is and what the ledger keys on). Falls back to a prefixed form."""
    try:
        from src.sync.git_providers import parse_repo_url

        return parse_repo_url(f"{provider}:{repo}").slug
    except Exception:  # noqa: BLE001
        return f"{provider}_{str(repo).replace('/', '-')}"


def normalise_actor(value: object) -> str:
    """An identity (e-mail, account id, login) as it is stored and compared."""
    return " ".join(str(value or "").split()).casefold()[:ACTOR_MAX]


def is_excluded(actor_ids: Iterable[object], excluded: Iterable[object]) -> bool:
    """Does any identity of the actor appear in the excluded list? Compared
    case-insensitively; an empty identity never matches."""
    bad = {normalise_actor(x) for x in excluded or ()} - {""}
    if not bad:
        return False
    return any(normalise_actor(a) in bad for a in actor_ids or ())


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _session(session: Any = None):
    from src.review.memories import _sync_session

    return _sync_session(session)


def excluded_reviewers_sync(ws: str, repo_slug: str | None, session: Any = None) -> list[str]:
    """The effective `learning_excluded_reviewers` of a repository. Blocking;
    [] when it cannot be read."""
    from src.review.memories import _effective_setting

    try:
        with _session(session) as s:
            value = _effective_setting(s, ws, repo_slug, "learning_excluded_reviewers")
        return [str(v) for v in (value or [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_excluded_lookup_failed ws=%s err_type=%s", ws,
                       type(exc).__name__)
        return []


# ─── Writers ─────────────────────────────────────────────────────────


def _key_filter(model: Any, ws: str, pr: PRRef | None, fingerprint: str):
    return (
        model.workspace_id == ws,
        model.pr_provider == (pr.provider if pr else ""),
        model.pr_repo == (pr.repo if pr else ""),
        model.pr_number == (int(pr.number) if pr else 0),
        model.fingerprint == fingerprint,
    )


def record_signal(
    workspace_id: str,
    repo_slug: str,
    snap: FindingSnapshot,
    signal: str,
    source: str,
    *,
    pr: PRRef | None = None,
    actor: str = "",
    also_known_as: Iterable[object] = (),
    actor_is_member: bool = False,
    reason: str = "",
    comment_id: str | None = None,
    run_id: str | None = None,
    sha: str | None = None,
    weight: float | None = None,
    replace_other_signals: bool = False,
    session: Any = None,
) -> str:
    """Write one signal. Returns what happened: `created`, `exists` (the same
    signal was already there), `excluded` (the actor teaches nothing),
    `invalid` (unknown signal/source or no usable finding) or `error`.

    `replace_other_signals` makes a person's verdict a verdict, not a pile:
    the actor's earlier signals of the same source on this (PR, finding) with
    another signal are removed first (dismissed → accepted replaces).
    Never raises.
    """
    try:
        if signal not in SIGNALS or source not in SOURCES:
            return "invalid"
        full = snap.complete()
        if not full.fingerprint or not (full.title or full.file_path):
            return "invalid"
        who = normalise_actor(actor)
        if source not in AUTO_SOURCES and (who or also_known_as):
            ids = [who, *[normalise_actor(a) for a in also_known_as]]
            ex = excluded_reviewers_sync(workspace_id, repo_slug, session)
            if is_excluded(ids, ex):
                logger.info("learning_signal_dropped reason=excluded_reviewer ws=%s "
                            "repo=%s signal=%s source=%s", workspace_id, repo_slug,
                            signal, source)
                return "excluded"
        from sqlalchemy import delete, select

        from src.db.models import FindingSignal as M

        with _session(session) as s:
            keys = _key_filter(M, workspace_id, pr, full.fingerprint)
            if replace_other_signals:
                s.execute(delete(M).where(
                    *keys, M.source == source, M.actor == who, M.signal != signal))
            exists = s.scalars(select(M.id).where(
                *keys, M.signal == signal, M.source == source, M.actor == who,
            ).limit(1)).first()
            if exists is not None:
                s.commit()
                return "exists"
            s.add(M(
                workspace_id=workspace_id, repo_slug=repo_slug,
                fingerprint=full.fingerprint, file_path=full.file_path,
                title=full.title, body=full.body, rule_id=full.rule_id,
                agent=full.agent, severity=full.severity, category=full.category,
                signal=signal, source=source,
                weight=float(WEIGHTS[signal] if weight is None else weight),
                reason=_clip(reason, REASON_MAX), actor=who,
                actor_is_member=bool(actor_is_member),
                pr_provider=pr.provider if pr else "", pr_repo=pr.repo if pr else "",
                pr_number=int(pr.number) if pr else 0,
                comment_id=str(comment_id) if comment_id else None,
                run_id=run_id, sha=(sha or None), embedded=False,
                created_at=_utcnow()))
            s.commit()
        return "created"
    except Exception as exc:  # noqa: BLE001 — learning never fails its caller
        logger.warning("learning_signal_failed ws=%s signal=%s err_type=%s err=%s",
                       workspace_id, signal, type(exc).__name__, str(exc)[:200])
        return "error"


def record_verdict(
    workspace_id: str, repo_slug: str, snap: FindingSnapshot, state: str, source: str,
    *, pr: PRRef | None, actor: str, also_known_as: Iterable[object] = (),
    actor_is_member: bool = False, reason: str = "", comment_id: str | None = None,
    run_id: str | None = None, sha: str | None = None, session: Any = None,
) -> str:
    """A person's accept/dismiss of a finding, replacing their earlier verdict
    on it. `state` is `accepted` or `dismissed`."""
    if state not in ("accepted", "dismissed"):
        return "invalid"
    return record_signal(
        workspace_id, repo_slug, snap, state, source, pr=pr, actor=actor,
        also_known_as=also_known_as, actor_is_member=actor_is_member, reason=reason,
        comment_id=comment_id, run_id=run_id, sha=sha, replace_other_signals=True,
        session=session)


def clear_verdict(
    workspace_id: str, fingerprint: str, source: str, *, pr: PRRef | None, actor: str,
    session: Any = None,
) -> int:
    """Withdraw an actor's verdicts of `source` on a finding. Returns rows
    removed. Never raises."""
    try:
        from sqlalchemy import delete

        from src.db.models import FindingSignal as M

        with _session(session) as s:
            res = s.execute(delete(M).where(
                *_key_filter(M, workspace_id, pr, fingerprint), M.source == source,
                M.actor == normalise_actor(actor), M.signal.in_(("dismissed", "accepted"))))
            s.commit()
            return int(res.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_clear_failed ws=%s err_type=%s", workspace_id,
                       type(exc).__name__)
        return 0


def pr_of_run(run_id: str) -> tuple[str, PRRef] | None:
    """(workspace, PR) a run reviewed, from the run store; None when unknown."""
    try:
        from src.api.review_runs import get_review_run_store

        found = get_review_run_store().pr_of(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_run_lookup_failed run=%s err_type=%s", run_id,
                       type(exc).__name__)
        return None
    if found is None:
        return None
    return found[0], PRRef(found[1], found[2], int(found[3]))


def snapshot_from_run(
    run_id: str, finding_key_value: str, *, file_path: str | None = None,
    title: str | None = None, rule_id: str | None = None,
) -> FindingSnapshot | None:
    """The finding of a stored run: the one whose feedback key is
    `finding_key_value`, or, when no key matches (the page mints its own,
    line-sensitive key, not this module's), the one with the same file, title and
    rule. What the run stored wins over what a client says about it."""
    try:
        from src.api.review_runs import get_review_run_store

        by_identity: FindingSnapshot | None = None
        want = (str(file_path or ""), str(title or "").strip(), rule_id or None)
        for item in get_review_run_store().findings_of(run_id) or []:
            line = item.get("line")
            key = feedback_key(str(item.get("file_path") or ""),
                              int(line) if isinstance(line, int) else 0,
                              str(item.get("title") or ""), item.get("rule_id") or None)
            if key == finding_key_value:
                return snapshot_of(item)
            have = (str(item.get("file_path") or ""), str(item.get("title") or "").strip(),
                    item.get("rule_id") or None)
            if by_identity is None and (want[0] or want[1]) and have == want:
                by_identity = snapshot_of(item)
        return by_identity
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_snapshot_failed run=%s err_type=%s", run_id,
                       type(exc).__name__)
    return None


def record_ui(
    workspace_id: str, run_id: str, state: str, *, actor: str,
    also_known_as: Iterable[object] = (), actor_is_member: bool = True,
    reason: str = "", finding_key_value: str = "",
    snapshot: FindingSnapshot | None = None, session: Any = None,
) -> str:
    """The reviews page's verdict on one finding of a run.

    The PR and the repository come from the run row (never from the client),
    and a run of another workspace teaches nothing. The finding comes from
    `snapshot` (what the page sent) or is recomputed from the run.
    """
    found = pr_of_run(run_id)
    if found is None or found[0] != workspace_id:
        return "invalid"
    pr = found[1]
    snap = snapshot
    if snap is None and finding_key_value:
        snap = snapshot_from_run(run_id, finding_key_value)
    if snap is None:
        return "invalid"
    return record_verdict(
        workspace_id, local_slug(pr.provider, pr.repo), snap, state, "ui", pr=pr,
        actor=actor, also_known_as=also_known_as, actor_is_member=actor_is_member,
        reason=reason, run_id=run_id, session=session)


def clear_ui(
    workspace_id: str, run_id: str, *, actor: str, finding_key_value: str = "",
    snapshot: FindingSnapshot | None = None, session: Any = None,
) -> int:
    """Take back the page verdict of `actor` on a finding of a run."""
    found = pr_of_run(run_id)
    if found is None or found[0] != workspace_id:
        return 0
    snap = snapshot
    if snap is None and finding_key_value:
        snap = snapshot_from_run(run_id, finding_key_value)
    if snap is None:
        return 0
    return clear_verdict(workspace_id, snap.complete().fingerprint, "ui", pr=found[1],
                         actor=actor, session=session)


# ─── Posted comments ─────────────────────────────────────────────────


def _posted_fields(item: Any) -> tuple[str, str, int | None, str, str, str | None] | None:
    """(comment_id, path, line, fingerprint token, full key, thread id) of one entry of a
    provider's `inline_comments` — a `PostedComment` or the dict of one."""
    def get(name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    comment_id = get("comment_id")
    if comment_id is None or isinstance(comment_id, bool) or str(comment_id).strip() == "":
        return None
    line = get("line")
    thread = str(get("thread_id") or "").strip() or None
    return (str(comment_id).strip(), str(get("path") or ""),
            line if isinstance(line, int) and not isinstance(line, bool) else None,
            str(get("fingerprint") or "").lower(), str(get("finding_key") or "").lower(),
            thread)


def record_posted(
    workspace_id: str, pr: PRRef, findings: Iterable[Any], inline_comments: Iterable[Any],
    *, sha: str | None = None, run_id: str | None = None, repo_slug: str | None = None,
    session: Any = None,
) -> int:
    """Remember which provider comment each posted finding became.

    `inline_comments` is what `post_review` returned (`PostedComment`s or
    their dicts). Each is matched to its finding by the full issue fingerprint
    (or the 16-hex token of the marker); a comment that matches nothing is
    skipped. Idempotent; returns how many rows were written. Never raises.
    """
    try:
        from sqlalchemy import select

        from src.db.models import PostedFindingComment as M

        by_full: dict[str, FindingSnapshot] = {}
        by_key: dict[str, str] = {}
        for f in findings or ():
            snap = snapshot_of(f)
            by_full.setdefault(snap.fingerprint, snap)
            by_key.setdefault(snap.fingerprint, feedback_key(
                snap.file_path, snap.line or 0, snap.title, snap.rule_id))
        slug = repo_slug or local_slug(pr.provider, pr.repo)
        written = 0
        with _session(session) as s:
            for item in list(inline_comments or ())[:MAX_POSTED_PER_RUN]:
                fields = _posted_fields(item)
                if fields is None:
                    continue
                comment_id, path, line, token, full_key, thread = fields
                fp = full_key if full_key in by_full else next(
                    (k for k in by_full if token and k.startswith(token)), "")
                snap = by_full.get(fp)
                if snap is None:
                    continue
                if s.scalars(select(M.id).where(
                        M.workspace_id == workspace_id, M.pr_provider == pr.provider,
                        M.pr_repo == pr.repo, M.pr_number == int(pr.number),
                        M.comment_id == comment_id).limit(1)).first() is not None:
                    continue
                s.add(M(
                    workspace_id=workspace_id, pr_provider=pr.provider, pr_repo=pr.repo,
                    pr_number=int(pr.number), comment_id=comment_id, run_id=run_id,
                    finding_key=by_key.get(fp), fingerprint=fp, repo_slug=slug,
                    file_path=path or snap.file_path, line=line, title=snap.title,
                    body_excerpt=snap.body, agent=snap.agent, rule_id=snap.rule_id,
                    severity=snap.severity, category=snap.category, sha=sha or None,
                    thread_id=thread, posted_at=_utcnow()))
                written += 1
            s.commit()
        return written
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_record_posted_failed ws=%s err_type=%s err=%s",
                       workspace_id, type(exc).__name__, str(exc)[:200])
        return 0


def find_posted(
    workspace_id: str, pr: PRRef, comment_id: str, *, session: Any = None,
) -> dict | None:
    """The finding a provider comment (or the thread it opened) was posted
    as, or None."""
    try:
        from sqlalchemy import or_, select

        from src.db.models import PostedFindingComment as M

        with _session(session) as s:
            row = s.scalars(select(M).where(
                M.workspace_id == workspace_id, M.pr_provider == pr.provider,
                M.pr_repo == pr.repo, M.pr_number == int(pr.number),
                or_(M.comment_id == str(comment_id), M.thread_id == str(comment_id))
            ).order_by(M.posted_at.desc()).limit(1)).first()
            return _posted_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_find_posted_failed err_type=%s", type(exc).__name__)
        return None


def find_posted_by_fingerprint(
    workspace_id: str, pr: PRRef, token: str, *, session: Any = None,
) -> dict | None:
    """A finding of this PR by the 16-hex token of its comment marker — the
    way back when the comment-id mapping was lost."""
    token = (token or "").lower()
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        return None
    try:
        from sqlalchemy import select

        from src.db.models import PostedFindingComment as M

        with _session(session) as s:
            row = s.scalars(select(M).where(
                M.workspace_id == workspace_id, M.pr_provider == pr.provider,
                M.pr_repo == pr.repo, M.pr_number == int(pr.number),
                M.fingerprint.like(f"{token}%")).order_by(M.posted_at.desc()).limit(1)).first()
            return _posted_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_find_posted_failed err_type=%s", type(exc).__name__)
        return None


def posted_for_pr(workspace_id: str, pr: PRRef, *, session: Any = None) -> list[dict]:
    """Every posted finding comment of one PR (for reaction polling)."""
    try:
        from sqlalchemy import select

        from src.db.models import PostedFindingComment as M

        with _session(session) as s:
            rows = s.scalars(select(M).where(
                M.workspace_id == workspace_id, M.pr_provider == pr.provider,
                M.pr_repo == pr.repo, M.pr_number == int(pr.number)).order_by(
                M.posted_at.desc()).limit(MAX_POSTED_PER_RUN)).all()
            return [_posted_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_posted_list_failed err_type=%s", type(exc).__name__)
        return []


def _posted_dict(row: Any) -> dict:
    return {
        "comment_id": row.comment_id, "fingerprint": row.fingerprint,
        "repo_slug": row.repo_slug, "file_path": row.file_path, "line": row.line,
        "title": row.title, "body": row.body_excerpt, "agent": row.agent,
        "rule_id": row.rule_id, "severity": row.severity, "category": row.category,
        "run_id": row.run_id, "sha": row.sha, "finding_key": row.finding_key,
        "pr_provider": row.pr_provider, "pr_repo": row.pr_repo,
        "pr_number": int(row.pr_number),
    }


def snapshot_of_posted(posted: dict) -> FindingSnapshot:
    return FindingSnapshot(
        title=posted.get("title") or "", file_path=posted.get("file_path") or "",
        body=posted.get("body") or "", rule_id=posted.get("rule_id") or None,
        agent=posted.get("agent"), severity=posted.get("severity"),
        category=posted.get("category"), fingerprint=posted.get("fingerprint") or "",
        line=posted.get("line"),
    ).complete()


# ─── Outcomes (issues ledger) ────────────────────────────────────────

_OUTCOME_SIGNAL = {"implemented": "implemented", "ignored": "ignored"}


def record_outcome(
    workspace_id: str, repo_slug: str, snap: FindingSnapshot, outcome: str, *,
    pr: PRRef | None, sha: str | None = None, source: str = "auto_next_commit",
    session: Any = None,
) -> str:
    """What became of a suggestion: `implemented` (acts as accepted) or
    `ignored` (weak, feeds only the rules job). A later outcome of the same
    suggestion replaces the earlier one the code wrote (a merge freezes an
    open issue as ignored, and the check made at that merge can still find
    the fix on the branch). Idempotent. Never raises."""
    signal = _OUTCOME_SIGNAL.get(outcome)
    if signal is None or source not in AUTO_SOURCES:
        return "invalid"
    try:
        from sqlalchemy import delete

        from src.db.models import FindingSignal as M

        full = snap.complete()
        with _session(session) as s:
            s.execute(delete(M).where(
                *_key_filter(M, workspace_id, pr, full.fingerprint),
                M.source.in_(AUTO_SOURCES), M.signal.in_(("implemented", "ignored"))))
            s.commit()
        return record_signal(
            workspace_id, repo_slug, full, signal, source, pr=pr, sha=sha, session=session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_outcome_failed ws=%s err_type=%s", workspace_id,
                       type(exc).__name__)
        return "error"


def _issue_snapshot(issue: Any) -> FindingSnapshot:
    return FindingSnapshot(
        title=issue.title or "", file_path=issue.file_path or "", body=issue.body or "",
        rule_id=issue.rule_id or None, agent=issue.agent or None,
        severity=issue.severity or None, category=issue.category or None,
        fingerprint=issue.fingerprint, line=issue.line)


def on_issue_outcome(outcome: Any) -> None:
    """The issues ledger's listener (`outcome_hooks.register_outcome_listener`).

      implemented / resolved_later  -> an `implemented` signal (the thread, if
                                       it was only resolved, is upgraded);
      unimplemented                 -> `ignored`, and a thread resolved but
                                       never fixed becomes a weak dismissal;
      reopened                      -> the automatic signals of the fix are
                                       withdrawn;
      dismissed / abandoned         -> nothing here (a dismissal teaches
                                       through the verdict that caused it).
    """
    try:
        kind = getattr(outcome, "kind", "")
        if kind not in ("implemented", "resolved_later", "unimplemented", "reopened"):
            return
        from sqlalchemy import delete

        from src.db.models import FindingSignal as M
        from src.db.models import ReviewIssue

        ws = outcome.workspace_id
        pr = PRRef(outcome.pr_provider, outcome.pr_repo, int(outcome.pr_number))
        with _session() as s:
            issue = s.get(ReviewIssue, outcome.issue_id)
            if issue is None or issue.workspace_id != ws:
                return
            snap = _issue_snapshot(issue)
            slug = issue.repo_slug
            if kind == "reopened":
                s.execute(delete(M).where(
                    *_key_filter(M, ws, pr, snap.fingerprint), M.source.in_(AUTO_SOURCES)))
                s.commit()
                return
            if kind == "unimplemented":
                _upgrade_resolved(s, ws, pr, snap.fingerprint, to="dismissed")
            else:
                _upgrade_resolved(s, ws, pr, snap.fingerprint, to=None)
            s.commit()
        sha = getattr(outcome, "fixed_in_sha", None)
        if kind == "unimplemented":
            record_outcome(ws, slug, snap, "ignored", pr=pr, sha=sha, source="auto_merge")
        else:
            record_outcome(ws, slug, snap, "implemented", pr=pr, sha=sha,
                           source="auto_next_commit")
    except Exception as exc:  # noqa: BLE001 — a listener never breaks the ledger
        logger.warning("learning_outcome_listener_failed err_type=%s", type(exc).__name__)


def _upgrade_resolved(s: Any, ws: str, pr: PRRef, fingerprint: str, *, to: str | None) -> None:
    """Resolved-thread signals of one (PR, finding): `to=None` drops them (the
    fix itself is recorded instead), `to="dismissed"` keeps them as weak
    dismissals — the thread was closed and the code never changed."""
    from sqlalchemy import select

    from src.db.models import FindingSignal as M

    rows = s.scalars(select(M).where(
        *_key_filter(M, ws, pr, fingerprint), M.signal == "resolved")).all()
    for row in rows:
        if to is None:
            s.delete(row)
        else:
            row.signal = to
            row.weight = WEIGHTS["resolved"]


def register_listener() -> None:
    """Hear the issues ledger. Idempotent."""
    from src.review.outcome_hooks import register_outcome_listener

    register_outcome_listener(on_issue_outcome)


register_listener()


# ─── Reads ───────────────────────────────────────────────────────────


def implementation_rate(
    workspace_id: str, repo_slug: str | None = None, since: datetime | None = None,
    *, visible: Sequence[str] | None = None, session: Any = None,
) -> dict:
    """{implemented, ignored, total, rate}: how many suggestions of pull
    requests that merged since `since` were taken. `ignored` are the ones the
    merge left unimplemented; `rate` is None when nothing was decided yet.

    Not a tally of this store's own: it is `issues.implementation_stats` over
    the ledger's frozen `close_outcome`, the one definition the issues page
    and the productivity metrics share. `visible` limits it to those repository
    slugs (a reader who may not see every repository).
    """
    empty = {"implemented": 0, "ignored": 0, "total": 0, "rate": None}
    try:
        from sqlalchemy import func, select

        from src.db.models import ReviewIssue as I
        from src.review.issues import implementation_stats

        stmt = select(I.close_outcome, func.count()).where(
            I.workspace_id == workspace_id, I.dup_of.is_(None),
            I.merged_at.is_not(None), I.close_outcome.is_not(None))
        if repo_slug:
            stmt = stmt.where(I.repo_slug == repo_slug)
        elif visible is not None:
            stmt = stmt.where(I.repo_slug.in_(list(visible)))
        if since is not None:
            stmt = stmt.where(I.merged_at >= since)
        with _session(session) as s:
            counts = s.execute(stmt.group_by(I.close_outcome)).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_rate_failed err_type=%s", type(exc).__name__)
        return empty
    stats = implementation_stats(
        {"close_outcome": o} for o, n in counts for _ in range(int(n)))
    done, ignored = stats["implemented"], stats["unimplemented"]
    total = done + ignored
    return {"implemented": done, "ignored": ignored, "total": total,
            "rate": round(done / total, 4) if total else None}


def window_start(days: int) -> datetime:
    return _utcnow() - timedelta(days=max(1, int(days)))


def signal_to_dict(row: Any, *, show_actor: bool = False) -> dict:
    return {
        "id": row.id, "repo_slug": row.repo_slug, "fingerprint": row.fingerprint,
        "file_path": row.file_path, "title": row.title, "rule_id": row.rule_id,
        "agent": row.agent, "severity": row.severity, "category": row.category,
        "signal": row.signal, "source": row.source, "weight": float(row.weight),
        "reason": row.reason, "actor": row.actor if show_actor else None,
        "pr_provider": row.pr_provider, "pr_repo": row.pr_repo,
        "pr_number": int(row.pr_number),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def forget_signal(workspace_id: str, signal_id: str, *, session: Any = None) -> dict | None:
    """Remove one signal (and its vector); the removed row as a dict, or None
    when this workspace has no such signal. Never raises on the vector side."""
    from sqlalchemy import select

    from src.db.models import FindingSignal as M

    with _session(session) as s:
        row = s.scalars(select(M).where(
            M.id == signal_id, M.workspace_id == workspace_id).limit(1)).first()
        if row is None:
            return None
        out = signal_to_dict(row)
        out["embedded"] = bool(row.embedded)
        s.delete(row)
        s.commit()
    if out["embedded"]:
        try:
            from src.review.learning import similarity

            similarity.delete_vectors([signal_id])
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning_forget_vector_failed err_type=%s", type(exc).__name__)
    return out
