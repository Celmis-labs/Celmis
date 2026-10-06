"""Thumbs on our comments are feedback too.

Providers do not tell us when somebody reacts, so reactions are READ, on the
two occasions the learning code is already awake: a new review of the same
pull request, and the merge. `poll_pr_reactions` goes over the posted finding
comments of one PR, asks the provider for the thumbs on each, and turns them
into signals:

    thumbs-down only   a `dismissed` signal (source reaction)
    thumbs-up only     an `accepted` signal
    both               nothing (the person has not made up their mind)

A person who took their reaction back has their reaction signal withdrawn.
Providers without reactions (Bitbucket) raise and are skipped; the poll never
raises and costs one request per posted comment, capped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from src.review.learning import signals as sig

logger = logging.getLogger(__name__)

#: One poll reads at most this many comments (a PR of hundreds of findings is
#: not worth hundreds of requests on every push).
MAX_COMMENTS = 40


def classify_reactions(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """{user: "dismissed" | "accepted"} from (user, "up" | "down") pairs.
    A user with both directions is left out. Pure."""
    seen: dict[str, set[str]] = {}
    for user, direction in pairs:
        who = sig.normalise_actor(user)
        if who and direction in ("up", "down"):
            seen.setdefault(who, set()).add(direction)
    out: dict[str, str] = {}
    for who, dirs in seen.items():
        if dirs == {"down"}:
            out[who] = "dismissed"
        elif dirs == {"up"}:
            out[who] = "accepted"
    return out


def _previous_reactors(ws: str, pr: sig.PRRef, fingerprint: str, session: Any) -> set[str]:
    from sqlalchemy import select

    from src.db.models import FindingSignal as M

    with sig._session(session) as s:
        rows = s.scalars(select(M.actor).where(
            *sig._key_filter(M, ws, pr, fingerprint), M.source == "reaction",
            M.signal.in_(("dismissed", "accepted")))).all()
        return {r for r in rows if r}


#: A provider that fails this many reads in a row (and has answered none) has no
#: reactions to read; one failed comment (a deleted one, a 404) does not mean that.
MAX_LEADING_FAILURES = 3


def _permission_check(pr: sig.PRRef, provider: Any) -> Callable[[str], bool]:
    """Who may teach the reviewer by a thumb: the same `command_permission` rule
    as for a reply. Unreadable settings fall back to the built-in rule."""
    from src.review.commands import gate

    try:
        from src.review.review_defaults import command_settings_for_repo

        mode = str(command_settings_for_repo(pr.provider, pr.repo).get("command_permission"))
    except Exception:  # noqa: BLE001
        mode = gate.REPO_ACCESS
    return gate.teaching_check(provider, repo=pr.repo, pr_number=int(pr.number), mode=mode)


def poll_pr_reactions(
    workspace_id: str, pr: sig.PRRef, provider: Any, *, skip_users: Iterable[str] = (),
    session: Any = None, permit: Callable[[str], bool] | None = None,
) -> dict:
    """Read the thumbs on this PR's finding comments and record them. Returns
    {checked, recorded, withdrawn, skipped}. Never raises.

    Signals are keyed by (PR, finding) while comments are keyed by comment: one
    finding can have several comments on a PR (an incremental re-review posts it
    again). The thumbs of all of a finding's comments are therefore read
    together, and a person's reaction is withdrawn only when none of the
    comments carries it any more (and every one of them could be read).

    A thumb counts only from somebody who may teach the reviewer here (`permit`,
    by default the repository's `command_permission` rule): on a public
    repository anyone can react, and one stranger's thumbs-down must not hide a
    finding."""
    result = {"checked": 0, "recorded": 0, "withdrawn": 0, "skipped": ""}
    try:
        posted = sig.posted_for_pr(workspace_id, pr, session=session)[:MAX_COMMENTS]
        skip = {sig.normalise_actor(u) for u in skip_users}
        # fingerprint -> [(row, pairs | None)] — None when the read failed.
        by_finding: dict[str, list[tuple[dict, list | None]]] = {}
        leading_failures = 0
        for row in posted:
            try:
                pairs: list | None = list(provider.list_comment_reactions(
                    pr.repo, pr.number, row["comment_id"]))
            except Exception as exc:  # noqa: BLE001 — no reactions here, or a failed read
                result["skipped"] = type(exc).__name__
                pairs = None
                if result["checked"] == 0:
                    leading_failures += 1
                    if leading_failures >= MAX_LEADING_FAILURES:
                        break
            else:
                result["checked"] += 1
            fingerprint = sig.snapshot_of_posted(row).fingerprint
            by_finding.setdefault(fingerprint, []).append((row, pairs))
        permit = permit or _permission_check(pr, provider)
        for fingerprint, comments in by_finding.items():
            _apply_finding(workspace_id, pr, fingerprint, comments, skip, session, result,
                           permit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_reaction_poll_failed ws=%s err_type=%s", workspace_id,
                       type(exc).__name__)
        result["skipped"] = type(exc).__name__
    return result


def _apply_finding(
    workspace_id: str, pr: sig.PRRef, fingerprint: str,
    comments: list[tuple[dict, list | None]], skip: set[str], session: Any, result: dict,
    permit: Callable[[str], bool],
) -> None:
    readable = [(row, pairs) for row, pairs in comments if pairs is not None]
    if not readable:
        return
    verdicts = {u: v for u, v in classify_reactions(
        [p for _, pairs in readable for p in pairs]).items() if u not in skip}
    row = readable[0][0]
    snap = sig.snapshot_of_posted(row)
    repo_slug = row.get("repo_slug") or sig.local_slug(pr.provider, pr.repo)
    excluded = sig.excluded_reviewers_sync(workspace_id, repo_slug, session)
    for user, state in verdicts.items():
        if sig.is_excluded([user], excluded):
            continue
        if not permit(user):
            continue
        # The comment the person reacted on (the newest one that carries it).
        home = next((r for r, pairs in readable
                     if any(sig.normalise_actor(u) == user for u, _ in pairs)), row)
        wrote = sig.record_verdict(
            workspace_id, repo_slug, snap, state, "reaction", pr=pr, actor=user,
            reason="false_positive" if state == "dismissed" else "",
            comment_id=str(home["comment_id"]), run_id=home.get("run_id"),
            sha=home.get("sha"), session=session)
        if wrote == "created":
            result["recorded"] += 1
    if len(readable) < len(comments):
        return  # a comment could not be read: do not take anybody's thumb back
    # `verdicts` still holds a person the permission check refused (or could
    # not read this time): what they gave before stays, an unreadable answer
    # is not a reason to take it back.
    for gone in _previous_reactors(workspace_id, pr, fingerprint, session) - set(verdicts):
        result["withdrawn"] += sig.clear_verdict(
            workspace_id, fingerprint, "reaction", pr=pr, actor=gone, session=session)
