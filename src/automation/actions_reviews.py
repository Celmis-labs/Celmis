"""Reviews, issues, indexing and questions about the code — as verbs.

The in-app agent and MCP could configure reviews and read dependency audits,
but could not do the daily work: start a review of a pull request, see how
the last ones went, read what a run found, close an issue, re-index a
repository, ask a question of the code or find out who owns a file. Each of
those already had a route (or several); this module is the one implementation
both surfaces call, so a gate added to the page cannot be missing from the
agent.

Nothing here has rules of its own:

  * the ROUTE functions are called as they are for reads (`reviews.history`,
    `reviews.get_findings`, `issues.list_issues`, `search.search`) — same
    queries, same tenancy, same words in a refusal;
  * the three writes whose logic used to live inside a route
    (`queue_pr_review(s)`, `open_pr_targets`, `apply_issue_status`) were
    extracted into functions here and the routes now call them, so there is
    one implementation per verb (tests/automation/
    test_one_implementation_per_verb.py);
  * the gates are the routes': `review` on the repository for starting a
    review, a member role for changing an issue, `read` for reading, and the
    research-access resolver for anything that shows code.

Separate from actions.py only so that two sets of verbs can grow at the same
time without touching the same file; it imports from there and nothing in
actions.py imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.automation.actions import (
    PROPOSER_ROLES,
    ActionError,
    Actor,
    _as_action,
    _clip,
    _require_repo_review,
    _user_for,
    resolve_repo,
)

logger = logging.getLogger(__name__)

#: The verbs this module adds, for the planner's catalogue and the tests.
READ_VERBS = ("list_reviews", "get_review_run", "list_issues", "ask_code",
              "search_code")
WRITE_VERBS = ("review_pr", "index_repo", "update_issue")

#: Bounds on what travels to a model prompt or back to an MCP caller.
MAX_LIST = 25
MAX_FINDINGS = 40
MAX_ISSUE_IDS = 25
MAX_INDEX_REPOS = 50
#: Repositories one question may span when none are named. Retrieval cost
#: grows with them, and "ask everything" over fifty repositories is not a
#: question anybody can verify the answer to.
MAX_ASK_REPOS = 8
MAX_ANSWER_CHARS = 6000
MAX_QUESTION_CHARS = 2000
ASK_TIMEOUT_S = 150

ISSUE_STATUSES = ("open", "fixed", "dismissed", "resolved")

#: Where each answer can be seen. The client translates the label and only
#: offers an href that is a path in this app.
_LINK_REVIEWS = {"label": "reviews", "href": "/reviews"}
_LINK_PRS = {"label": "pull_requests", "href": "/pull-requests"}
_LINK_ISSUES = {"label": "issues", "href": "/issues"}
_LINK_REPOS = {"label": "repositories", "href": "/repositories"}
_LINK_SEARCH = {"label": "search", "href": "/search"}


# ─── small helpers ───────────────────────────────────────────────────


def _registered(actor: Actor) -> dict[str, Any]:
    """slug → registration, for the actor's workspace only."""
    from src.api.auto_review import get_auto_review_store

    return {c.repo_slug: c
            for c in get_auto_review_store().list_for_workspace(actor.workspace_id)}


def _number(value: Any) -> int:
    try:
        n = int(str(value).lstrip("#").strip())
    except (TypeError, ValueError):
        raise ActionError("Say which pull request: its number, e.g. 42.") from None
    if n < 1:
        raise ActionError("A pull request number is a positive integer.")
    return n


def _limit(value: Any, default: int, cap: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _pr_url(cfg: Any, number: int | None) -> str | None:
    """The provider's page for a PR, from the registered repository URL."""
    if cfg is None or not number:
        return None
    base = (getattr(cfg, "url", "") or "").rstrip("/").removesuffix(".git")
    if not base.startswith("http"):
        return None
    provider = getattr(cfg, "provider", "")
    if provider == "gitlab":
        return f"{base}/-/merge_requests/{number}"
    if provider == "bitbucket":
        return f"{base}/pull-requests/{number}"
    return f"{base}/pull/{number}"


async def _can_read(actor: Actor, user: Any, slug: str, cache: dict[str, bool]) -> bool:
    """The team's `read` grant on one repository, remembered per call."""
    if slug in cache:
        return cache[slug]
    from src.api.deps import enforce_repo_permission

    try:
        await enforce_repo_permission(slug, user, "read", actor.workspace_id)
        cache[slug] = True
    except Exception:  # noqa: BLE001 — a repository they may not read is not named
        cache[slug] = False
    return cache[slug]


# ─── review a pull request ───────────────────────────────────────────


def queue_pr_review(
    cfg: Any, number: int, *, user_id: str, post_comments: bool, source: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Queue one review and say what happened, as the review routes do.

    Returns `(item, inline_payload)`. The run row exists before the job, so it
    shows as queued at once. `inline_payload` is set only when the queue was
    unavailable: the caller must then run it itself (`run_review_inline`) —
    a route hands it to its background tasks, the agent to a thread.
    """
    from src.review.dispatch import enqueue_review_run

    try:
        res = enqueue_review_run(
            cfg.provider, cfg.full_name, number, user_id=user_id,
            workspace_id=cfg.workspace_id, post_comments=post_comments,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("manual_review_enqueue_failed repo=%s pr=%s",
                         cfg.repo_slug, number)
        return ({"number": number, "run_id": None, "status": "failed",
                 "reason": f"Could not record the review ({type(exc).__name__})."},
                None)
    item = {"number": number, "run_id": res.run_id, "status": res.status,
            "reason": res.reason}
    return item, (res.payload if res.status == "inline" else None)


def run_review_inline(payload: dict[str, Any]) -> None:
    """The queue is unavailable: run the review here, as the worker would."""
    from src.review.dispatch import execute_review

    try:
        execute_review(payload)
    except Exception:  # noqa: BLE001 — recorded on the run already
        logger.exception("manual_review_inline_failed run=%s", payload.get("run_id"))


def open_pr_targets(
    cfg: Any, user: Any, *, branch: str | None, q: str,
    numbers: list[int] | None,
) -> tuple[list[int], list[int]]:
    """(targets, untargeted): the open PR numbers a bulk review covers — the
    listed `numbers` that are still open, or every open PR matching `q` and
    `branch` — split by the repo's target branches. Fresh from the provider
    (this spends model budget on what it reads), at most `BULK_LIMIT`
    targets. Raises HTTPException exactly as the route did.

    A PR whose base branch the target patterns leave out is not queued: the
    orchestrator's gate would only skip it, writing a "skipped" run and
    using up the bulk limit. The gate stays the authority for anything this
    misses (an unknown base branch is let through)."""
    from fastapi import HTTPException

    from src.api.routers import repos as repos_router
    from src.repos import open_pulls
    from src.review.dispatch import BULK_LIMIT

    secret, email, gitlab = repos_router._repo_credential(cfg, user)
    listing = repos_router._open_listing(
        cfg, secret, email, branch=branch, refresh=True, gitlab=gitlab)
    from src.review.review_defaults import target_branches_for_repo

    if numbers:
        wanted_numbers = {int(n) for n in numbers}
        wanted = sorted((p for p in listing.items if p.number in wanted_numbers),
                        key=lambda p: p.number)
    else:
        wanted = list(open_pulls.select(listing.items, q=q, target=branch or None))
    patterns = target_branches_for_repo(cfg.provider, cfg.full_name)
    targets: list[int] = []
    untargeted: list[int] = []
    for pr in wanted:
        if repos_router._is_targeted(pr.target_branch, patterns):
            targets.append(pr.number)
        else:
            untargeted.append(pr.number)
    if len(targets) > BULK_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(f"{len(targets)} open pull requests match; at most "
                    f"{BULK_LIMIT} can be reviewed in one request — narrow the "
                    f"search or the target-branch filter."),
        )
    return targets, untargeted


def queue_pr_reviews(
    cfg: Any, targets: list[int], *, user_id: str, post_comments: bool,
    source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`queue_pr_review` over a list: (items, inline payloads)."""
    items: list[dict[str, Any]] = []
    inline: list[dict[str, Any]] = []
    for n in targets:
        item, payload = queue_pr_review(
            cfg, n, user_id=user_id, post_comments=post_comments, source=source)
        items.append(item)
        if payload is not None:
            inline.append(payload)
    return items, inline


def audit_bulk_review(
    user: Any, workspace_id: str, slug: str, *, requested: int, queued: int,
    branch: str | None, q: str | None, ip: str | None, via: str = "",
    skipped_not_targeted: int = 0,
) -> None:
    """The audit-log entry of a bulk review — one for the page and the agent."""
    from src.security.audit import record_action

    detail: dict[str, Any] = {"requested": requested, "queued": queued,
                              "branch": branch or "", "q": bool(q)}
    if via:
        detail["via"] = via
    if skipped_not_targeted:
        detail["skipped_not_targeted"] = skipped_not_targeted
    record_action(
        action="review.bulk_queued", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=slug, ip=ip, detail=detail)


#: Strong references to the inline reviews started from an action. A task
#: nobody holds can be collected mid-run.
_INLINE_TASKS: set[asyncio.Task] = set()


def _spawn_inline(payloads: list[dict[str, Any]]) -> None:
    for payload in payloads:
        task = asyncio.ensure_future(asyncio.to_thread(run_review_inline, payload))
        _INLINE_TASKS.add(task)
        task.add_done_callback(_INLINE_TASKS.discard)


async def review_pr(
    actor: Actor, session: Any = None, *, repo_slug: str | None,
    number: Any = None, all_open: bool = False, branch: str | None = None,
    q: str = "", numbers: list[int] | None = None, post_comments: bool = True,
) -> dict[str, Any]:
    """Queue a review of one pull request, or of every open one of a repository.

    The same two routes the repository page's buttons call
    (`/api/repos/{slug}/pulls/{n}/review` and `.../review-all`): the run rows
    are created first, the work goes to the queue, `review` on the repository
    is required, and a bulk request is capped at `BULK_LIMIT`. Works for
    GitHub, GitLab and Bitbucket alike — the provider is the registration's.
    With `post_comments` true (the page's default) the findings are posted on
    the pull request; false reviews without posting.
    """
    from fastapi import HTTPException

    user = _user_for(actor)
    slug = resolve_repo(actor, repo_slug)
    await _require_repo_review(actor, user, slug)
    cfg = _registered(actor)[slug]

    if all_open or numbers:
        try:
            targets, untargeted = await asyncio.to_thread(
                open_pr_targets, cfg, user, branch=(branch or None),
                q=str(q or ""), numbers=[_number(n) for n in numbers or []] or None)
        except HTTPException as exc:
            raise ActionError(str(exc.detail)) from None
        source = "bulk"
        not_targeted = [{"repo": f"{slug}#{n}", "reason": "base branch not targeted"}
                        for n in untargeted]
        if not targets:
            return {"repo": slug, "provider": cfg.provider, "requested": 0,
                    "queued": [], "skipped": not_targeted, "items": [],
                    "links": [_LINK_PRS, _LINK_REVIEWS]}
    else:
        targets = [_number(number)]
        source = "manual"
        not_targeted = []

    items, inline = await asyncio.to_thread(
        queue_pr_reviews, cfg, targets, user_id=user.id,
        post_comments=bool(post_comments), source=source)
    _spawn_inline(inline)

    queued = [{"repo": f"{slug}#{i['number']}", "job_id": i["run_id"],
               "run_id": i["run_id"], "number": i["number"]}
              for i in items if i["status"] in ("queued", "inline")]
    skipped = [{"repo": f"{slug}#{i['number']}",
                "reason": i.get("reason") or i["status"]}
               for i in items if i["status"] not in ("queued", "inline")]
    skipped += not_targeted
    if source == "bulk":
        audit_bulk_review(
            user, actor.workspace_id, slug, requested=len(targets),
            queued=len(queued), branch=branch, q=q, ip=None, via=actor.label,
            skipped_not_targeted=len(not_targeted))
    logger.info("review_pr via=%s repo=%s requested=%d queued=%d by=%s",
                actor.label, slug, len(targets), len(queued), actor.email)
    return {
        "repo": slug, "provider": cfg.provider, "requested": len(targets),
        "queued": queued, "skipped": skipped, "items": items,
        "run_id": queued[0]["run_id"] if queued else None,
        "pr_urls": {str(n): _pr_url(cfg, n) for n in targets[:MAX_LIST]},
        "links": [_LINK_REVIEWS, _LINK_PRS],
    }


# ─── reading review runs ─────────────────────────────────────────────


def _run_row(out: Any, cfg: Any | None) -> dict[str, Any]:
    """One run as a compact row. `out` is a `ReviewRunOut`."""
    number = getattr(out, "pr_number", None)
    return {
        "run_id": out.id,
        "pr_ref": out.pr_ref,
        "provider": getattr(out, "pr_provider", None),
        "repo": getattr(out, "pr_repo", None),
        "repo_slug": getattr(cfg, "repo_slug", None),
        "pr_number": number,
        "pr_url": _pr_url(cfg, number),
        "status": out.status,
        "verdict": out.verdict,
        "findings": {"total": out.findings_count, "critical": out.critical,
                     "error": out.error, "warning": out.warning,
                     "info": out.info},
        "posted": out.posted,
        "started_at": out.started_at,
        "elapsed_seconds": out.elapsed_seconds,
        "summary": _clip(out.summary or ""),
        "reason": getattr(out, "status_reason", None),
    }


def _by_full_name(actor: Actor) -> dict[tuple[str, str], Any]:
    return {(c.provider, c.full_name): c for c in _registered(actor).values()}


async def list_reviews(
    actor: Actor, session: Any = None, *, repo_slug: str | None = None,
    status: str | None = None, limit: Any = 10,
) -> dict[str, Any]:
    """The newest review runs of one repository or of the workspace.

    Reads `reviews.history` — the page's own list — and narrows it. Runs of a
    repository the caller's team may not read are not named.
    """
    from src.api.routers import reviews as reviews_router

    user = _user_for(actor)
    wanted: str | None = None
    if repo_slug and str(repo_slug).strip():
        wanted = resolve_repo(actor, repo_slug)
        if not await _can_read(actor, user, wanted, {}):
            raise ActionError(f"Requires 'read' on {wanted}")
    n = _limit(limit, 10, MAX_LIST)
    runs = await asyncio.to_thread(
        reviews_router.history, 200, user, actor.workspace_id)
    by_name = _by_full_name(actor)
    allowed: dict[str, bool] = {}
    rows: list[dict[str, Any]] = []
    for out in runs:
        cfg = by_name.get((getattr(out, "pr_provider", None),
                           getattr(out, "pr_repo", None)))
        if wanted and (cfg is None or cfg.repo_slug != wanted):
            continue
        if status and out.status != status and out.verdict != status:
            continue
        if cfg is not None and not await _can_read(actor, user, cfg.repo_slug, allowed):
            continue
        rows.append(_run_row(out, cfg))
        if len(rows) >= n:
            break
    return {"scope": "repo" if wanted else "workspace", "repo": wanted,
            "runs": rows, "count": len(rows),
            "links": [_LINK_REVIEWS, _LINK_PRS]}


async def get_review_run(
    actor: Actor, session: Any = None, *, run_id: str | None = None,
    repo_slug: str | None = None, number: Any = None, limit: Any = 20,
) -> dict[str, Any]:
    """One run's summary and its findings, bounded.

    By run id, or by repository + PR number for the latest run of that pull
    request. `reviews.get_run` / `get_findings` are called as they are, so the
    tenancy rule (a run of another workspace is "not found") is theirs.
    """
    from fastapi import HTTPException

    from src.api.review_runs import get_review_run_store
    from src.api.routers import reviews as reviews_router

    user = _user_for(actor)
    ws = actor.workspace_id
    cfgs = _registered(actor)
    if not run_id:
        slug = resolve_repo(actor, repo_slug)
        cfg = cfgs[slug]
        n = _number(number)
        found = await asyncio.to_thread(
            lambda: get_review_run_store().list_for_pr(
                ws, cfg.provider, cfg.full_name, n, user_id=user.id, limit=1))
        if not found:
            raise ActionError(f"No review of {cfg.full_name}#{n} yet.")
        run_id = found[0].id

    try:
        out = await asyncio.to_thread(reviews_router.get_run, run_id, user, ws)
        page = await asyncio.to_thread(
            reviews_router.get_findings, run_id, _limit(limit, 20, MAX_FINDINGS),
            0, user, ws)
    except HTTPException as exc:
        raise ActionError(str(exc.detail)) from None

    by_name = _by_full_name(actor)
    cfg = by_name.get((out.pr_provider, out.pr_repo))
    if cfg is not None and not await _can_read(actor, user, cfg.repo_slug, {}):
        raise ActionError(f"Requires 'read' on {cfg.repo_slug}")
    findings = [{
        "id": f.get("id"), "severity": f.get("severity"),
        "file": f.get("file_path"), "line": f.get("line"),
        "title": _clip(str(f.get("title") or "")),
        "agent": f.get("agent"), "rule": f.get("rule_id"),
        "body": _clip(str(f.get("body") or "")),
    } for f in (page.get("findings") or [])[:MAX_FINDINGS]]
    return {
        **_run_row(out, cfg),
        "agents_run": out.agents_run, "agents_failed": out.agents_failed,
        "findings_list": findings,
        "findings_shown": len(findings),
        "findings_total": page.get("total", len(findings)),
        "truncated": page.get("total", 0) > len(findings),
        "links": [_LINK_REVIEWS, _LINK_PRS, _LINK_ISSUES],
    }


# ─── indexing ────────────────────────────────────────────────────────


async def index_repo(
    actor: Actor, session: Any = None, *, repo_slugs: list[str] | None = None,
    owner: str | None = None, force: bool | None = None,
) -> dict[str, Any]:
    """Queue a (re-)index of named repositories, or of the whole workspace.

    Queued like "Index all" — a clone and a parse take a minute a repository —
    with the SAME dedup key registration and the button use, so pressing it
    while an index is pending is a skip, never a second clone. Naming
    repositories means "do it again": they are re-indexed even if a graph
    exists; with none named, repositories that already have one are left alone
    unless `force`. `review` on each repository is required.
    """
    from src.config import get_settings, is_valid_repo_slug
    from src.repos.indexing import index_dedup_key
    from src.sync.queue import KIND_INDEX_REPO_FULL, enqueue

    user = _user_for(actor)
    registered = _registered(actor)
    named = [s for s in dict.fromkeys(repo_slugs or []) if str(s).strip()]
    if named:
        slugs = [resolve_repo(actor, s) for s in named]
        if force is None:
            force = True
    else:
        slugs = sorted(registered)
        if owner and str(owner).strip():
            prefix = str(owner).strip().rstrip("/") + "/"
            slugs = [s for s in slugs if registered[s].full_name.startswith(prefix)]
        force = bool(force)
    if not slugs:
        raise ActionError("Nothing matched — no repositories in scope.")
    if len(slugs) > MAX_INDEX_REPOS:
        raise ActionError(
            f"That is {len(slugs)} repositories; at most {MAX_INDEX_REPOS} can "
            f"be queued at once. Narrow it down, or run it in batches.")

    settings = get_settings()
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for slug in slugs:
        try:
            await _require_repo_review(actor, user, slug)
        except ActionError as exc:
            skipped.append({"repo": slug, "reason": str(exc)})
            continue
        if not is_valid_repo_slug(slug):
            skipped.append({"repo": slug, "reason": "not a valid repository slug"})
            continue
        if not force and settings.repo_graph_path(slug).exists():
            skipped.append({"repo": slug, "reason": "already indexed"})
            continue
        job_id = await asyncio.to_thread(
            enqueue, kind=KIND_INDEX_REPO_FULL,
            payload={"repo_slug": slug, "workspace_id": actor.workspace_id,
                     "user_id": user.id},
            dedup_key=index_dedup_key(slug, actor.workspace_id),
            enqueued_by=actor.email)
        if job_id is None:
            skipped.append({"repo": slug, "reason": "an index is already queued or running"})
        else:
            queued.append({"repo": slug, "job_id": job_id})
    logger.info("index_repo via=%s ws=%s queued=%d skipped=%d by=%s", actor.label,
                actor.workspace_id, len(queued), len(skipped), actor.email)
    return {"queued": queued, "skipped": skipped, "forced": bool(force),
            "links": [_LINK_REPOS]}


# ─── issues ──────────────────────────────────────────────────────────


def _issue_row(i: Any) -> dict[str, Any]:
    return {
        "id": i.id, "status": i.status, "severity": i.severity,
        "category": i.category, "title": _clip(i.title),
        "repo": i.repo_slug, "file": i.file_path, "line": i.line,
        "pr_number": i.pr_number, "pr_title": _clip(i.pr_title or ""),
        "pr_url": i.pr_url, "occurrences": i.occurrences,
        "last_seen_at": i.last_seen_at.isoformat() if i.last_seen_at else None,
    }


async def list_issues(
    actor: Actor, session: Any, *, status: str | None = "open",
    severity: str | None = None, repo_slug: str | None = None,
    pr: Any = None, q: str | None = None, limit: Any = 15,
) -> dict[str, Any]:
    """Tracked review issues, worst first, with the counts per status.

    The page's own query (`issues.list_issues`), called as it is; `repo_slug`
    narrows it and needs the team's `read` grant on that repository.
    """
    from src.api.routers import issues as issues_router

    user = _user_for(actor)
    repo = None
    if repo_slug and str(repo_slug).strip():
        repo = resolve_repo(actor, repo_slug)
        if not await _can_read(actor, user, repo, {}):
            raise ActionError(f"Requires 'read' on {repo}")
    # Workspace-wide, the page lists every repository's issues; the agent
    # leaves out the ones whose repository the asker's team grants exclude, as
    # it does for review runs (`list_reviews`).
    hidden: list[str] = []
    if repo is None:
        allowed: dict[str, bool] = {}
        for slug, cfg in _registered(actor).items():
            if not await _can_read(actor, user, slug, allowed):
                hidden += [slug, cfg.full_name]
    status_csv = ",".join(s for s in (status or "").split(",")
                          if s.strip() in ISSUE_STATUSES) or None
    out = await _as_action(issues_router.list_issues(
        status=status_csv, severity=severity or None, category=None, repo=repo,
        exclude_repo=hidden or None,
        pr=_number(pr) if pr not in (None, "") else None,
        scope=None, resolution=None, outcome=None, include_duplicates=False,
        q=q or None, sort="severity", limit=_limit(limit, 15, MAX_LIST),
        offset=0, session=session, _user=user, ws=actor.workspace_id))
    return {
        "issues": [_issue_row(i) for i in out.items],
        "count": len(out.items), "total": out.total,
        "status_counts": out.status_counts, "repo": repo,
        "links": [_LINK_ISSUES],
    }


async def apply_issue_status(
    session: Any, user: Any, ws: str, issue_id: str, status: str,
) -> tuple[Any, Any]:
    """Set an issue's status. Returns `(row, pull_request_row_or_None)`.

    The body of `PATCH /api/issues/{id}`: the member role (a global admin is
    exempt), the workspace check (another tenant's id is "not found") and the
    bookkeeping a close or a reopen carries. Raises HTTPException as the
    route did — the action turns it into a sentence.
    """
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from sqlalchemy import select

    from src.api import deps as deps_module
    from src.db.models import ReviewIssue, ReviewPullRequest

    if not user.is_admin:
        role = await asyncio.to_thread(deps_module.workspace_role, user.id, ws)
        if role not in deps_module.ISSUE_WRITE_ROLES:
            raise HTTPException(
                status_code=403,
                detail="Changing an issue requires member or above on this workspace",
            )
    row = await session.get(ReviewIssue, issue_id)
    if row is None or row.workspace_id != ws:
        raise HTTPException(status_code=404, detail="Issue not found")

    if status != row.status:
        row.status = status
        if status == "open":
            row.resolution_source = None
            row.closed_at = None
            row.fixed_in_sha = None
            # What an automatic resolution recorded goes with it.
            row.fixed_by_pr_number = None
            row.fixed_by_pr_url = None
            row.resolution_note = None
            row.last_verified_blob = None
        else:
            row.resolution_source = "manual"
            row.closed_at = datetime.now(UTC)
            if status != "fixed":
                row.fixed_in_sha = None
        if status != "open":
            # Its repeats were judged through it; with it closed by a person
            # they stand on their own (see `release_orphan_dups`).
            from sqlalchemy import update

            await session.execute(update(ReviewIssue).where(
                ReviewIssue.workspace_id == ws,
                ReviewIssue.dup_of == row.id,
                ReviewIssue.status == "open",
                ReviewIssue.merged_at.is_not(None),
            ).values(dup_of=None).execution_options(synchronize_session=False))
        await session.commit()
        await session.refresh(row)
        logger.info("review_issue_status id=%s status=%s by=%s",
                    issue_id, status, user.email)

    pr = (await session.execute(select(ReviewPullRequest).where(
        ReviewPullRequest.workspace_id == ws,
        ReviewPullRequest.provider == row.pr_provider,
        ReviewPullRequest.repo == row.pr_repo,
        ReviewPullRequest.number == row.pr_number,
    ))).scalar_one_or_none()
    return row, pr


def issue_ids(issue_id: Any = None, ids: Any = None) -> list[str]:
    """The ids a step names, one or several, de-duplicated and bounded."""
    raw: list[Any] = []
    if issue_id:
        raw.append(issue_id)
    if isinstance(ids, (list, tuple)):
        raw.extend(ids)
    out = [str(i).strip() for i in dict.fromkeys(raw) if str(i).strip()]
    if not out:
        raise ActionError("Name the issue: its id, from the list of issues.")
    if len(out) > MAX_ISSUE_IDS:
        raise ActionError(f"At most {MAX_ISSUE_IDS} issues can be changed at once.")
    return out


def issue_status(value: Any) -> str:
    """The status a person means, in the words they use for it."""
    word = str(value or "").strip().lower()
    word = {"close": "resolved", "closed": "resolved", "done": "resolved",
            "ignore": "dismissed", "ignored": "dismissed", "wontfix": "dismissed",
            "reopen": "open", "reopened": "open"}.get(word, word)
    if word not in ISSUE_STATUSES:
        raise ActionError(
            f"Status must be one of: {', '.join(ISSUE_STATUSES)} (got {value!r}).")
    return word


async def update_issue(
    actor: Actor, session: Any, *, issue_id: Any = None, ids: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    """Change the status of one or several review issues
    (open | fixed | dismissed | resolved)."""
    from fastapi import HTTPException

    user = _user_for(actor)
    wanted = issue_status(status)
    done: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for one in issue_ids(issue_id, ids):
        try:
            row, _pr = await apply_issue_status(
                session, user, actor.workspace_id, one, wanted)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise ActionError(str(exc.detail)) from None
            failed.append({"repo": one, "reason": str(exc.detail)})
            continue
        done.append({"id": row.id, "status": row.status, "title": _clip(row.title),
                     "repo": row.repo_slug, "severity": row.severity})
    if not done and failed:
        raise ActionError(failed[0]["reason"])
    return {"updated": done, "skipped": failed, "count": len(done),
            "status": wanted, "links": [_LINK_ISSUES]}


# ─── questions about the code ────────────────────────────────────────


async def ask_code(
    actor: Actor, session: Any = None, *, question: str,
    repo_slugs: list[str] | None = None, include_code: bool = True,
) -> dict[str, Any]:
    """Answer a question about the code of one or several repositories.

    The Q&A pipeline as it is (`qa._generate_full`: retrieval over the vault
    and the graph, the caller's research access applied inside it, the model
    call booked as Q&A spend), non-streaming. The hard-stop budget gate is
    the one the streaming route runs first. Output is bounded; sources are
    the files the answer was built from.
    """
    from src.api.routers import qa as qa_router
    from src.llm.budget import BudgetExceeded
    from src.llm.budget import enforce as enforce_budget

    text = str(question or "").strip()
    if not text:
        raise ActionError("Ask a question about the code.")
    user = _user_for(actor)
    registered = _registered(actor)
    named = list(dict.fromkeys(repo_slugs or []))
    slugs = [resolve_repo(actor, s) for s in named] if named else sorted(registered)
    allowed: dict[str, bool] = {}
    slugs = [s for s in slugs if await _can_read(actor, user, s, allowed)]
    if not slugs:
        raise ActionError("No repository to ask about — name one you can read.")
    if len(slugs) > MAX_ASK_REPOS:
        raise ActionError(
            f"That is {len(slugs)} repositories; name at most {MAX_ASK_REPOS} "
            f"to ask about.")
    try:
        enforce_budget(actor.workspace_id)
    except BudgetExceeded as exc:
        raise ActionError(f"The workspace budget is used up: {exc}") from None

    try:
        answer, meta = await asyncio.wait_for(
            qa_router._generate_full(
                target_repos=slugs, question=text[:MAX_QUESTION_CHARS], history=[],
                user_id=user.id, is_admin=bool(getattr(user, "is_admin", False)),
                workspace_id=actor.workspace_id, include_code=bool(include_code)),
            timeout=ASK_TIMEOUT_S)
    except TimeoutError:
        raise ActionError("The answer took too long — narrow the question or the repositories.") from None
    except ActionError:
        raise
    except Exception as exc:  # noqa: BLE001
        from src.llm.errors import classify

        logger.exception("ask_code_failed repos=%s", slugs)
        failure = classify(exc)
        raise ActionError(f"{failure.code}: {failure.hint}" if failure.hint
                          else failure.code) from None
    truncated = len(answer) > MAX_ANSWER_CHARS
    files = [str(f) for f in (meta.get("files_read") or [])][:15]
    return {
        "question": text[:300],
        "answer": answer[:MAX_ANSWER_CHARS] + ("…" if truncated else ""),
        "truncated": truncated,
        "repos": slugs,
        "files": files,
        "files_total": len(meta.get("files_read") or []),
        "blocked_repos": list(meta.get("blocked_repos") or []),
        "links": [_LINK_SEARCH],
    }


# ─── finding things in the code ──────────────────────────────────────

SEARCH_KINDS = ("search", "usages", "owner", "architecture")


async def search_code(
    actor: Actor, session: Any = None, *, kind: str = "search",
    query: str | None = None, repo_slug: str | None = None,
    path: str | None = None, limit: Any = 15,
) -> dict[str, Any]:
    """Find a symbol or text, who uses a symbol, who owns a path, or read a
    repository's architecture summary.

    kind
      search        symbols and documentation notes matching `query`
                    (`search.search`, the search page's own function);
      usages        what calls / imports the symbol `query` in `repo_slug`;
      owner         who owns `path` in `repo_slug` (git blame + CODEOWNERS);
      architecture  the cached architecture summary of `repo_slug`.

    Every kind goes through the caller's research access: a repository that
    is hidden contributes nothing, a "metadata only" one shows no code, and a
    path a deny glob conceals is dropped.
    """
    user = _user_for(actor)
    kind = str(kind or "search").strip().lower()
    if kind not in SEARCH_KINDS:
        raise ActionError(f"kind must be one of: {', '.join(SEARCH_KINDS)}.")
    n = _limit(limit, 15, 50)
    slug = None
    if repo_slug and str(repo_slug).strip():
        slug = resolve_repo(actor, repo_slug)
        if not await _can_read(actor, user, slug, {}):
            raise ActionError(f"Requires 'read' on {slug}")

    if kind == "search":
        from src.api.routers import search as search_router

        q = str(query or "").strip()
        if len(q) < 2:
            raise ActionError("Say what to search for (at least two characters).")
        res = await asyncio.to_thread(
            search_router.search, q[:200], slug, n, user, actor.workspace_id)
        notes = [{"repo": x.get("repo_slug") or x.get("repo"),
                  "path": x.get("note_path") or x.get("path"),
                  "title": _clip(str(x.get("title") or "")),
                  "score": x.get("score")} for x in (res.get("notes") or [])[:5]]
        return {"kind": kind, "query": q, "repo": slug,
                "symbols": res.get("symbols") or [],
                "notes": notes, "count": len(res.get("symbols") or []),
                "note_count": len(notes), "links": [_LINK_SEARCH]}

    if slug is None:
        raise ActionError(f"Name the repository for kind={kind}.")
    access = await _access(user, actor.workspace_id, slug)

    if kind == "architecture":
        from src.db.models import RepoSummary

        if access is not None and not access.researchable:
            raise ActionError(f"{slug} is not open to research for you.")
        row = await session.get(RepoSummary, slug) if session is not None else None
        if row is None:
            return {"kind": kind, "repo": slug, "summary_md": "",
                    "note": "no architecture summary yet — it is built from the repository page",
                    "links": [_LINK_REPOS]}
        return {"kind": kind, "repo": slug,
                "summary_md": (row.summary_md or "")[:MAX_ANSWER_CHARS],
                "computed_at": row.computed_at.isoformat() if row.computed_at else None,
                "links": [_LINK_REPOS]}

    if kind == "owner":
        target = str(path or query or "").strip().lstrip("/")
        if not target:
            raise ActionError("Say which file or folder: path.")
        if access is not None and (not access.researchable
                                   or not access.path_visible(target)):
            raise ActionError(f"{slug} does not show {target} to you.")
        from src.ownership.builder import lookup_owner

        found = await asyncio.to_thread(lookup_owner, slug, target)
        if found is None:
            return {"kind": kind, "repo": slug, "path": target,
                    "note": "no ownership snapshot for this repository yet"}
        return {"kind": kind, "repo": slug, "path": target,
                "primary_owner": found.get("primary_owner"),
                "top_authors": (found.get("top_authors") or [])[:5],
                "codeowners": (found.get("codeowners") or [])[:10],
                "matched_via": found.get("matched_via")}

    # usages
    symbol = str(query or "").strip()
    if not symbol:
        raise ActionError("Say which symbol: query.")
    if access is not None and not access.code_visible:
        raise ActionError(f"{slug} shows no code to you.")
    from src.mcp_server import tools as graph_tools

    res = await asyncio.to_thread(
        graph_tools.find_callers, symbol, slug, 1, max(n * 2, 20))
    callers = [c for c in res.get("callers") or []
               if access is None or access.path_visible(str(c.get("file") or ""))]
    return {"kind": kind, "repo": slug, "symbol": symbol,
            "usages": [{"name": c.get("name"), "kind": c.get("kind"),
                        "file": c.get("file"), "line": c.get("start_line")}
                       for c in callers[:n]],
            "count": min(len(callers), n), "truncated": len(callers) > n,
            "links": [_LINK_SEARCH]}


async def _access(user: Any, workspace_id: str, slug: str) -> Any:
    from src.access import resolve_access

    decisions = await asyncio.to_thread(
        lambda: resolve_access(user_id=user.id,
                               is_admin=bool(getattr(user, "is_admin", False)),
                               workspace_id=workspace_id, repos=[slug]))
    return decisions.get(slug)


# ─── what the planner needs to say before the press ──────────────────


def plan_step(actor: Actor, action: str, args: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any], list[str]]:
    """Check a write step of this module and say exactly what it will do.

    Returns `(normalised arguments, preview, repository slugs)`; raises
    ActionError with the sentence the card shows. Only what needs no database
    — the action itself re-checks everything when it runs and stays the
    authority.
    """
    if action == "review_pr":
        slug = resolve_repo(actor, args.get("repo_slug"))
        numbers = [_number(n) for n in (args.get("numbers") or [])]
        all_open = bool(args.get("all_open")) or bool(numbers)
        number = None if all_open else _number(args.get("number"))
        norm = {
            "repo_slug": slug, "number": number, "all_open": all_open,
            "numbers": numbers or None, "branch": (args.get("branch") or None),
            "q": str(args.get("q") or ""),
            "post_comments": args.get("post_comments") is not False,
        }
        return norm, {"kind": "review_pr", "repo": slug, "number": number,
                      "all_open": all_open, "numbers": numbers,
                      "branch": norm["branch"],
                      "post_comments": norm["post_comments"]}, [slug]
    if action == "update_issue":
        ids = issue_ids(args.get("issue_id"), args.get("issue_ids") or args.get("ids"))
        wanted = issue_status(args.get("status"))
        slug = (resolve_repo(actor, args["repo_slug"])
                if str(args.get("repo_slug") or "").strip() else None)
        return ({"issue_ids": ids, "status": wanted, "repo_slug": slug},
                {"kind": "issue", "ids": ids, "status": wanted, "repo": slug},
                [slug] if slug else [])
    raise ActionError(f"No such step: {action}")


def roles_for(action: str) -> frozenset[str]:
    """The workspace roles the verb's route admits."""
    if action == "update_issue":
        from src.api.deps import ISSUE_WRITE_ROLES

        return ISSUE_WRITE_ROLES
    if action == "review_pr":
        # The route asks for the `review` grant on the repository and for no
        # workspace role at all, so a viewer a team lets review gets the same
        # card, and the same press, as anybody else. The grant is the
        # action's to check.
        from src.users.roles import VALID_WORKSPACE_ROLES

        return frozenset(VALID_WORKSPACE_ROLES)
    return PROPOSER_ROLES


__all__ = [
    "READ_VERBS", "WRITE_VERBS", "SEARCH_KINDS", "ISSUE_STATUSES",
    "apply_issue_status", "ask_code", "get_review_run", "index_repo",
    "issue_ids", "issue_status", "list_issues", "list_reviews",
    "audit_bulk_review", "open_pr_targets", "plan_step", "queue_pr_review", "queue_pr_reviews",
    "review_pr", "roles_for", "run_review_inline", "search_code", "update_issue",
]
