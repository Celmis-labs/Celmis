"""Agent catalog + workspace-level prompt override storage (Stage 11).

    GET  /api/agents                        — list of 5 review agents with
                                              description, current system
                                              prompt (default or overridden),
                                              default model, and metadata.
    PUT  /api/agents/{name}/prompt          — override the system prompt for
                                              this agent at the workspace level.
    DELETE /api/agents/{name}/prompt        — reset to built-in default.

Storage: uses the existing credential store as a generic key/value under
provider="__agent_prompt__" to avoid a new migration for something small.
The value is Fernet-encrypted like any other secret — cheap, safe, and
already backed by concurrent-access tests.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_workspace_admin,
)
from src.llm.keys import workspace_slot
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ─── Agent registry ──────────────────────────────────────────────────

_AGENTS = {
    "defect": {
        "display_name": "Defect",
        "role": "Single-file provable defects — the main finder",
        "focus": [
            "Wrong results, exceptions, unintended behaviour when the code runs",
            "Copy-paste survivors, dead branches, off-by-ones, wrong arguments",
            "Check-then-act races, unawaited async, swallowed exceptions",
            "New behaviour-changing branches nothing exercises",
        ],
        "context_used": [
            "Diff hunks — every changed line, swept to the last one",
            "Style guide + brief blast radius",
            "Repo-specific rules from admin panel",
        ],
        "default_severity": "warning",
        "verdict_impact": "critical — failure blocks APPROVE verdict",
        "settings_model_field": "defect_model",
    },
    "contract": {
        "display_name": "Contract",
        "role": "Cross-file claims — callers, consumers, sibling repos",
        "focus": [
            "A caller the change breaks — quoted from the graph, on the caller's line",
            "Serialization/API boundaries a consumer still expects",
            "Cross-repo semantic drift (deterministic grep — mandatory finding)",
            "Backward compatibility with a NAMED caller, never with a guessed one",
        ],
        "context_used": [
            "Full symbol graph blast radius (callers/callees)",
            "Cross-repo caller count (materialized edges)",
            "Cross-repo drift signal (deterministic grep)",
            "Repo overview",
        ],
        "default_severity": "warning",
        "verdict_impact": "critical — failure blocks APPROVE verdict",
        "settings_model_field": "contract_model",
    },
    "security": {
        "display_name": "Security",
        "role": "OWASP Top 10 / CWE Top 25 adversarial review",
        "focus": [
            "A01–A10 OWASP 2025 categories",
            "CWE Top 25 — SQL injection, XSS, SSRF, hardcoded secrets",
            "Auth/session/authorization bypasses",
            "Cleartext storage of sensitive data",
        ],
        "context_used": [
            "Diff hunks — full context of changed lines",
            "Style guide + repo overview",
        ],
        "default_severity": "critical",
        "verdict_impact": "critical — failure blocks APPROVE verdict",
        "settings_model_field": "security_model",
    },
    "verifier": {
        "display_name": "Verifier",
        "role": "Deduplication + false-positive filter",
        "focus": [
            "Merge findings that reference the same line/issue",
            "Drop findings with low confidence (< 0.5)",
            "Cross-check against the diff — no phantom line numbers",
            "Suppress findings that other agents already made stronger",
        ],
        "context_used": [
            "All findings from the finder agents",
            "Original diff for phantom-line-number verification",
        ],
        "default_severity": "n/a — post-processor",
        "verdict_impact": "runs after all agents; doesn't affect verdict directly",
        "settings_model_field": "verifier_model",
    },
}


def _default_system_prompt(agent_name: str) -> str:
    """Import the agent module and read its `_SYSTEM` global."""
    import importlib

    module = importlib.import_module(f"src.review.agents.{agent_name}")
    return getattr(module, "_SYSTEM", "")


def _default_user_template(agent_name: str) -> str:
    import importlib

    module = importlib.import_module(f"src.review.agents.{agent_name}")
    return getattr(module, "_USER_TEMPLATE", "")


# ─── Overrides via credential store ──────────────────────────────────

# Provider slug used for the prompt overrides. Doesn't clash with any real
# LLM/git provider — the credentials_v2 (user_id, provider, account_label)
# tuple is naturally distinct.
_PROMPT_PROVIDER = "__agent_prompt__"


def _load_override(agent_name: str, workspace_id: str = "default") -> str | None:
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    try:
        stored = store.load(
            provider=_PROMPT_PROVIDER,
            user_id=workspace_slot(workspace_id),   # per-workspace override
            account_label=agent_name,
        )
    except CredentialStoreError:
        return None
    if stored is None:
        return None
    return stored.secret


def _save_override(agent_name: str, prompt: str, updated_by: str, workspace_id: str = "default") -> None:
    from src.credentials import get_credential_store

    store = get_credential_store()
    store.save(
        provider=_PROMPT_PROVIDER,
        secret=prompt,
        metadata={"updated_by": updated_by},
        user_id=workspace_slot(workspace_id),
        account_label=agent_name,
    )


def _delete_override(agent_name: str, workspace_id: str = "default") -> None:
    from src.credentials import get_credential_store

    store = get_credential_store()
    store.delete(
        provider=_PROMPT_PROVIDER,
        user_id=workspace_slot(workspace_id),
        account_label=agent_name,
    )


# ─── Schemas ─────────────────────────────────────────────────────────


class AgentOut(BaseModel):
    name: str
    display_name: str
    role: str
    focus: list[str]
    context_used: list[str]
    default_severity: str
    verdict_impact: str
    settings_model_field: str
    system_prompt: str
    user_prompt_template: str
    has_override: bool


class AgentPromptIn(BaseModel):
    system_prompt: str = Field(min_length=10, max_length=100_000)


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("", response_model=list[AgentOut])
def list_agents(
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[AgentOut]:
    """List all 5 review agents with descriptions and current prompts.

    Returns default + override status so the UI can render an inline diff
    ("2 lines changed vs default") without a separate call.
    """
    out: list[AgentOut] = []
    for name, meta in _AGENTS.items():
        override = _load_override(name, workspace_id)
        prompt = override if override is not None else _default_system_prompt(name)
        out.append(AgentOut(
            name=name,
            display_name=meta["display_name"],
            role=meta["role"],
            focus=meta["focus"],
            context_used=meta["context_used"],
            default_severity=meta["default_severity"],
            verdict_impact=meta["verdict_impact"],
            settings_model_field=meta["settings_model_field"],
            system_prompt=prompt,
            user_prompt_template=_default_user_template(name),
            has_override=override is not None,
        ))
    return out


@router.get("/{name}", response_model=AgentOut)
def get_agent(
    name: str, _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    meta = _AGENTS[name]
    override = _load_override(name, workspace_id)
    prompt = override if override is not None else _default_system_prompt(name)
    return AgentOut(
        name=name,
        display_name=meta["display_name"],
        role=meta["role"],
        focus=meta["focus"],
        context_used=meta["context_used"],
        default_severity=meta["default_severity"],
        verdict_impact=meta["verdict_impact"],
        settings_model_field=meta["settings_model_field"],
        system_prompt=prompt,
        user_prompt_template=_default_user_template(name),
        has_override=override is not None,
    )


@router.put("/{name}/prompt", response_model=AgentOut)
def override_prompt(
    name: str,
    payload: AgentPromptIn,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    """Workspace-level override of an agent's system prompt.

    Persisted (Fernet-encrypted) in the credentials store, scoped to the
    caller's workspace. Takes effect on the next review run.
    """
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    _save_override(name, payload.system_prompt, updated_by=user.email, workspace_id=workspace_id)
    logger.info("agent_prompt_overridden name=%s workspace=%s by=%s len=%d",
                name, workspace_id, user.email, len(payload.system_prompt))
    return get_agent(name, _user=user, workspace_id=workspace_id)


@router.delete("/{name}/prompt", response_model=AgentOut)
def reset_prompt(
    name: str, user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    _delete_override(name, workspace_id)
    logger.info("agent_prompt_reset name=%s workspace=%s by=%s", name, workspace_id, user.email)
    return get_agent(name, _user=user, workspace_id=workspace_id)


# ─── Public helper — used by LLMReviewAgent to fetch override ───────


def get_effective_system_prompt(agent_name: str, workspace_id: str = "default") -> str:
    """Called from the review pipeline. Returns the workspace's override if set,
    else the default."""
    override = _load_override(agent_name, workspace_id)
    if override is not None:
        return override
    return _default_system_prompt(agent_name)


__all__ = [
    "router",
    "get_effective_system_prompt",
]
