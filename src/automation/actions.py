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
    #: The repo list of the MCP token this actor acts for (exact slugs and/or
    #: globs). ``None`` = no token narrows it. When set, only these repositories
    #: of the workspace exist for the actor — the superadmin who issued the
    #: token is authoritative — and a repo outside the list is as unknown as
    #: one that was never registered.
    token_filter: tuple[str, ...] | None = None


class ActionError(RuntimeError):
    """A refusal the caller can act on — not an internal failure."""


def workspace_configs(actor: Actor) -> list[Any]:
    """The repositories registered in the actor's workspace that exist FOR the
    actor: all of them, or — for an actor holding a token with a repo list —
    the ones that list covers."""
    from src.api.auto_review import get_auto_review_store

    configs = get_auto_review_store().list_for_workspace(actor.workspace_id)
    if actor.token_filter is None:
        return configs
    from src.access.effective import match_any

    return [c for c in configs
            if match_any(actor.token_filter, c.repo_slug, getattr(c, "full_name", ""))]


def _token_slugs(actor: Actor) -> set[str] | None:
    """The slugs an actor's token lists, or ``None`` when no token narrows it."""
    if actor.token_filter is None:
        return None
    return {c.repo_slug for c in workspace_configs(actor)}


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

    from src.config import is_valid_repo_slug

    # Refused before anything is stored: the slug names the clone, graph and
    # vault directories, and a stored slug that is not one safe path segment
    # makes every later per-repo path lookup raise.
    if not is_valid_repo_slug(parsed.slug):
        raise ActionError(
            f"Unsupported repository name {parsed.slug!r}: only letters, "
            "digits, '.', '_' and '-' (and no '..') are supported.")

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
        owned = {c.repo_slug for c in workspace_configs(actor)}
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


async def _visible_dep_repos(actor: Actor, session: Any, run: Any) -> set[str] | None:
    """The repositories of ``run`` this actor may see; ``None`` = all of them.
    A token's repo list is the whole answer; otherwise the person's own access
    (default-deny included) decides, as it does for the page."""
    from src.api.routers import deps as deps_router

    return await deps_router._visible_repos(
        session, run, _user_for(actor), actor.workspace_id, only=_token_slugs(actor))


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
    summary = dict(run.summary or {})
    visible = await _visible_dep_repos(actor, session, run)
    if visible is not None:
        # The run covers every repository of the workspace: the caller gets
        # the totals and slugs of the ones it may see, and a whitelist of the
        # rest — the same rebuild the page's route applies.
        from src.api.routers import deps as deps_router
        from src.db.models import DepFinding

        rows = [r for r in await session.scalars(
            select(DepFinding).where(DepFinding.run_id == run.id))
            if r.repo_slug in visible]
        summary = deps_router._restricted_summary(summary, rows, visible)
    return {
        "run_id": run.id,
        "status": run.status,
        "error": run.error or "",
        "summary": summary,
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
    visible = await _visible_dep_repos(actor, session, run)
    if visible is not None:
        rows = [r for r in rows if r.repo_slug in visible]

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
    from src.config import get_settings
    from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
    from src.sync.queue import KIND_GENERATE_VAULT, enqueue

    registered = {c.repo_slug: c for c in workspace_configs(actor)}

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
    registered = {c.repo_slug: c for c in workspace_configs(actor)}
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


async def list_repos(actor: Actor) -> dict[str, Any]:
    """What this workspace has, and the state of each repository — the ones the
    asker may read: owners/admins see all, everyone else the repositories a team
    of theirs grants `read` on (as `GET /api/repos` and the review-settings
    overview). A repository they may not read is neither named nor counted.

    A read verb, and the first one this surface has had. Everything in this
    module until now queued expensive work, which is why the chat refused
    "which repositories do I have" — not because the question is hard, but
    because there was no verb for it and the two-press gate would have been
    absurd on an answer.

    That gap is what made the agent feel narrow: it could start a documentation
    build over twenty repositories and could not say which twenty.
    """
    from src.api.deps import readable_repo_slugs
    from src.config import get_settings, is_valid_repo_slug

    settings = get_settings()
    configs = sorted(workspace_configs(actor), key=lambda c: c.full_name)
    if actor.token_filter is not None:
        # The token's repo list is authoritative; `configs` is already that list.
        allowed = {c.repo_slug for c in configs}
    else:
        allowed = await readable_repo_slugs(
            _user_for(actor), actor.workspace_id, [c.repo_slug for c in configs])
    repos = []
    for cfg in configs:
        if cfg.repo_slug not in allowed:
            continue
        if not is_valid_repo_slug(cfg.repo_slug):
            # A row stored before slugs were validated: report it, never
            # let it break the whole listing.
            repos.append({
                "repo": cfg.repo_slug, "full_name": cfg.full_name,
                "provider": cfg.provider, "branch": cfg.branch,
                "indexed": False, "documented": False,
                "auto_review": cfg.enabled,
                "auto_review_mode": cfg.mode if cfg.enabled else None,
            })
            continue
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


# ─── review rules and settings ──────────────────────────────────────────
#
# Three verbs that change how a repository — or the whole workspace — is
# REVIEWED rather than queue work over a set. They are here and not in the
# chat for the reason everything else is: the permission check, the
# validation and the write path must be the ones the HTTP API uses, so the
# agent can never do what the person could not do on the page. Each one ends
# in the same router function the page calls, with the same gates in front
# of it; nothing here writes a policy row of its own.

#: The settings a sentence may change. A whitelist, not "whatever the schema
#: accepts": prompt templates, MCP sources and per-agent model overrides are
#: forms with previews and validation of their own, and a chat that could set
#: them would be a worse copy of those pages.
#:
#: Some of these may not exist in the installed schema yet — they are being
#: added to the review settings separately. Which ones are live is read from
#: the schema at run time (`review_setting_keys`), so a key the code does not
#: know is refused by name rather than written somewhere nothing reads it.
REVIEW_SETTING_KEYS: tuple[str, ...] = (
    "run_on_drafts", "approve_when_clean", "request_changes_on_critical",
    "committable_suggestions", "comment_min_severity", "max_inline_comments",
    "summary_enabled", "review_language", "disabled_agents",
    "agent_prompt_guidelines",
)

#: Not a column of the workspace review defaults, but a setting of both
#: scopes all the same: the team guidelines ADDED to an agent's prompt
#: (src/review/prompt_guidelines.py), stored per repository on the policy and
#: per workspace beside the agent prompts (src/api/routers/agents.py).
#: Replacing an agent's prompt is deliberately NOT a key here: that is the
#: advanced mode, with a warning and a preview on the page.
_GUIDELINES_KEY = "agent_prompt_guidelines"

#: The section of /review-settings that edits each of them — where a link
#: after a change sends the person to see it (web/components/review-settings/
#: model.ts FIELD_SECTION says the same for every field).
REVIEW_SETTING_SECTION: dict[str, str] = {
    "run_on_drafts": "general", "approve_when_clean": "general",
    "request_changes_on_critical": "general", "committable_suggestions": "general",
    "review_language": "general", "comment_min_severity": "filters",
    "max_inline_comments": "filters", "summary_enabled": "summary",
    "disabled_agents": "categories", "agent_prompt_guidelines": "prompts",
}


def _settings_href(repo: str | None = None, section: str | None = None) -> str:
    """A link into /review-settings: the scope (Global, or one repository)
    and the section, as web/lib/review-settings-routes.ts builds them."""
    from urllib.parse import urlencode

    query = {k: v for k, v in (("repo", repo), ("section", section))
             if v and not (k == "section" and v == "general")}
    return "/review-settings" + (f"?{urlencode(query)}" if query else "")


#: How many rules one sentence may add. A policy holds at most 20 (the
#: schema's own bound), and a "rule" list longer than this is a paste of a
#: style guide, which belongs in the prompt template on the policy page.
MAX_RULES_PER_PROPOSAL = 10

#: Spellings people use for a rule's severity, folded into the four a rule
#: can carry (src.review.policy_rules.SEVERITY_HINTS).
_SEVERITY_ALIASES = {
    "low": "info", "minor": "info", "note": "info", "nit": "info",
    "medium": "warning", "moderate": "warning", "warn": "warning",
    "high": "error", "major": "error", "blocker": "critical",
}

_RULES_STORE = "src.review.rules_store"
_RULES_GENERATE = "src.review.rules_generate"


def _module_available(name: str) -> bool:
    """Whether an optional module ships in this build — without importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def rules_store_available() -> bool:
    """True when proposals go to the review-rules store (as PENDING), False
    when they are appended to the repository policy directly."""
    return _module_available(_RULES_STORE)


def rules_generation_available() -> bool:
    return _module_available(_RULES_GENERATE)


def review_setting_keys(scope: str) -> tuple[str, ...]:
    """The whitelisted keys the installed schema for `scope` really has."""
    from src.api.schemas import ReviewPolicyIn, WorkspaceReviewDefaultsIn

    model = WorkspaceReviewDefaultsIn if scope == "workspace" else ReviewPolicyIn
    return tuple(k for k in REVIEW_SETTING_KEYS
                 if k in model.model_fields or k == _GUIDELINES_KEY)


def _normalise_guidelines(value: Any) -> dict[str, str]:
    """{agent: text} as it will be written, or a refusal naming the problem.

    A JSON object of agent → guidelines; "" (or null) for an agent removes
    its guidelines at that scope. The agents and the 2000-character cap are
    the API's own.
    """
    import json as _json

    from src.api.routers.review_policies import _OVERRIDABLE_AGENT_ORDER
    from src.review.prompt_guidelines import GUIDELINES_MAX_CHARS

    shape = ('agent_prompt_guidelines: give an object of agent → guidelines, '
             'e.g. {"security": "- Flag …"}.')
    if isinstance(value, str):
        try:
            value = _json.loads(value)
        except ValueError:
            raise ActionError(shape) from None
    if not isinstance(value, dict) or not value:
        raise ActionError(shape)
    out: dict[str, str] = {}
    for agent, text in value.items():
        name = str(agent).strip()
        if name not in _OVERRIDABLE_AGENT_ORDER:
            raise ActionError(
                f"agent_prompt_guidelines: unknown agent {name!r} — the agents "
                f"are: {', '.join(_OVERRIDABLE_AGENT_ORDER)}")
        body = "" if text is None else str(text).strip()
        if len(body) > GUIDELINES_MAX_CHARS:
            raise ActionError(
                f"agent_prompt_guidelines.{name}: at most {GUIDELINES_MAX_CHARS} "
                f"characters ({len(body)} given).")
        out[name] = body
    return out


def resolve_repo(actor: Actor, repo: str | None) -> str:
    """The registered slug `repo` names in the actor's workspace, or refuse.

    Either spelling a person uses — the slug or owner/name — is accepted, the
    same two `_require_repo_in_workspace` accepts on the policy routes.
    """

    wanted = (repo or "").strip()
    if not wanted:
        raise ActionError("Name the repository.")
    for cfg in workspace_configs(actor):
        if wanted in (cfg.repo_slug, cfg.full_name):
            return cfg.repo_slug
    raise ActionError(f"Not registered in this workspace: {wanted}")


def normalise_review_rules(rules: Any) -> list[dict[str, Any]]:
    """The rules as they will be stored, or a refusal naming the bad one.

    Validated against the same `FolderRule` schema the policy page saves
    through, so a rule the chat accepts is a rule the page can show.
    """
    from pydantic import ValidationError

    from src.api.schemas import FolderRule
    from src.review.policy_rules import SEVERITY_HINTS

    if not isinstance(rules, list) or not rules:
        raise ActionError("There are no rules to add.")
    if len(rules) > MAX_RULES_PER_PROPOSAL:
        raise ActionError(
            f"That is {len(rules)} rules; at most {MAX_RULES_PER_PROPOSAL} "
            "can be added at once.")

    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(rules, start=1):
        if not isinstance(raw, dict):
            raise ActionError(f"Rule {idx} is not a rule.")
        instructions = str(raw.get("instructions") or raw.get("prompt") or "").strip()
        title = str(raw.get("title") or "").strip() or None
        glob = str(raw.get("path_glob") or raw.get("pattern") or "").strip() or None
        severity = str(raw.get("severity") or "").strip().lower() or None
        if severity is not None:
            severity = _SEVERITY_ALIASES.get(severity, severity)
            if severity not in SEVERITY_HINTS:
                raise ActionError(
                    f"Rule {idx}: severity must be one of "
                    f"{', '.join(SEVERITY_HINTS)}.")
        agents_raw = raw.get("agents") or []
        if not isinstance(agents_raw, list):
            agents_raw = [agents_raw]
        agents = [str(a).strip() for a in agents_raw if str(a).strip()]
        if not instructions:
            raise ActionError(f"Rule {idx} says nothing — it has no instructions.")
        try:
            FolderRule(pattern=glob or "**", prompt=instructions, title=title,
                       severity_hint=severity, agents=agents)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise ActionError(f"Rule {idx}: {where}: {first.get('msg')}") from None
        if agents:
            from src.api.routers.review_policies import _rule_target_agents

            known = _rule_target_agents()
            unknown = [a for a in agents if a not in known]
            if unknown:
                raise ActionError(
                    f"Rule {idx}: unknown agent(s) {', '.join(unknown)} — a rule "
                    f"can target: {', '.join(known)}")
        out.append({"title": title, "instructions": instructions,
                    "path_glob": glob, "severity": severity,
                    "agents": list(dict.fromkeys(agents)) or None})
    return out


def review_setting_value(scope: str, key: str, value: Any) -> Any:
    """`value` as the schema for `scope` reads it, or a refusal.

    Three refusals, in the order a person needs them: a key the agent may not
    touch at all; a key it may touch but this installation's schema does not
    have yet; and a value the schema rejects. The value is parsed by the very
    model the HTTP route parses its body with, so "true", 1 and "yes" mean
    what they mean on the page and nothing else.
    """
    from pydantic import ValidationError

    from src.api.schemas import ReviewPolicyIn, WorkspaceReviewDefaultsIn

    if scope not in ("workspace", "repo"):
        raise ActionError("scope must be workspace or repo.")
    if key not in REVIEW_SETTING_KEYS:
        raise ActionError(
            f"{key!r} cannot be changed from here. The settings I can change "
            f"are: {', '.join(REVIEW_SETTING_KEYS)}.")
    if key not in review_setting_keys(scope):
        where = ("workspace review defaults" if scope == "workspace"
                 else "repository review policies")
        raise ActionError(
            f"{key!r} is not a setting of the {where} in this version of "
            "Celmis, so there is nothing to change.")
    if key == _GUIDELINES_KEY:
        return _normalise_guidelines(value)
    model = WorkspaceReviewDefaultsIn if scope == "workspace" else ReviewPolicyIn
    if key == "disabled_agents" and isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    try:
        parsed = model.model_validate({key: value})
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ActionError(f"{key}: {first.get('msg')}") from None
    parsed_value = getattr(parsed, key)
    if key in ("comment_min_severity", "review_language"):
        # The routes' own readers of these two, so the plan refuses exactly
        # what the save would refuse — and says it in the same words.
        from fastapi import HTTPException

        from src.api.routers import review_policies as rp

        reader = (rp._comment_min_severity_from_payload
                  if key == "comment_min_severity"
                  else rp._review_language_from_payload)
        try:
            parsed_value = reader(parsed_value)
        except HTTPException as exc:
            raise ActionError(str(exc.detail)) from None
    if key == "disabled_agents" and parsed_value is not None:
        from src.api.routers.review_policies import TOGGLEABLE_AGENTS

        unknown = [a for a in parsed_value if a not in TOGGLEABLE_AGENTS]
        if unknown:
            raise ActionError(
                f"disabled_agents: unknown agent(s) {', '.join(unknown)} — the "
                f"switchable agents are: {', '.join(TOGGLEABLE_AGENTS)}")
    return parsed_value


async def _update_guidelines(actor: Actor, session: Any, user: Any, scope: str,
                             changes: dict[str, str],
                             repo_slug: str | None) -> dict[str, Any]:
    """Set (or clear, with "") the team guidelines of the named agents.

    Per agent, merged into what the scope already holds — "add guidelines
    for security" must not wipe the defect agent's. Workspace: the same
    store and the same gate (`require_prompt_editor`) as PUT
    /api/agents/{name}/guidelines. Repository: PUT /api/review-policies'
    own function, with the stored map laid under the change.
    """
    import asyncio

    from src.api.deps import require_prompt_editor

    await _as_action(require_prompt_editor(user=user,
                                           workspace_id=actor.workspace_id))
    slug: str | None = None
    if scope == "workspace":
        from src.api.routers import agents as agents_router

        unknown = [a for a in changes if a not in agents_router._AGENTS]
        if unknown:
            raise ActionError(f"No workspace prompt for agent(s): {', '.join(unknown)}")

        def _write() -> None:
            for agent, text in changes.items():
                if text:
                    agents_router._save_guidelines(
                        agent, text, updated_by=actor.email,
                        workspace_id=actor.workspace_id)
                else:
                    agents_router._delete_guidelines(agent, actor.workspace_id)

        await asyncio.to_thread(_write)
    else:
        from src.db.models import RepoReviewPolicy

        slug = resolve_repo(actor, repo_slug)
        await _require_repo_review(actor, user, slug)
        row = await session.get(RepoReviewPolicy, slug)
        merged = dict(getattr(row, "agent_prompt_guidelines", None) or {}) if row else {}
        for agent, text in changes.items():
            if text:
                merged[agent] = text
            else:
                merged.pop(agent, None)
        await _upsert_policy_fields(actor, session, user, slug,
                                    {_GUIDELINES_KEY: merged})
    logger.info("review_setting_changed scope=%s repo=%s key=%s agents=%s ws=%s "
                "by=%s via=%s", scope, slug, _GUIDELINES_KEY,
                ",".join(sorted(changes)), actor.workspace_id, actor.email,
                actor.label)
    return {"scope": scope, "repo": slug, "key": _GUIDELINES_KEY,
            "value": _plain(changes), "effective": _plain(changes),
            "count": len(changes),
            "links": [{"label": "policy" if slug else "defaults",
                       "href": _settings_href(slug, "prompts")}]}


def _user_for(actor: Actor):
    """The person behind `actor`, as the HTTP dependencies would hand it."""
    from src.users import get_user_store

    user = get_user_store().get_by_id(actor.user_id) if actor.user_id else None
    if user is None or not getattr(user, "is_active", True):
        raise ActionError("Could not tell who is asking — sign in again.")
    _require_member(actor, user)
    return user


def _require_member(actor: Actor, user: Any) -> None:
    """The asker belongs to `actor.workspace_id` (a global admin may act in any).

    The HTTP routes never need this: `current_workspace_id` only ever hands a
    caller a workspace they are in. An `Actor` is built from a workspace id
    that was true when it was made — a plan read a minute ago, a token minted
    last week — so the action asks again, with the same rule the
    review-defaults routes apply (`_require_member`): multi-tenant only, since
    the shared single-tenant default has no membership to ask. Blocking.
    """
    if getattr(user, "is_admin", False):
        return
    from src.deployment import is_multi_tenant

    if not is_multi_tenant():
        return
    from src.api.deps import workspace_role

    if workspace_role(user.id, actor.workspace_id) is None:
        raise ActionError("You are not a member of this workspace.")


async def _require_workspace_member(actor: Actor) -> None:
    """`_require_member` for a caller that has only an `Actor` — the executor,
    before any verb, so even the verbs that never look the person up (the
    repository list, a dependency audit, the canned answers) are refused to
    somebody who is not in the workspace."""
    import asyncio

    from src.deployment import is_multi_tenant

    if not is_multi_tenant():
        return
    from src.users import get_user_store

    user = get_user_store().get_by_id(actor.user_id) if actor.user_id else None
    if user is None:
        raise ActionError("Could not tell who is asking — sign in again.")
    await asyncio.to_thread(_require_member, actor, user)


async def _as_action(coro: Any) -> Any:
    """Await a router call, turning its HTTP refusal into an ActionError.

    The 403 and 422 sentences the page shows are the ones the agent shows:
    same gate, same words.
    """
    from fastapi import HTTPException

    try:
        return await coro
    except HTTPException as exc:
        raise ActionError(str(exc.detail)) from None


#: Who may PROPOSE a rule or ask for generated ones: anybody who works on the
#: code. A proposal is pending until an editor approves it, so the bar is the
#: one `member` already clears for changing an issue's status.
PROPOSER_ROLES = frozenset({"member", "editor", "admin", "owner"})


async def _require_role(actor: Actor, user: Any, roles: frozenset[str],
                        what: str) -> None:
    """A workspace role in `roles`, or a global admin — the shape of every
    role gate in src.api.deps."""
    import asyncio

    from src.api.deps import workspace_role

    if getattr(user, "is_admin", False):
        return
    role = await asyncio.to_thread(workspace_role, user.id, actor.workspace_id)
    if role not in roles:
        raise ActionError(
            f"{what} requires one of these roles on this workspace: "
            f"{', '.join(sorted(roles))} (yours: {role or 'none'}).")


async def _require_repo_review(actor: Actor, user: Any, slug: str) -> None:
    """The team gate the policy routes put in front of a write: `review` on
    the repository (fall-open where no team holds a grant, single-tenant)."""
    from src.api.deps import enforce_repo_permission

    await _as_action(enforce_repo_permission(slug, user, "review",
                                             actor.workspace_id))


async def _upsert_policy_fields(actor: Actor, session: Any, user: Any,
                                slug: str, changes: dict[str, Any]) -> Any:
    """Change some fields of one repository's policy through PUT's own code.

    PUT is a full replace, so the body is the stored policy with `changes`
    laid over it. Every field the stored row says nothing about is left out —
    absent means "keep" on that route — and so is `agent_llm_overrides`,
    which absent also keeps, and which re-sent would be re-validated against
    models that may have moved since it was saved.
    """
    from pydantic import ValidationError

    from src.api.routers.review_policies import (
        _stored_folder_rules,
        upsert_policy,
    )
    from src.api.schemas import ReviewPolicyIn
    from src.db.models import RepoReviewPolicy

    row = await session.get(RepoReviewPolicy, slug)
    if row is not None and row.workspace_id != actor.workspace_id:
        raise ActionError("Policy not found in this workspace")
    body: dict[str, Any] = {}
    if row is not None:
        for name in ReviewPolicyIn.model_fields:
            if name == "agent_llm_overrides" or name in changes:
                continue
            if name == "folder_rules":
                body[name] = [r.model_dump(exclude_none=True)
                              for r in _stored_folder_rules(row.folder_rules)]
                continue
            value = getattr(row, name, None)
            if value is not None:
                body[name] = value
    body.update(changes)
    try:
        payload = ReviewPolicyIn.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ActionError(f"{where}: {first.get('msg')}") from None
    return await _as_action(upsert_policy(
        repo_slug=slug, payload=payload, request=None, session=session,
        user=user, _perm=user, ws_id=actor.workspace_id))


def _rules_links(slug: str | None, *, pending: bool) -> list[dict[str, str]]:
    """Where the person goes to see what was just added: the queue of
    proposals waiting for an editor, and the repository's settings, where its
    legacy folder rules are listed (section "Advanced")."""
    links = []
    if pending:
        links.append({"label": "pending",
                      "href": "/admin/review-rules?status=pending"})
    if slug:
        links.append({"label": "policy",
                      "href": _settings_href(slug, "advanced")})
    return links


def _plain(value: Any) -> Any:
    """Something a JSON column can hold, from whatever a store returned."""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, (list, tuple)):
            return [_plain(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _plain(v) for k, v in value.items()}
        if hasattr(value, "model_dump"):
            return _plain(value.model_dump())
        if hasattr(value, "__dict__"):
            return {k: _plain(v) for k, v in vars(value).items()
                    if not k.startswith("_")}
        return str(value)


async def propose_review_rules(
    actor: Actor,
    session: Any,
    *,
    repo_slug: str | None,
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add review rules — as PENDING proposals when the rules store exists.

    The store (`src.review.rules_store`) keeps rules an editor approves before
    any review reads them, which is the right home for rules a model wrote
    down from a sentence. Where this build has no store, the rules are
    appended to the repository policy's custom rules through the policy
    page's own save, behind the same gates that save has — and the plan the
    person confirmed said so, because there they are live at once.
    """
    import importlib
    import inspect

    normalised = normalise_review_rules(rules)
    user = _user_for(actor)
    slug = resolve_repo(actor, repo_slug) if repo_slug else None

    if rules_store_available():
        await _require_role(actor, user, PROPOSER_ROLES, "Proposing review rules")
        if slug:
            await _require_repo_review(actor, user, slug)
        store = importlib.import_module(_RULES_STORE)
        result = store.propose_rules(actor.workspace_id, slug, normalised,
                                     origin="agent", created_by=actor.email)
        if inspect.isawaitable(result):
            result = await result
        logger.info("review_rules_proposed repo=%s n=%d ws=%s by=%s via=%s",
                    slug or "*", len(normalised), actor.workspace_id,
                    actor.email, actor.label)
        return {"repo": slug, "rules": normalised, "status": "pending",
                "count": len(normalised), "proposal": _plain(result),
                "links": _rules_links(slug, pending=True)}

    if not slug:
        raise ActionError(
            "Workspace-wide rules need the review-rules list, which this "
            "installation does not have yet. Name a repository and they are "
            "added to its review policy.")
    from src.api.deps import require_prompt_editor

    await _as_action(require_prompt_editor(user=user,
                                           workspace_id=actor.workspace_id))
    await _require_repo_review(actor, user, slug)

    from src.api.routers.review_policies import _stored_folder_rules
    from src.db.models import RepoReviewPolicy

    row = await session.get(RepoReviewPolicy, slug)
    existing = ([] if row is None or row.workspace_id != actor.workspace_id
                else [r.model_dump(exclude_none=True)
                      for r in _stored_folder_rules(row.folder_rules)])
    added = [{k: v for k, v in {
        "pattern": r["path_glob"] or "**", "prompt": r["instructions"],
        "title": r["title"], "severity_hint": r["severity"],
        "agents": r["agents"],
    }.items() if v is not None} for r in normalised]
    await _upsert_policy_fields(actor, session, user, slug,
                                {"folder_rules": existing + added})
    logger.info("review_rules_appended repo=%s n=%d ws=%s by=%s via=%s",
                slug, len(added), actor.workspace_id, actor.email, actor.label)
    return {"repo": slug, "rules": normalised, "status": "active",
            "count": len(added), "links": _rules_links(slug, pending=False)}


async def generate_review_rules(
    actor: Actor, session: Any, *, repo_slug: str,
) -> dict[str, Any]:
    """Draft rules for a repository from its code, through the generator.

    The generator (`src.review.rules_generate`) is optional in this build. It
    spends model time and what it writes are proposals, so it sits behind the
    proposer's gates; where it is absent the plan was already refused before
    anybody pressed anything (see `chat.resolve_scope`).
    """
    import importlib
    import inspect

    if not rules_generation_available():
        raise ActionError(RULES_GENERATION_MISSING)
    user = _user_for(actor)
    slug = resolve_repo(actor, repo_slug)
    await _require_role(actor, user, PROPOSER_ROLES, "Generating review rules")
    await _require_repo_review(actor, user, slug)

    gen = importlib.import_module(_RULES_GENERATE)
    result = gen.generate_rules(actor.workspace_id, slug, actor)
    if inspect.isawaitable(result):
        result = await result
    logger.info("review_rules_generation_started repo=%s ws=%s by=%s via=%s",
                slug, actor.workspace_id, actor.email, actor.label)
    plain = _plain(result)
    out: dict[str, Any] = {"repo": slug, "status": "pending", "result": plain,
                           "links": _rules_links(slug, pending=True)}
    if isinstance(plain, dict) and plain.get("job_id"):
        out["queued"] = [{"repo": slug, "job_id": str(plain["job_id"])}]
    else:
        out["count"] = 1
    return out


#: What a person is told when the generator is not in this build — how to get
#: the same result by hand, rather than only that it cannot be done.
RULES_GENERATION_MISSING = (
    "This installation cannot draft review rules automatically yet. Tell me "
    "the rules in a sentence (\"add review rules for <repo>: …\") and I will "
    "add them, or write them yourself on the repository's review policy, "
    "Rules tab.")


async def update_review_setting(
    actor: Actor,
    session: Any,
    *,
    scope: str,
    key: str,
    value: Any,
    repo_slug: str | None = None,
) -> dict[str, Any]:
    """Change one review setting, for the workspace or for one repository.

    Workspace: PUT /api/review-defaults's own function, behind its own gate
    (owner or admin of the workspace, or a global admin). Repository: PUT
    /api/review-policies/{slug}'s, behind its three (editor or above, the
    repository is this workspace's, and `review` on it through the caller's
    teams). The value is parsed by the route's own schema first.
    """
    import asyncio

    parsed = review_setting_value(scope, key, value)
    user = _user_for(actor)

    if key == _GUIDELINES_KEY:
        return await _update_guidelines(actor, session, user, scope, parsed, repo_slug)

    if scope == "workspace":
        from src.api.deps import is_workspace_admin
        from src.api.routers.review_defaults import put_review_defaults
        from src.api.schemas import WorkspaceReviewDefaultsIn

        if not await asyncio.to_thread(is_workspace_admin, user,
                                       actor.workspace_id):
            raise ActionError("Requires owner/admin on this workspace")
        out = await _as_action(put_review_defaults(
            payload=WorkspaceReviewDefaultsIn.model_validate({key: parsed}),
            request=None, session=session, user=user,
            ws_id=actor.workspace_id))
        effective = (getattr(out, "effective", None) or {}).get(key, parsed)
        logger.info("review_setting_changed scope=workspace key=%s ws=%s by=%s "
                    "via=%s", key, actor.workspace_id, actor.email, actor.label)
        return {"scope": "workspace", "repo": None, "key": key,
                "value": _plain(parsed), "effective": _plain(effective),
                "count": 1,
                "links": [{"label": "defaults",
                           "href": _settings_href(None, REVIEW_SETTING_SECTION.get(key))}]}

    from src.api.deps import require_prompt_editor

    slug = resolve_repo(actor, repo_slug)
    await _as_action(require_prompt_editor(user=user,
                                           workspace_id=actor.workspace_id))
    await _require_repo_review(actor, user, slug)
    out = await _upsert_policy_fields(actor, session, user, slug, {key: parsed})
    effective = getattr(out, f"{key}_effective", parsed)
    logger.info("review_setting_changed scope=repo repo=%s key=%s ws=%s by=%s "
                "via=%s", slug, key, actor.workspace_id, actor.email, actor.label)
    return {"scope": "repo", "repo": slug, "key": key,
            "value": _plain(parsed), "effective": _plain(effective),
            "count": 1,
            "links": [{"label": "policy",
                       "href": _settings_href(slug, REVIEW_SETTING_SECTION.get(key))}]}


# ─── reading the review settings ─────────────────────────────────────
#
# The one verb in this section only LOOKS. "Which review settings are on, and
# how does that work" had no verb, so the planner returned no steps and the
# person got a generic paragraph about a configuration the agent had never
# seen. Nothing here resolves anything itself: the layering is
# `review_defaults.resolve`, the override list is `overridden_fields`, the
# guidelines are `resolve_guidelines` — the functions the settings page and a
# review already read, so this answer cannot disagree with either.

#: How much of one value, one guideline or one list may travel. The snapshot
#: goes into a model prompt, and a person's own text is the one part of it
#: with no bound of its own.
_SNAPSHOT_TEXT = 300
_SNAPSHOT_LIST = 20
#: Repositories named in the workspace overview. The counts are always exact;
#: only the names are cut.
_SNAPSHOT_REPOS = 40


def _clip(value: Any) -> Any:
    """`value` shaped for a prompt: strings cut, lists cut, nothing else."""
    if isinstance(value, str):
        return value if len(value) <= _SNAPSHOT_TEXT else value[:_SNAPSHOT_TEXT] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:_SNAPSHOT_LIST]]
    return value


async def _active_rules_count(session: Any, ws: str, slug: str | None) -> int | None:
    """Enabled review rules in force: the repository's composed with the
    workspace's, or the workspace's own. None when the store is not there."""
    if not rules_store_available():
        return None
    try:
        from src.review import rules_store

        if slug:
            return len(await rules_store.effective_rules_for(ws, slug, session=session))
        return (await rules_store.counts(ws, scope="workspace",
                                         session=session))["active"]
    except Exception as exc:  # noqa: BLE001 — a count is optional, never the answer
        logger.warning("review_settings_rules_unavailable ws=%s err=%s", ws, exc)
        return None


async def read_review_settings(
    actor: Actor, session: Any, repo_slug: str | None = None,
) -> dict[str, Any]:
    """The review settings in force, and where each one comes from.

    `repo_slug` None is the workspace: its defaults over the built-in values,
    plus which repositories override what. A slug is one repository: its
    policy over the workspace's over the built-in. Members read, as on the
    GET routes; a repository is also subject to the team's `read` grant, the
    rule the settings overview applies per repository.
    """
    import asyncio

    from sqlalchemy import select

    from src.api.auto_review import get_auto_review_store
    from src.api.deps import enforce_repo_permission
    from src.api.routers import agents as agents_router
    from src.api.routers.review_defaults import _require_member, overridden_fields
    from src.api.routers.review_policies import (
        _load_workspace_defaults,
        _workspace_review_language_layer,
    )
    from src.db.models import RepoReviewPolicy
    from src.review.prompt_guidelines import resolve_guidelines
    from src.review.review_defaults import (
        INHERITABLE_FIELDS,
        agent_participation,
        install_defaults,
        resolve,
    )

    user = _user_for(actor)
    ws = actor.workspace_id
    await _as_action(_require_member(user, ws))

    slug: str | None = None
    if repo_slug and str(repo_slug).strip():
        slug = resolve_repo(actor, repo_slug)
        await _as_action(enforce_repo_permission(slug, user, "read", ws))

    # First, before anything else touches the session: a database behind the
    # migration is rolled back inside, and a rollback expires what is loaded.
    ws_defaults = await _load_workspace_defaults(session, ws)
    row = None
    if slug:
        row = await session.get(RepoReviewPolicy, slug)
        if row is not None and row.workspace_id != ws:
            row = None   # another tenant's row under the same slug: not ours
    values, sources = resolve(row, ws_defaults, install_defaults())

    language, language_source = await asyncio.to_thread(
        _workspace_review_language_layer, ws)
    own_language = getattr(row, "review_language", None) if row else None

    fields: dict[str, Any] = {
        name: {"value": _clip(values[name]), "source": sources[name]}
        for name in INHERITABLE_FIELDS
    }
    fields["review_language"] = {
        "value": own_language or language,
        "source": "repo" if own_language else language_source,
    }

    participation = agent_participation(
        values["disabled_agents"], values["enabled_agents"])

    # Guidelines and replaced prompts are per agent, in two stores: the
    # workspace's in the credential store (blocking), the repository's on its
    # policy row. `resolve_guidelines` is what a review calls to combine them.
    repo_guidelines = dict(getattr(row, "agent_prompt_guidelines", None) or {}) if row else {}
    extend = set(getattr(row, "agent_guidelines_extend", None) or []) if row else set()
    repo_overrides = dict(getattr(row, "agent_prompt_overrides", None) or {}) if row else {}

    def _per_agent() -> tuple[dict[str, Any], list[str]]:
        found: dict[str, Any] = {}
        replaced: list[str] = []
        for agent in agents_router._AGENTS:
            entries = resolve_guidelines(
                repo_text=repo_guidelines.get(agent),
                workspace_text=agents_router.get_workspace_guidelines(agent, ws),
                extend=agent in extend)
            if entries:
                text = "\n\n".join(e.text for e in entries)
                found[agent] = {
                    "from": "+".join(e.source for e in entries),
                    "chars": len(text), "text": _clip(text),
                }
            repo_text = repo_overrides.get(agent)
            if (isinstance(repo_text, str) and repo_text.strip()) or (
                    agents_router._load_override(agent, ws) is not None):
                replaced.append(agent)
        return found, replaced

    guidelines, replaced = await asyncio.to_thread(_per_agent)

    configs = await asyncio.to_thread(get_auto_review_store().list_for_workspace, ws)
    repos_overview: list[dict[str, Any]] = []
    if slug:
        cfg = next((c for c in configs if c.repo_slug == slug), None)
        auto_review = ({"enabled": bool(cfg.enabled), "branch": cfg.branch,
                        "mode": cfg.mode} if cfg is not None else None)
    else:
        policies = {r.repo_slug: r for r in (await session.scalars(
            select(RepoReviewPolicy).where(RepoReviewPolicy.workspace_id == ws)
        )).all()}
        from src.api.deps import readable_repo_slugs

        visible = await readable_repo_slugs(user, ws, [c.repo_slug for c in configs])
        seen: set[str] = set()
        total = 0
        auto_on = 0
        for c in sorted(configs, key=lambda c: c.full_name):
            if c.repo_slug in seen:
                continue
            seen.add(c.repo_slug)
            if c.repo_slug not in visible:  # a repository they may not read is not named
                continue
            total += 1
            auto_on += 1 if c.enabled else 0
            policy = policies.get(c.repo_slug) or policies.get(c.full_name)
            overridden = overridden_fields(policy) if policy is not None else []
            off = policy is not None and not policy.enabled
            if (overridden or off) and len(repos_overview) < _SNAPSHOT_REPOS:
                repos_overview.append({
                    "repo": c.repo_slug, "overridden": overridden,
                    "review_enabled": not off, "auto_review": bool(c.enabled),
                })
        auto_review = {"repos_total": total,
                       "auto_review_on": auto_on}

    return {
        "scope": "repo" if slug else "workspace",
        "repo": slug,
        "review_enabled": (row is None or bool(row.enabled)) if slug else None,
        "fields": fields,
        "agents_on": [a for a, on in participation.items() if on],
        "agents_off": [a for a, on in participation.items() if not on],
        "guidelines": guidelines,
        "prompt_replaced_agents": replaced,
        "rules_enabled": await _active_rules_count(session, ws, slug),
        "auto_review": auto_review,
        "repos": repos_overview,
        "links": [{"label": "policy" if slug else "defaults",
                   "href": _settings_href(slug)}],
    }
