"""Workspace review defaults — the layer under every repository's policy.

Endpoints (the caller's ACTIVE workspace only):
    GET /api/review-defaults — members read
    PUT /api/review-defaults — workspace owner / admin (or a global admin)

What a repository's review does when its own policy (/api/review-policies)
says nothing: which agents take part and whether the verifier's veto runs,
per-agent model / output ceiling / reasoning / temperature, the inline-comment
threshold and cap, the PR summary and its instructions, the "review started"
comment, the review language, ignore globs, target branches and the
prefilter's suppressed rules.

    repo policy (non-null) > workspace default (non-null) > install > built-in

Two of those already had a workspace home before this router existed — the
per-agent LLM block (`agents`) and `review_language` in the workspace LLM
config that /settings/llm edits. They stay there: this router reads and writes
them in place, through the same validator /api/llm/config uses, rather than
growing a second workspace copy that could disagree with the first. The rest
live in `workspace_review_defaults` (one row per workspace).

Write is the workspace admin's, like PUT /api/llm/config: these settings
change what every repository of the workspace spends and posts. There is no
prompt text here, so the prompt editor's gate is not the right one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    is_workspace_admin,
    require_workspace_admin,
    workspace_role,
)
from src.api.schemas import WorkspaceReviewDefaultsIn, WorkspaceReviewDefaultsOut
from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review-defaults", tags=["review-defaults"])

#: Fields stored on the defaults row (the LLM-config ones are handled apart).
_ROW_FIELDS = (
    "disabled_agents", "verifier_enabled", "comment_min_severity",
    "max_inline_comments", "summary_enabled", "summary_instructions",
    "started_comment_enabled", "ignore_globs", "target_branches",
    "suppressed_rules",
)


async def _require_member(user: User, ws_id: str) -> None:
    """403 for a caller who is not a member of the active workspace — only
    reachable for the shared single-tenant default, which `current_workspace_id`
    may hand anyone. The same rule /overrides-summary applies."""
    if user.is_admin:
        return
    role = await asyncio.to_thread(workspace_role, user.id, ws_id)
    if role is None:
        from src.deployment import is_multi_tenant
        if is_multi_tenant():
            raise HTTPException(status_code=403, detail="Not a member of this workspace")


def _workspace_llm(ws_id: str) -> dict[str, Any]:
    """The workspace LLM config blob, {} when unreadable. Blocking."""
    try:
        from src.api.routers.llm import _load_workspace_config
        return _load_workspace_config(ws_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_defaults_llm_config_unavailable ws=%s err=%s", ws_id, exc)
        return {}


async def _repo_override_counts(session: AsyncSession, ws_id: str) -> dict[str, int]:
    """field → how many repo policies of this workspace override it."""
    rows = (await session.scalars(
        select(RepoReviewPolicy).where(RepoReviewPolicy.workspace_id == ws_id)
    )).all()
    counts: dict[str, int] = {name: 0 for name in (*_ROW_FIELDS, "review_language", "agents")}
    for row in rows:
        for name in _ROW_FIELDS:
            value = getattr(row, name, None)
            if name == "summary_instructions" and isinstance(value, str):
                value = value.strip() or None
            if value is not None:
                counts[name] += 1
        if getattr(row, "review_language", None):
            counts["review_language"] += 1
        if row.agent_llm_overrides or any(
            getattr(row, c, None) for c in (
                "architect_model", "security_model", "quality_model", "verifier_model")
        ):
            counts["agents"] += 1
    return counts


async def _out(
    session: AsyncSession, user: User, ws_id: str,
    defaults: dict[str, Any] | None, row: WorkspaceReviewDefaults | None,
) -> WorkspaceReviewDefaultsOut:
    from src.api.routers.review_policies import (
        COMMENT_SEVERITY_LEVELS,
        TOGGLEABLE_AGENTS,
        _effective_agents_for_display,
        _install_defaults,
        _language_codes,
        _llm_agent_names,
    )
    from src.review.review_defaults import resolve

    install = _install_defaults()
    effective, sources = resolve(None, defaults, install)
    cfg = await asyncio.to_thread(_workspace_llm, ws_id)
    language = cfg.get("review_language")
    language = language.strip() if isinstance(language, str) and language.strip() else None
    effective["review_language"] = language or "en"
    sources = {**sources, "review_language": "workspace" if language else "install"}
    install = {**install, "review_language": "en"}
    agents = {
        k: dict(v) for k, v in (cfg.get("agents") or {}).items() if isinstance(v, dict)
    }
    sources["agents"] = "workspace" if agents else "install"
    can_edit = await asyncio.to_thread(is_workspace_admin, user, ws_id)
    own = defaults or {}
    return WorkspaceReviewDefaultsOut(
        workspace_id=ws_id,
        **{name: own.get(name) for name in _ROW_FIELDS},
        review_language=language,
        agents=agents,
        agents_effective=await _effective_agents_for_display({}, ws_id),
        install=install,
        effective=effective,
        sources=sources,
        toggleable_agents=list(TOGGLEABLE_AGENTS),
        llm_agents=list(_llm_agent_names()),
        review_languages=list(_language_codes()),
        comment_severity_levels=list(COMMENT_SEVERITY_LEVELS),
        repo_overrides=await _repo_override_counts(session, ws_id),
        can_edit=bool(can_edit),
        updated_by=getattr(row, "updated_by", None),
        updated_at=getattr(row, "updated_at", None),
    )


@router.get("", response_model=WorkspaceReviewDefaultsOut)
async def get_review_defaults(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> WorkspaceReviewDefaultsOut:
    """The active workspace's review defaults, what is in force for a
    repository that overrides nothing, and where each value comes from."""
    from src.review.review_defaults import defaults_from_row

    await _require_member(user, ws_id)
    row = await session.get(WorkspaceReviewDefaults, ws_id)
    return await _out(session, user, ws_id, defaults_from_row(row), row)


def _clean_branches(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(b.strip() for b in raw if b and b.strip()))


@router.put("", response_model=WorkspaceReviewDefaultsOut)
async def put_review_defaults(
    payload: WorkspaceReviewDefaultsIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> WorkspaceReviewDefaultsOut:
    """Change the active workspace's review defaults. A key absent keeps what
    is stored; null goes back to the install default. Everything is validated
    before anything is written, so a 422 leaves no half-saved defaults."""
    from src.api.routers.review_policies import (
        TOGGLEABLE_AGENTS,
        _comment_min_severity_from_payload,
        _ignore_globs_from_payload,
        _review_language_from_payload,
        _suppressed_rules_from_payload,
    )
    from src.review.review_defaults import defaults_from_row

    fields = payload.model_fields_set
    updates: dict[str, Any] = {}

    if "disabled_agents" in fields:
        if payload.disabled_agents is None:
            updates["disabled_agents"] = None
        else:
            unknown = [a for a in payload.disabled_agents if a not in TOGGLEABLE_AGENTS]
            if unknown:
                raise HTTPException(status_code=422, detail=(
                    f"disabled_agents: unknown agent(s) {', '.join(unknown)} — "
                    f"the switchable agents are: {', '.join(TOGGLEABLE_AGENTS)}"
                ))
            names = list(dict.fromkeys(payload.disabled_agents))
            # The veto is a stage with its own switch here; the deny-list's
            # old spelling of off is translated rather than stored, so the
            # workspace layer has one way to say it.
            if "verifier" in names:
                names.remove("verifier")
                if "verifier_enabled" not in fields:
                    updates["verifier_enabled"] = False
            updates["disabled_agents"] = names
    if "verifier_enabled" in fields:
        updates["verifier_enabled"] = payload.verifier_enabled
    if "comment_min_severity" in fields:
        updates["comment_min_severity"] = _comment_min_severity_from_payload(
            payload.comment_min_severity)
    if "max_inline_comments" in fields:
        updates["max_inline_comments"] = payload.max_inline_comments
    if "summary_enabled" in fields:
        updates["summary_enabled"] = payload.summary_enabled
    if "summary_instructions" in fields:
        updates["summary_instructions"] = (payload.summary_instructions or "").strip() or None
    if "started_comment_enabled" in fields:
        updates["started_comment_enabled"] = payload.started_comment_enabled
    if "ignore_globs" in fields:
        globs = _ignore_globs_from_payload(payload.ignore_globs)
        updates["ignore_globs"] = globs or None
    if "target_branches" in fields:
        updates["target_branches"] = (
            None if payload.target_branches is None
            else (_clean_branches(payload.target_branches) or None)
        )
    if "suppressed_rules" in fields:
        updates["suppressed_rules"] = _suppressed_rules_from_payload(payload.suppressed_rules)

    language_set = "review_language" in fields
    language = _review_language_from_payload(payload.review_language) if language_set else None
    agents_set = "agents" in fields and payload.agents is not None

    # The LLM-config half: validated against the config as it will be AFTER
    # this save, by the validator /api/llm/config uses, and saved under the
    # same lock every writer of that blob holds.
    if language_set or agents_set:
        from src.api.routers.llm import (
            _agent_overrides_from_payload,
            _load_workspace_config,
            _save_workspace_config,
            workspace_config_lock,
        )

        def _save_llm() -> None:
            with workspace_config_lock(ws_id):
                cfg = dict(_load_workspace_config(ws_id))
                if agents_set:
                    cfg["agents"] = _agent_overrides_from_payload(
                        payload.agents or {}, cfg, ws_id)
                if language_set:
                    if language is None:
                        cfg.pop("review_language", None)
                    else:
                        cfg["review_language"] = language
                _save_workspace_config(cfg, user.email, ws_id)

        await asyncio.to_thread(_save_llm)

    row = await session.get(WorkspaceReviewDefaults, ws_id)
    if updates:
        if row is None:
            row = WorkspaceReviewDefaults(workspace_id=ws_id)
            session.add(row)
        for name, value in updates.items():
            setattr(row, name, value)
        row.updated_by = user.email
        await session.commit()
        await session.refresh(row)

    changed = sorted(
        [*updates, *(["review_language"] if language_set else []),
         *(["agents"] if agents_set else [])]
    )
    logger.info("review_defaults_saved ws=%s by=%s fields=%s",
                ws_id, user.email, ",".join(changed) or "-")
    if changed:
        from src.security import audit

        audit.record_action(
            action="review_defaults.changed", actor=user.email, actor_id=user.id,
            workspace_id=ws_id, target="review-defaults", ip=client_ip(request),
            detail={"fields": changed},
        )
    return await _out(session, user, ws_id, defaults_from_row(row), row)
