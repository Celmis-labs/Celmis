"""Operations verbs: spend, usage, budget, alerts, jobs, audit extras, members.

The in-app agent and the MCP server could register a repository and read an
audit, and could not answer "what did we spend this month", "which alerts are
open", "why is that job dead" — questions whose answers the pages already
had. Nothing here computes anything of its own: every verb calls the route
function the page calls, behind the SAME gate that route enforces, so the
agent cannot show a person a figure the page would refuse them, and cannot
change something the page would not let them change.

Kept apart from `actions.py` so the two files can grow without touching each
other; only its helpers (`Actor`, `ActionError`, `_user_for`, `_as_action`,
`_require_role`) are shared.

Gates, in one place
-------------------
    get_spend / get_budget   owner/admin of the workspace (require_workspace_admin)
    set_budget               owner/admin of the workspace
    get_usage                owner/admin/editor (require_analytics_access)
    list_alerts / ack_alert  any member (as the routes)
    list_jobs                any member, THIS workspace's rows only — the route
                             shows a global admin every tenant's rows; an agent
                             answering in one workspace must not
    retry_job / cancel_job   owner/admin AND a row of this workspace
    cancel_dep_audit         any member (the route's gate)
    audit_delta/export_sbom  any member of the run's workspace
    list_members             any member of the workspace
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.automation.actions import (
    ActionError,
    Actor,
    _as_action,
    _require_role,
    _user_for,
)

logger = logging.getLogger(__name__)

#: Verbs that only look. `EXPLAINED_READS` (chat.py) is the subset whose answer
#: is written by a second model call from what they return.
OPS_READS: tuple[str, ...] = (
    "get_spend", "get_usage", "get_budget", "list_alerts", "list_jobs",
    "audit_delta", "export_sbom", "list_members",
)
#: Verbs that change something. Planned, shown on a card, run on the second press.
OPS_WRITES: tuple[str, ...] = (
    "set_budget", "ack_alert", "retry_job", "cancel_job", "cancel_dep_audit",
)

#: How much of anything long travels back. Results go into a chat bubble and a
#: model prompt; the page is one link away for the rest.
_TOP = 8
_SERIES = 90
_TEXT = 300
_PAGE_LIMIT = 50

#: Where the same figures live in the app, for the "open the page" links.
LINKS = {
    "spend": "/admin/usage",
    "usage": "/analytics",
    "alerts": "/alerts",
    "jobs": "/admin/jobs",
    "deps": "/dependencies",
    "members": "/admin/users",
}


def _link(label: str, key: str) -> dict[str, str]:
    return {"label": label, "href": LINKS[key]}


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _opt(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _trim(text: Any, n: int = _TEXT) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _absolute(path: str) -> str:
    """The path on this installation's public address, when it has one — an MCP
    client is not in the browser and a bare path means nothing to it."""
    try:
        from src.config import get_settings

        base = str(getattr(get_settings(), "public_base_url", "") or "").rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return f"{base}{path}" if base else path


# ─── spend, usage, budget ────────────────────────────────────────────


async def _require_spend_reader(actor: Actor, user: Any) -> None:
    """Spend and budget are for whoever pays: owner/admin of the workspace (or
    a global admin) — the gate of `/api/spend/*` (`require_workspace_admin`)."""
    from src.api.deps import require_workspace_admin

    await _as_action(require_workspace_admin(user=user, workspace_id=actor.workspace_id))


async def get_spend(
    actor: Actor, session: Any, *, days: int = 30, surface: str | None = None,
    model: str | None = None, repo: str | None = None, bucket: str = "day",
) -> dict[str, Any]:
    """LLM spend for a period: totals, the top rows per breakdown, and the
    series. `GET /api/spend/summary` and `/daily` are the code; workspace owner
    and admin only, so the same people may ask."""
    from src.api.routers import spend

    user = _user_for(actor)
    await _require_spend_reader(actor, user)
    days = _clamp(days, 1, 365, 30)
    common = dict(days=days, since=None, until=None, surface=_opt(surface),
                  repo=_opt(repo), model=_opt(model), operation=None, agent=None)
    summary = await _as_action(spend.summary(
        **common, limit=_TOP, session=session, _user=user, ws=actor.workspace_id))
    series = await _as_action(spend.daily(
        **common, bucket=bucket if bucket in ("hour", "day", "week", "month") else "day",
        session=session, _user=user, ws=actor.workspace_id))

    def rows(items: list[Any]) -> list[dict[str, Any]]:
        return [{"key": r.key, "label": r.label or r.key, "calls": r.calls,
                 "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                 "cost_usd": r.cost_usd} for r in items[:_TOP]]

    return {
        "days": summary.days, "since": summary.since, "until": summary.until,
        "calls": summary.calls, "tokens_in": summary.tokens_in,
        "tokens_out": summary.tokens_out, "cost_usd": summary.cost_usd,
        "cache_hit_pct": summary.cache_hit_pct,
        "estimated_share_pct": summary.estimated_share_pct,
        "filters": {k: v for k, v in (("surface", common["surface"]),
                                      ("model", common["model"]),
                                      ("repo", common["repo"])) if v},
        "by_surface": rows(summary.by_surface), "by_model": rows(summary.by_model),
        "by_agent": rows(summary.by_agent), "by_repo": rows(summary.by_repo),
        "by_operation": rows(summary.by_operation),
        "daily": [p.model_dump() for p in series[-_SERIES:]],
        "links": [_link("usage", "spend")],
    }


async def get_usage(actor: Actor, *, days: int = 30) -> dict[str, Any]:
    """Review-run usage (runs, tokens, cost) — `GET /api/usage/summary`, behind
    `require_analytics_access`: a lead's view, not a member's."""
    from src.api.deps import ANALYTICS_ROLES
    from src.api.routers.usage import usage_summary

    user = _user_for(actor)
    await _require_role(actor, user, ANALYTICS_ROLES, "Usage figures")
    out = await asyncio.to_thread(
        usage_summary, days=_clamp(days, 1, 365, 30), user=user,
        workspace_id=actor.workspace_id)
    data = out.model_dump()
    data["daily"] = data["daily"][-31:]
    data["links"] = [_link("usage", "usage")]
    return data


async def get_budget(actor: Actor) -> dict[str, Any]:
    """The monthly cap and the position against it — `GET /api/spend/budget`."""
    from src.api.routers import spend

    user = _user_for(actor)
    await _require_spend_reader(actor, user)
    out = await _as_action(spend.get_budget(_user=user, ws=actor.workspace_id))
    return {**out.model_dump(), "links": [_link("budget", "spend")]}


def parse_budget(cap: Any, alert_pct: Any = 80, hard_stop: Any = False) -> dict[str, Any]:
    """The route's own schema, so a value it would reject is refused here with
    its words — before a card is shown, not after the press."""
    from pydantic import ValidationError

    from src.api.routers.spend import BudgetIn

    try:
        parsed = BudgetIn.model_validate({
            "monthly_usd_cap": cap, "alert_pct": 80 if alert_pct is None else alert_pct,
            "hard_stop": bool(hard_stop)})
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ActionError(
            f"Budget {first.get('loc', ('value',))[-1]}: {first.get('msg')}") from None
    return parsed.model_dump()


async def set_budget(
    actor: Actor, session: Any, *, monthly_usd_cap: Any, alert_pct: Any = 80,
    hard_stop: Any = False,
) -> dict[str, Any]:
    """Set the workspace's monthly spend cap — `PUT /api/spend/budget`, owner or
    admin of the workspace. `hard_stop` blocks further calls once it is
    reached; 0 switches the cap off."""
    from src.api.deps import require_workspace_admin
    from src.api.routers import spend

    user = _user_for(actor)
    await _as_action(require_workspace_admin(user=user, workspace_id=actor.workspace_id))
    payload = spend.BudgetIn.model_validate(parse_budget(monthly_usd_cap, alert_pct, hard_stop))
    out = await _as_action(spend.put_budget(
        payload=payload, session=session, admin=user, ws=actor.workspace_id))
    logger.info("budget_set_via_action ws=%s by=%s via=%s", actor.workspace_id,
                actor.email, actor.label)
    return {**out.model_dump(), "count": 1, "links": [_link("budget", "spend")]}


# ─── alerts ──────────────────────────────────────────────────────────


async def list_alerts(
    actor: Actor, session: Any, *, status: str | None = None, limit: int = 20,
) -> dict[str, Any]:
    """The workspace's incoming monitoring alerts, newest first —
    `GET /api/alerts`. `status` filters new | acked | fixed."""
    from src.api.routers import alerts

    user = _user_for(actor)
    wanted = (_opt(status) or "").lower()
    if wanted and wanted not in ("new", "acked", "fixed"):
        raise ActionError("status must be new, acked or fixed.")
    rows = await _as_action(alerts.list_alerts(
        limit=200, session=session, _user=user, workspace_id=actor.workspace_id))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    picked = [r for r in rows if not wanted or r.status == wanted]
    picked = picked[:_clamp(limit, 1, _PAGE_LIMIT, 20)]
    return {
        "alerts": [{"id": r.id, "title": _trim(r.title, 160), "body": _trim(r.body),
                    "severity": r.severity, "status": r.status, "source": r.source,
                    "repo": r.repo_hint or "", "created_at": r.created_at}
                   for r in picked],
        "count": len(picked), "by_status": counts, "filter": wanted or None,
        "links": [_link("alerts", "alerts")],
    }


async def ack_alert(
    actor: Actor, session: Any, *, alert_id: str, status: str = "acked",
) -> dict[str, Any]:
    """Mark an alert acked (or fixed, or new again) — `PATCH /api/alerts/{id}`."""
    from src.api.routers import alerts

    wanted = (_opt(status) or "acked").lower()
    if wanted not in ("new", "acked", "fixed"):
        raise ActionError("status must be new, acked or fixed.")
    if not _opt(alert_id):
        raise ActionError("Name the alert to acknowledge (its id).")
    user = _user_for(actor)
    out = await _as_action(alerts.patch_alert(
        alert_id=str(alert_id).strip(),
        payload=alerts.AlertPatchIn(status=wanted), session=session,
        _user=user, workspace_id=actor.workspace_id))
    logger.info("alert_status_via_action id=%s status=%s by=%s via=%s",
                out.id, wanted, actor.email, actor.label)
    return {"id": out.id, "title": _trim(out.title, 160), "status": out.status,
            "count": 1, "links": [_link("alerts", "alerts")]}


# ─── jobs ────────────────────────────────────────────────────────────

_JOB_FIELDS = ("id", "kind", "status", "attempts", "max_attempts", "last_error",
               "next_run_at", "started_at", "finished_at", "created_at",
               "enqueued_by")


def _slim_job(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _JOB_FIELDS if k in row}
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    if out.get("last_error"):
        out["last_error"] = _trim(out["last_error"])
    return out


async def list_jobs(
    actor: Actor, *, status: str | None = None, kind: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Background jobs of THIS workspace with counts per status —
    `GET /api/jobs` and `/stats`, minus the global admin's cross-tenant view."""
    from src.sync import queue as jq

    _user_for(actor)
    ws = actor.workspace_id
    rows = await asyncio.to_thread(
        lambda: jq.list_jobs(status=_opt(status), kind=_opt(kind),
                             limit=_clamp(limit, 1, _PAGE_LIMIT, 20), workspace_id=ws))
    stats = await asyncio.to_thread(lambda: jq.stats(workspace_id=ws))
    return {"jobs": [_slim_job(r) for r in rows], "count": len(rows), "stats": stats,
            "links": [_link("jobs", "jobs")]}


async def _own_job(actor: Actor, job_id: str) -> dict[str, Any]:
    """The row, once the caller is an admin of the workspace that owns it. 404
    wording for another tenant's id, as the route: a 403 would confirm it exists."""
    from src.api.deps import require_workspace_admin
    from src.sync import queue as jq

    user = _user_for(actor)
    await _as_action(require_workspace_admin(user=user, workspace_id=actor.workspace_id))
    if not _opt(job_id):
        raise ActionError("Name the job (its id).")
    job = await asyncio.to_thread(jq.get_job, str(job_id).strip())
    if job is None or not job.get("workspace_id") or job["workspace_id"] != actor.workspace_id:
        raise ActionError("job not found")
    return job


async def retry_job(actor: Actor, *, job_id: str) -> dict[str, Any]:
    """Put a dead, failed or cancelled job back in the queue —
    `POST /api/jobs/{id}/retry`, workspace owner/admin."""
    from src.sync import queue as jq

    job = await _own_job(actor, job_id)
    if not await asyncio.to_thread(jq.retry_dead, job["id"]):
        raise ActionError("job is not in a retryable state")
    logger.info("job_retry_via_action id=%s by=%s via=%s", job["id"], actor.email, actor.label)
    return {"id": job["id"], "kind": job.get("kind"), "status": "pending",
            "count": 1, "links": [_link("jobs", "jobs")]}


async def cancel_job(actor: Actor, *, job_id: str) -> dict[str, Any]:
    """Ask a RUNNING job to stop at its next checkpoint —
    `POST /api/jobs/{id}/cancel`, workspace owner/admin."""
    from src.sync import queue as jq

    job = await _own_job(actor, job_id)
    if not await asyncio.to_thread(jq.request_cancel, job["id"]):
        raise ActionError("job is not running")
    logger.info("job_cancel_via_action id=%s by=%s via=%s", job["id"], actor.email, actor.label)
    return {"id": job["id"], "kind": job.get("kind"), "status": "cancelling",
            "count": 1, "links": [_link("jobs", "jobs")]}


# ─── dependency audit extras ─────────────────────────────────────────


async def _run_or_latest(actor: Actor, session: Any, run_id: str | None, *,
                         statuses: tuple[str, ...] | None) -> Any:
    from sqlalchemy import select

    from src.db.models import DepAuditRun

    if _opt(run_id):
        run = await session.get(DepAuditRun, str(run_id).strip())
        if run is None or run.workspace_id != actor.workspace_id:
            raise ActionError("Run not found")
        return run
    query = select(DepAuditRun).where(DepAuditRun.workspace_id == actor.workspace_id)
    if statuses:
        query = query.where(DepAuditRun.status.in_(statuses))
    run = (await session.scalars(
        query.order_by(DepAuditRun.created_at.desc()).limit(1))).first()
    if run is None:
        raise ActionError("No audit is running." if statuses == ("queued", "running")
                          else "No audit run found.")
    return run


async def cancel_dep_audit(
    actor: Actor, session: Any, *, run_id: str | None = None,
) -> dict[str, Any]:
    """Stop a queued or running dependency audit (the live one when `run_id` is
    omitted) — `POST /api/deps/{run_id}/cancel`."""
    from src.api.routers import deps

    user = _user_for(actor)
    run = await _run_or_latest(actor, session, run_id, statuses=("queued", "running"))
    out = await _as_action(deps.cancel_run(
        run_id=run.id, session=session, user=user, workspace_id=actor.workspace_id))
    return {"run_id": out.id, "status": out.status, "error": out.error or "",
            "count": 1, "links": [_link("deps", "deps")]}


def _short(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("repo", "package", "ecosystem", "version", "severity", "id", "summary",
            "fixed_version")
    return [{k: (_trim(v, 160) if isinstance(v, str) else v)
             for k, v in it.items() if k in keep} for it in items[:_TOP * 2]]


async def audit_delta(
    actor: Actor, session: Any, *, run_id: str | None = None,
) -> dict[str, Any]:
    """What changed against the previous audit (latest finished run when
    `run_id` is omitted) — `GET /api/deps/{run_id}/delta`."""
    from src.api.routers import deps

    user = _user_for(actor)
    run = await _run_or_latest(actor, session, run_id, statuses=("done",))
    raw = await _as_action(deps.run_delta(
        run_id=run.id, session=session, user=user, workspace_id=actor.workspace_id))
    counts = raw.get("counts") or {}
    return {
        "run_id": run.id, "previous_run_id": raw.get("previous_run_id"),
        "first_run": bool(raw.get("first_run")), "headline": raw.get("headline", ""),
        "counts": counts,
        "appeared": _short(raw.get("appeared") or []),
        "resolved": _short(raw.get("resolved") or []),
        "out_of_scope": counts.get("out_of_scope", 0),
        "truncated": any(len(raw.get(k) or []) > _TOP * 2 for k in ("appeared", "resolved")),
        "links": [_link("deps", "deps")],
    }


async def export_sbom(
    actor: Actor, session: Any, *, run_id: str | None = None, repo: str | None = None,
) -> dict[str, Any]:
    """Where to download the CycloneDX bill of materials of a finished run —
    a link to `GET /api/deps/{run_id}/sbom`, not the file. One repository with
    `repo`, otherwise a zip of all. The download is behind the caller's own
    session or token, like the route."""
    from urllib.parse import quote

    _user_for(actor)
    run = await _run_or_latest(actor, session, run_id, statuses=("done",))
    if run.status != "done":
        raise ActionError("Audit is not finished — export once the run completes.")
    path = f"/api/deps/{run.id}/sbom"
    if _opt(repo):
        path += f"?repo={quote(str(repo).strip(), safe='/')}"
    return {"run_id": run.id, "repo": _opt(repo), "path": path, "url": _absolute(path),
            "format": "CycloneDX JSON" if _opt(repo) else "zip of CycloneDX JSON files",
            "links": [{"label": "sbom", "href": path}]}


# ─── members ─────────────────────────────────────────────────────────


async def list_members(actor: Actor, session: Any) -> dict[str, Any]:
    """The workspace's members with their roles and teams —
    `GET /api/workspaces/{id}/members` (any member may read the roster), plus
    team membership. Read only: inviting and changing roles is not a verb."""
    from sqlalchemy import select

    from src.api.routers import workspaces
    from src.db.models import Team, TeamMember

    user = _user_for(actor)
    rows = await _as_action(workspaces.list_members(
        ws_id=actor.workspace_id, session=session, user=user))
    teams = (await session.execute(
        select(TeamMember.user_id, Team.name)
        .join(Team, Team.id == TeamMember.team_id)
        .where(Team.workspace_id == actor.workspace_id))).all()
    by_user: dict[str, list[str]] = {}
    for uid, name in teams:
        by_user.setdefault(uid, []).append(name)
    members = [{"user_id": m.user_id, "name": m.name, "email": m.email, "role": m.role,
                "teams": sorted(by_user.get(m.user_id, []))} for m in rows]
    return {"members": members[:100], "count": len(members),
            "links": [_link("members", "members")]}


__all__ = [
    "OPS_READS", "OPS_WRITES", "ack_alert", "audit_delta", "cancel_dep_audit",
    "cancel_job", "export_sbom", "get_budget", "get_spend", "get_usage",
    "list_alerts", "list_jobs", "list_members", "parse_budget", "retry_job",
    "set_budget",
]
