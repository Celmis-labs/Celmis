"""Review rules — the workspace's rules library (/admin/review-rules).

Endpoints:
    GET    /api/review-rules                    — list (scope/repo/status/origin/q)
    POST   /api/review-rules                    — create one rule
    PATCH  /api/review-rules/{id}               — edit one rule
    DELETE /api/review-rules/{id}               — delete one rule
    POST   /api/review-rules/bulk-status        — approve / reject / re-queue many
    POST   /api/review-rules/bulk-delete        — delete many
    GET    /api/review-rules/library            — the built-in library (search)
    POST   /api/review-rules/library/add        — copy library entries in
    POST   /api/review-rules/generate           — propose rules for a repo (job)
    POST   /api/review-rules/import             — import repo convention files (job)
    POST   /api/review-rules/generate-from-history — rules from past feedback (job)
    GET    /api/review-rules/jobs               — recent jobs
    GET    /api/review-rules/jobs/{id}          — one job's progress

Who may do what, by the same rules as the per-repo review policies:

    read     any member of the active workspace — rules are what every
             reviewer of the workspace's code is held to;
    write    editor, admin or owner of the active workspace (rules are
             prompts, the editor's job); and for a repository's rules the
             repository must be registered in this workspace and the
             caller's teams must grant `review` on it.

Every row is read and written in the ACTIVE workspace only: an id or a slug
of another workspace answers 404, never its data. Every write is audited.

The reading and writing itself is `src.review.rules_store` — the same module
the Celmis agent's actions call, so a rule cannot be valid in one place and
refused in the other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    client_ip,
    current_workspace_id,
    enforce_repo_permission,
    get_current_user,
    require_prompt_editor,
)
from src.db.session import get_async_session
from src.review import rules_store as store
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review-rules", tags=["review-rules"])

Status = Literal["active", "pending", "rejected"]
Severity = Literal["info", "warning", "error", "critical"]


# ─── Schemas ─────────────────────────────────────────────────────────


class ReviewRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None = a workspace-wide rule; a slug = that repository's rule.
    repo_slug: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=store.MAX_TITLE)
    instructions: str = Field(min_length=1, max_length=store.MAX_INSTRUCTIONS)
    path_glob: str | None = Field(default=None, max_length=store.MAX_GLOB)
    severity: Severity = "warning"
    agents: list[str] = Field(default_factory=list, max_length=20)
    examples_good: str | None = Field(default=None, max_length=store.MAX_EXAMPLE)
    examples_bad: str | None = Field(default=None, max_length=store.MAX_EXAMPLE)
    status: Literal["active", "pending"] = "active"


class ReviewRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=store.MAX_TITLE)
    instructions: str | None = Field(
        default=None, min_length=1, max_length=store.MAX_INSTRUCTIONS)
    path_glob: str | None = Field(default=None, max_length=store.MAX_GLOB)
    severity: Severity | None = None
    agents: list[str] | None = Field(default=None, max_length=20)
    examples_good: str | None = Field(default=None, max_length=store.MAX_EXAMPLE)
    examples_bad: str | None = Field(default=None, max_length=store.MAX_EXAMPLE)
    status: Status | None = None


class BulkStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=500)
    status: Status


class BulkDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=500)


class LibraryAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=100)
    repo_slug: str | None = Field(default=None, max_length=300)
    status: Literal["active", "pending"] = "active"


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_slug: str = Field(min_length=1, max_length=300)
    count: int = Field(default=8, ge=1, le=20)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_slug: str = Field(min_length=1, max_length=300)


# ─── Helpers ─────────────────────────────────────────────────────────


def _registered_slug(repo_slug: str, ws_id: str) -> str | None:
    """The slug `repo_slug` is registered under in `ws_id` (either spelling
    accepted, the registered slug returned), or None. Blocking."""
    from src.api.auto_review import get_auto_review_store

    for cfg in get_auto_review_store().list_for_workspace(ws_id):
        if repo_slug in (cfg.repo_slug, cfg.full_name):
            return cfg.repo_slug
    return None


async def _writable_repo(repo_slug: str, user: User, ws_id: str) -> str:
    """404 unless the repository is this workspace's, 403 unless the
    caller's teams grant `review` on it; the registered slug."""
    slug = await asyncio.to_thread(_registered_slug, repo_slug.strip(), ws_id)
    if slug is None:
        raise HTTPException(status_code=404,
                            detail="Repository not registered in this workspace")
    await enforce_repo_permission(slug, user, "review", ws_id)
    return slug


async def _can_edit(user: User, ws_id: str) -> bool:
    if user.is_admin:
        return True
    from src.api.deps import workspace_role
    from src.users.roles import PROMPT_EDITOR_ROLES

    return await asyncio.to_thread(workspace_role, user.id, ws_id) in PROMPT_EDITOR_ROLES


def _audit(action: str, *, user: User, ws_id: str, target: str | None,
           request: Request | None, detail: dict[str, Any]) -> None:
    from src.security.audit import record_action

    record_action(
        action=action, actor=user.email, actor_id=user.id, workspace_id=ws_id,
        target=target or "workspace", ip=client_ip(request), detail=detail,
    )


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, store.RuleValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, store.RuleConflictError | store.RuleLimitError):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


async def _rules_in_ws(ids: list[int], ws_id: str, user: User,
                       session: AsyncSession) -> list[dict]:
    """The rules of `ws_id` among `ids`, with `review` checked on every
    repository they belong to. 404 when none of them is this workspace's."""
    rows = await store.get_rules(ws_id, ids, session=session)
    if not rows:
        raise HTTPException(status_code=404, detail="No such rule in this workspace")
    for slug in sorted({r["repo_slug"] for r in rows if r["repo_slug"]}):
        await enforce_repo_permission(slug, user, "review", ws_id)
    return rows


def _legacy_folder_rules(policy_row: Any) -> list[dict]:
    out = []
    for rule in (getattr(policy_row, "folder_rules", None) or []):
        if isinstance(rule, dict) and rule.get("pattern") and rule.get("prompt"):
            out.append({
                "pattern": str(rule["pattern"]), "prompt": str(rule["prompt"]),
                "title": str(rule.get("title") or ""),
                "severity_hint": str(rule.get("severity_hint") or ""),
                "agents": list(rule.get("agents") or []),
            })
    return out


# ─── Endpoints: rules ────────────────────────────────────────────────


@router.get("")
async def list_review_rules(
    scope: Literal["all", "workspace", "repo"] = Query(default="all"),
    repo: str | None = Query(default=None, max_length=300),
    status_: Status | None = Query(default=None, alias="status"),
    origin: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The workspace's rules. With `repo`, that repository's own rules, the
    legacy `folder_rules` of its review policy (read-only here, edited on the
    policy page) and how many workspace rules also apply to it."""
    from src.db.models import RepoReviewPolicy

    repo_slug = (repo or "").strip() or None
    scope_arg = None if scope == "all" else scope
    if repo_slug:
        scope_arg = "repo"
    rules = await store.list_rules(
        ws_id, repo_slug, status_, scope=scope_arg, origin=origin, q=q, session=session)
    everything = await store.list_rules(
        ws_id, repo_slug, scope=scope_arg, session=session)
    counts = {"all": len(everything), "active": 0, "pending": 0, "rejected": 0}
    for r in everything:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    legacy: list[dict] = []
    workspace_active = 0
    if repo_slug:
        row = await session.get(RepoReviewPolicy, repo_slug)
        if row is not None and row.workspace_id == ws_id:
            legacy = _legacy_folder_rules(row)
        workspace_active = len(await store.list_rules(
            ws_id, None, "active", scope="workspace", session=session))
    return {
        "rules": rules,
        "counts": counts,
        "repo_slug": repo_slug,
        "legacy_folder_rules": legacy,
        "workspace_active_count": workspace_active,
        "can_edit": await _can_edit(user, ws_id),
        "target_agents": list(store.target_agents()),
        "severities": list(store.SEVERITIES),
        "origins": list(store.ORIGINS),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_review_rule(
    payload: ReviewRuleIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    repo_slug = None
    if payload.repo_slug and payload.repo_slug.strip():
        repo_slug = await _writable_repo(payload.repo_slug, user, ws_id)
    try:
        rule = await store.create_rule(
            ws_id, repo_slug=repo_slug, title=payload.title,
            instructions=payload.instructions, path_glob=payload.path_glob,
            severity=payload.severity, agents=payload.agents,
            examples_good=payload.examples_good, examples_bad=payload.examples_bad,
            status=payload.status, origin="manual", created_by=user.email,
            session=session,
        )
    except (store.RuleValidationError, store.RuleConflictError,
            store.RuleLimitError) as exc:
        raise _store_error(exc) from exc
    _audit("review_rules.created", user=user, ws_id=ws_id, target=repo_slug,
           request=request, detail={"ids": [rule["id"]], "status": rule["status"]})
    return rule


@router.post("/bulk-status")
async def bulk_status(
    payload: BulkStatusIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Approve (active), reject or send back to pending — many at once."""
    rows = await _rules_in_ws(payload.ids, ws_id, user, session)
    done = await store.set_status(ws_id, [r["id"] for r in rows], payload.status,
                                  user.email, session=session)
    _audit("review_rules.status_changed", user=user, ws_id=ws_id,
           target=",".join(sorted({r["repo_slug"] or "workspace" for r in rows})),
           request=request, detail={"ids": done, "status": payload.status})
    return {"updated": done, "status": payload.status}


@router.post("/bulk-delete")
async def bulk_delete(
    payload: BulkDeleteIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    rows = await _rules_in_ws(payload.ids, ws_id, user, session)
    done = await store.delete_rules(ws_id, [r["id"] for r in rows], user.email,
                                    session=session)
    _audit("review_rules.deleted", user=user, ws_id=ws_id,
           target=",".join(sorted({r["repo_slug"] or "workspace" for r in rows})),
           request=request, detail={"ids": done,
                                    "titles": [r["title"][:80] for r in rows][:50]})
    return {"deleted": done}


# ─── Endpoints: library ──────────────────────────────────────────────


@router.get("/library")
async def library(
    q: str | None = Query(default=None, max_length=200),
    language: str | None = Query(default=None, max_length=32),
    tag: str | None = Query(default=None, max_length=32),
    repo: str | None = Query(default=None, max_length=300),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The built-in library. `added` marks the entries the chosen scope (the
    workspace, or `repo`) already holds by title."""
    from src.review.rules_library import LANGUAGES, TAGS, search_library

    repo_slug = (repo or "").strip() or None
    scope_rules = await store.list_rules(
        ws_id, repo_slug, scope="repo" if repo_slug else "workspace", session=session)
    have = {" ".join(r["title"].split()).casefold() for r in scope_rules}
    entries = []
    for entry in search_library(q, language=language, tag=tag):
        item = entry.as_dict()
        item["added"] = " ".join(entry.title.split()).casefold() in have
        entries.append(item)
    return {"rules": entries, "languages": list(LANGUAGES), "tags": list(TAGS)}


@router.post("/library/add", status_code=status.HTTP_201_CREATED)
async def library_add(
    payload: LibraryAddIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    repo_slug = None
    if payload.repo_slug and payload.repo_slug.strip():
        repo_slug = await _writable_repo(payload.repo_slug, user, ws_id)
    try:
        ids = await store.add_from_library(ws_id, repo_slug, payload.ids,
                                           payload.status, user.email, session=session)
    except store.RuleValidationError as exc:
        raise _store_error(exc) from exc
    _audit("review_rules.library_added", user=user, ws_id=ws_id, target=repo_slug,
           request=request, detail={"ids": ids, "library_ids": payload.ids[:50],
                                    "status": payload.status})
    return {"created": ids, "skipped": len(set(payload.ids)) - len(ids)}


# ─── Endpoints: generate / import ────────────────────────────────────


async def _start(kind: str, repo_slug: str, user: User, ws_id: str,
                 background: BackgroundTasks, request: Request) -> dict[str, Any]:
    from src.review import rules_generate

    slug = await _writable_repo(repo_slug, user, ws_id)
    job, created = await rules_generate.start_job(ws_id, slug, kind, user)
    if created:
        # Runs after the response is sent, in this process; the row is what
        # the page polls, and a row orphaned by a restart reads as failed.
        background.add_task(rules_generate.run_job, job["id"], user)
        _audit(f"review_rules.{kind}_started", user=user, ws_id=ws_id, target=slug,
               request=request, detail={"job_id": job["id"]})
    return job


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    payload: GenerateIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Propose rules for a repository with the workspace's review model.
    Proposals arrive pending; nothing reaches a review until approved."""
    return await _start("generate", payload.repo_slug, user, ws_id, background, request)


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_from_repo(
    payload: ImportIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Read the repository's convention files into pending rules."""
    return await _start("import", payload.repo_slug, user, ws_id, background, request)


@router.post("/generate-from-history", status_code=status.HTTP_202_ACCEPTED)
async def generate_from_history(
    payload: ImportIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Propose rules from what the team dismissed and fixed in past reviews of
    this repository. Proposals arrive pending, origin "learned"."""
    return await _start("history", payload.repo_slug, user, ws_id, background, request)


@router.get("/jobs")
async def jobs(
    repo: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=10, ge=1, le=50),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[dict[str, Any]]:
    from src.review.rules_generate import list_jobs

    return await list_jobs(ws_id, (repo or "").strip() or None, limit=limit)


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: str,
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    from src.review.rules_generate import get_job

    job = await get_job(ws_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job in this workspace")
    return job


# ─── Endpoints: one rule (after the static paths above) ──────────────


@router.patch("/{rule_id}")
async def update_review_rule(
    rule_id: int,
    payload: ReviewRulePatch,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    await _rules_in_ws([rule_id], ws_id, user, session)
    changes = payload.model_dump(exclude_unset=True)
    try:
        rule = await store.update_rule(ws_id, rule_id, changes, user.email, session=session)
    except (store.RuleValidationError, store.RuleConflictError) as exc:
        raise _store_error(exc) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail="No such rule in this workspace")
    _audit("review_rules.updated", user=user, ws_id=ws_id, target=rule["repo_slug"],
           request=request, detail={"ids": [rule_id], "fields": sorted(changes)})
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review_rule(
    rule_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    rows = await _rules_in_ws([rule_id], ws_id, user, session)
    await store.delete_rules(ws_id, [rule_id], user.email, session=session)
    _audit("review_rules.deleted", user=user, ws_id=ws_id, target=rows[0]["repo_slug"],
           request=request, detail={"ids": [rule_id], "titles": [rows[0]["title"][:80]]})
