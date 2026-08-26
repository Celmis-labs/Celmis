"""Claude Code embedded agent — connection + sessions API.

    GET    /api/claude/connection            — connection status (never the token)
    PUT    /api/claude/connection            — save a setup-token (personal|workspace)
    POST   /api/claude/test-connection       — re-check a saved token, no re-paste
    DELETE /api/claude/connection/{scope}    — disconnect
    GET    /api/agent-sessions               — list sessions in the active workspace
    POST   /api/agent-sessions               — create + start a session
    GET    /api/agent-sessions/{id}          — one session row
    GET    /api/agent-sessions/{id}/stream   — SSE: replay + live tail (Last-Event-ID aware)
    POST   /api/agent-sessions/{id}/stop     — graceful stop

(`/api/agents` is already taken by review-agent prompt overrides.)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from src.agent.modes import STANDARD, is_valid_mode, is_valid_model
from src.api.deps import current_workspace_id, get_current_user
from src.db.models import AgentSession, AgentSessionEvent
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["claude-code"])

#: A session in one of these will never produce another event. Anything else —
#: `queued`, `running` — may still be alive somewhere, or be resumable, and a
#: client must keep trying rather than declaring the page finished.
#: A session that can never continue. `paused` is deliberately NOT here — it
#: has no process, but it is waiting, not over, and the difference is the whole
#: feature. The UI offers "carry on" for one and "start a new session" for the
#: other, and conflating them puts the wrong button in front of the user.
_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})

#: A session with nothing running behind it, so the SSE stream must close
#: rather than wait on a process that does not exist. `paused` belongs here and
#: not above: the stream is finished, the conversation is not.
_STREAM_CLOSED_STATUSES = _TERMINAL_STATUSES | {"paused"}


# ─── Schemas ─────────────────────────────────────────────────────────


class ConnectionStatusOut(BaseModel):
    # Presence: a row exists in this slot.
    personal: bool
    workspace: bool
    workspace_saved_by: str | None = None
    # Validity: what the last verification of that row found. A different
    # question from presence, and the reason this payload grew — a token can
    # be present and dead, and the page has to be able to say so. One of
    # absent | never_checked | verified | stale | failed | unreachable.
    personal_state: str = "absent"
    personal_verified_at: str | None = None
    personal_reason: str | None = None
    workspace_state: str = "absent"
    workspace_verified_at: str | None = None
    workspace_reason: str | None = None


class ConnectionIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    scope: str = Field(default="personal", pattern="^(personal|workspace)$")
    model_config = ConfigDict(extra="forbid")


class ConnectionTestIn(BaseModel):
    scope: str = Field(default="personal", pattern="^(personal|workspace)$")
    model_config = ConfigDict(extra="forbid")


class ConnectionTestOut(BaseModel):
    """Shaped after /api/llm/test-connection so the two feel like one product."""

    ok: bool
    scope: str
    detail: str
    checked_at: str | None = None
    latency_ms: int | None = None
    #: Answered from the stored result rather than the provider — the button
    #: is rate-limited, see VERIFY_CACHE_SECONDS.
    cached: bool = False


#: More than this and the clone alone outlives anyone's patience, and the
#: agent's context fills with trees before it reads a line.
MAX_SESSION_REPOS = 5


class SessionCreateIn(BaseModel):
    # Empty ONLY when project_id carries the set instead — see the model
    # validator. Every pre-project caller still sends it, and it still names
    # the session's primary repo.
    repo_slug: str = Field(default="", max_length=300)
    # Additional repos cloned alongside `repo_slug`, so one session can answer
    # a question that spans services. `repo_slug` stays first and required —
    # every existing caller sends only that.
    repo_slugs: list[str] = Field(default_factory=list, max_length=MAX_SESSION_REPOS)
    # Start from a project instead of naming repos: the project's own members
    # become the session's set, resolved server-side so a stale browser tab
    # cannot clone a repo the project dropped an hour ago.
    project_id: str | None = Field(default=None, max_length=100)
    #: Files staged before this session existed, by `staged-attachments`.
    #: The runner moves them into the clone and names them in the first
    #: prompt — see `adopt_staged`.
    staging_id: str | None = Field(default=None, max_length=64)
    prompt: str = Field(min_length=3, max_length=20_000)
    title: str = Field(default="", max_length=300)
    # "standard" (one agent) | "workflow" (subagents fan out in parallel).
    mode: str = Field(default=STANDARD, max_length=32)
    # "" = whatever the connected account's CLI defaults to.
    model: str = Field(default="", max_length=64)
    model_config = ConfigDict(extra="forbid")

    @field_validator("project_id")
    @classmethod
    def _well_formed_project_id(cls, v: str | None) -> str | None:
        # projects.id is a real Postgres uuid column. Comparing it against
        # free-form text makes the driver raise on the way out — a 500 for what
        # is plainly a bad request. Reject the shape here instead.
        if v is None or not v.strip():
            return None
        v = v.strip()
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("project_id must be a UUID") from None
        return v

    @model_validator(mode="after")
    def _names_something_to_clone(self) -> SessionCreateIn:
        if not self.repo_slug.strip() and not self.project_id:
            raise ValueError("Name a repository or a project.")
        return self

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        v = (v or STANDARD).strip().lower()
        if not is_valid_mode(v):
            raise ValueError(f"Unknown mode {v!r}")
        return v

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        # A typo fails deep inside the CLI with an opaque error, long after the
        # session row exists and the repo has been cloned. Aliases pass, and so
        # does any well-formed claude-* id — new models must not need a deploy.
        v = (v or "").strip()
        if not is_valid_model(v):
            raise ValueError(f"Unsupported model {v!r}")
        return v


class SessionOut(BaseModel):
    id: str
    repo_slug: str
    title: str
    prompt: str
    status: str
    result: dict
    error: str | None
    created_at: str
    updated_at: str
    created_by: str | None = None
    mode: str = STANDARD
    model: str = ""
    repo_slugs: list[str] = []
    project_id: str | None = None
    #: How many times this conversation was picked back up. "resumed 6 times
    #: over three days" is the difference between a session and a long-running
    #: one, and nothing else in this payload says it.
    resume_count: int = 0
    #: When the stored transcript is swept and the session stops being
    #: resumable. The UI needs it to say "resumable until Friday" rather than
    #: offering a button that will 409.
    resumable_until: str | None = None


def _to_out(row: AgentSession) -> SessionOut:
    # Users live in the SQLite store — map creator id → email in Python.
    created_by = None
    if row.user_id:
        try:
            from src.users.store import get_user_store
            u = get_user_store().get_by_id(row.user_id)
            created_by = u.email if u else None
        except Exception:  # noqa: BLE001
            created_by = None
    return SessionOut(
        id=row.id, repo_slug=row.repo_slug, title=row.title, prompt=row.prompt,
        status=row.status, result=dict(row.result or {}), error=row.error,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        created_by=created_by,
        mode=getattr(row, "mode", "") or STANDARD,
        model=getattr(row, "model", "") or "",
        repo_slugs=list(getattr(row, "slugs", None) or [row.repo_slug]),
        project_id=getattr(row, "project_id", None),
        resume_count=getattr(row, "resume_count", 0) or 0,
        resumable_until=(row.resumable_until.isoformat()
                         if getattr(row, "resumable_until", None) else None),
    )


# ─── Connection ──────────────────────────────────────────────────────


def _require_workspace_admin(user: User, workspace_id: str, detail: str) -> None:
    """Owner/admin gate for the shared workspace slot.

    Was written out three times over as many endpoints and is now one place —
    a fourth copy is how one of them ends up without the check.
    """
    if user.is_admin:
        return
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _S

    from src.db.models import WorkspaceMember
    from src.db.session import get_database_url

    eng = create_engine(get_database_url().replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"), pool_pre_ping=True)
    try:
        with _S(eng) as s:
            m = s.get(WorkspaceMember, (workspace_id, user.id))
            if m is None or m.role not in ("owner", "admin"):
                raise HTTPException(status_code=403, detail=detail)
    finally:
        eng.dispose()


@router.get("/api/claude/connection", response_model=ConnectionStatusOut)
def connection_status(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectionStatusOut:
    """Presence AND validity, read from the rows. Never makes a provider call —
    this renders on every page load; the probes live on save and on
    /api/claude/test-connection."""
    from src.agent.connection import status
    return ConnectionStatusOut(**status(user.id, workspace_id))


@router.put("/api/claude/connection", response_model=ConnectionStatusOut)
def save_connection(
    payload: ConnectionIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectionStatusOut:
    """Save a `claude setup-token` value — after Anthropic agrees it works.

    A token Anthropic refuses comes back as a 400 carrying the provider's own
    reason and is not stored. A token we could not check (network, 5xx) IS
    stored, marked unverified; `status()` reports it as such and never as
    connected-and-fine. See `save_token` for why the two fall opposite ways.
    """
    from src.agent.connection import TokenRejected, save_token, status, token_looks_valid

    if not token_looks_valid(payload.token):
        # The cheap gate: no network call to tell an operator they pasted the
        # wrong string. `save_token` re-checks it — this one exists for the
        # friendlier wording.
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like a Claude Code token — run "
                   "`claude setup-token` and paste the sk-ant-oat… value.",
        )
    if payload.scope == "workspace":
        # Sharing one person's subscription across a workspace is the admin's
        # explicit call (the UI shows the terms warning before this request).
        _require_workspace_admin(
            user, workspace_id, "Only a workspace owner/admin may share a token")
    try:
        save_token(
            token=payload.token, user_id=user.id, workspace_id=workspace_id,
            scope=payload.scope, saved_by=user.email,
        )
    except TokenRejected as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Anthropic rejected that token: {exc.reason}",
        ) from None
    return ConnectionStatusOut(**status(user.id, workspace_id))


@router.post("/api/claude/test-connection", response_model=ConnectionTestOut)
def test_connection(
    payload: ConnectionTestIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectionTestOut:
    """Re-check a saved token without re-pasting it, and write the answer back.

    Named after /api/llm/test-connection on purpose — the operator meets the
    same button twice and it should mean the same thing. Like that one it
    answers 200 with `ok=False` rather than raising: "your token is dead" is a
    result, not a request error.
    """
    from src.agent.connection import recheck_token

    if payload.scope == "workspace":
        # Spending the shared subscription's quota is the admin's call, and it
        # is the same slot they alone may write.
        _require_workspace_admin(
            user, workspace_id, "Only a workspace owner/admin may test the shared token")
    result = recheck_token(
        user_id=user.id, workspace_id=workspace_id, scope=payload.scope)
    if result is None:
        return ConnectionTestOut(
            ok=False, scope=payload.scope,
            detail="No token saved for this scope — paste one first.",
        )
    return ConnectionTestOut(
        ok=result.ok,
        scope=payload.scope,
        detail=("connected — Anthropic accepted this token" if result.ok
                else result.reason),
        checked_at=result.checked_at,
        latency_ms=result.latency_ms,
        cached=result.cached,
    )


@router.delete("/api/claude/connection/{scope}", status_code=204)
def delete_connection(
    scope: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    if scope not in ("personal", "workspace"):
        raise HTTPException(status_code=400, detail="scope must be personal|workspace")
    from src.agent.connection import delete_token
    if scope == "workspace":
        _require_workspace_admin(user, workspace_id, "Workspace admin required")
    delete_token(user_id=user.id, workspace_id=workspace_id, scope=scope)


# ─── Sessions ────────────────────────────────────────────────────────


@router.get("/api/agent-sessions", response_model=list[SessionOut])
async def list_sessions(
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[SessionOut]:
    rows = (await session.scalars(
        select(AgentSession)
        .where(AgentSession.workspace_id == workspace_id)
        .order_by(AgentSession.created_at.desc())
        .limit(limit)
    )).all()
    return [_to_out(r) for r in rows]


async def _live_session_id(session, workspace_id: str) -> str | None:
    """The workspace's one live session, if it has one.

    Counts `queued` and `running` and nothing else. A `paused` session holds no
    process, no CLI subprocess and no slot — counting it would make the
    resumable-conversation feature its own denial of service, where one
    unfinished chat from last week blocks every new session in the workspace.
    """
    from sqlalchemy import select

    from src.db.models import AgentSession

    row = (await session.scalars(
        select(AgentSession).where(
            AgentSession.workspace_id == workspace_id,
            AgentSession.status.in_(("queued", "running")),
        )
    )).first()
    return row.id if row is not None else None


@router.post("/api/agent-sessions", response_model=SessionOut, status_code=201)
async def create_session(
    payload: SessionCreateIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> SessionOut:
    from src.agent.connection import resolve_connection
    from src.agent.runner import start_session

    # A project contributes its members, resolved here rather than trusted from
    # the browser: the tab that rendered the picker may be an hour old, and the
    # set the session actually clones has to be the set the project holds now.
    project_slugs: list[str] = []
    if payload.project_id:
        from src.db.models import Project, ProjectRepo
        project = (await session.scalars(
            select(Project).where(
                Project.id == payload.project_id,
                Project.workspace_id == workspace_id,
            )
        )).first()
        if project is None:
            # 404 rather than 403: whether a project exists in another
            # workspace is not this caller's business.
            raise HTTPException(status_code=404, detail="Project not found.")
        project_slugs = list(await session.scalars(
            select(ProjectRepo.repo_slug)
            .where(ProjectRepo.project_id == project.id)
            .order_by(ProjectRepo.created_at, ProjectRepo.repo_slug)
        ))
        if not project_slugs:
            raise HTTPException(
                status_code=400,
                detail="That project has no repositories yet.",
            )

    # Tenant boundary. A slug the caller's workspace does not own must be
    # REFUSED, not quietly dropped: silently auditing four of five repos is
    # worse than an error, because nobody notices the fifth is missing.
    wanted: list[str] = []
    for slug in [*project_slugs, payload.repo_slug, *payload.repo_slugs]:
        if slug and slug not in wanted:
            wanted.append(slug)
    if len(wanted) > MAX_SESSION_REPOS:
        raise HTTPException(
            status_code=400,
            detail=f"A session covers at most {MAX_SESSION_REPOS} repositories.",
        )
    from src.api.auto_review import get_auto_review_store
    owned = {c.repo_slug for c in get_auto_review_store().list_all()
             if c.workspace_id == workspace_id}
    unknown = [s for s in wanted if s not in owned]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Not registered in this workspace: {', '.join(unknown)}",
        )

    if resolve_connection(user.id, workspace_id) is None:
        raise HTTPException(
            status_code=400,
            detail="Connect a Claude account first (personal or workspace).",
        )

    # One live session per workspace (v1): a shared subscription has one
    # concurrency budget — parallel sessions would trip its limits mid-run.
    live = await _live_session_id(session, workspace_id)
    if live is not None:
        raise HTTPException(
            status_code=409,
            detail="A session is already running in this workspace — wait or stop it.",
        )

    row = AgentSession(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        user_id=user.id,
        repo_slug=wanted[0],
        repo_slugs=wanted,
        project_id=payload.project_id or None,
        title=payload.title or payload.prompt[:80],
        prompt=payload.prompt,
        mode=payload.mode,
        model=payload.model,
        status="queued",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    # Move anything staged before the session existed under the session's own
    # id, so the runner finds it where it looks.
    if payload.staging_id:
        from src.agent.workspace import staging_root
        src_dir, dst_dir = staging_root(payload.staging_id), staging_root(row.id)
        if src_dir.exists() and src_dir != dst_dir:
            try:
                dst_dir.parent.mkdir(parents=True, exist_ok=True)
                src_dir.replace(dst_dir)
            except Exception as exc:  # noqa: BLE001 — attachments are not the session
                logger.warning("staging_claim_failed session=%s err=%s", row.id, exc)

    await start_session(row.id)
    logger.info("agent_session_created id=%s ws=%s repo=%s mode=%s model=%s by=%s",
                row.id, workspace_id, payload.repo_slug, payload.mode,
                payload.model or "(default)", user.email)
    return _to_out(row)


@router.get("/api/agent-sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> SessionOut:
    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_out(row)


@router.post("/api/agent-sessions/{session_id}/stop", status_code=202)
async def stop(
    session_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    from src.agent.runner import stop_session

    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")
    stopped = await stop_session(session_id)
    return {"ok": True, "stopping": stopped}


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)

    model_config = ConfigDict(extra="forbid")


@router.post("/api/agent-sessions/{session_id}/messages", status_code=202)
async def send_message(
    session_id: str,
    payload: TurnIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Add another message to a live session.

    A session used to be one message: you asked, it worked, it pushed. Keeping
    the conversation open is what makes "now also run the tests" and "no, fix
    it this way instead" possible without starting again from a cold clone.

    The tenant check comes FIRST and the liveness check second, and the order
    is the point: `is_running` is workspace-blind, so answering 409 before
    404 would turn this route into a way of asking whether somebody else's
    session id is alive.
    """
    from datetime import datetime

    from src.agent.runner import send_turn, start_session

    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if send_turn(session_id, payload.text):
        logger.info("agent_turn_queued session=%s by=%s", session_id, user.email)
        return {"ok": True, "resumed": False}

    # Not live here. If it is PAUSED it is not over — it is waiting, and this
    # message is what wakes it. The prompt becomes the row's own so the
    # resumed run has something to start from, and the CLI is handed the same
    # session id with `resume=`, so the conversation continues rather than
    # beginning again against a cold clone.
    if row.status == "paused":
        until = row.resumable_until
        if until is not None and until < datetime.now(UTC):
            raise HTTPException(
                status_code=409,
                detail="This conversation is past its retention window and "
                       "its transcript has been swept. Start a new session.",
            )
        # One live session per workspace still holds: a paused session counts
        # for nothing, but waking it must not jump the queue past a running one.
        live = await _live_session_id(session, workspace_id)
        if live and live != session_id:
            raise HTTPException(
                status_code=409,
                detail="Another session is running in this workspace. "
                       "Finish it before resuming this one.",
            )
        row.prompt = payload.text
        row.status = "queued"
        row.resume_count = (row.resume_count or 0) + 1
        await session.commit()
        await start_session(session_id, resume=True)
        logger.info("agent_session_resumed session=%s by=%s count=%d",
                    session_id, user.email, row.resume_count)
        return {"ok": True, "resumed": True}

    raise HTTPException(
        status_code=409,
        detail="That session is no longer accepting messages. "
               "Start a new one to carry on.",
    )


@router.post("/api/agent-sessions/{session_id}/finish", status_code=202)
async def finish(
    session_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """End the conversation cleanly: no more turns, then push and open the PR.

    Distinct from `/stop`, which interrupts. A conversation has no end the
    runner can detect — a result frame now means "your turn", not "finished" —
    so somebody has to say when it is over, and saying so is not an error.
    """
    from src.agent.runner import finish_session

    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not finish_session(session_id):
        raise HTTPException(status_code=409, detail="That session is not live.")
    logger.info("agent_finish_requested session=%s by=%s", session_id, user.email)
    return {"ok": True}


#: What an attachment may be. Text only, and deliberately so: the agent has
#: Read but not Bash, so it cannot run anything to open a binary. A .xlsx here
#: would arrive as bytes it can do nothing with — export it as CSV instead.
_TEXT_SUFFIXES = {
    ".txt", ".log", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".diff", ".patch", ".sql", ".xml", ".ini", ".toml", ".conf", ".env.example",
}
#: Screenshots. These were REFUSED, on the reasoning that the agent has Read
#: and no shell, so an image would arrive as bytes it cannot open. That
#: reasoning was wrong and unchecked: the bundled CLI handles image/png and
#: image/jpeg, so Read opens them. A screenshot of a failing dashboard is
#: often the fastest description of a bug there is, and refusing it made the
#: attach button useless for the case people reach for first.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_ATTACH_SUFFIXES = _TEXT_SUFFIXES | _IMAGE_SUFFIXES

#: A log worth reading is not a log worth pasting whole. Past this it stops
#: being context and starts being the whole context window.
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
#: Images get their own, larger ceiling. A retina screenshot of a full window
#: is routinely 3–5 MB, and it does not cost context the way text does — the
#: model sees a resized image, not the file. One cap for both would have
#: rejected the ordinary case while claiming to support it.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: Everything staged for one not-yet-started session. Without it the staging
#: endpoint is an unbounded upload target that costs disk before any session
#: exists to own it.
MAX_STAGED_TOTAL_BYTES = 30 * 1024 * 1024


@router.post("/api/agent-sessions/{session_id}/attachments", status_code=201)
async def attach(
    session_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Put a file where the running agent can read it.

    It lands in `_attachments/` inside the session's own sandbox, which is the
    directory the PreToolUse hook already bounds every tool path to — so this
    needs no new permission and opens no new path. The agent is told the
    relative path and reads it with the tool it already has.

    Text only. The agent has Read and not Bash, so a spreadsheet or a
    screenshot would arrive as bytes it cannot open; refusing with a reason
    beats accepting something that silently does nothing.
    """
    from src.agent.runner import is_running, send_turn, session_root

    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_running(session_id):
        raise HTTPException(status_code=409,
                            detail="That session is no longer live.")

    name = Path(file.filename or "attachment.txt").name
    if Path(name).suffix.lower() not in _ATTACH_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=(f"{Path(name).suffix or 'That file type'} is not something "
                    f"the agent can open. Text works — logs, CSV, JSON, "
                    f"markdown, diffs — and so do screenshots: PNG, JPEG, GIF, "
                    f"WebP."),
        )
    suffix = Path(name).suffix.lower()
    ceiling = MAX_IMAGE_BYTES if suffix in _IMAGE_SUFFIXES else MAX_ATTACHMENT_BYTES
    blob = await file.read(ceiling + 1)
    if len(blob) > ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"{'Images' if suffix in _IMAGE_SUFFIXES else 'Attachments'} "
                   f"are capped at "
                   f"{ceiling // 1024 // 1024} MB.",
        )
    root = session_root(session_id)
    if root is None:
        raise HTTPException(status_code=409, detail="Session has no workspace.")

    target_dir = root / "_attachments"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    try:
        target.write_bytes(blob)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"could not store the attachment: {exc}") from exc

    rel = f"_attachments/{name}"
    logger.info("agent_attachment session=%s file=%s bytes=%d by=%s",
                session_id, rel, len(blob), user.email)
    # The agent is told it exists rather than being handed the contents: a two
    # megabyte log pasted into a turn is the context window gone, and it can
    # read exactly the part it needs.
    send_turn(session_id,
              f"I attached a file at `{rel}` ({len(blob)} bytes). Read it.")
    return {"ok": True, "path": rel, "bytes": len(blob)}


# ─── SSE stream: replay + live tail ──────────────────────────────────


@router.get("/api/agent-sessions/{session_id}/stream")
async def stream(
    session_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    row = await session.get(AgentSession, session_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")

    # Cursor: explicit ?after= wins, else the browser's Last-Event-ID.
    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    return EventSourceResponse(
        _event_stream(session_id, cursor), media_type="text/event-stream", ping=15,
    )


async def _event_stream(session_id: str, after: int) -> AsyncIterator[dict]:
    import json

    from src.agent.runner import is_running, subscribe, unsubscribe
    from src.db import async_session

    def _frame(event_id: int, event: str, data: dict) -> dict:
        return {
            "id": str(event_id),
            "event": event,
            "data": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        }

    # 1) Subscribe FIRST so nothing emitted during replay is lost.
    queue = subscribe(session_id)

    try:
        # 2) Replay persisted events past the cursor.
        last_id = after
        async with async_session() as s:
            rows = (await s.scalars(
                select(AgentSessionEvent)
                .where(
                    AgentSessionEvent.session_id == session_id,
                    AgentSessionEvent.id > after,
                )
                .order_by(AgentSessionEvent.id)
            )).all()
        for ev in rows:
            last_id = ev.id
            yield _frame(ev.id, ev.event, dict(ev.data or {}))

        # 3) Live tail, de-duplicated by id.
        if queue is None:
            # `stream_end` is a statement about THIS CONNECTION, not about the
            # session. `subscribe()` returns None whenever the session is not
            # in this process's registry — which includes the case where the
            # API restarted while the row is still `running`. The client used
            # to read it as terminal, set its done flag and never reconnect,
            # so a deploy left the page permanently dead.
            #
            # The session's own status travels with the frame, and the client
            # decides.
            from src.db.models import AgentSession
            async with async_session() as s:
                status = (await s.scalars(
                    select(AgentSession.status)
                    .where(AgentSession.id == session_id)
                )).first() or "unknown"
            yield _frame(last_id, "stream_end",
                         {"live": False, "session_status": str(status),
                          # `final` means "cannot continue", not "no stream".
                          # A paused session closes the stream and stays
                          # resumable, so the client keeps the composer.
                          "final": str(status) in _TERMINAL_STATUSES,
                          "resumable": str(status) == "paused"})
            return
        while True:
            payload = await queue.get()
            if payload["id"] <= last_id:
                continue
            last_id = payload["id"]
            yield _frame(payload["id"], payload["event"], payload["data"])
            if payload["event"] in ("done", "error") and not is_running(session_id):
                return
    except asyncio.CancelledError:
        raise
    finally:
        if queue is not None:
            unsubscribe(session_id, queue)


__all__ = ["router"]


@router.post("/api/agent-sessions/staged-attachments", status_code=201)
async def stage_attachment(
    file: UploadFile = File(...),
    staging_id: str = Form(default=""),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Attach a file BEFORE the session exists.

    The paperclip used to live only inside a running session, and that is the
    wrong moment: the thing people want to attach is the production log that
    made them open the session at all. But there is no workspace to write into
    until the runner clones, and it does not clone until the session starts.

    So the file is staged under a client-minted id, and the runner moves it in
    as soon as the clone exists — see `adopt_staged`. The first prompt then
    names the files, because an attachment the model is not told about is a
    file in a directory it has no reason to list.

    The staging id is generated here when the caller does not supply one, and
    it is a uuid4 either way: it is a capability, so it must not be guessable
    and it must not be the caller's to choose freely.
    """
    import re
    import uuid as _uuid

    from src.agent.workspace import staging_root

    # A client-supplied id is accepted only in its exact minted shape. Anything
    # else is a path fragment somebody would like us to join onto a directory.
    if staging_id and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            staging_id):
        raise HTTPException(status_code=400, detail="Malformed staging id")
    staging_id = staging_id or str(_uuid.uuid4())

    name = Path(file.filename or "attachment.txt").name
    suffix = Path(name).suffix.lower()
    if suffix not in _ATTACH_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=(f"{suffix or 'That file type'} is not something the agent "
                    f"can open. Text works — logs, CSV, JSON, markdown, diffs — "
                    f"and so do screenshots: PNG, JPEG, GIF, WebP."),
        )
    ceiling = MAX_IMAGE_BYTES if suffix in _IMAGE_SUFFIXES else MAX_ATTACHMENT_BYTES
    blob = await file.read(ceiling + 1)
    if len(blob) > ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"{'Images' if suffix in _IMAGE_SUFFIXES else 'Attachments'} "
                   f"are capped at {ceiling // 1024 // 1024} MB.",
        )

    target_dir = staging_root(staging_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    # A staging area with no ceiling is an upload endpoint with no ceiling.
    existing = sum(f.stat().st_size for f in target_dir.iterdir() if f.is_file())
    if existing + len(blob) > MAX_STAGED_TOTAL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That is more than {MAX_STAGED_TOTAL_BYTES // 1024 // 1024} MB "
                   f"of attachments for one session.",
        )
    (target_dir / name).write_bytes(blob)

    logger.info("agent_attachment_staged staging=%s file=%s bytes=%d by=%s ws=%s",
                staging_id, name, len(blob), user.email, workspace_id)
    return {"staging_id": staging_id, "name": name, "bytes": len(blob)}
