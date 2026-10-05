"""MCP tools for operations and review configuration, written once.

Both MCP builders (`server.py` for stdio, `http_app.py` for the HTTP mount) call
`register_ops_tools`, so a tool cannot exist on a laptop and not in production.
Each body is one call into `src.automation.actions_ops` / `actions` — the same
functions the in-app agent runs — and a refusal comes back as
`{"ok": False, "error": ...}`, an answer an agent can act on, not a traceback
it retries.

Scopes (the `_TOOL_SCOPES` map in http_app is built from `OPS_TOOL_SCOPES`):

    read:graph     get_spend, get_budget, list_alerts, list_jobs, audit_delta,
                   export_sbom, list_members
    read:reviews   get_usage, get_review_settings
    write:repos    ack_alert, retry_job, cancel_job, cancel_dep_audit — the
                   operational writes, next to start_dep_audit
    write:config   set_budget, update_review_setting, propose_review_rules,
                   generate_review_rules — they change how the workspace is
                   configured; the scope is new, so no token issued earlier
                   carries it

A scope is a ceiling; the action still applies the caller's own workspace role.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

READ_GRAPH = "read:graph"
READ_REVIEWS = "read:reviews"
WRITE_REPOS = "write:repos"
WRITE_CONFIG = "write:config"

OPS_TOOL_SCOPES: dict[str, str] = {
    "get_spend": READ_GRAPH,
    "get_budget": READ_GRAPH,
    "list_alerts": READ_GRAPH,
    "list_jobs": READ_GRAPH,
    "audit_delta": READ_GRAPH,
    "export_sbom": READ_GRAPH,
    "list_members": READ_GRAPH,
    "get_usage": READ_REVIEWS,
    "get_review_settings": READ_REVIEWS,
    "ack_alert": WRITE_REPOS,
    "retry_job": WRITE_REPOS,
    "cancel_job": WRITE_REPOS,
    "cancel_dep_audit": WRITE_REPOS,
    "set_budget": WRITE_CONFIG,
    "update_review_setting": WRITE_CONFIG,
    "propose_review_rules": WRITE_CONFIG,
    "generate_review_rules": WRITE_CONFIG,
}


def enforcing(scope: str) -> Callable[[Any], Any]:
    """Per-call scope check for the HTTP mount.

    `tools/list` is filtered by `_TOOL_SCOPES`, but a hidden tool can still be
    CALLED by name, so a read-only token would reach a write tool it cannot see.
    This refuses the call itself. A token with no scopes at all is the legacy
    full-access kind (the listing treats it the same way) and passes; any
    scoped token needs `scope` or `admin`.
    """
    import asyncio
    import functools

    def check(name: str) -> None:
        from src.mcp_server.scopes import _check_scopes

        try:
            from mcp.server.auth.middleware.auth_context import get_access_token

            token = get_access_token()
        except Exception:  # noqa: BLE001 — no auth context: nothing to enforce
            token = None
        if token is not None and (token.scopes or []):
            _check_scopes({scope}, name)

    def decorator(fn: Any) -> Any:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                check(fn.__name__)
                return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            check(fn.__name__)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def register_ops_tools(
    mcp: Any,
    actor: Callable[..., Any],
    in_session: Callable[..., Any],
    scoped: Callable[[str], Callable[[Any], Any]],
) -> None:
    """Register the tools on `mcp`.

    `actor(label, writing=...)` and `in_session(fn)` are the builder's own
    (identity resolution and the async session differ in plumbing, not in
    meaning); `scoped(scope)` is the builder's per-tool scope decorator —
    `require_scopes` on stdio, `enforcing` on HTTP (which also filters
    tools/list by `_TOOL_SCOPES`).
    """
    from src.automation import actions, actions_ops
    from src.automation.actions import ActionError

    async def _read(fn: Callable[[Any, Any], Any]) -> dict[str, Any]:
        try:
            who = actor("mcp")
            return {"ok": True, **await in_session(lambda s: fn(who, s))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    async def _write(fn: Callable[[Any, Any], Any]) -> dict[str, Any]:
        try:
            who = actor("mcp", writing=True)
            return {"ok": True, **await in_session(lambda s: fn(who, s))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    # ─── reads ───────────────────────────────────────────────────────

    @mcp.tool(
        name="get_spend",
        description=(
            "LLM spend of the workspace over a period: totals, tokens, cache "
            "hit, the top surfaces / models / agents / repositories / "
            "operations, and a daily series. days 1-365 (default 30); "
            "optional surface, model and repo_slug filters; bucket "
            "hour|day|week|month for the series. Workspace owner or admin "
            "only, as the Usage page."
        ),
    )
    @scoped(READ_GRAPH)
    async def _get_spend(
        days: int = 30, surface: str | None = None, model: str | None = None,
        repo_slug: str | None = None, bucket: str = "day",
    ) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.get_spend(
            a, s, days=days, surface=surface, model=model, repo=repo_slug,
            bucket=bucket))

    @mcp.tool(
        name="get_usage",
        description=(
            "Code-review run usage over a period: runs (completed / failed), "
            "tokens, cost and a daily series. Needs owner, admin or editor on "
            "the workspace — the same gate as the Analytics page."
        ),
    )
    @scoped(READ_REVIEWS)
    async def _get_usage(days: int = 30) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.get_usage(a, days=days))

    @mcp.tool(
        name="get_budget",
        description=(
            "The workspace's monthly spend cap and how much of it is used: "
            "cap_usd, spent_usd, used_pct, alert_pct, hard_stop, over_cap."
        ),
    )
    @scoped(READ_GRAPH)
    async def _get_budget() -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.get_budget(a))

    @mcp.tool(
        name="list_alerts",
        description=(
            "Incoming monitoring alerts of the workspace, newest first: id, "
            "title, severity, status (new | acked | fixed), repo hint. "
            "Optional status filter; limit up to 50. Use the id with "
            "ack_alert."
        ),
    )
    @scoped(READ_GRAPH)
    async def _list_alerts(
        status: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.list_alerts(
            a, s, status=status, limit=limit))

    @mcp.tool(
        name="list_jobs",
        description=(
            "Background jobs of this workspace (indexing, documentation, "
            "audits, reviews) with counts per status. Optional status "
            "(pending | running | done | failed | dead | cancelled) and kind "
            "filters; limit up to 50. Use the id with retry_job / cancel_job."
        ),
    )
    @scoped(READ_GRAPH)
    async def _list_jobs(
        status: str | None = None, kind: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.list_jobs(
            a, status=status, kind=kind, limit=limit))

    @mcp.tool(
        name="audit_delta",
        description=(
            "What changed in the dependency audit against the previous run: "
            "vulnerabilities that appeared, that were resolved, and those "
            "that only left the audit's scope (not fixes). Omit run_id for "
            "the latest finished run."
        ),
    )
    @scoped(READ_GRAPH)
    async def _audit_delta(run_id: str | None = None) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.audit_delta(a, s, run_id=run_id))

    @mcp.tool(
        name="export_sbom",
        description=(
            "Where to download the CycloneDX SBOM of a finished dependency "
            "audit: returns a url (the existing export endpoint), not the "
            "file. repo_slug for one repository, otherwise a zip of all. "
            "Omit run_id for the latest finished run. Fetch the url with the "
            "same bearer token."
        ),
    )
    @scoped(READ_GRAPH)
    async def _export_sbom(
        run_id: str | None = None, repo_slug: str | None = None,
    ) -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.export_sbom(
            a, s, run_id=run_id, repo=repo_slug))

    @mcp.tool(
        name="list_members",
        description=(
            "Members of the workspace with their roles and teams. Read only "
            "— inviting people or changing roles is done in the app."
        ),
    )
    @scoped(READ_GRAPH)
    async def _list_members() -> dict[str, Any]:
        return await _read(lambda a, s: actions_ops.list_members(a, s))

    @mcp.tool(
        name="get_review_settings",
        description=(
            "The code-review settings in force: workspace defaults or one "
            "repository (repo_slug), where each value comes from (repo | "
            "workspace | install), which agents run, guidelines, rules and "
            "which repositories override the defaults."
        ),
    )
    @scoped(READ_REVIEWS)
    async def _get_review_settings(repo_slug: str | None = None) -> dict[str, Any]:
        return await _read(lambda a, s: actions.read_review_settings(
            a, s, repo_slug=repo_slug))

    # ─── writes ──────────────────────────────────────────────────────

    @mcp.tool(
        name="set_budget",
        description=(
            "Set the workspace's monthly spend cap in USD (0 = no cap), the "
            "alert percentage (1-100) and hard_stop (block calls once the cap "
            "is reached). Needs owner or admin on the workspace."
        ),
    )
    @scoped(WRITE_CONFIG)
    async def _set_budget(
        monthly_usd_cap: float, alert_pct: int = 80, hard_stop: bool = False,
    ) -> dict[str, Any]:
        return await _write(lambda a, s: actions_ops.set_budget(
            a, s, monthly_usd_cap=monthly_usd_cap, alert_pct=alert_pct,
            hard_stop=hard_stop))

    @mcp.tool(
        name="ack_alert",
        description=(
            "Mark an incoming alert acked (default), fixed, or new again. "
            "alert_id from list_alerts."
        ),
    )
    @scoped(WRITE_REPOS)
    async def _ack_alert(alert_id: str, status: str = "acked") -> dict[str, Any]:
        return await _write(lambda a, s: actions_ops.ack_alert(
            a, s, alert_id=alert_id, status=status))

    @mcp.tool(
        name="retry_job",
        description=(
            "Put a dead, failed or cancelled background job back in the "
            "queue. job_id from list_jobs. Needs owner or admin on the "
            "workspace, and the job must belong to it."
        ),
    )
    @scoped(WRITE_REPOS)
    async def _retry_job(job_id: str) -> dict[str, Any]:
        return await _write(lambda a, s: actions_ops.retry_job(a, job_id=job_id))

    @mcp.tool(
        name="cancel_job",
        description=(
            "Ask a RUNNING background job to stop at its next checkpoint. "
            "job_id from list_jobs. Needs owner or admin on the workspace, "
            "and the job must belong to it."
        ),
    )
    @scoped(WRITE_REPOS)
    async def _cancel_job(job_id: str) -> dict[str, Any]:
        return await _write(lambda a, s: actions_ops.cancel_job(a, job_id=job_id))

    @mcp.tool(
        name="cancel_dep_audit",
        description=(
            "Stop a queued or running dependency audit. Omit run_id for the "
            "live one; a finished run is left as it is."
        ),
    )
    @scoped(WRITE_REPOS)
    async def _cancel_dep_audit(run_id: str | None = None) -> dict[str, Any]:
        return await _write(lambda a, s: actions_ops.cancel_dep_audit(
            a, s, run_id=run_id))

    @mcp.tool(
        name="update_review_setting",
        description=(
            "Change ONE code-review setting for the workspace "
            "(scope='workspace', owner/admin) or one repository "
            "(scope='repo' + repo_slug, editor or higher plus the repo's "
            "review grant). key: run_on_drafts | approve_when_clean | "
            "request_changes_on_critical | committable_suggestions | "
            "comment_min_severity (info|warning|error|critical) | "
            "max_inline_comments (1-100) | summary_enabled | review_language "
            "| disabled_agents (list) | agent_prompt_guidelines "
            "({agent: text}, ADDED to the built-in prompt). value null "
            "inherits again. Validated by the same schema as the settings "
            "page."
        ),
    )
    @scoped(WRITE_CONFIG)
    async def _update_review_setting(
        scope: str, key: str, value: Any = None, repo_slug: str | None = None,
    ) -> dict[str, Any]:
        return await _write(lambda a, s: actions.update_review_setting(
            a, s, scope=scope, key=key, value=value, repo_slug=repo_slug))

    @mcp.tool(
        name="propose_review_rules",
        description=(
            "Propose review rules for one repository (or the workspace where "
            "the review-rules list exists). rules: a list of {title, "
            "instructions, path_glob?, severity? info|warning|error|critical, "
            "agents?}; at most 10 at a time. Where the rules list exists they "
            "are saved PENDING for an editor's approval."
        ),
    )
    @scoped(WRITE_CONFIG)
    async def _propose_review_rules(
        rules: list[dict[str, Any]], repo_slug: str | None = None,
    ) -> dict[str, Any]:
        return await _write(lambda a, s: actions.propose_review_rules(
            a, s, repo_slug=repo_slug, rules=rules))

    @mcp.tool(
        name="generate_review_rules",
        description=(
            "Draft review rules for one repository from its code, for "
            "approval. Refused where the installation cannot generate them."
        ),
    )
    @scoped(WRITE_CONFIG)
    async def _generate_review_rules(repo_slug: str) -> dict[str, Any]:
        return await _write(lambda a, s: actions.generate_review_rules(
            a, s, repo_slug=repo_slug))

    logger.debug("mcp_ops_tools_registered count=%d", len(OPS_TOOL_SCOPES))


__all__ = ["OPS_TOOL_SCOPES", "register_ops_tools"]
