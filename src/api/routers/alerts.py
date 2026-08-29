"""Monitoring alerts — inbound ingest + workspace alert list.

Scenario: a Grafana (or any) alert fires → lands here → the user opens it on
their phone and presses "Fix with Claude", which pre-fills an agent session
with the alert context.

Ingest URL: POST /webhook/alerts/{workspace_id}.{secret}
The secret half is stored Fernet-encrypted per workspace (provider
"alert_ingest" in the credential store) and compared with constant-time
equality. Unauthenticated by design (monitoring can't do OAuth), tenant-bound
by construction — a token only ever writes into its own workspace.

Payload formats:
  * Grafana unified alerting webhook: {"alerts":[{"labels":{...},
    "annotations":{...},"status":"firing"}], "title": ..., "state": ...}
  * generic: {"title": "...", "body": "...", "severity": "critical",
    "repo": "owner/name or slug"}
"""

from __future__ import annotations

import hmac
import logging
import secrets as _secrets
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user, require_workspace_admin
from src.db.models import IncomingAlert
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["alerts"])

_INGEST_PROVIDER = "alert_ingest"


# ─── Ingest token management ─────────────────────────────────────────


def _load_secret(workspace_id: str) -> str | None:
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError
    from src.llm.keys import workspace_slot
    try:
        row = get_credential_store().load(
            provider=_INGEST_PROVIDER, user_id=workspace_slot(workspace_id),
            account_label="default",
        )
    except CredentialStoreError:
        return None
    return row.secret if row else None


class IngestTokenOut(BaseModel):
    ingest_path: str    # POST here from Grafana — relative; UI absolutizes


@router.post("/api/alerts/ingest-token", response_model=IngestTokenOut)
def create_ingest_token(
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> IngestTokenOut:
    """Create (or rotate) this workspace's alert ingest URL."""
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    secret = _secrets.token_urlsafe(24)
    get_credential_store().save(
        provider=_INGEST_PROVIDER, secret=secret,
        metadata={"saved_by": user.email},
        user_id=workspace_slot(workspace_id), account_label="default",
    )
    return IngestTokenOut(ingest_path=f"/webhook/alerts/{workspace_id}.{secret}")


@router.get("/api/alerts/ingest-token", response_model=IngestTokenOut | None)
def get_ingest_token(
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> IngestTokenOut | None:
    secret = _load_secret(workspace_id)
    if not secret:
        return None
    return IngestTokenOut(ingest_path=f"/webhook/alerts/{workspace_id}.{secret}")


# ─── Inbound webhook ─────────────────────────────────────────────────


@router.post("/webhook/alerts/{token}", status_code=202)
async def ingest(
    token: str,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    workspace_id, _, provided = token.partition(".")
    if not workspace_id or not provided:
        raise HTTPException(status_code=404, detail="not found")
    expected = _load_secret(workspace_id)
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=404, detail="not found")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON") from None

    created = 0
    parsed = _parse_alerts(payload)
    for alert in parsed:
        # Before the row, before the dispatch, before the prompt.
        alert["title"], alert["body"] = _redact_alert(alert["title"], alert["body"])
        session.add(IncomingAlert(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            source=alert["source"],
            title=alert["title"][:500],
            body=alert["body"][:8000],
            severity=alert["severity"],
            repo_hint=alert.get("repo_hint"),
        ))
        created += 1
    await session.commit()
    logger.info("alerts_ingested ws=%s n=%d", workspace_id, created)

    # An alert that only lands in a table is an inbox you have to remember to
    # open, which is the thing an alert exists to save you from. `severity`
    # and `repo_hint` above are exactly what the binding matcher gates on —
    # they were being stored for a routing step that was never taken.
    #
    # AFTER the response, not during it: the sender is a monitoring system
    # that retries on anything but a 2xx, and a slow chat webhook must not
    # turn one firing alert into a stream of duplicates. That is what the 202
    # on this endpoint already promised.
    if parsed:
        background.add_task(
            _dispatch_alerts, workspace_id, parsed, _alerts_link(),
        )
    return {"ok": True, "created": created}


def _alerts_link() -> str | None:
    """An absolute address for the alerts page, or nothing at all.

    NOT derived from the request, and that is the whole point. The request
    that carries an alert in comes from somebody else's monitoring: the
    sender writes its own Host header, and the reverse proxy passes it
    through — Caddy overwrites X-Forwarded-Host, it does not overwrite Host.
    Measured against production, an alert POSTed with

        Host: evil2.example.test

    put `http://evil2.example.test/alerts` behind the Open button of a card
    delivered into the workspace's chat room, under this product's branding,
    beside a title and body the same sender wrote. Holding the ingest token
    is the only requirement, and that token is handed to third-party
    monitoring on purpose — it is not a high-security secret.

    Deriving a URL from the request is safe when the URL goes back to whoever
    made the request: `webhooks.py::_public_base` shows an admin their own
    origin, in their own browser, and is right to. It stops being safe the
    moment the URL travels to somebody else. That difference, not the header
    names, is what decides it.

    So the address comes from configuration — the only party in this exchange
    that is not the sender. Unset means NO link rather than a guessed one:
    a card without a button still carries the alarm, and losing the alarm is
    the one failure this feature cannot have. A relative path would be worse
    than nothing; Google Chat rejects a card whose button is not an absolute
    URL, and the rejected card is the alert.
    """
    from src.config import get_settings

    base = (get_settings().public_base_url or "").strip().rstrip("/")
    if not base:
        logger.warning(
            "alert_link_unset — set PUBLIC_BASE_URL to put an Open button on "
            "alert notifications; sending them without one",
        )
        return None
    if not base.startswith(("http://", "https://")):
        logger.warning(
            "alert_link_no_scheme PUBLIC_BASE_URL=%r — needs http:// or "
            "https://; sending alerts without a link", base,
        )
        return None
    return f"{base}/alerts"


def _dispatch_alerts(
    workspace_id: str, alerts: list[dict[str, Any]], link_url: str | None,
) -> None:
    """Hand each parsed alert to the channel dispatcher.

    Non-raising, like `notify` itself: this runs after the response has gone,
    so an exception here has nobody left to tell and would only show up as an
    unhandled-task warning in the log.
    """
    from src.notifications import notify

    for alert in alerts:
        try:
            notify(
                workspace_id=workspace_id,
                event="alert_received",
                repo_slug=alert.get("repo_hint"),
                title=alert["title"][:500],
                body_md=alert["body"][:2000],
                severity=alert["severity"],
                link_url=link_url,
                extra={"source": alert["source"]},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_dispatch_failed ws=%s err=%s", workspace_id, exc)


def _redact_alert(title: str, body: str) -> tuple[str, str]:
    """Strip secrets out of an alert before it is stored or sent anywhere.

    An alert body is written by somebody else's monitoring, about a failure —
    precisely the text most likely to carry a credential. A connection string
    in a `could not connect to` line. An Authorization header in a dumped
    request. A token in an environment dump. It used to go three places
    verbatim: into `incoming_alerts`, out to a chat room, and into a model
    prompt when somebody pressed Fix with Claude.

    `mode="markdown"` rather than `"code"`: an alert is prose with fragments
    in it, and the code pipeline's entropy stage fires on ordinary sentences.

    FAIL-CLOSED ON THE SECRET, NOT ON THE ALERT. The redactor raises rather
    than let text through unchecked, which is right where the caller can
    refuse to send. Here the caller cannot: an alert that never arrives is
    the failure this whole feature exists to prevent. So a redactor that
    breaks costs the operator the text and not the alarm — the row still
    lands, the severity and the source are intact, and the body says why it
    is missing.
    """
    from src.security.redactor import redact

    out = []
    for value in (title, body):
        try:
            cleaned, _ = redact(value or "", source_hint="alert", mode="markdown")
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_redaction_failed err=%s", exc)
            cleaned = "[redaction unavailable — body withheld]"
        out.append(cleaned)
    return out[0], out[1]


def _parse_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Grafana unified-alerting webhook or a generic single alert."""
    out: list[dict[str, Any]] = []
    if isinstance(payload.get("alerts"), list):
        for a in payload["alerts"][:20]:
            labels = a.get("labels") or {}
            ann = a.get("annotations") or {}
            title = (
                ann.get("summary") or labels.get("alertname")
                or payload.get("title") or "Alert"
            )
            body_parts = [
                ann.get("description") or "",
                "\n".join(f"{k}={v}" for k, v in labels.items()),
            ]
            sev = (labels.get("severity") or "warning").lower()
            out.append({
                "source": "grafana",
                "title": str(title),
                "body": "\n".join(p for p in body_parts if p).strip(),
                "severity": sev if sev in ("info", "warning", "error", "critical") else "warning",
                "repo_hint": labels.get("repo") or labels.get("service"),
            })
        return out
    title = payload.get("title") or payload.get("message")
    if title:
        sev = str(payload.get("severity") or "warning").lower()
        out.append({
            "source": str(payload.get("source") or "generic"),
            "title": str(title),
            "body": str(payload.get("body") or payload.get("description") or ""),
            "severity": sev if sev in ("info", "warning", "error", "critical") else "warning",
            "repo_hint": payload.get("repo"),
        })
    return out


# ─── Workspace alert list ────────────────────────────────────────────


class AlertOut(BaseModel):
    id: str
    source: str
    title: str
    body: str
    severity: str
    status: str
    repo_hint: str | None
    session_id: str | None
    created_at: str


def _to_out(row: IncomingAlert) -> AlertOut:
    return AlertOut(
        id=row.id, source=row.source, title=row.title, body=row.body,
        severity=row.severity, status=row.status, repo_hint=row.repo_hint,
        session_id=row.session_id,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


@router.get("/api/alerts", response_model=list[AlertOut])
async def list_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[AlertOut]:
    rows = (await session.scalars(
        select(IncomingAlert)
        .where(IncomingAlert.workspace_id == workspace_id)
        .order_by(IncomingAlert.created_at.desc())
        .limit(limit)
    )).all()
    return [_to_out(r) for r in rows]


class AlertPatchIn(BaseModel):
    status: str | None = None       # acked | fixed
    session_id: str | None = None   # link to an agent session


@router.patch("/api/alerts/{alert_id}", response_model=AlertOut)
async def patch_alert(
    alert_id: str,
    payload: AlertPatchIn,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> AlertOut:
    row = await session.get(IncomingAlert, alert_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Alert not found")
    if payload.status in ("new", "acked", "fixed"):
        row.status = payload.status
    if payload.session_id is not None:
        row.session_id = payload.session_id
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


__all__ = ["router"]


#: How long an incoming alert is kept. Days, and settable.
#:
#: There was no answer to this at all: the table had no DELETE and no sweep,
#: so whatever arrived stayed for the life of the installation. That turns a
#: transient leak into a permanent one, and makes an erasure request
#: unanswerable — the GDPR export walks a person's rows and an alert body can
#: name one.
_RETENTION_ENV = "CELMIS_ALERT_RETENTION_DAYS"


def alert_retention_days() -> int:
    """The window, or 90. Zero or negative disables the sweep."""
    import os

    raw = os.environ.get(_RETENTION_ENV, "")
    try:
        return int(raw) if str(raw).strip() else 90
    except (TypeError, ValueError):
        logger.warning("alert_retention_bad_value %s=%r — using 90", _RETENTION_ENV, raw)
        return 90


def purge_expired_alerts() -> int:
    """Delete alerts older than the retention window. Returns how many.

    Synchronous and self-contained, like the audit purge it runs beside:
    called from the nightly scheduler thread, not from a request.
    """
    days = alert_retention_days()
    if days <= 0:
        return 0
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from src.db.session import get_database_url

    cutoff = datetime.now(UTC) - timedelta(days=days)
    url = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as s:
            result = s.execute(
                delete(IncomingAlert).where(IncomingAlert.created_at < cutoff))
            s.commit()
            deleted = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert_purge_failed err=%s", exc)
        return 0
    finally:
        engine.dispose()
    if deleted:
        logger.info("alerts_purged n=%d older_than_days=%d", deleted, days)
    return deleted
