"""What a non-human caller may do to a workspace.

Three surfaces are converging on the same verbs: MCP (an external Claude Code
registering repositories and asking for an audit), the embedded agent, and —
next — a ticket connector that turns "audit these four services" into work and
posts the result back. Writing the verbs once means the tenancy check, the
budget-spending decision and the audit log live in one place instead of three
that drift.

Everything here is workspace-scoped and takes an explicit actor. Nothing reads
an ambient request context: a connector processing a queue has no request, and
the moment these functions guess at a workspace they become the way one
tenant's automation reaches another's repositories.

The functions are deliberately thin over the same code paths the HTTP API
uses. A second implementation of "start an audit" is a second set of rules
about live runs, dedup and forced restarts — and the one nobody maintains is
the one that corrupts the queue.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: An automated caller must never be able to enqueue an unbounded fan-out.
MAX_AUDIT_REPOS = 50


@dataclass(frozen=True)
class Actor:
    """Who is acting, and on whose behalf.

    `user_id` and `email` are for the audit trail and for resolving the git
    credential; `label` says which surface asked, so a log line distinguishes
    a person clicking Run from a ticket connector reacting to a webhook.
    """

    user_id: str
    email: str
    workspace_id: str
    label: str = "automation"


class ActionError(RuntimeError):
    """A refusal the caller can act on — not an internal failure."""


def register_repo(
    actor: Actor,
    url: str,
    branch: str | None = None,
    *,
    index: bool = True,
) -> dict[str, Any]:
    """Register a repository in the actor's workspace and queue its index.

    Idempotent by design: registering something already registered returns the
    existing row rather than erroring, because a connector replaying a ticket
    must not fail on the second attempt.

    `index` defaults to True for the same reason POST /api/repos does — a
    registered repository nothing clones has no graph, and a review of it runs
    on the diff alone. `index=False` is for bulk registration that does not
    want a clone per repo. The returned dict says which happened, under
    `index_status`; it never lies by omission.
    """
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.sync.git_providers import parse_repo_url

    try:
        parsed = parse_repo_url(url)
    except Exception as exc:  # noqa: BLE001
        raise ActionError(f"Could not read a repository out of {url!r}: {exc}") from None

    full_name = f"{parsed.owner}/{parsed.name}"
    store = get_auto_review_store()

    # The 1:1 repo→workspace binding the webhook router depends on. Without
    # this check an automation could quietly move another tenant's repository.
    bound = store.existing_workspace_binding(parsed.provider.value, full_name)
    if bound is not None and bound != actor.workspace_id:
        raise ActionError("That repository is registered in another workspace.")

    from src.repos.indexing import (
        INDEX_NOT_REQUESTED,
        INDEX_QUEUED,
        queue_index_if_needed,
    )

    def _index(slug: str) -> str:
        if not index:
            return INDEX_NOT_REQUESTED
        return queue_index_if_needed(
            slug, workspace_id=actor.workspace_id,
            user_id=actor.user_id, enqueued_by=actor.email,
        )

    existing = store.get_in_workspace(actor.workspace_id, parsed.slug)
    if existing is not None:
        # A repeat registration still asks: the usual reason a connector
        # replays one is that the first attempt left the repository with no
        # graph, and answering "already_registered" while it stays unindexed
        # is the silence this whole change exists to remove. An existing graph
        # or a live job short-circuits inside queue_index_if_needed, so this
        # cannot re-clone anything.
        status = _index(existing.repo_slug)
        return {
            "slug": existing.repo_slug, "full_name": existing.full_name,
            "provider": existing.provider, "branch": existing.branch,
            "already_registered": True,
            "index_queued": status == INDEX_QUEUED, "index_status": status,
        }

    from src.api.routers.repos import _default_mode

    cfg = RepoConfig(
        user_id=actor.user_id,
        repo_slug=parsed.slug,
        provider=parsed.provider.value,
        full_name=full_name,
        url=url,
        workspace_id=actor.workspace_id,
        branch=(branch or "").strip() or None,
        enabled=False,
        mode=_default_mode(parsed.provider.value, False),
    )
    store.upsert(cfg)
    status = _index(parsed.slug)
    logger.info("automation_repo_registered slug=%s ws=%s index=%s by=%s via=%s",
                parsed.slug, actor.workspace_id, status, actor.email, actor.label)
    return {
        "slug": parsed.slug, "full_name": full_name,
        "provider": parsed.provider.value, "branch": cfg.branch,
        "already_registered": False,
        "index_queued": status == INDEX_QUEUED, "index_status": status,
    }


async def _live_queue_job(session: Any, workspace_id: str) -> tuple[str, str] | None:
    """(job_id, status) of the pending/running job backing this workspace's
    audit, or None when nothing is queued."""
    from sqlalchemy import text as _text

    row = (await session.execute(_text(
        "SELECT id, status FROM sync_jobs WHERE dedup_key = :dk "
        "AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1"
    ), {"dk": f"deps_audit:{workspace_id}"})).first()
    return (str(row[0]), str(row[1])) if row is not None else None


async def start_dep_audit(
    actor: Actor,
    session: Any,
    *,
    repo_slugs: list[str] | None = None,
    owner: str | None = None,
    branch: str | None = None,
    report_engine: str = "none",
    force: bool = False,
) -> dict[str, Any]:
    """Queue a dependency audit and return the run.

    Refuses rather than queues a second one while a run is live: an audit
    clones repositories and calls advisory databases, and two of them racing
    over the same clones is how a report ends up describing neither branch.

    `force` supersedes a run that has stopped reporting progress. It also
    self-heals the case where the backing queue job vanished — deleted from the
    Jobs page, lost in a redeploy — which otherwise leaves the row "running"
    for ever and blocks every future audit in the workspace.

    THIS IS THE ONLY IMPLEMENTATION. The HTTP route had its own copy, and the
    two had already diverged: the repository cap and the ownership check lived
    here and nowhere else, so an audit over two hundred repositories was
    refused when an agent asked through MCP and accepted when a person asked
    through the web. The same operation is not allowed to mean two things
    depending on which door it came through — and the next thing to diverge
    would have been the audit-log entry, which is what the evidence pack is
    built from.
    """
    from sqlalchemy import select

    from src.db.models import DepAuditRun

    if repo_slugs and len(repo_slugs) > MAX_AUDIT_REPOS:
        raise ActionError(
            f"An audit covers at most {MAX_AUDIT_REPOS} repositories at once.",
        )
    if report_engine not in ("none", "api", "claude_code"):
        raise ActionError("report_engine must be none, api or claude_code.")

    if repo_slugs:
        from src.api.auto_review import get_auto_review_store
        owned = {c.repo_slug for c in
                 get_auto_review_store().list_for_workspace(actor.workspace_id)}
        unknown = [s for s in repo_slugs if s not in owned]
        if unknown:
            # Named and refused, not silently dropped — an audit that covers
            # three of four repositories and says nothing is the failure mode
            # this whole surface exists to avoid.
            raise ActionError(
                f"Not registered in this workspace: {', '.join(sorted(unknown))}",
            )

    live = (await session.scalars(
        select(DepAuditRun).where(
            DepAuditRun.workspace_id == actor.workspace_id,
            DepAuditRun.status.in_(("queued", "running")),
        )
    )).first()
    if live is not None:
        job = await _live_queue_job(session, actor.workspace_id)
        if job is not None and not force:
            raise ActionError(
                f"An audit is already running in this workspace (run {live.id}). "
                f"Wait for it, or cancel it first.",
            )
        if job is None:
            # Self-heal: the run row outlived its queue job, so nothing is
            # actually working on it and nothing ever will.
            live.error = "orphaned (queue job was deleted or lost) — restarted"
        else:
            # Free the dedup slot. A worker somehow still alive keeps writing
            # to the OLD run id and stops itself at its next checkpoint — the
            # auditor treats a non-running run row as a cancellation.
            from src.sync.queue import mark_cancelled, request_cancel

            job_id_old, job_status = job
            if job_status == "running":
                request_cancel(job_id_old)
            mark_cancelled(job_id_old, "superseded by a restart")
            live.error = ("restarted (previous run stopped reporting progress)")
        live.status = "error"
        await session.commit()

    run = DepAuditRun(id=str(uuid.uuid4()), workspace_id=actor.workspace_id,
                      status="queued")
    session.add(run)
    await session.commit()
    await session.refresh(run)

    from src.sync.queue import KIND_DEPS_AUDIT, enqueue
    job_id = enqueue(
        kind=KIND_DEPS_AUDIT,
        payload={
            "run_id": run.id,
            "workspace_id": actor.workspace_id,
            "repo_slugs": repo_slugs or None,
            "owner": (owner or "").strip() or None,
            "branch": (branch or "").strip() or None,
            "report_engine": report_engine,
            "user_id": actor.user_id,
        },
        dedup_key=f"deps_audit:{actor.workspace_id}",
        enqueued_by=f"{actor.email} ({actor.label})",
    )
    if job_id is None:
        run.status = "error"
        run.error = ("another dependency-audit job still holds the queue slot "
                     "for this workspace")
        await session.commit()
        await session.refresh(run)
        raise ActionError(run.error)

    logger.info("automation_audit_queued run=%s job=%s ws=%s by=%s via=%s",
                run.id, job_id, actor.workspace_id, actor.email, actor.label)
    return {"run_id": run.id, "status": run.status, "job_id": job_id}


async def get_dep_audit(
    actor: Actor, session: Any, run_id: str | None = None,
) -> dict[str, Any]:
    """A run's status and summary — the latest one when `run_id` is omitted."""
    from sqlalchemy import select

    from src.db.models import DepAuditRun

    query = select(DepAuditRun).where(DepAuditRun.workspace_id == actor.workspace_id)
    if run_id:
        query = query.where(DepAuditRun.id == run_id)
    else:
        query = query.order_by(DepAuditRun.created_at.desc()).limit(1)

    run = (await session.scalars(query)).first()
    if run is None:
        raise ActionError("No audit run found.")
    return {
        "run_id": run.id,
        "status": run.status,
        "error": run.error or "",
        "summary": dict(run.summary or {}),
        "created_at": run.created_at.isoformat() if run.created_at else "",
    }


async def list_dep_findings(
    actor: Actor, session: Any, run_id: str, *,
    severity: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Findings of a run, worst first — the material a report is written from."""
    from sqlalchemy import select

    from src.db.models import DepAuditRun, DepFinding

    run = (await session.scalars(
        select(DepAuditRun).where(
            DepAuditRun.id == run_id,
            DepAuditRun.workspace_id == actor.workspace_id,
        )
    )).first()
    if run is None:
        raise ActionError("No audit run found.")

    query = select(DepFinding).where(DepFinding.run_id == run_id)
    rows = list(await session.scalars(query))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    if severity:
        wanted = severity.strip().lower()
        rows = [r for r in rows if (r.severity or "").lower() == wanted]
    rows.sort(key=lambda r: order.get((r.severity or "").lower(), 9))

    return [
        {
            "repo": r.repo_slug,
            "ecosystem": r.ecosystem,
            "package": r.package,
            "installed": r.current_version,
            "latest": r.latest_version or "",
            "outdated": r.outdated,
            "severity": r.severity,
            "recommendation": r.recommendation,
            "vulnerabilities": len(r.vulns or []),
        }
        for r in rows[:max(1, min(limit, 500))]
    ]


__all__ = [
    "Actor",
    "ActionError",
    "MAX_AUDIT_REPOS",
    "get_dep_audit",
    "list_dep_findings",
    "register_repo",
    "start_dep_audit",
]


#: A bulk vault build is many minutes per repository. The cap is lower than the
#: audit's because each one calls a model once per module, not once in total.
MAX_VAULT_REPOS = 20


async def generate_docs(
    actor: Actor,
    session: Any,
    *,
    repo_slugs: list[str] | None = None,
    owner: str | None = None,
    missing_only: bool = False,
    language: str | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Queue documentation for a SET of repositories.

    The single-repository route existed and this did not, so "generate
    documentation for every service that has none" meant finding them in a list
    of forty and pressing a button forty times. That is the shape of request
    that a sentence answers well and a form answers badly — a set defined by a
    condition rather than by enumeration.

    `missing_only` is that condition made explicit: it selects the repositories
    with no vault yet, which is the reason somebody asks for this in the first
    place. Without it the same phrase means "regenerate everything", which is
    hours of model time and almost never what was meant.

    Returns one entry per repository, including the ones it refused and why:
    a bulk action that silently covers nine of ten is the failure this surface
    exists to avoid.
    """
    from src.api.auto_review import get_auto_review_store
    from src.config import get_settings
    from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
    from src.sync.queue import KIND_GENERATE_VAULT, enqueue

    store = get_auto_review_store()
    registered = {c.repo_slug: c for c in store.list_for_workspace(actor.workspace_id)}

    if repo_slugs:
        unknown = [s for s in repo_slugs if s not in registered]
        if unknown:
            raise ActionError(
                f"Not registered in this workspace: {', '.join(sorted(unknown))}")
        chosen = list(dict.fromkeys(repo_slugs))
    else:
        chosen = sorted(registered)
        if owner:
            prefix = owner.strip().rstrip("/") + "/"
            chosen = [s for s in chosen
                      if registered[s].full_name.startswith(prefix)]
            if not chosen:
                raise ActionError(f"No repositories under {owner!r} in this workspace.")

    settings = get_settings()
    skipped: list[dict[str, str]] = []

    # A repository that was never indexed has no symbol graph, so a vault build
    # would produce documents written from filenames. Refused by name rather
    # than queued and quietly poor.
    ready = []
    for slug in chosen:
        if not settings.repo_graph_path(slug).exists():
            skipped.append({"repo": slug, "reason": "not indexed yet"})
            continue
        if missing_only and settings.repo_vault_path(slug).exists() and \
                any(settings.repo_vault_path(slug).rglob("*.md")):
            skipped.append({"repo": slug, "reason": "already has documentation"})
            continue
        ready.append(slug)

    if not ready:
        raise ActionError(
            "Nothing to generate. " + "; ".join(
                f"{s['repo']}: {s['reason']}" for s in skipped[:10])
            if skipped else "Nothing to generate — no repositories matched.")

    if len(ready) > MAX_VAULT_REPOS:
        raise ActionError(
            f"That is {len(ready)} repositories; at most {MAX_VAULT_REPOS} can "
            f"be queued at once. Narrow it down, or run it in batches.")

    resolved_language = resolve_doc_language(language, actor.workspace_id)
    resolved_engine = resolve_doc_engine(engine, actor.workspace_id)

    queued: list[dict[str, str]] = []
    for slug in ready:
        cfg = registered[slug]
        job_id = enqueue(
            kind=KIND_GENERATE_VAULT,
            payload={
                "repo_url": cfg.url, "repo_slug": slug,
                "workspace_id": actor.workspace_id, "user_id": actor.user_id,
                "language": resolved_language, "engine": resolved_engine,
            },
            dedup_key=f"generate_vault:{actor.workspace_id}:{slug}",
            enqueued_by=f"{actor.email} ({actor.label})",
        )
        if job_id is None:
            # Already queued. Reported, not counted as started — the whole
            # point of returning per-repository results.
            skipped.append({"repo": slug, "reason": "a build is already queued"})
        else:
            queued.append({"repo": slug, "job_id": job_id})

    logger.info("automation_docs_queued ws=%s queued=%d skipped=%d by=%s via=%s",
                actor.workspace_id, len(queued), len(skipped), actor.email,
                actor.label)
    return {
        "queued": queued,
        "skipped": skipped,
        "language": resolved_language,
        "engine": resolved_engine,
    }


#: A misread "turn it on everywhere" must not silently arm review on a whole
#: estate. Lower than the audit cap on purpose: an audit costs model time,
#: this one changes what happens to every future pull request.
MAX_AUTO_REVIEW_REPOS = 25


def set_auto_review(
    actor: Actor,
    *,
    repo_slugs: list[str] | None = None,
    owner: str | None = None,
    enabled: bool = True,
    branch: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Arm or disarm automatic review, optionally pinning the branch.

    The HTTP API had this as two routes — a toggle and a branch setter — and
    the chat had no way to reach either, so "turn on review for the release
    branch" was a sentence the agent could read and not act on. This is the
    one implementation; the routes delegate to it.

    `branch` is not a filter. It is the ref every surface then works from:
    index, dependency audit and the agent workspace all read `cfg.branch`, so
    setting it here is the difference between reviewing the branch somebody
    named and quietly reviewing whatever the provider calls default.
    """
    from src.api.auto_review import get_auto_review_store
    from src.api.routers.repos import _default_mode

    store = get_auto_review_store()
    registered = {c.repo_slug: c
                  for c in store.list_for_workspace(actor.workspace_id)}
    if not registered:
        raise ActionError("No repositories are registered in this workspace.")

    if repo_slugs:
        unknown = [s for s in repo_slugs if s not in registered]
        if unknown:
            raise ActionError(
                "Not registered in this workspace: " + ", ".join(sorted(unknown))
            )
        chosen = list(dict.fromkeys(repo_slugs))
    else:
        chosen = sorted(registered)
        if owner:
            prefix = owner.rstrip("/") + "/"
            chosen = [s for s in chosen
                      if registered[s].full_name.startswith(prefix)]

    if not chosen:
        raise ActionError("Nothing matched — no repositories in scope.")
    if len(chosen) > MAX_AUTO_REVIEW_REPOS:
        raise ActionError(
            f"That is {len(chosen)} repositories; at most "
            f"{MAX_AUTO_REVIEW_REPOS} can be changed at once."
        )

    updated: list[dict[str, Any]] = []
    for slug in chosen:
        cfg = registered[slug]
        cfg.enabled = bool(enabled)
        cfg.mode = _default_mode(cfg.provider, cfg.enabled, requested_mode=mode)
        if branch:
            cfg.branch = branch
        store.upsert(cfg)
        updated.append({
            "repo": slug, "enabled": cfg.enabled,
            "mode": cfg.mode, "branch": cfg.branch,
        })
        logger.info("auto_review_set repo=%s enabled=%s branch=%s ws=%s by=%s "
                    "via=%s", slug, cfg.enabled, cfg.branch,
                    actor.workspace_id, actor.email, actor.label)

    return {"updated": updated, "count": len(updated)}


def list_repos(actor: Actor) -> dict[str, Any]:
    """What this workspace has, and the state of each repository.

    A read verb, and the first one this surface has had. Everything in this
    module until now queued expensive work, which is why the chat refused
    "which repositories do I have" — not because the question is hard, but
    because there was no verb for it and the two-press gate would have been
    absurd on an answer.

    That gap is what made the agent feel narrow: it could start a documentation
    build over twenty repositories and could not say which twenty.
    """
    from src.api.auto_review import get_auto_review_store
    from src.config import get_settings

    settings = get_settings()
    store = get_auto_review_store()
    repos = []
    for cfg in sorted(store.list_for_workspace(actor.workspace_id),
                      key=lambda c: c.full_name):
        vault = settings.repo_vault_path(cfg.repo_slug)
        repos.append({
            "repo": cfg.repo_slug,
            "full_name": cfg.full_name,
            "provider": cfg.provider,
            "branch": cfg.branch,
            "indexed": settings.repo_graph_path(cfg.repo_slug).exists(),
            "documented": vault.exists() and any(vault.rglob("*.md")),
            "auto_review": cfg.enabled,
            "auto_review_mode": cfg.mode if cfg.enabled else None,
        })
    return {
        "repos": repos,
        "count": len(repos),
        "indexed": sum(1 for r in repos if r["indexed"]),
        "documented": sum(1 for r in repos if r["documented"]),
        "auto_review_on": sum(1 for r in repos if r["auto_review"]),
    }
