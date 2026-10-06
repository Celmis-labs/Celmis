"""Agent catalog + workspace-level prompt override storage (Stage 11).

    GET  /api/agents                        — list of the review agents with
                                              description, current system
                                              prompt (default or overridden),
                                              default model, and metadata.
    PUT  /api/agents/{name}/prompt          — override the system prompt for
                                              this agent at the workspace level.
    DELETE /api/agents/{name}/prompt        — reset to built-in default.
    PUT  /api/agents/{name}/guidelines      — the workspace's team guidelines
                                              for this agent: text ADDED to
                                              its prompt (at most 2000 chars,
                                              src/review/prompt_guidelines.py).
    DELETE /api/agents/{name}/guidelines    — drop them.

Replacing the prompt (`/prompt`) is the advanced mode; guidelines are the
default customisation and are appended to whichever prompt won.

Every write needs editor, admin or owner of the active workspace
(`require_prompt_editor`): editing prompts is what the editor role is for.

Storage: uses the existing credential store as a generic key/value under
provider="__agent_prompt__" (replacements) and "__agent_guidelines__"
(guidelines) to avoid a new migration for something small.
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
    require_prompt_editor,
)
from src.llm.keys import workspace_slot
from src.review.prompt_guidelines import (
    GUIDELINES_MAX_CHARS,
    MIGRATION_REVISION,
    clamp_guidelines,
    classify_legacy_override,
)
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
    "performance": {
        "display_name": "Performance",
        "role": "Costs that grow with something — the Performance category",
        "focus": [
            "N+1: a query or HTTP call per item inside a loop",
            "Quadratic work, loop-invariant work, the same call twice",
            "Blocking I/O on async or per-request paths",
            "Memory that grows with input, unbounded queries, DOM thrash",
        ],
        "context_used": [
            "Diff hunks — every changed line, swept to the last one",
            "Brief blast radius — how often the changed code is reached",
            "Style guide + repo-specific rules from admin panel",
        ],
        "default_severity": "warning",
        "verdict_impact": "critical — failure blocks APPROVE verdict",
        "settings_model_field": "performance_model",
    },
    "business_logic": {
        "display_name": "Business logic",
        "role": "The change against the PR's stated intent — off by default",
        "focus": [
            "Contradictions between the diff and the PR title/description",
            "Acceptance criteria or promised behaviour nothing implements",
            "The Jira task the PR names: each numbered criterion checked against the diff",
            "Edge cases the description names or plainly implies",
            "Skips quietly when neither the PR nor its Jira task says what the change is for",
        ],
        "context_used": [
            "PR title, description, acceptance criteria and issue keys",
            "The Jira task (summary, description, acceptance criteria) when Jira is connected",
            "Diff hunks",
            "Repo-specific rules from admin panel",
        ],
        "default_severity": "warning",
        "verdict_impact": "critical when it runs — failure blocks APPROVE; a skip does not",
        "settings_model_field": "business_logic_model",
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
#: The workspace's team guidelines, one row per agent, beside the overrides.
_GUIDELINES_PROVIDER = "__agent_guidelines__"
#: One installation-wide row once the pre-2.3.2 overrides were sorted into
#: guidelines and replacements (`migrate_workspace_prompt_overrides`).
_MIGRATION_PROVIDER = "__agent_prompt_migration__"


def _store_load(provider: str, agent_name: str, workspace_id: str) -> str | None:
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    try:
        stored = store.load(
            provider=provider,
            user_id=workspace_slot(workspace_id),   # per-workspace value
            account_label=agent_name,
        )
    except CredentialStoreError:
        return None
    if stored is None:
        return None
    return stored.secret


def _store_save(provider: str, agent_name: str, text: str, metadata: dict,
                workspace_id: str) -> None:
    from src.credentials import get_credential_store

    get_credential_store().save(
        provider=provider,
        secret=text,
        metadata=metadata,
        user_id=workspace_slot(workspace_id),
        account_label=agent_name,
    )


def _store_delete(provider: str, agent_name: str, workspace_id: str) -> None:
    from src.credentials import get_credential_store

    get_credential_store().delete(
        provider=provider,
        user_id=workspace_slot(workspace_id),
        account_label=agent_name,
    )


def _load_override(agent_name: str, workspace_id: str = "default") -> str | None:
    return _store_load(_PROMPT_PROVIDER, agent_name, workspace_id)


def _save_override(agent_name: str, prompt: str, updated_by: str, workspace_id: str = "default") -> None:
    _store_save(_PROMPT_PROVIDER, agent_name, prompt,
                {"updated_by": updated_by}, workspace_id)


def _delete_override(agent_name: str, workspace_id: str = "default") -> None:
    _store_delete(_PROMPT_PROVIDER, agent_name, workspace_id)


def _load_guidelines(agent_name: str, workspace_id: str = "default") -> str | None:
    return _store_load(_GUIDELINES_PROVIDER, agent_name, workspace_id)


def _save_guidelines(agent_name: str, text: str, updated_by: str,
                     workspace_id: str = "default") -> None:
    _store_save(_GUIDELINES_PROVIDER, agent_name, text,
                {"updated_by": updated_by}, workspace_id)


def _delete_guidelines(agent_name: str, workspace_id: str = "default") -> None:
    _store_delete(_GUIDELINES_PROVIDER, agent_name, workspace_id)


# ─── 2.3.2: sorting the old overrides (workspace layer) ─────────────
#
# The repository layer is converted by Alembic migration c5d6e7f8a9b0. This
# layer lives in the credential store — encrypted, and in a database Alembic
# does not manage — so it is converted here, by the API's startup hook (and
# `python -m src.review.prompt_guidelines_cli migrate`). The rule is the same
# one (`classify_legacy_override`): a short text with no output contract and
# no "You are …" opening becomes guidelines; anything else stays a
# replacement, exactly as it behaved before.
#
# ONCE per installation, then marked: after the upgrade a short replacement
# is something a person chose in the Advanced box, and a second pass would
# quietly turn it into guidelines. Nothing on the read path converts or
# writes — a review, a page load or a test only ever reads.
# Reversible with `revert_workspace_prompts_migration` (the CLI's `revert`).

#: The slot the installation-wide marker lives in — not a workspace.
_SYSTEM_SLOT = "__system__"


def _convert_slot(store, slot: str) -> list[str]:
    """Move one workspace slot's guideline-like overrides to guidelines."""
    converted: list[str] = []
    for agent_name in _AGENTS:
        stored = store.load(provider=_PROMPT_PROVIDER, user_id=slot,
                            account_label=agent_name, update_last_used=False)
        if stored is None or classify_legacy_override(stored.secret) != "guidelines":
            continue
        if store.load(provider=_GUIDELINES_PROVIDER, user_id=slot,
                      account_label=agent_name, update_last_used=False) is not None:
            continue   # never overwrite guidelines someone already wrote
        store.save(provider=_GUIDELINES_PROVIDER, secret=stored.secret.strip(),
                   metadata={"updated_by": "migration",
                             "migrated_from": _PROMPT_PROVIDER,
                             "migration": MIGRATION_REVISION},
                   user_id=slot, account_label=agent_name)
        store.delete(provider=_PROMPT_PROVIDER, user_id=slot, account_label=agent_name)
        converted.append(agent_name)
    return converted


def migrate_workspace_prompt_overrides(*, store=None) -> dict[str, list[str]] | None:
    """Sort every workspace's pre-2.3.2 overrides once. Returns {slot:
    agents converted}, or None when it had already run (or cannot run here:
    a store that cannot list its slots is left exactly as it was)."""
    if store is None:
        from src.credentials import get_credential_store
        store = get_credential_store()
    if store.load(provider=_MIGRATION_PROVIDER, user_id=_SYSTEM_SLOT,
                  account_label=MIGRATION_REVISION, update_last_used=False) is not None:
        return None
    if not hasattr(store, "slots_with"):
        logger.warning("agent_prompt_migration_skipped — this credential store "
                       "cannot list workspaces; run the CLI per workspace")
        return None
    done: dict[str, list[str]] = {}
    for slot in store.slots_with(provider=_PROMPT_PROVIDER):
        converted = _convert_slot(store, slot)
        if converted:
            done[slot] = converted
            # The audit line: which agents' prompts changed meaning — never
            # their text.
            logger.warning(
                "agent_prompt_overrides_became_guidelines slot=%s agents=%s "
                "migration=%s — these overrides read as guideline lists and are "
                "now ADDED to the built-in prompt instead of replacing it",
                slot, ",".join(converted), MIGRATION_REVISION)
    store.save(provider=_MIGRATION_PROVIDER, secret="done",
               metadata={"migration": MIGRATION_REVISION, "converted": done},
               user_id=_SYSTEM_SLOT, account_label=MIGRATION_REVISION)
    logger.info("agent_prompt_migration_done workspaces=%d agents=%d",
                len(done), sum(len(v) for v in done.values()))
    return done


def revert_workspace_prompts_migration(*, store=None) -> dict[str, list[str]]:
    """Undo `migrate_workspace_prompt_overrides`: every guideline it created
    goes back to being the replacement override it was, and the marker is
    removed. Guidelines a person wrote after the upgrade are left alone.
    Returns {slot: agents restored}."""
    if store is None:
        from src.credentials import get_credential_store
        store = get_credential_store()
    restored: dict[str, list[str]] = {}
    for slot in store.slots_with(provider=_GUIDELINES_PROVIDER):
        for agent_name in _AGENTS:
            stored = store.load(provider=_GUIDELINES_PROVIDER, user_id=slot,
                                account_label=agent_name, update_last_used=False)
            if stored is None or (stored.metadata or {}).get("migration") != MIGRATION_REVISION:
                continue
            if store.load(provider=_PROMPT_PROVIDER, user_id=slot,
                          account_label=agent_name, update_last_used=False) is None:
                store.save(provider=_PROMPT_PROVIDER, secret=stored.secret,
                           metadata={"updated_by": "migration-revert"},
                           user_id=slot, account_label=agent_name)
            store.delete(provider=_GUIDELINES_PROVIDER, user_id=slot,
                         account_label=agent_name)
            restored.setdefault(slot, []).append(agent_name)
    store.delete(provider=_MIGRATION_PROVIDER, user_id=_SYSTEM_SLOT,
                 account_label=MIGRATION_REVISION)
    logger.warning("agent_prompt_guidelines_reverted workspaces=%d agents=%d",
                   len(restored), sum(len(v) for v in restored.values()))
    return restored


def guidelines_hint(agent_name: str) -> str:
    """The short default description of what an agent looks for — Kodus's
    category description — shown beside the guidelines box. What the
    built-in prompt already covers, so a team writes only what it adds."""
    meta = _AGENTS.get(agent_name) or {}
    focus = "\n".join(f"- {f}" for f in meta.get("focus", []))
    return f"{meta.get('role', '')}\n{focus}".strip()


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
    #: The prompt the agent starts from at this workspace: the replacement
    #: when there is one (advanced mode), else the built-in.
    system_prompt: str
    user_prompt_template: str
    #: True when the workspace REPLACES the built-in prompt.
    has_override: bool
    #: The workspace's team guidelines — ADDED to `system_prompt`. "" = none.
    guidelines: str = ""
    has_guidelines: bool = False
    guidelines_max: int = GUIDELINES_MAX_CHARS
    #: What the agent already looks for, in a few lines (Kodus's default
    #: category description) — the guidelines box's help text.
    guidelines_hint: str = ""


class AgentPromptIn(BaseModel):
    system_prompt: str = Field(min_length=10, max_length=100_000)


class AgentGuidelinesIn(BaseModel):
    guidelines: str = Field(min_length=1, max_length=GUIDELINES_MAX_CHARS)


# ─── Endpoints ───────────────────────────────────────────────────────


def _agent_out(name: str, workspace_id: str) -> AgentOut:
    meta = _AGENTS[name]
    override = _load_override(name, workspace_id)
    prompt = override if override is not None else _default_system_prompt(name)
    guidelines = clamp_guidelines(_load_guidelines(name, workspace_id))
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
        guidelines=guidelines,
        has_guidelines=bool(guidelines),
        guidelines_hint=guidelines_hint(name),
    )


@router.get("", response_model=list[AgentOut])
def list_agents(
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[AgentOut]:
    """List the review agents with descriptions, current prompts and the
    workspace's guidelines for each.

    Returns default + override status so the UI can render an inline diff
    ("2 lines changed vs default") without a separate call.
    """
    return [_agent_out(name, workspace_id) for name in _AGENTS]


@router.get("/{name}", response_model=AgentOut)
def get_agent(
    name: str, _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    return _agent_out(name, workspace_id)


@router.put("/{name}/prompt", response_model=AgentOut)
def override_prompt(
    name: str,
    payload: AgentPromptIn,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    """Workspace-level REPLACEMENT of an agent's system prompt (advanced).

    Persisted (Fernet-encrypted) in the credentials store, scoped to the
    caller's workspace. Takes effect on the next review run. Guidelines, if
    any, are still appended to it.
    """
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    _save_override(name, payload.system_prompt, updated_by=user.email, workspace_id=workspace_id)
    logger.info("agent_prompt_overridden name=%s workspace=%s by=%s len=%d",
                name, workspace_id, user.email, len(payload.system_prompt))
    return get_agent(name, _user=user, workspace_id=workspace_id)


@router.delete("/{name}/prompt", response_model=AgentOut)
def reset_prompt(
    name: str, user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    _delete_override(name, workspace_id)
    logger.info("agent_prompt_reset name=%s workspace=%s by=%s", name, workspace_id, user.email)
    return get_agent(name, _user=user, workspace_id=workspace_id)


@router.put("/{name}/guidelines", response_model=AgentOut)
def set_guidelines(
    name: str,
    payload: AgentGuidelinesIn,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    """The workspace's team guidelines for this agent — appended to its
    prompt in every repository that does not set its own."""
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    text = payload.guidelines.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Guidelines are empty — use DELETE to remove them")
    _save_guidelines(name, text, updated_by=user.email, workspace_id=workspace_id)
    logger.info("agent_guidelines_set name=%s workspace=%s by=%s len=%d",
                name, workspace_id, user.email, len(text))
    return get_agent(name, _user=user, workspace_id=workspace_id)


@router.delete("/{name}/guidelines", response_model=AgentOut)
def reset_guidelines(
    name: str, user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentOut:
    if name not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent {name!r}")
    _delete_guidelines(name, workspace_id)
    logger.info("agent_guidelines_reset name=%s workspace=%s by=%s",
                name, workspace_id, user.email)
    return get_agent(name, _user=user, workspace_id=workspace_id)


# ─── Public helpers — used by the review pipeline ───────────────────


def get_effective_system_prompt(agent_name: str, workspace_id: str = "default") -> str:
    """Called from the review pipeline. Returns the workspace's override if set,
    else the default."""
    override = _load_override(agent_name, workspace_id)
    if override is not None:
        return override
    return _default_system_prompt(agent_name)


def get_workspace_guidelines(agent_name: str, workspace_id: str = "default") -> str:
    """The workspace's guidelines for `agent_name`, clamped; "" for none."""
    return clamp_guidelines(_load_guidelines(agent_name, workspace_id))


__all__ = [
    "router",
    "get_effective_system_prompt",
    "get_workspace_guidelines",
    "migrate_workspace_prompt_overrides",
    "revert_workspace_prompts_migration",
]
