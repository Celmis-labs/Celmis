"""Headless Claude Code session runner.

One asyncio task per session inside the api process. Events go to the
append-only `agent_session_events` table (the BIGINT id is the stream cursor)
AND to any live in-process subscriber queues — the SSE endpoint subscribes
FIRST, then replays from the table, then drains the queue de-duplicating by
id, so a phone reconnect can never miss or duplicate an event.

Security model (v1):
    * No Bash. The agent gets Read/Grep/Glob/Edit/Write/MultiEdit + the
      internal Celmis MCP tools. All git (commit/push) is done by the runner
      with hooks disabled — the agent writing .git/hooks achieves nothing.
    * A PreToolUse hook denies any file path outside the session workspace, so
      the agent cannot read other tenants' clones or the credential store. It
      has to be a hook: `can_use_tool` is skipped for everything in
      `allowed_tools`, and blanket-allows what is not in it.
    * The CLI subprocess gets a per-session HOME/CLAUDE_CONFIG_DIR and an
      explicit env (subscription token, telemetry/autoupdate off) — never the
      api process environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# One instance id per process start — heartbeat scoping for orphan-marking.
RUNNER_INSTANCE = str(uuid.uuid4())[:8]

#: Hard cap on ONE TURN's work — from the moment the query goes in to the
#: result frame coming back. It used to wrap the whole session, which was right
#: while a session was one prompt and became a bug the moment it became a
#: conversation: the clock ran while the person was reading, and because the
#: timeout raises inside `_drive_agent` it never reached the push, so a chat
#: that spent forty minutes across four messages had its edits deleted with the
#: workspace. Time spent waiting for a human is bounded separately, below.
TURN_WALL_CLOCK_SECONDS = 40 * 60
#: A backstop on the whole conversation, ending it the way a person would —
#: push, open the PR, free the slot — rather than by raising. Nothing should
#: reach it: a turn is capped above and silence ends a chat in fifteen minutes.
#: It exists so "a turn every fourteen minutes, forever" is not unbounded.
#:
#: Four hours rather than a working day because `_mint_mcp_token` derives the
#: agent's MCP token lifetime from this number, and that token cannot be
#: re-minted mid-session — it is handed to the CLI subprocess in its env at
#: spawn. So this is the real trade: a longer cap is a longer-lived credential,
#: and a shorter one breaks the graph tools partway through a long chat. Four
#: hours of continuous back-and-forth is already far past any real session,
#: and the token is read-only, single-user and never leaves that subprocess.
SESSION_WALL_CLOCK_SECONDS = 4 * 60 * 60
HEARTBEAT_SECONDS = 30

# How long a conversation waits for the next human message before wrapping up
# on its own: pushing what was done, opening the PR, and freeing the
# one-live-session slot the workspace has. The 409 that enforces that slot is
# a real budget, not an accident, so the answer to "a chat holds it while
# somebody is at lunch" is this timeout — not weakening the rule.
#
# The consequence, measured rather than assumed: a session no longer reaches
# `done` when the agent stops talking. It PARKS here. A one-shot caller that
# creates a session and polls for `done` therefore waits this long before the
# branch is pushed, where it used to be seconds. That is the cost of the
# feature, not a hang — and the cure is POST /finish, which takes it to `done`
# in about fifteen seconds (the UI's "Finish & push" button does exactly
# that). Anything scripted against this API should call it rather than wait.
_IDLE_TIMEOUT_SECONDS = int(os.environ.get("CELMIS_AGENT_IDLE_SECONDS", 15 * 60))

#: How long a paused conversation stays resumable. The transcripts are the
#: largest thing stored per session, so this is a retention decision, not a
#: preference: a conversation kept for ever is one nobody pruned.
RESUMABLE_DAYS = int(os.environ.get("CELMIS_RESUMABLE_DAYS", 14))

_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "TodoWrite",
    # Both names of the subagent tool — granted explicitly, since a tool that
    # is in neither list has no permission path at all now that there is no
    # can_use_tool callback to fall through to.
    "Agent", "Task",
    "mcp__celmis__*",
    "mcp__celmis",
    # Execution, and the only execution there is. It runs in the sandbox
    # container, never here — see src/agent/exec_tool.py for why Bash stays on
    # the deny list below rather than being granted alongside it.
    "mcp__exec__run",
    "mcp__exec",
]
# Agent/Task (one tool, two names) are granted, and how they behave is the
# difference between the two session modes (src/agent/modes.py):
#   * standard — the PreToolUse hook rewrites each call to
#     run_in_background=False, so the subagent reports inline;
#   * workflow — background launches are left alone, and
#     _read_until_settled waits for them after the parent's turn ends.
# Session a3a51dc7 is what happens with neither: the model delegated in the
# background, said "I'm looking into it", and that sentence was the user's
# entire output because the loop stopped at the first result frame.
_DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch", "NotebookEdit"]

# Explicit, non-empty MCP scopes — an EMPTY scope list is full access.
_MCP_SCOPES = ["read:graph", "read:groups", "read:reviews"]


#: Put on the turn queue to end a conversation cleanly. A session used to end
#: when the model stopped talking; in a chat that only means "your turn", so
#: the end has to be a person's decision.
_FINISH = object()


@dataclass
class _Running:
    task: asyncio.Task
    client: Any | None = None                 # ClaudeSDKClient once connected
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    #: Further messages for a live session. The HTTP handler only ever PUTS
    #: here — `client.query()` is called by the runner's own task, because the
    #: SDK client cannot be driven from a different async context.
    turns: asyncio.Queue = field(default_factory=asyncio.Queue)
    #: Sandbox root, once the clone exists. An attachment is written under it
    #: so the agent can Read it without any new permission.
    workspace_root: Any | None = None
    #: The workspace itself, published the moment the clone exists.
    #:
    #: `_run_session` only receives it when `_drive_agent` RETURNS, so on a
    #: cancellation — which is what a deploy does to every live session — its
    #: local `workspace` is still None and there is nothing to push. That is
    #: how the first attempt at fixing this ended up calling a function that
    #: only sends a web notification. The object has to be reachable while the
    #: session is still running, not after it finishes.
    workspace: Any | None = None
    #: Every command the agent ran in the sandbox, with its exit code.
    #:
    #: This is the evidence half of "what was deployed". A commit that says
    #: what changed but not what was RUN leaves the reader — and, later, an
    #: auditor — to take the change on faith. Recorded here rather than parsed
    #: back out of the event log because the log is prose and this needs to be
    #: a list of facts.
    exec_runs: list[dict] = field(default_factory=list)


_registry: dict[str, _Running] = {}


# ─── Event log + live fanout ─────────────────────────────────────────


async def emit(session_id: str, event: str, data: dict) -> int:
    """Append an event row and fan it out to live subscribers. Returns id."""
    from sqlalchemy import insert

    from src.db import async_session
    from src.db.models import AgentSessionEvent

    async with async_session() as s:
        res = await s.execute(
            insert(AgentSessionEvent)
            .values(session_id=session_id, event=event, data=data)
            .returning(AgentSessionEvent.id)
        )
        event_id = int(res.scalar_one())
        await s.commit()

    running = _registry.get(session_id)
    if running:
        payload = {"id": event_id, "event": event, "data": data}
        for q in list(running.subscribers):
            # A subscriber that stopped draining drops events rather than
            # blocking the writer.
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)
    return event_id


def subscribe(session_id: str) -> asyncio.Queue | None:
    """Queue of live events, or None when the session isn't running here."""
    running = _registry.get(session_id)
    if running is None:
        return None
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    running.subscribers.append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    running = _registry.get(session_id)
    if running and q in running.subscribers:
        running.subscribers.remove(q)


def is_running(session_id: str) -> bool:
    return session_id in _registry


def send_turn(session_id: str, text: str) -> bool:
    """Queue another message for a live session. False when it is not here.

    False is the honest answer for two different situations — the session
    finished, or it belongs to another process — and the caller reports both
    the same way, because from outside they are the same: no more turns can be
    delivered.
    """
    running = _registry.get(session_id)
    if running is None:
        return False
    running.turns.put_nowait(text)
    return True


def record_exec(session_id: str, command: str, exit_code: int | None,
                *, ok: bool, elapsed: float) -> None:
    """Note one sandbox run, so the commit can say what was verified.

    Called from the exec tool. Silent when the session is not in this
    process — a lost note must never fail a command the agent is waiting on.
    """
    running = _registry.get(session_id)
    if running is None:
        return
    running.exec_runs.append({
        "command": command[:400],
        "exit_code": exit_code,
        "ok": ok,
        "elapsed": round(elapsed, 1),
    })


def exec_runs(session_id: str) -> list[dict]:
    running = _registry.get(session_id)
    return list(running.exec_runs) if running else []


def session_root(session_id: str):
    """The sandbox directory of a live session, or None.

    This is the same root the PreToolUse hook bounds every tool path to, so
    anything written under it is already reachable by the agent and nothing
    outside it becomes reachable by writing here.
    """
    running = _registry.get(session_id)
    if running is None:
        return None
    return getattr(running, "workspace_root", None)


def finish_session(session_id: str) -> bool:
    """Ask a live session to wrap up: no more turns, push, open the PR."""
    running = _registry.get(session_id)
    if running is None:
        return False
    running.turns.put_nowait(_FINISH)
    return True


def running_count_for_workspace(workspace_id: str) -> int:
    # Registry has no ws info; callers check the DB. Kept for symmetry.
    return len(_registry)


# ─── Session status writes (shielded — must land even on cancel) ─────


async def _set_status(session_id: str, status: str, *, error: str | None = None,
                      result: dict | None = None,
                      resumable_until=None) -> None:
    from sqlalchemy import update

    from src.db import async_session
    from src.db.models import AgentSession

    async def _write() -> None:
        async with async_session() as s:
            values: dict = {"status": status, "updated_at": datetime.now(UTC)}
            if error is not None:
                values["error"] = error[:2000]
            if result is not None:
                values["result"] = result
            if resumable_until is not None:
                values["resumable_until"] = resumable_until
            await s.execute(
                update(AgentSession).where(AgentSession.id == session_id).values(**values)
            )
            await s.commit()

    await asyncio.shield(_write())


async def _heartbeat_loop(session_id: str) -> None:
    from sqlalchemy import update

    from src.db import async_session
    from src.db.models import AgentSession

    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            async with async_session() as s:
                await s.execute(
                    update(AgentSession)
                    .where(AgentSession.id == session_id)
                    .values(last_heartbeat_at=datetime.now(UTC))
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_heartbeat_failed session=%s err=%s", session_id, exc)


# ─── Path guard ──────────────────────────────────────────────────────


_PATH_KEYS = ("file_path", "path", "notebook_path", "directory")


def _escapes_workspace(root: Path, tool_input: dict) -> str | None:
    """Return the offending path when a tool call points outside `root`.

    With several repos in one session `root` is their shared parent, so a path
    in repo B is reachable from repo A — that is the point of the feature —
    while everything above the parent stays closed. `resolve()` follows
    symlinks, so a link planted inside the workspace cannot be used to step
    out of it, and the comparison is on path PARTS: a string prefix would let
    `/w/session-1-evil` pass for being inside `/w/session-1`.
    """
    root = root.resolve()
    for key in _PATH_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        p = Path(str(raw))
        candidate = (root / p).resolve() if not p.is_absolute() else p.resolve()
        if candidate != root and root not in candidate.parents:
            return str(raw)
    return None


# The Agent tool, under both of its registered names.
_SUBAGENT_TOOLS = ("Agent", "Task")


def _make_path_hook(root: Path, *, allow_background_subagents: bool = False):
    """PreToolUse hook: the real sandbox boundary, plus the subagent policy.

    Fires for every tool call, including ones pre-approved through
    `allowed_tools` — which is every tool we grant, so without it the agent
    could read and write anywhere the API process can.
    """
    root = root.resolve()

    async def pre_tool_use(input_data: dict, tool_use_id: Any, context: Any) -> dict:
        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") or {}

        # Enforce the deny list here as well as in `disallowed_tools`. A
        # BACKGROUND subagent is given its own fixed set of built-in tools —
        # Bash among them — rather than the parent's, so in workflow mode the
        # option alone is not something to bet shell access on. The hook fires
        # for subagent calls too, so this holds wherever the call came from.
        if tool_name in _DISALLOWED_TOOLS:
            logger.warning("agent_tool_denied tool=%s", tool_name)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"{tool_name} is not available in Celmis agent sessions.",
                }
            }

        # Subagents: force the foreground. Left alone, the Agent tool launches
        # in the BACKGROUND (its default) and returns only an agentId — the
        # work itself lands in a later turn. `updatedInput` rewrites the call
        # so the subagent's report comes back inline in the tool result, which
        # is a shape this runner can stream to the user as it happens.
        if tool_name in _SUBAGENT_TOOLS:
            # Workflow mode wants the fan-out, and the read loop is prepared to
            # collect it — leave the model's choice alone.
            if allow_background_subagents:
                return {}
            if tool_input.get("run_in_background") is False:
                return {}
            logger.info("agent_subagent_forced_foreground tool=%s", tool_name)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {**tool_input, "run_in_background": False},
                }
            }

        # MCP tools used to be waved through here — `return {}`, no deny check
        # and no path check. That was survivable only because the deny list
        # made the shell unreachable: an MCP tool could name a path outside the
        # sandbox and nothing looked. The moment this agent gets Bash, or any
        # MCP server that writes files, that exemption is the single hole in
        # the only filesystem barrier there is.
        #
        # They are checked like everything else now. Celmis's own MCP tools are
        # reads over the graph and take no paths, so the check costs them
        # nothing; a server that DOES take a path is exactly the case worth
        # stopping.
        offending = _escapes_workspace(root, tool_input)
        if offending:
            logger.warning("agent_path_denied tool=%s path=%s", tool_name, offending)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"Path outside the session workspace: {offending}",
                }
            }
        return {}

    return pre_tool_use


# ─── The session task ────────────────────────────────────────────────


async def start_session(session_id: str, *, resume: bool = False) -> None:
    """Register + launch the runner task for a queued or paused session row.

    `resume` is the whole difference between "run this prompt" and "carry on".
    It reaches `_build_options`, which passes the same session id to the CLI as
    `resume=` instead of `session_id=`, and the transcript comes back out of
    Postgres through the store adapter. The workspace is cloned fresh either
    way — the edits are not in it, they are in the branch that was pushed.
    """
    task = asyncio.create_task(
        _run_session(session_id, resume=resume),
        name=f"agent-{session_id[:8]}")
    _registry[session_id] = _Running(task=task)


async def stop_session(session_id: str) -> bool:
    """Graceful stop: interrupt the SDK client, escalate to task.cancel().

    The sentinel goes on the queue FIRST. Interrupting relies on the runner
    loop seeing a result frame — and between turns there is no turn in flight
    to interrupt, so a conversation parked on `turns.get()` would sit there
    until the idle timeout while the caller was told it was stopping.
    """
    running = _registry.get(session_id)
    if running is None:
        return False
    running.turns.put_nowait(_FINISH)
    if running.client is not None:
        try:
            # Only meaningful when a turn IS in flight; harmless otherwise.
            await asyncio.wait_for(running.client.interrupt(), timeout=10)
            return True
        except Exception:  # noqa: BLE001
            pass
    return True


async def _run_session(session_id: str, *, resume: bool = False) -> None:
    from sqlalchemy import select

    from src.db import async_session
    from src.db.models import AgentSession

    async with async_session() as s:
        row = (await s.execute(
            select(AgentSession).where(AgentSession.id == session_id)
        )).scalar_one_or_none()
    if row is None:
        _registry.pop(session_id, None)
        return

    hb = asyncio.create_task(_heartbeat_loop(session_id))
    workspace = None
    clean_stop = False
    result_data: dict = {}
    try:
        await _set_status(session_id, "running")
        # No timeout wraps this any more. `_drive_agent` bounds each TURN by
        # TURN_WALL_CLOCK_SECONDS and the whole conversation by
        # SESSION_WALL_CLOCK_SECONDS, both from inside, so a session that runs
        # out of time still returns its workspace and still gets pushed. A
        # timeout out here raises instead, and skips the push below.
        workspace, result_data, clean_stop, paused = await _drive_agent(
            session_id, row, resume=resume)
        # Push on any CONTROLLED stop, not only a clean one. Reaching this line
        # means the agent returned a result frame — including "hit the turn
        # limit" — so the tree is in a state it chose to leave. A crash raises
        # instead and never gets here. Refusing to push an error stop is how
        # real edits used to be deleted with the workspace a moment later.
        if workspace is not None:
            # The commit message is the only record most people will ever
            # read of what an agent did to a repository, so it gets the whole
            # story: what was asked, what the agent says it did, what the diff
            # says it did, and every command that was run to check it.
            pushed = await asyncio.to_thread(
                _push_safely, workspace,
                summary=(result_data or {}).get("summary", ""),
                prompt=getattr(row, "prompt", "") or "",
                requested_by=_requester(row),
                title=getattr(row, "title", "") or "",
                verifications=exec_runs(row.id))
            if pushed:
                if pushed.get("branch"):
                    pr = await asyncio.to_thread(
                        _open_pr, row, workspace, pushed)
                    if pr:
                        pushed.update(pr)
                result_data.update(pushed)
                await emit(session_id, "pushed", pushed)
        if paused:
            # Nobody replied, so the conversation is not over — it is waiting.
            # The branch is already pushed above, the transcript is already in
            # Postgres, and no slot is held: `paused` is counted by nothing.
            # Reporting this as `done` is what made a session a one-shot thing.
            from datetime import timedelta
            await _set_status(
                session_id, "paused", result=result_data,
                resumable_until=datetime.now(UTC)
                + timedelta(days=RESUMABLE_DAYS))
            await emit(session_id, "paused", {"result": result_data,
                                              "resumable": True})
            await _push_terminal(row, "done", result_data)
        elif clean_stop:
            await _set_status(session_id, "done", result=result_data)
            await emit(session_id, "done", {"result": result_data})
            await _push_terminal(row, "done", result_data)
        else:
            # The run ended on an error result (turn limit, internal failure).
            # Reporting that as "done" made a truncated run look finished.
            detail = (result_data.get("summary")
                      or "Claude Code stopped before finishing the task.")
            # A conversation that used up its turn budget is not a crash: the
            # work is real and about to be pushed, and the user needs to be
            # told to open a fresh session rather than shown a red error with
            # no next step.
            if "turn" in detail.lower() and "limit" in detail.lower():
                detail = (f"{detail} Start a new session to carry on — "
                          f"everything done so far is pushed below.")
            await _set_status(session_id, "error", error=detail, result=result_data)
            await emit(session_id, "error", {"detail": detail,
                                             "result": result_data})
            await _push_terminal(row, "error", result_data, detail=detail)
    except asyncio.CancelledError:
        # PUSH FIRST. This branch used to do neither, on the reasoning in the
        # comment it carried — "cancelling is something the user just did on
        # purpose" — and that reasoning was about a caller it does not have.
        #
        # `stop_session` never gets here: it queues _FINISH and interrupts, so
        # a user pressing Stop goes down the clean path and its work is pushed.
        # The ONLY caller of task.cancel() is `shutdown_all`, i.e. the
        # container going down. So this branch is the deploy path, and it ran
        # `cleanup_workspace` in the `finally` below without pushing anything —
        # every deploy silently destroyed whatever any live session had
        # written.
        #
        # Shielded because we are already being cancelled: without it the
        # await is cancelled again immediately and the push never starts.
        cancelled_ws = workspace
        if cancelled_ws is None:
            # The usual case: `_drive_agent` was cancelled mid-run, so it never
            # returned and the local above is still None. The registry has it.
            entry = _registry.get(session_id)
            cancelled_ws = getattr(entry, "workspace", None) if entry else None
        if cancelled_ws is not None:
            try:
                # A REAL git push. The first version of this called
                # `_push_terminal`, which despite the name only sends a web
                # notification — so it announced work that had just been
                # deleted. Shielded because we are already being cancelled and
                # an unshielded await is cancelled again immediately.
                pushed = await asyncio.shield(asyncio.to_thread(
                    _push_safely, cancelled_ws,
                    requested_by=_requester(row),
                    summary=(result_data or {}).get("summary", ""),
                    prompt=getattr(row, "prompt", "") or "",
                    title=getattr(row, "title", "") or "",
                    verifications=exec_runs(session_id)))
                if pushed:
                    result_data = {**result_data, **pushed}
                    logger.info("agent_cancel_pushed id=%s branch=%s",
                                session_id, pushed.get("branch"))
            except Exception as exc:  # noqa: BLE001 — never mask the cancel
                logger.error("agent_cancel_push_failed id=%s err=%s",
                             session_id, exc)
        await _set_status(session_id, "cancelled", result=result_data)
        await emit(session_id, "done", {"cancelled": True})
    except TimeoutError:
        await _set_status(session_id, "error", error="session wall-clock limit reached")
        await emit(session_id, "error", {"detail": "Session hit the time limit."})
        await _push_terminal(row, "error", result_data,
                             detail="Hit the 40-minute limit.")
    except Exception as exc:  # noqa: BLE001
        detail = _classify_error(exc)
        logger.exception("agent_session_failed id=%s", session_id)
        await _set_status(session_id, "error", error=detail)
        await emit(session_id, "error", {"detail": detail})
        await _push_terminal(row, "error", result_data, detail=detail)
    finally:
        hb.cancel()
        if workspace is not None:
            from src.agent.workspace import cleanup_workspace
            await asyncio.to_thread(cleanup_workspace, session_id)
        _registry.pop(session_id, None)


async def _push_terminal(row, status: str, result: dict,
                         *, detail: str = "") -> None:
    """Notify the person who started this that it is over.

    The whole point of running an agent from a phone is not to sit and watch
    it, so the result has to find them rather than the other way round. Fired
    only on terminal states, and never on a cancel — they were looking at the
    screen when they pressed it.

    Best-effort in the strongest sense: a notification failure must not touch
    the session's own outcome, which is already committed by this point.
    """
    try:
        from src.notifications import webpush

        if not webpush.is_enabled():
            return
        repo = getattr(row, "repo_slug", "") or "repo"
        title = getattr(row, "title", "") or getattr(row, "prompt", "")[:60]
        if status == "done":
            branch = result.get("branch")
            body = (f"{repo} · pushed {branch}" if branch
                    else f"{repo} · finished with no changes")
        else:
            body = f"{repo} · {detail[:140] or 'stopped early'}"
        await asyncio.to_thread(
            webpush.send_to_user,
            row.user_id,
            title=f"{'Finished' if status == 'done' else 'Stopped'}: {title[:60]}",
            body=body,
            url=f"/claude/{row.id}",
            tag=f"session-{row.id[:8]}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_push_notify_failed session=%s err=%s",
                       getattr(row, "id", "?"), str(exc)[:200])


def _requester(row) -> str:
    """The human this session was started by, as `Name <email>`.

    An agent commit was authored by `Celmis Agent <agent@celmis.local>` with
    no GitHub user association and no signature. From git alone an auditor
    could not tell WHICH person authorised a change to a repository — only the
    trailer `Celmis session: <uuid>` linked back, and resolving that requires
    Celmis access. For a product whose argument is that a machine can be
    trusted to push, "who let it" is the first question.

    Empty when the user cannot be resolved: a wrong name is worse than none.
    """
    from src.users import get_user_store

    user_id = getattr(row, "user_id", "") or ""
    if not user_id:
        return ""
    try:
        # `get_by_id`, not `get`. The first version of this called `get()`,
        # which does not exist — and the bare `except` below swallowed the
        # AttributeError and returned "", so the trailer silently never
        # appeared. The unit test passed because it called `_commit_message`
        # with an explicit string and never exercised this function.
        #
        # The except is kept, because a commit must not fail over a missing
        # display name, and narrowed to what can legitimately go wrong: a
        # store that is unreachable. An AttributeError now propagates in
        # tests instead of being absorbed.
        user = get_user_store().get_by_id(user_id)
    except (OSError, RuntimeError, ValueError):
        return ""
    if user is None or not getattr(user, "email", ""):
        return ""
    return f"{getattr(user, 'name', '') or user.email} <{user.email}>"


def _push_safely(workspace, *, summary: str = "", prompt: str = "",
                 requested_by: str = "", title: str = "",
                 verifications: list[dict] | None = None) -> dict | None:
    from src.agent.workspace import commit_and_push
    try:
        return commit_and_push(workspace, summary=summary, prompt=prompt,
                               requested_by=requested_by, title=title,
                               verifications=verifications or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_push_failed session=%s err=%s", workspace.session_id, exc)
        return {"push_error": str(exc)[:300]}


def _open_pr(row, workspace, pushed: dict) -> dict | None:
    """Best-effort PR/MR creation after a branch push — the user asked for
    'push to a separate branch AND open a PR'. Falls back silently to the
    compare_url the UI already renders."""
    from src.credentials import resolve_git_credential
    from src.http import build_client
    from src.sync.git_providers import parse_repo_url

    try:
        parsed = parse_repo_url(workspace.clean_url)
        provider = parsed.provider.value
        creds = resolve_git_credential(provider, workspace_id=row.workspace_id)
        if creds is None:
            return None
        title = (row.title or row.prompt or "Celmis agent changes")[:120]
        branch = pushed["branch"]
        base = workspace.default_branch or "main"
        full = f"{parsed.owner}/{parsed.name}"
        with build_client(timeout=20.0) as client:
            if provider == "github":
                r = client.post(
                    f"https://api.github.com/repos/{full}/pulls",
                    headers={"Authorization": f"Bearer {creds.secret}",
                             "Accept": "application/vnd.github+json"},
                    json={"title": title, "head": branch, "base": base,
                          "body": "Automated change by a Celmis agent session."},
                )
                if r.status_code in (200, 201):
                    return {"pr_url": r.json().get("html_url")}
            elif provider == "gitlab":
                import urllib.parse
                proj = urllib.parse.quote(full, safe="")
                r = client.post(
                    f"https://gitlab.com/api/v4/projects/{proj}/merge_requests",
                    headers={"PRIVATE-TOKEN": creds.secret},
                    json={"source_branch": branch, "target_branch": base,
                          "title": title},
                )
                if r.status_code in (200, 201):
                    return {"pr_url": r.json().get("web_url")}
            elif provider == "bitbucket":
                email = (creds.metadata or {}).get("atlassian_email")
                auth = (str(email), creds.secret) if email else None
                headers = ({} if auth
                           else {"Authorization": f"Bearer {creds.secret}"})
                r = client.post(
                    f"https://api.bitbucket.org/2.0/repositories/{full}/pullrequests",
                    auth=auth, headers=headers,
                    json={"title": title,
                          "source": {"branch": {"name": branch}},
                          "destination": {"branch": {"name": base}}},
                )
                if r.status_code in (200, 201):
                    return {"pr_url": (r.json().get("links", {})
                                       .get("html", {}).get("href"))}
        logger.warning("agent_pr_create_failed session=%s status=%s",
                       workspace.session_id, r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_pr_create_failed session=%s err=%s",
                       workspace.session_id, exc)
    return None


def _classify_error(exc: Exception) -> str:
    text = str(exc)[:500]
    lowered = text.lower()
    if "401" in text or "authentication" in lowered or "oauth" in lowered:
        return "Claude token rejected — reconnect your Claude account (auth error)."
    if "rate" in lowered and "limit" in lowered:
        return "Claude subscription rate limit hit — try again later."
    return text


# How long to keep reading after a result frame that still has background tasks
# in flight. The CLI's own ceiling for this wait is 10 minutes
# (CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS); ours is shorter because the session
# wall clock has to hold too, and a stuck task must not eat it all.
_TAIL_WAIT_SECONDS = 5 * 60


def _task_message_types() -> tuple[Any, ...]:
    """``(TaskStarted, TaskNotification, TaskUpdated, terminal_statuses)``.

    Imported defensively: the SDK pin is a range, and these classes only exist
    in newer versions. On an older SDK the tracker simply never sees a task,
    which degrades to the previous behaviour instead of an ImportError.
    """
    try:
        from claude_agent_sdk import (
            TERMINAL_TASK_STATUSES,
            TaskNotificationMessage,
            TaskStartedMessage,
            TaskUpdatedMessage,
        )
    except ImportError:  # pragma: no cover — depends on the installed SDK
        logger.info("agent_sdk_without_task_messages")
        return ((), (), (), frozenset({"completed", "failed", "stopped", "killed"}))
    return (
        (TaskStartedMessage,), (TaskNotificationMessage,), (TaskUpdatedMessage,),
        TERMINAL_TASK_STATUSES,
    )


async def _read_until_settled(client, session_id: str, task_types: tuple,
                              tail_wait_seconds: int = _TAIL_WAIT_SECONDS,
                              stream=None):
    """Yield messages until the run has actually settled.

    ``receive_response()`` stops at the FIRST result frame, and that is not the
    same thing as the run being over: a result ends one TURN, and a background
    task that is still running wakes the parent for a follow-up turn ending in
    a later result. Stopping at the first one is what lost session a3a51dc7 —
    the subagent's entire answer was in the turn we never read.

    Draining to end-of-stream is not an option either: this client keeps stdin
    open, so the CLI never closes stdout and the iterator would hang until the
    wall clock. So the rule is the SDK's own: stop on a result frame ONLY when
    no task is in flight, and bound the wait when one is.
    """
    from claude_agent_sdk import ResultMessage

    started, notified, updated, terminal = task_types
    pending: set[str] = set()
    deadline: float | None = None
    loop = asyncio.get_running_loop()

    # The iterator is created by the CALLER in a multi-turn session and passed
    # in. Creating it here per turn would abandon a suspended async generator
    # on every turn — each one still holding the transport — and the messages
    # of turn two would arrive on an iterator nobody was reading.
    if stream is None:
        stream = client.receive_messages().__aiter__()
    while True:
        try:
            if deadline is None:
                message = await stream.__anext__()
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning("agent_tail_wait_expired session=%s pending=%s",
                                   session_id, len(pending))
                    await emit(session_id, "meta", {
                        "status": "background_task_timeout",
                        "note": "A background task did not finish in time; "
                                "its result is not included.",
                    })
                    return
                message = await asyncio.wait_for(stream.__anext__(), remaining)
        except StopAsyncIteration:
            return
        except TimeoutError:
            logger.warning("agent_tail_wait_timeout session=%s", session_id)
            return

        # Task lifecycle: track what is in flight, and let the user see it.
        if started and isinstance(message, started):
            pending.add(str(getattr(message, "task_id", "") or ""))
            await emit(session_id, "meta", {
                "status": "task_started",
                "task": str(getattr(message, "description", "") or "")[:200],
            })
            continue
        if (notified and isinstance(message, notified)) or \
                (updated and isinstance(message, updated)):
            status = str(getattr(message, "status", "") or "")
            if status in terminal:
                pending.discard(str(getattr(message, "task_id", "") or ""))
            summary = str(getattr(message, "summary", "") or "")
            if summary:
                await emit(session_id, "text", {"text": summary})
            continue

        yield message

        if isinstance(message, ResultMessage):
            if not pending:
                return
            # A follow-up turn is coming with the task's actual output.
            logger.info("agent_result_with_pending_tasks session=%s pending=%d",
                        session_id, len(pending))
            deadline = loop.time() + tail_wait_seconds


async def _notify_turn_done(row, session_id: str, result: dict) -> None:
    """Tell the workspace's channels that the agent finished a turn.

    The session page is the only place this shows up otherwise, and nobody
    watches a page while an agent works — they close the tab. Without this the
    conversation is only continuable by someone who happens to look.

    Deliberately per TURN, not per session: a session may be parked for
    fifteen minutes and then continue, so "done" at the session level arrives
    far too late to answer "is it my move yet".

    Best effort in every direction. A workspace with no channels configured
    delivers nothing and that is fine; a failing webhook must not touch the
    conversation, which is why this cannot raise.
    """
    summary = (result.get("summary") or "").strip()
    if not summary:
        return
    try:
        from src.notifications.dispatch import notify

        await asyncio.to_thread(
            notify,
            workspace_id=row.workspace_id,
            event="agent_turn_done",
            repo_slug=getattr(row, "repo_slug", None),
            title="Claude Code finished a step",
            # Trimmed hard: a chat card is glanced at, and the full transcript
            # is one click away in the session.
            body_md=summary[:1200],
            severity="info",
            link_url=f"/claude/{session_id}",
            extra={"session_id": session_id,
                   "turns": result.get("turns"),
                   "cost_usd": result.get("cost_usd")},
        )
    except Exception as exc:  # noqa: BLE001 — a chat webhook is not the work
        logger.warning("agent_turn_notify_failed session=%s err=%s",
                       session_id, exc)


async def _drive_agent(session_id: str, row, *,
                       resume: bool = False) -> tuple[Any, dict, bool, bool]:
    """Clone, run the SDK loop, stream events. Returns (workspace, result, clean)."""
    from src.agent.connection import resolve_connection
    from src.agent.workspace import prepare_multi_workspace

    conn = resolve_connection(row.user_id, row.workspace_id)
    if conn is None:
        raise RuntimeError("No Claude connection — connect your account on the Claude page.")

    # Repo URLs from the auto-review registry (the same source repos are added
    # from). `slugs` covers rows written before multi-repo, which have only
    # repo_slug.
    from src.api.auto_review import get_auto_review_store
    store = get_auto_review_store()
    slugs = list(getattr(row, "slugs", None) or [row.repo_slug])

    specs: list[tuple[str, str, str | None]] = []
    for slug in slugs:
        cfg = store.get(row.user_id, slug)
        if cfg is None:
            # Any registration of this repo in the same workspace will do.
            same_ws = [c for c in store.list_all()
                       if c.repo_slug == slug and c.workspace_id == row.workspace_id]
            cfg = same_ws[0] if same_ws else None
        if cfg is None:
            raise RuntimeError(f"Repo {slug!r} is not registered in this workspace.")
        specs.append((slug, cfg.url, cfg.branch or None))

    await emit(session_id, "meta", {
        "repo": ", ".join(s for s, _, _ in specs), "status": "cloning",
        "repos": [s for s, _, _ in specs],
        "note": ("Preparing an isolated workspace (clone can take up to a minute)…"
                 if len(specs) == 1 else
                 f"Cloning {len(specs)} repositories into one workspace…"),
    })
    workspace = await asyncio.to_thread(
        prepare_multi_workspace, session_id, specs, row.workspace_id,
    )
    await emit(session_id, "meta", {"status": "starting_agent",
                                    "branch": f"celmis-agent/{session_id[:8]}"})

    from src.agent.modes import get_spec
    spec = get_spec(getattr(row, "mode", None))

    mcp_token = _mint_mcp_token(row.user_id)
    options = _build_options(workspace, conn.token, mcp_token, resume=resume,
                             spec=spec, model=getattr(row, "model", "") or "")
    logger.info("agent_session_options session=%s mode=%s model=%s effort=%s",
                session_id, spec.name, options.model or "(cli default)", spec.effort)

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
    task_types = _task_message_types()

    result_data: dict = {}
    clean_stop = False
    saw_result = False
    timed_out = False
    #: Set when the conversation stopped because nobody replied — as opposed to
    #: somebody finishing it. The difference decides `paused` versus `done`,
    #: and therefore whether it can be picked up again.
    paused = False
    #: Cumulative usage already written to the ledger. The SDK reports totals
    #: for the whole conversation on every result, so this is what turns them
    #: back into per-turn deltas.
    spend_prior: tuple[int, int, int] = (0, 0, 0)
    client = ClaudeSDKClient(options=options)
    running = _registry.get(session_id)
    if running is not None:
        # Published so the attachment route can write into the same directory
        # the path hook already bounds every tool call to.
        running.workspace_root = workspace.root_dir or workspace.repo_dir
        running.workspace = workspace

    # Files attached BEFORE the session started — the production log that made
    # somebody open it. They were staged outside the workspace because there
    # was no workspace yet; now there is one.
    from src.agent.workspace import adopt_staged
    staged = await asyncio.to_thread(adopt_staged, session_id, workspace)
    if staged:
        await emit(session_id, "note", {
            "text": "Attached before start: " + ", ".join(staged)})
    async with client:
        if running:
            running.client = client
        # One iterator for the whole conversation — see _read_until_settled.
        stream = client.receive_messages().__aiter__()
        turn_text: Any = row.prompt
        if staged:
            # Named in the prompt itself. An attachment the model is not told
            # about is a file sitting in a directory it has no reason to list.
            turn_text = (f"{row.prompt}\n\nFiles attached with this request:\n"
                         + "\n".join(f"  {path}" for path in staged)
                         + "\nRead them before you start.")
        idle_seconds = _IDLE_TIMEOUT_SECONDS
        deadline = asyncio.get_running_loop().time() + SESSION_WALL_CLOCK_SECONDS
        while True:
            # One turn's work, bounded. A timeout here is caught, not raised:
            # forty minutes of edits are worth pushing even though the agent
            # never got to say so.
            try:
                async with asyncio.timeout(TURN_WALL_CLOCK_SECONDS):
                    await client.query(turn_text)
                    async for message in _read_until_settled(
                            client, session_id, task_types, spec.tail_wait_seconds,
                            stream=stream):
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock) and block.text.strip():
                                    await emit(session_id, "text", {"text": block.text})
                                elif isinstance(block, ToolUseBlock):
                                    await emit(session_id, "tool_use", {
                                        "name": block.name,
                                        "input": _preview(block.input),
                                    })
                        elif isinstance(message, UserMessage):
                            content = message.content if isinstance(message.content, list) else []
                            for block in content:
                                if isinstance(block, ToolResultBlock):
                                    await emit(session_id, "tool_result", {
                                        "preview": _preview_result(block.content),
                                        "is_error": bool(block.is_error),
                                    })
                        elif isinstance(message, ResultMessage):
                            saw_result = True
                            clean_stop = not message.is_error
                            # `num_turns`, `duration_ms` and `total_cost_usd` are all
                            # conversation totals, so a later result supersedes an earlier
                            # one rather than adding to it — but `summary` is this turn's
                            # answer, and replacing it wholesale is how a multi-turn
                            # session would report only its last word.
                            result_data = {
                                "turns": message.num_turns,
                                "duration_ms": message.duration_ms,
                                "cost_usd": message.total_cost_usd,
                                "summary": (message.result or "")[:2000],
                            }
                            # Sync DB write → off the event loop. The usage on a result is
                            # cumulative, so the ledger is told what was already booked and
                            # writes only the difference.
                            spend_prior = await asyncio.to_thread(
                                _record_agent_spend, row, message, spend_prior)
                            await emit(session_id, "result", result_data)
            except TimeoutError:
                # The agent is still going after forty minutes on one message.
                # Stop it, but keep everything it wrote: the caller pushes on
                # any controlled stop, and this is one.
                logger.warning("agent_turn_timeout session=%s after=%ss",
                               session_id, TURN_WALL_CLOCK_SECONDS)
                timed_out = True
                clean_stop = False
                await emit(session_id, "note", {
                    "text": f"Stopped after {TURN_WALL_CLOCK_SECONDS // 60} "
                            "minutes on one message. Anything already changed "
                            "is pushed; start a new session to carry on."})
                break

            # The turn is over — so say so, out of band.
            #
            # This is what makes "drop the logs and go get coffee" actually
            # work. The page is the only place a finished turn appears, and
            # nobody watches a page for ten minutes; they close the tab. An
            # earlier design answered that by finishing the session when no SSE
            # subscriber was left, which is backwards: it ends the conversation
            # precisely when the person has stepped away, and the whole point
            # is that they come back to it.
            #
            # So the session stays exactly as it is, and the notification
            # travels instead — through the same per-workspace channels
            # (Google Chat, Slack, Discord) everything else already uses.
            await _notify_turn_done(row, session_id, result_data)

            # In a one-shot session that was the end of the run; in a
            # conversation it means "your move". Wait for the next message, and
            # let silence be what ends it — a chat spends most of its life
            # waiting for a human, so a single wall clock over the whole session
            # would spend itself on somebody getting coffee.
            #
            # The absolute deadline caps the wait rather than racing it, so the
            # backstop lands on this line — between turns, with nothing in
            # flight — instead of interrupting work.
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.info("agent_session_deadline session=%s", session_id)
                await emit(session_id, "note", {
                    "text": "Session closed after "
                            f"{SESSION_WALL_CLOCK_SECONDS // 3600} hours. "
                            "Start a new one to carry on."})
                break
            try:
                nxt = await asyncio.wait_for(
                    running.turns.get(), min(idle_seconds, remaining)) \
                    if running else _FINISH
            except TimeoutError:
                logger.info("agent_idle_timeout session=%s after=%ss",
                            session_id, idle_seconds)
                paused = True
                await emit(session_id, "note", {
                    "text": f"Paused after {idle_seconds // 60} minutes with "
                            "no reply. Everything so far is pushed. Open this "
                            "session again to carry on where it left off."})
                break
            if nxt is _FINISH:
                break
            turn_text = str(nxt)
            await emit(session_id, "user", {"text": turn_text[:4000]})
        if running:
            running.client = None

    if not saw_result and not timed_out:
        # The stream ended without a result frame: the CLI died, was killed, or
        # closed stdout mid-run. Saying "done" here is how an agent's edits used
        # to be deleted while the session reported success — the caller skips the
        # push on a non-clean stop and then removes the workspace.
        #
        # A turn we cut off ourselves is a different thing and must not raise:
        # the CLI was alive and working, and raising here would throw away the
        # forty minutes of edits the timeout exists to salvage.
        raise RuntimeError(
            "Claude Code ended without returning a result — the CLI exited "
            "early. Nothing was pushed."
        )
    return workspace, result_data, clean_stop, paused


def _sdk_usage(message) -> tuple[int, int, int]:
    """``(input, output, cached_input)`` tokens from an SDK ``ResultMessage``.

    ``ResultMessage.usage`` is a plain dict in the current SDK but has moved
    shape before — read it defensively, and report zeros rather than guessing
    when the field is missing.
    """
    usage = getattr(message, "usage", None) or {}

    def _get(*names: str) -> int:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            try:
                if value:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    cached = _get("cache_read_input_tokens") + _get("cache_creation_input_tokens")
    return _get("input_tokens"), _get("output_tokens"), cached


def _record_agent_spend(row, message, prior=(0, 0, 0)) -> tuple[int, int, int]:
    """Ledger row for a Claude Code agent session. Never raises.

    Returns the cumulative usage seen so far, which the caller passes back as
    `prior` on the next result.

    `ResultMessage.usage` is CUMULATIVE across the conversation — `num_turns`
    on the same message is a conversation counter, not a per-query one. With
    one result per session that distinction never showed. A multi-turn session
    produces one result per turn, each carrying the running total, so writing
    it whole would bill turn 1 ten times over: a ten-turn chat writes roughly
    fifty-five turns of tokens to the tenant's Usage page. Only the delta is
    recorded.

    Cost is recorded as 0 with ``cost_source="subscription"``: the session runs
    on the user's connected Claude subscription, so the tokens are real but no
    per-token money is charged. ``ResultMessage.total_cost_usd`` (the
    API-equivalent price) is kept in the session result payload instead, so the
    ledger's dollar column stays "money actually spent".
    """
    try:
        from src.llm.budget import SURFACE_AGENT, record_spend

        total_in, total_out, total_cached = _sdk_usage(message)
        if not (total_in or total_out):
            logger.info("agent_spend_no_usage session=%s", getattr(row, "id", "?"))
            return prior
        # max(0, …) because a provider that resets or reorders its counters
        # must not produce a negative ledger row.
        tokens_in = max(0, total_in - prior[0])
        tokens_out = max(0, total_out - prior[1])
        cached = max(0, total_cached - prior[2])
        if not (tokens_in or tokens_out):
            return (total_in, total_out, total_cached)
        record_spend(
            workspace_id=getattr(row, "workspace_id", "default") or "default",
            surface=SURFACE_AGENT,
            agent="claude_code",
            model=str(getattr(message, "model", "") or "claude-code"),
            provider="anthropic",
            cost_usd=0.0,
            cost_source="subscription",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens_in=cached,
            user_id=getattr(row, "user_id", None),
            repo_slug=getattr(row, "repo_slug", None),
        )
        return (total_in, total_out, total_cached)
    except Exception as exc:  # noqa: BLE001 — ledger must never break a session
        logger.warning("agent_spend_record_failed err=%s", exc)
        # `prior` unchanged: a write that failed was not booked, so the next
        # result must still be able to book it rather than treat it as spent.
        return prior


def _build_options(workspace, oauth_token: str, mcp_token: str,
                   *, resume: bool = False,
                   spec=None, model: str = ""):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from src.agent.exec_tool import build_exec_server
    from src.agent.modes import get_spec
    from src.agent.transcript_store import PostgresTranscriptStore
    spec = spec or get_spec(None)

    api_port = os.environ.get("PORT", "8000")
    env = {
        "HOME": str(workspace.home_dir),
        "CLAUDE_CONFIG_DIR": str(workspace.home_dir / ".claude"),
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return ClaudeAgentOptions(
        cwd=str(workspace.root_dir or workspace.repo_dir),
        env=env,
        # WE mint the CLI's session id, rather than sniffing it back out of
        # the message stream: `--session-id` is forwarded to the subprocess, and
        # our own row id is already a uuid4. So resuming is just passing the
        # same string again, and nothing has to be recorded to make that work.
        **({"resume": workspace.session_id} if resume
           else {"session_id": workspace.session_id}),
        # The durable copy of the conversation. The subprocess still writes its
        # own under CLAUDE_CONFIG_DIR — that copy is scratch and dies with the
        # container, which is the whole reason this one exists.
        session_store=PostgresTranscriptStore(workspace.session_id),
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=_DISALLOWED_TOOLS,
        permission_mode="acceptEdits",
        max_turns=spec.max_turns,
        model=(model or spec.default_model) or None,
        effort=spec.effort,
        # A PreToolUse hook, not `can_use_tool`, is what contains the agent.
        # The callback used to do this job and was worse than useless: the SDK
        # skips it for anything in allowed_tools (CanUseToolShadowedWarning),
        # so it never saw the tools we grant — while for tools we had NOT
        # granted it fell through to a blanket allow. That is how `Agent` got
        # in. Hooks run for every call, granted or not.
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[_make_path_hook(
                    workspace.root_dir or workspace.repo_dir,
                    allow_background_subagents=spec.allow_background_subagents,
                )]),
            ],
        },
        mcp_servers={
            # In-process, so the workspace directory is closed over rather than
            # passed by the model, and no new HTTP surface or token exists.
            "exec": build_exec_server(
                workspace.root_dir or workspace.repo_dir,
                branch_name=getattr(workspace, "branch", None) or "work",
                session_id=workspace.session_id),
            "celmis": {
                "type": "http",
                # Trailing slash on purpose: without it Starlette answers 307,
                    # and a redirected POST is not something the MCP
                    # streamable-HTTP client is guaranteed to follow.
                    "url": f"http://localhost:{api_port}/mcp/",
                "headers": {"Authorization": f"Bearer {mcp_token}"},
            },
        },
        system_prompt={
            "type": "preset", "preset": "claude_code",
            "append": (
                (f"Your working directory holds {len(workspace.repos)} cloned "
                 "repositories as sibling directories: "
                 + ", ".join(r.path.name for r in workspace.repos)
                 + ". You may read and edit across all of them; each is pushed "
                   "to its own branch, and only the ones you actually change. "
                 if len(workspace.repos) > 1 else "")
                + "You are working inside an isolated clone of the repository on a "
                "dedicated branch. Make the requested change with minimal, focused "
                "edits. You have Celmis MCP tools (mcp__celmis__*) for cross-repo "
                "symbol search, architecture and impact analysis — prefer them for "
                "code exploration.\n\n"
                # Without this the model has no idea execution is possible: it
                # was told the opposite for every session before this one, and
                # a tool it does not know about is a tool it does not use.
                "To run anything — tests, a linter, a type check, a build — use "
                "mcp__exec__run. It executes in an isolated sandbox container "
                "against a copy of your current tree, so your edits are "
                "included and anything it writes is discarded. Bash is not "
                "available and this is why: your edits live beside other "
                "tenants' credentials, and the sandbox does not. VERIFY your "
                "change before you finish — running the project's own tests is "
                "worth more than re-reading the diff.\n\n"
                "Committing and pushing is handled automatically after you "
                "finish. Subagents run in the foreground here, so their report "
                "comes straight back to you. End with a short summary of what "
                "you changed and what you ran to check it."
            ),
        },
    )


def _mint_mcp_token(user_id: str) -> str:
    from src.mcp_server.auth import JwtConfig, issue_token
    return issue_token(
        JwtConfig.from_env(),
        subject=user_id,
        scopes=list(_MCP_SCOPES),
        client_id="celmis-agent",
        expires_in=SESSION_WALL_CLOCK_SECONDS + 20 * 60,
    )


def _preview(value: Any, limit: int = 400) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _preview_result(content: Any, limit: int = 500) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        text = "\n".join(parts)
    else:
        text = str(content or "")
    return text if len(text) <= limit else text[:limit] + "…"


# ─── Startup / shutdown hooks ────────────────────────────────────────


async def mark_orphaned_sessions() -> int:
    """Mark 'running' rows with a stale heartbeat as errored. Deploy-safe:
    a live session on another instance keeps heartbeating and is untouched."""
    from datetime import timedelta

    from sqlalchemy import or_, update

    from src.db import async_session
    from src.db.models import AgentSession

    cutoff = datetime.now(UTC) - timedelta(seconds=HEARTBEAT_SECONDS * 3)
    async with async_session() as s:
        res = await s.execute(
            update(AgentSession)
            .where(
                AgentSession.status.in_(("running", "queued")),
                or_(
                    AgentSession.last_heartbeat_at.is_(None),
                    AgentSession.last_heartbeat_at < cutoff,
                ),
            )
            .values(status="error", error="orphaned (server restarted mid-session)")
        )
        await s.commit()
        return int(res.rowcount or 0)


#: How long a shutdown waits for sessions to push what they have. A container
#: stop gives us a SIGTERM grace period before SIGKILL (docker's default is
#: 10s, and compose's `stop_grace_period` can raise it), so this has to fit
#: inside that or the wait is decorative.
SHUTDOWN_PUSH_SECONDS = int(os.environ.get("CELMIS_SHUTDOWN_PUSH_SECONDS", 25))


async def shutdown_all() -> None:
    """Cancel every live session — and WAIT, so each one can push first.

    Cancelling and returning immediately is what this used to do, and it made
    the push in the CancelledError handler unreachable in practice: the event
    loop stopped before the shielded coroutine got a chance to run. The work
    was gone by the time the container came back.
    """
    running_tasks = [r.task for r in _registry.values()]
    if not running_tasks:
        return
    logger.info("agent_shutdown cancelling=%d waiting=%ds",
                len(running_tasks), SHUTDOWN_PUSH_SECONDS)
    for task in running_tasks:
        task.cancel()
    # return_exceptions: a session that fails to push must not stop the others
    # from trying, and CancelledError itself arrives here as a result.
    done, pending = await asyncio.wait(
        running_tasks, timeout=SHUTDOWN_PUSH_SECONDS)
    if pending:
        logger.error(
            "agent_shutdown_timeout still_running=%d after=%ds — their work "
            "may not have been pushed", len(pending), SHUTDOWN_PUSH_SECONDS)
    else:
        logger.info("agent_shutdown_complete pushed=%d", len(done))


__all__ = [
    "RUNNER_INSTANCE",
    "emit",
    "subscribe",
    "unsubscribe",
    "is_running",
    "start_session",
    "stop_session",
    "mark_orphaned_sessions",
    "shutdown_all",
]
