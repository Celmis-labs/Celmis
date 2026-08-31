"""Resource-usage history API (global admin — installation-level data).

    GET /api/ops/metrics?hours=24          — samples + aggregates for charts
    GET /api/ops/metrics.csv?hours=24      — CSV export for docs/sizing sheets
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_admin, require_ops_access
from src.db.models import ResourceSample
from src.db.session import get_async_session
from src.ops.build import build_info, toolchain_info
from src.security.log_filter import redact_text
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops", tags=["ops"])

_FIELDS = (
    "cpu_pct", "rss_mb", "sys_mem_pct", "load1",
    "reviews_running", "jobs_running", "jobs_pending", "agent_sessions_running",
    "llm_calls", "llm_tokens_in", "llm_tokens_out", "http_requests",
)


class SampleOut(BaseModel):
    ts: str
    cpu_pct: float
    rss_mb: float
    sys_mem_pct: float
    load1: float
    reviews_running: int
    jobs_running: int
    jobs_pending: int
    agent_sessions_running: int
    llm_calls: int
    llm_tokens_in: int
    llm_tokens_out: int
    http_requests: int


class MetricsOut(BaseModel):
    samples: list[SampleOut]
    # per-field {avg, max} over the window + totals for delta counters
    aggregates: dict[str, dict[str, float]]
    window_hours: int


async def _rows(session: AsyncSession, hours: int) -> list[ResourceSample]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return list((await session.scalars(
        select(ResourceSample).where(ResourceSample.ts >= cutoff)
        .order_by(ResourceSample.ts)
    )).all())


@router.get("/metrics", response_model=MetricsOut)
async def metrics(
    hours: int = Query(default=24, ge=1, le=336),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_admin),
) -> MetricsOut:
    rows = await _rows(session, hours)
    # Downsample for the UI: at 30s cadence 24h = 2880 rows — cap at ~600 points.
    step = max(1, len(rows) // 600)
    sampled = rows[::step]

    aggregates: dict[str, dict[str, float]] = {}
    for f in _FIELDS:
        values = [float(getattr(r, f) or 0) for r in rows]
        if not values:
            aggregates[f] = {"avg": 0.0, "max": 0.0, "total": 0.0}
            continue
        aggregates[f] = {
            "avg": round(sum(values) / len(values), 2),
            "max": round(max(values), 2),
            "total": round(sum(values), 2),
        }

    return MetricsOut(
        samples=[SampleOut(
            ts=r.ts.isoformat(),
            **{f: getattr(r, f) or 0 for f in _FIELDS},
        ) for r in sampled],
        aggregates=aggregates,
        window_hours=hours,
    )


@router.get("/metrics.csv", response_class=PlainTextResponse)
async def metrics_csv(
    hours: int = Query(default=24, ge=1, le=336),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_admin),
) -> PlainTextResponse:
    rows = await _rows(session, hours)
    lines = ["ts," + ",".join(_FIELDS)]
    for r in rows:
        lines.append(
            r.ts.isoformat() + "," +
            ",".join(str(getattr(r, f) or 0) for f in _FIELDS)
        )
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition":
                 f'attachment; filename="celmis-metrics-{hours}h.csv"'},
    )


# ─── Debug log tail (global admin) ───────────────────────────────────
#
# The box is not always SSH-reachable, so `docker logs` can be off the
# table. This exposes the in-memory ring buffer (src/ops/logbuf.py) so an
# admin can see what the server is actually doing — from any network.


@router.get("/logs")
def logs_tail(
    limit: int = Query(default=200, ge=1, le=2000),
    level: str | None = Query(default=None, description="min level, e.g. WARNING"),
    contains: str | None = Query(default=None, description="substring filter"),
    logger_prefix: str | None = Query(default=None, alias="logger"),
    _access: str = Depends(require_ops_access),
) -> dict:
    from src.ops import logbuf
    return {
        "records": logbuf.tail(
            limit=limit, level=level, contains=contains,
            logger_prefix=logger_prefix,
        ),
        "stats": logbuf.stats(),
    }


@router.get("/logs.txt", response_class=PlainTextResponse)
def logs_tail_text(
    limit: int = Query(default=200, ge=1, le=2000),
    level: str | None = Query(default=None),
    contains: str | None = Query(default=None),
    logger_prefix: str | None = Query(default=None, alias="logger"),
    _access: str = Depends(require_ops_access),
) -> PlainTextResponse:
    """Same tail as plain text — friendlier for curl/grep."""
    from src.ops import logbuf
    lines = [
        f"{r['ts']} {r['level']:<8} {r['logger']} ({r['module']}) {r['message']}"
        + (f"\n{r['exc']}" if r.get("exc") else "")
        for r in logbuf.tail(limit=limit, level=level, contains=contains,
                             logger_prefix=logger_prefix)
    ]
    return PlainTextResponse("\n".join(lines) or "(no records)")


@router.get("/check-repo")
def check_repo_access(
    slug: str = Query(description="registered repo slug"),
    _access: str = Depends(require_ops_access),
) -> dict:
    """Ask the provider directly whether the stored token can see this repo.

    A clone failure says 'you may not have access' and stops there; this
    reports the provider's own status code and message for BOTH auth styles
    Bitbucket accepts, which is what actually tells the two causes apart
    (wrong credential shape vs. token genuinely lacking access).
    """
    from src.api.auto_review import get_auto_review_store
    from src.credentials import resolve_git_credential
    from src.http import build_client

    cfg = next((c for c in get_auto_review_store().list_all()
                if c.repo_slug == slug), None)
    if cfg is None:
        return {"error": f"repo {slug!r} is not registered"}
    creds = resolve_git_credential(cfg.provider, user_id=cfg.user_id,
                                   workspace_id=cfg.workspace_id)
    if creds is None:
        return {"repo": cfg.full_name, "error": "no credential in this workspace"}

    from src.ops.credential_shape import describe_token, safe_metadata

    secret = creds.secret
    meta = creds.metadata or {}
    out: dict = {
        "repo": cfg.full_name, "provider": cfg.provider,
        # Whose credential this call is about to use. The lookup goes through
        # `list_all()`, so an operator can land on any workspace's repo — the
        # answer should say which one rather than leave it to be inferred.
        "workspace_id": cfg.workspace_id,
        "token": describe_token(secret),
        "metadata": safe_metadata(meta),
        "attempts": [],
    }

    def _try(label: str, **kw) -> None:
        try:
            with build_client(timeout=20.0) as c:
                r = c.get(url, **kw)
            out["attempts"].append({
                "auth": label, "status": r.status_code,
                # Same cleaning the logs get. A provider is not supposed to
                # echo a credential back in an error body, and this endpoint
                # is not the place to find out that one does.
                "body": redact_text(r.text[:300]),
            })
        except Exception as exc:  # noqa: BLE001
            out["attempts"].append({"auth": label, "error": str(exc)[:200]})

    if cfg.provider == "bitbucket":
        url = f"https://api.bitbucket.org/2.0/repositories/{cfg.full_name}"
        email = str(meta.get("atlassian_email") or "")
        if email:
            _try("basic:email+token (API shape)", auth=(email, secret))
        _try("basic:x-bitbucket-api-token-auth", auth=("x-bitbucket-api-token-auth", secret))
        _try("bearer", headers={"Authorization": f"Bearer {secret}"})
        # Which repos DOES this token see? Narrows "no access" vs "wrong repo".
        try:
            with build_client(timeout=20.0) as c:
                r = c.get("https://api.bitbucket.org/2.0/repositories",
                          params={"role": "member", "pagelen": 50},
                          auth=(email, secret) if email
                          else ("x-bitbucket-api-token-auth", secret))
            if r.status_code == 200:
                out["visible_repos"] = [
                    v.get("full_name") for v in r.json().get("values", [])
                ][:50]
            else:
                out["visible_repos_status"] = r.status_code
        except Exception as exc:  # noqa: BLE001
            out["visible_repos_error"] = str(exc)[:200]
    elif cfg.provider == "github":
        url = f"https://api.github.com/repos/{cfg.full_name}"
        _try("bearer", headers={"Authorization": f"Bearer {secret}",
                                "Accept": "application/vnd.github+json"})
    else:
        url = f"https://gitlab.com/api/v4/projects/{cfg.full_name.replace('/', '%2F')}"
        _try("private-token", headers={"PRIVATE-TOKEN": secret})
    return out


@router.get("/diag")
async def diagnostics(
    session: AsyncSession = Depends(get_async_session),
    _access: str = Depends(require_ops_access),
) -> dict:
    """One-shot state dump for debugging: queue counts, stuck jobs, live
    agent sessions, latest dependency-audit runs. Read-only."""
    from sqlalchemy import text as _text

    out: dict = {"build": build_info(), "toolchain": toolchain_info(),
                 "disk": _disk()}
    try:
        rows = (await session.execute(_text(
            "SELECT status, count(*) FROM sync_jobs GROUP BY status"
        ))).all()
        out["jobs_by_status"] = {r[0]: int(r[1]) for r in rows}
        stuck = (await session.execute(_text(
            "SELECT id, kind, status, attempts, started_at, locked_by, "
            "       left(coalesce(last_error,''), 300) AS err "
            "FROM sync_jobs WHERE status IN ('pending','running') "
            "ORDER BY created_at LIMIT 20"
        ))).mappings().all()
        out["active_jobs"] = [dict(r) | {"started_at": str(r["started_at"])}
                              for r in stuck]
    except Exception as exc:  # noqa: BLE001
        out["jobs_error"] = str(exc)[:300]
    try:
        runs = (await session.execute(_text(
            "SELECT id, workspace_id, status, left(coalesce(error,''),300) AS error, "
            "       summary->>'phase' AS phase, created_at, updated_at "
            "FROM dep_audit_runs ORDER BY created_at DESC LIMIT 5"
        ))).mappings().all()
        out["dep_audit_runs"] = [
            dict(r) | {"created_at": str(r["created_at"]),
                       "updated_at": str(r["updated_at"])}
            for r in runs
        ]
    except Exception as exc:  # noqa: BLE001
        out["deps_error"] = str(exc)[:300]
    try:
        sess = (await session.execute(_text(
            "SELECT id, workspace_id, status, last_heartbeat_at "
            "FROM agent_sessions ORDER BY created_at DESC LIMIT 5"
        ))).mappings().all()
        out["agent_sessions"] = [
            dict(r) | {"last_heartbeat_at": str(r["last_heartbeat_at"])}
            for r in sess
        ]
    except Exception as exc:  # noqa: BLE001
        out["sessions_error"] = str(exc)[:300]
    # Slugs, not just ids: every other row here identifies a tenant by
    # workspace_id, but the `X-Workspace` header takes a slug — without the
    # mapping the ids in this dump can't be acted on.
    try:
        ws = (await session.execute(_text(
            "SELECT w.id, w.slug, w.name, "
            "       (SELECT count(*) FROM workspace_members m "
            "         WHERE m.workspace_id = w.id) AS members "
            "FROM workspaces w ORDER BY w.created_at LIMIT 50"
        ))).mappings().all()
        out["workspaces"] = [dict(r) for r in ws]
    except Exception as exc:  # noqa: BLE001
        out["workspaces_error"] = str(exc)[:300]

    # Git connections — shape only, never the secret: which slot answered,
    # the token's first chars (enough to tell ATATT from ghp_ from glpat-),
    # and the metadata that decides the auth mode. This is what turns a
    # "clone failed 403" into an answerable question.
    try:
        from src.api.auto_review import get_auto_review_store
        from src.credentials import get_credential_store
        from src.ops.credential_shape import describe_token, safe_metadata

        store = get_credential_store()
        repos = list(get_auto_review_store().list_all())
        # Which slots to inspect: every workspace that owns a repo, plus the
        # legacy/default ones. `list()` is per-slot and secret-free by design.
        slots = {"default"} | {f"ws:{c.workspace_id}" for c in repos} \
            | {c.user_id for c in repos}
        conns: list[dict] = []
        for slot in sorted(slots):
            for row in store.list(user_id=slot):
                if row["provider"] not in ("github", "gitlab", "bitbucket"):
                    continue
                meta = row.get("metadata") or {}
                entry = {
                    "provider": row["provider"],
                    "slot": slot,
                    "account_label": row.get("account_label", ""),
                    "metadata": safe_metadata(meta),
                    "updated_at": str(row.get("updated_at")),
                }
                # Shape, never bytes. This returned the first six characters
                # of every live token on the installation plus its exact
                # length; `describe_token` answers the same three questions —
                # right kind? complete? the same one as that other slot? —
                # with a format label, a length and a hash.
                try:
                    loaded = store.load(provider=row["provider"], user_id=slot,
                                        account_label=row.get("account_label", "default"))
                    entry["token"] = describe_token(loaded.secret if loaded else "")
                except Exception as exc:  # noqa: BLE001
                    entry["token_error"] = str(exc)[:120]
                conns.append(entry)
        out["git_connections"] = conns
        out["registered_repos"] = [
            {"slug": c.repo_slug, "provider": c.provider,
             "full_name": c.full_name, "workspace_id": c.workspace_id}
            for c in repos
        ]
    except Exception as exc:  # noqa: BLE001
        out["connections_error"] = str(exc)[:300]
    return out


def _disk() -> dict:
    """Free space where Docker builds. A deploy that runs out of it fails in
    the middle of a COPY with a message about copy_file_range, several minutes
    in and nowhere near the cause — so the number belongs in the dump you
    read first."""
    import shutil

    try:
        total, used, free = shutil.disk_usage("/")
        gb = 1024 ** 3
        return {
            "total_gb": round(total / gb, 1),
            "free_gb": round(free / gb, 1),
            "used_pct": round(used / total * 100, 1),
            # The build needs roughly 4GB of headroom; below that a deploy is
            # a coin flip rather than a failure you can predict.
            "enough_for_build": free >= 4 * gb,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


@router.get("/agent-session/{session_id}")
async def agent_session_dump(
    session_id: str,
    limit: int = Query(80, ge=1, le=400),
    session: AsyncSession = Depends(get_async_session),
    _access: str = Depends(require_ops_access),
) -> dict:
    """Everything a session left behind: status, result, error and the event
    log by type.

    A session can finish `done` and still show the user nothing — that is a
    missing *event stream*, not a failed run, and the two are indistinguishable
    from the outside. `events_by_type` separates them: no `text`/`tool_use`
    rows means the agent produced nothing; rows present but no UI output means
    the SSE side is at fault. Read-only.
    """
    from sqlalchemy import text as _text

    out: dict = {"session_id": session_id}
    try:
        row = (await session.execute(_text(
            "SELECT id, workspace_id, user_id, repo_slug, status, "
            "       left(coalesce(error,''), 600) AS error, result, "
            "       left(prompt, 400) AS prompt, "
            "       created_at, updated_at, last_heartbeat_at "
            "FROM agent_sessions WHERE id = :sid"
        ), {"sid": session_id})).mappings().first()
        if not row:
            out["found"] = False
            return out
        out["found"] = True
        out["session"] = dict(row) | {
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_heartbeat_at": str(row["last_heartbeat_at"]),
        }
    except Exception as exc:  # noqa: BLE001
        out["session_error"] = str(exc)[:300]
        return out

    try:
        counts = (await session.execute(_text(
            "SELECT event, count(*) FROM agent_session_events "
            "WHERE session_id = :sid GROUP BY event"
        ), {"sid": session_id})).all()
        out["events_by_type"] = {r[0]: int(r[1]) for r in counts}
        events = (await session.execute(_text(
            "SELECT id, event, left(data::text, 500) AS data, created_at "
            "FROM agent_session_events WHERE session_id = :sid "
            "ORDER BY id DESC LIMIT :lim"
        ), {"sid": session_id, "lim": limit})).mappings().all()
        out["events"] = [dict(r) | {"created_at": str(r["created_at"])}
                         for r in reversed(events)]
    except Exception as exc:  # noqa: BLE001
        out["events_error"] = str(exc)[:300]
    return out


__all__ = ["router"]


@router.get("/review-settings")
def review_settings(_user: User = Depends(require_admin)) -> dict:
    """The deadlines and budgets as this process resolved them.

    Was the body of the webhook sub-app's `/healthz`, whose routes are copied
    into the main app — so it answered on the public `/backend/healthz`, ahead
    of the plain one, to anybody who asked. Model names, deadlines, cache size
    and which backends are configured are a map of the installation.

    The need it served is real and unchanged: `env_file` points at a `.env`
    that does not exist in the container, so a setting the compose file does
    not forward silently takes the code default and nothing outside could tell
    which had happened. An operator can still see it; a stranger cannot.
    """
    from src.review.webhook import resolved_review_settings

    return {"review_settings": resolved_review_settings()}


@router.get("/readyz")
async def readyz_detail(_user: User = Depends(require_admin)) -> dict:
    """Per-dependency readiness, with the errors.

    The public `/readyz` answers ok/not-ok and a status code, which is the
    whole contract a probe has. This carries the rest: a user count, and
    `str(exc)[:200]` from whatever a failed connection raised — which for a
    bad DSN is a fragment of the DSN. That was reachable unauthenticated at
    `/backend/readyz`.
    """
    from src.api.main import readiness_checks

    checks, critical_down = await readiness_checks()
    return {"ok": not critical_down, "checks": checks}
