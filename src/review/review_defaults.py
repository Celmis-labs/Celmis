"""Workspace review defaults: the layer between a repo policy and the install.

Every review setting a repository can override is resolved the same way,
by the review that runs and by the page that shows it:

    repo policy (non-null) > workspace default (non-null) > install > built-in

"install" is env / `ReviewSettings` (REVIEW_MAX_INLINE_COMMENTS,
REVIEW_VERIFIER_ENABLED, the suppressed-rule set) or, where no env knob
exists, the built-in value (every agent runs, summary on, every branch).

Per-agent model / output ceiling / reasoning are not here: they already have
a workspace layer in the LLM config blob and their own resolver
(`src.review.settings.resolve_agent_llm`). The review language likewise lives
in that blob (`review_language`).

Pure functions plus one blocking loader; no FastAPI, so the orchestrator can
import it without the API's import graph.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

logger = logging.getLogger(__name__)

Source = Literal["repo", "workspace", "install"]

#: The settings a workspace may default and a repository may override, in the
#: order the API reports them. NULL / None at a layer always means "inherit".
INHERITABLE_FIELDS: tuple[str, ...] = (
    "disabled_agents",
    "verifier_enabled",
    "comment_min_severity",
    "max_inline_comments",
    "summary_enabled",
    "summary_instructions",
    "started_comment_enabled",
    "ignore_globs",
    "target_branches",
    "suppressed_rules",
)

#: Fields whose value is a list (copied, never shared, between layers).
_LIST_FIELDS = frozenset({
    "disabled_agents", "ignore_globs", "target_branches", "suppressed_rules",
})


def defaults_from_row(row: Any) -> dict[str, Any] | None:
    """A `WorkspaceReviewDefaults` row (or any object with its attributes)
    as the dict the resolver reads; None stays None."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for name in INHERITABLE_FIELDS:
        value = getattr(row, name, None)
        out[name] = list(value) if (name in _LIST_FIELDS and value is not None) else value
    return out


def install_defaults(settings: Any | None = None) -> dict[str, Any]:
    """What every field resolves to when neither a repo nor the workspace
    says anything. `settings` is a `ReviewSettings`; read when omitted."""
    if settings is None:
        from src.review.settings import get_review_settings
        settings = get_review_settings()
    return {
        "disabled_agents": [],
        "verifier_enabled": bool(getattr(settings, "verifier_enabled", False)),
        "comment_min_severity": "info",
        "max_inline_comments": int(getattr(settings, "max_inline_comments", 20)),
        "summary_enabled": True,
        "summary_instructions": None,
        "started_comment_enabled": True,
        "ignore_globs": [],
        "target_branches": [],
        "suppressed_rules": sorted(getattr(settings, "suppressed_rules", ()) or ()),
    }


def _layer_value(layer: Any, name: str) -> Any:
    if layer is None:
        return None
    value = layer.get(name) if isinstance(layer, dict) else getattr(layer, name, None)
    if name == "summary_instructions" and isinstance(value, str) and not value.strip():
        return None
    return value


def resolve(
    repo: Any, workspace: dict[str, Any] | None, install: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Source]]:
    """(effective values, which layer each came from) for every inheritable
    field. `verifier_enabled` folds in the deny-list's old spelling of off:
    "verifier" in the effective `disabled_agents` wins, as it does in
    `ReviewOrchestrator._verifier_enabled`."""
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for name in INHERITABLE_FIELDS:
        for source, layer in (("repo", repo), ("workspace", workspace)):
            value = _layer_value(layer, name)
            if value is not None:
                values[name] = list(value) if name in _LIST_FIELDS else value
                sources[name] = source        # type: ignore[assignment]
                break
        else:
            default = install.get(name)
            values[name] = list(default) if isinstance(default, list) else default
            sources[name] = "install"
    if "verifier" in (values.get("disabled_agents") or []):
        values["verifier_enabled"] = False
        sources["verifier_enabled"] = sources["disabled_agents"]
    return values, sources


def inherited_value(
    name: str, workspace: dict[str, Any] | None, install: dict[str, Any],
) -> tuple[Any, Source]:
    """What `name` resolves to for a repository that does not set it."""
    values, sources = resolve(None, workspace, install)
    return values[name], sources[name]


def blank_policy() -> dict[str, Any]:
    """The policy dict of a repository with no policy row: every field
    "inherit", nothing switched off. What a review reads when something other
    than the row (workspace defaults, workspace review rules) has to ride on
    a policy the repository never wrote."""
    out: dict[str, Any] = {
        "enabled": True,
        "prompt_template": "",
        "folder_rules": [],
        "agent_prompt_overrides": {},
        "agents": {},
        "mcp_sources": [],
        "review_language": None,
    }
    for name in INHERITABLE_FIELDS:
        out[name] = None
    return out


def merge_policy(policy: Any, workspace: dict[str, Any] | None) -> Any:
    """The policy dict a review reads, with the workspace defaults filled in
    wherever the repository says nothing.

    Unresolved fields stay None so the orchestrator's own install fallbacks
    (REVIEW_MAX_INLINE_COMMENTS, REVIEW_VERIFIER_ENABLED, …) keep answering —
    one place states each install default, not two. No workspace row →
    the policy unchanged (None stays None: "no policy, defaults apply").
    """
    if not workspace or not any(workspace.get(n) is not None for n in INHERITABLE_FIELDS):
        return policy
    if policy is None:
        merged: dict[str, Any] = blank_policy()
    elif isinstance(policy, dict):
        merged = dict(policy)
    else:
        # A test double or legacy object: nothing to merge into safely.
        return policy
    for name in INHERITABLE_FIELDS:
        if merged.get(name) is None and workspace.get(name) is not None:
            value = workspace[name]
            merged[name] = list(value) if name in _LIST_FIELDS else value
        merged.setdefault(name, None)
    return merged


def _sync_url() -> str | None:
    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw_url:
        return None
    if "+psycopg" in raw_url:
        return raw_url
    if raw_url.startswith("sqlite"):
        return raw_url.replace("sqlite+aiosqlite://", "sqlite://")
    return (raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
                   .replace("postgresql://", "postgresql+psycopg://"))


def load_workspace_defaults_sync(workspace_id: str | None) -> dict[str, Any] | None:
    """The workspace's defaults row as a dict, or None. Blocking; never raises
    — a review must not fail because its defaults could not be read, it falls
    back to the install defaults and says so in the log."""
    if not workspace_id:
        return None
    url = _sync_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.db.models import WorkspaceReviewDefaults

        engine = create_engine(url, pool_pre_ping=True)
        try:
            with Session(engine) as s:
                return defaults_from_row(s.get(WorkspaceReviewDefaults, workspace_id))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_review_defaults_load_failed ws=%s err=%s",
                       workspace_id, exc)
        return None
