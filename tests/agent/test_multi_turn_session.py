"""A session is a conversation, not a single question.

Every message used to start a new session: a fresh clone, a fresh context, and
a push and a pull request at the end of each one. "Now also run the tests" or
"no, do it this way instead" meant beginning again from nothing.

The SDK was always ready for this — `ClaudeSDKClient.__aenter__` connects with
no prompt and substitutes an empty stream precisely so the CLI's stdin stays
open, and `query()` is just a line written to that transport. The runner even
says so in a comment. What was missing was somewhere for the next message to
arrive and a loop to wait for it.

Two things this must not break, and both are pinned below: the push stays
terminal-only (it lives outside `_drive_agent`, so a loop inside it fires the
push once, at the real end), and the one-live-session-per-workspace rule stays
exactly as strict — an idle timeout frees the slot instead.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

from src.agent import runner

ROOT = Path(__file__).resolve().parents[2]
RUNNER = (ROOT / "src" / "agent" / "runner.py").read_text()
ROUTES = (ROOT / "src" / "api" / "routers" / "claude_code.py").read_text()


def _register(session_id: str):
    """A registry entry with no real task behind it."""
    running = runner._Running(task=asyncio.get_event_loop().create_future())  # type: ignore[arg-type]
    runner._registry[session_id] = running
    return running


# ─── delivering a turn ───────────────────────────────────────────────


def test_a_turn_reaches_a_live_session():
    async def go():
        running = _register("s-live")
        try:
            assert runner.send_turn("s-live", "also run the tests") is True
            assert running.turns.get_nowait() == "also run the tests"
        finally:
            runner._registry.pop("s-live", None)

    asyncio.run(go())


def test_a_turn_for_an_unknown_session_is_refused_not_dropped():
    """False covers two situations — finished, or living in another process —
    and from outside they are the same fact: no further turn can arrive."""
    assert runner.send_turn("nobody", "hello") is False
    assert runner.finish_session("nobody") is False


def test_the_http_handler_never_touches_the_sdk_client():
    """The SDK client cannot be driven from a different async context, so the
    request handler may only put on the queue."""
    body = inspect.getsource(runner.send_turn)
    assert "turns.put_nowait" in body
    assert "query" not in body, "the route would call the SDK from its own task"


def test_finishing_is_a_sentinel_rather_than_a_cancel():
    """A person ending a conversation is not an error, and cancelling would
    skip the push that the whole session was for."""
    async def go():
        running = _register("s-fin")
        try:
            assert runner.finish_session("s-fin") is True
            assert running.turns.get_nowait() is runner._FINISH
        finally:
            runner._registry.pop("s-fin", None)

    asyncio.run(go())


# ─── the loop ────────────────────────────────────────────────────────


def test_the_message_iterator_spans_every_turn():
    """Created per turn it would abandon a suspended async generator each
    time — each still holding the transport — and turn two's messages would
    arrive on an iterator nobody reads."""
    body = inspect.getsource(runner._drive_agent)
    assert "client.receive_messages().__aiter__()" in body, (
        "the iterator is no longer owned by the caller"
    )
    assert "stream=stream" in body
    settle = inspect.getsource(runner._read_until_settled)
    assert "if stream is None:" in settle, "it creates its own again"


def test_the_loop_waits_for_the_next_turn_rather_than_returning():
    body = inspect.getsource(runner._drive_agent)
    assert "while True:" in body
    assert "running.turns.get()" in body
    assert "asyncio.wait_for" in body, "no idle bound — a chat would hold the slot forever"


def test_silence_ends_the_session_instead_of_a_wall_clock():
    """A conversation spends most of its life waiting for a human, so one
    clock over the whole session would spend itself on somebody's lunch."""
    assert runner._IDLE_TIMEOUT_SECONDS > 0
    body = inspect.getsource(runner._drive_agent)
    assert "idle_seconds" in body
    # `asyncio.TimeoutError` is an alias of the builtin since 3.11, and the
    # linter rewrote it. What matters is that a timeout is caught here.
    assert "TimeoutError" in body


def test_the_users_own_turns_are_in_the_transcript():
    """Without this the log shows only the agent's half, and the opening
    prompt appears solely as the page heading."""
    body = inspect.getsource(runner._drive_agent)
    assert 'emit(session_id, "user"' in body


def test_the_push_is_still_terminal_only():
    """It lives OUTSIDE _drive_agent, so a loop inside fires it once at the
    real end. Both research plans called this a blocker; it never was."""
    session = inspect.getsource(runner._run_session)
    drive = inspect.getsource(runner._drive_agent)
    assert "_push_terminal" in session
    assert "_push_terminal" not in drive, "the push moved inside the turn loop"


def test_stopping_a_parked_conversation_actually_stops_it():
    """Interrupting relies on the loop seeing a result frame. Between turns
    there is no turn in flight, so a parked conversation would sit until the
    idle timeout while the caller was told it was stopping."""
    body = inspect.getsource(runner.stop_session)
    put = body.find("turns.put_nowait")
    interrupt = body.find("interrupt()")
    assert 0 < put < interrupt, "the sentinel must be queued before interrupting"


# ─── the clocks ──────────────────────────────────────────────────────


def test_the_wall_clock_no_longer_wraps_the_whole_conversation():
    """It was right while a session was one prompt. As a conversation it
    counts the minutes somebody spends READING, and because the timeout raises
    inside _drive_agent it never reaches the push — four messages over forty
    minutes had their edits deleted along with the workspace."""
    body = inspect.getsource(runner._run_session)
    i = body.find("_drive_agent(")
    assert i > 0
    assert "asyncio.timeout" not in body[:i], (
        "a timeout still wraps the whole conversation, and raising there "
        "skips the push below it"
    )


def test_one_turn_is_still_bounded():
    """Dropping the outer clock must not mean an agent can grind forever on a
    single message."""
    body = inspect.getsource(runner._drive_agent)
    assert "asyncio.timeout(runner.TURN_WALL_CLOCK_SECONDS)" in body.replace(
        "asyncio.timeout(TURN_WALL_CLOCK_SECONDS)",
        "asyncio.timeout(runner.TURN_WALL_CLOCK_SECONDS)")
    assert 0 < runner.TURN_WALL_CLOCK_SECONDS <= 60 * 60


def test_a_cut_off_turn_keeps_its_edits():
    """The whole point of catching it here rather than letting it raise."""
    body = inspect.getsource(runner._drive_agent)
    assert "except TimeoutError:" in body
    assert "timed_out = True" in body
    # …and the no-result guard must not then throw them away.
    assert "if not saw_result and not timed_out:" in body


def test_the_absolute_deadline_lands_between_turns():
    """Racing it against work would interrupt an edit halfway. Capping the
    idle wait puts it where nothing is in flight."""
    body = inspect.getsource(runner._drive_agent)
    assert "min(idle_seconds, remaining)" in body
    assert "deadline" in body


def test_the_mcp_token_does_not_outlive_the_session_by_much():
    """It is minted once into the CLI subprocess env and cannot be re-minted,
    so the session cap IS the credential lifetime. Raising one raises the
    other, which is easy to do without noticing."""
    body = inspect.getsource(runner._mint_mcp_token)
    assert "SESSION_WALL_CLOCK_SECONDS" in body
    assert runner.SESSION_WALL_CLOCK_SECONDS <= 4 * 60 * 60, (
        "the agent's MCP token now lives longer than a working half-day"
    )


# ─── turn budget ─────────────────────────────────────────────────────


def test_the_turn_ceiling_fits_a_conversation():
    """--max-turns is documented as conversation-scoped, not per query. At 50
    a ten-message chat with a few tool calls each walks into it, and the
    runner reports a turn-limit result as a failed session."""
    from src.agent.modes import STANDARD, WORKFLOW, get_spec

    assert get_spec(STANDARD).max_turns >= 200
    assert get_spec(WORKFLOW).max_turns >= 400


def test_running_out_of_turns_tells_the_user_what_to_do():
    body = inspect.getsource(runner._run_session)
    assert "Start a new session to carry on" in body


# ─── the routes ──────────────────────────────────────────────────────


def test_the_tenant_check_comes_before_the_liveness_check():
    """`is_running` is workspace-blind. Answering 409 before 404 would make
    this route a way to ask whether another tenant's session id is alive."""
    for handler in ("async def send_message(", "async def finish("):
        i = ROUTES.find(handler)
        assert i > 0, handler
        body = ROUTES[i:i + 2000]
        tenant = body.find("row.workspace_id != workspace_id")
        live = body.find("send_turn(" if "send_message" in handler
                         else "finish_session(")
        assert 0 < tenant < live, f"{handler} checks liveness first"


def test_a_dead_session_is_409_not_500():
    i = ROUTES.find("async def send_message(")
    body = ROUTES[i:i + 4000]
    assert "status_code=409" in body
    assert "Start a new one" in body, "409 with no next step is not actionable"


def test_a_message_to_a_paused_session_resumes_it_instead_of_refusing():
    """A paused session is not dead — it is waiting, and this message is what
    wakes it. Refusing here would make the whole resumable-conversation
    feature unreachable from the one place a person would use it."""
    i = ROUTES.find("async def send_message(")
    body = ROUTES[i:i + 4000]
    assert 'row.status == "paused"' in body
    assert "start_session(session_id, resume=True)" in body
    assert "row.resume_count" in body


def test_resuming_still_respects_one_live_session_per_workspace():
    """A paused session costs nothing, but waking it must not jump the queue
    past one that is actually running."""
    i = ROUTES.find("async def send_message(")
    body = ROUTES[i:i + 4000]
    assert "_live_session_id(session, workspace_id)" in body


def test_a_paused_session_does_not_block_the_workspace():
    """Counting `paused` in the live check would turn the feature into its own
    denial of service: one unfinished chat from last week blocking every new
    session."""
    i = ROUTES.find("async def _live_session_id(")
    body = ROUTES[i:i + 1200]
    assert '"queued", "running"' in body
    assert "paused" not in body.split('"""')[2] if '"""' in body else True


def test_an_expired_transcript_is_refused_with_a_reason():
    """The transcripts are the largest thing stored per session, so they are
    swept. Resuming against a swept one would start a cold conversation
    wearing the old one's id."""
    i = ROUTES.find("async def send_message(")
    body = ROUTES[i:i + 4000]
    assert "resumable_until" in body
    assert "retention window" in body


# ─── attachments ─────────────────────────────────────────────────────


def test_screenshots_are_accepted_alongside_text():
    """Screenshots were REFUSED, on the reasoning that the agent has Read and
    no shell so an image arrives as bytes it cannot open.

    That reasoning was wrong and unchecked. The bundled CLI handles image/png
    and image/jpeg — Read opens them — so the restriction rejected the very
    thing people reach for first: a picture of the failing dashboard. Verified
    against the binary, not assumed a second time.
    """
    from src.api.routers.claude_code import _ATTACH_SUFFIXES

    for ok in (".log", ".csv", ".json", ".md", ".diff",
               ".png", ".jpg", ".jpeg", ".webp"):
        assert ok in _ATTACH_SUFFIXES, ok
    # Still refused: nothing here can open them, and accepting a file that
    # silently does nothing is worse than saying no.
    for no in (".xlsx", ".pdf", ".zip", ".exe"):
        assert no not in _ATTACH_SUFFIXES, no


def test_an_image_gets_its_own_size_ceiling():
    """A retina screenshot of a full window is routinely 3–5 MB, and it does
    not cost context the way text does. One cap for both would have rejected
    the ordinary case while claiming to support it."""
    from src.api.routers.claude_code import (
        MAX_ATTACHMENT_BYTES,
        MAX_IMAGE_BYTES,
    )

    assert MAX_IMAGE_BYTES > MAX_ATTACHMENT_BYTES
    assert MAX_IMAGE_BYTES >= 5 * 1024 * 1024


def test_the_413_names_the_ceiling_that_was_applied():
    """Telling somebody their 6 MB screenshot exceeds a 2 MB limit, when the
    limit applied was 10, sends them to compress a file that would have gone
    through."""
    i = ROUTES.find("status_code=413")
    assert "ceiling // 1024 // 1024" in ROUTES[i:i + 400]


def test_a_rejected_type_says_why_and_what_works():
    i = ROUTES.find("async def attach(")
    body = ROUTES[i:i + 3000]
    assert "status_code=415" in body
    assert "CSV" in body
    assert "screenshots" in body, "the message still implies images are refused"
    assert "no shell to open it with" not in body, "the old, false reason"


def test_an_attachment_lands_inside_the_sandbox():
    """`_attachments/` sits under the root the PreToolUse hook already bounds
    every tool path to, so this opens no new path and needs no new
    permission."""
    i = ROUTES.find("async def attach(")
    body = ROUTES[i:i + 3000]
    assert "session_root(session_id)" in body
    assert '"_attachments"' in body
    # The name is taken apart so an upload cannot climb out of it.
    assert "Path(file.filename or" in body and ").name" in body


def test_the_agent_is_told_the_path_not_handed_the_bytes():
    """A two-megabyte log pasted into a turn is the context window gone, and
    the agent can read exactly the part it needs."""
    i = ROUTES.find("async def attach(")
    body = ROUTES[i:i + 3000]
    assert "send_turn(" in body
    assert "Read it." in body
    assert "blob.decode" not in body


def test_attachments_are_capped():
    from src.api.routers.claude_code import MAX_ATTACHMENT_BYTES

    assert 0 < MAX_ATTACHMENT_BYTES <= 10 * 1024 * 1024
    i = ROUTES.find("async def attach(")
    assert "status_code=413" in ROUTES[i:i + 3000]


def test_the_session_root_is_only_exposed_for_a_live_session():
    body = inspect.getsource(runner.session_root)
    assert "_registry.get(session_id)" in body
    assert "return None" in body


# ─── the deploy used to destroy the work ─────────────────────────────


def test_a_cancelled_session_pushes_before_its_workspace_is_deleted():
    """`task.cancel()` has exactly one caller — `shutdown_all`, i.e. the
    container going down. A user pressing Stop never reaches it: `stop_session`
    queues _FINISH and interrupts, taking the clean path that pushes.

    So this branch IS the deploy path, and it did neither: no push, then
    `cleanup_workspace` in the `finally` deleted the tree. Every deploy
    silently destroyed whatever a live session had written. The comment
    justifying it — "cancelling is something the user just did on purpose" —
    described a caller it does not have.
    """
    body = inspect.getsource(runner._run_session)
    i = body.index("except asyncio.CancelledError:")
    branch = body[i:body.index("except TimeoutError:", i)]

    # `_push_safely`, NOT `_push_terminal`. My first fix asserted the latter
    # and passed — while `_push_terminal`, despite the name, only sends a web
    # notification. It announced work that had just been deleted, and the test
    # agreed with it. The push is the thing that writes to git.
    assert "_push_safely" in branch, "the deploy path still discards the work"
    assert "commit_and_push" in inspect.getsource(runner._push_safely), (
        "_push_safely no longer reaches git"
    )
    # Shielded, or the await is cancelled again immediately and never runs.
    assert "asyncio.shield" in branch


def test_the_cancel_path_can_actually_reach_a_workspace():
    """The other half of the same mistake. `_run_session`'s own `workspace` is
    only assigned when `_drive_agent` RETURNS, and a cancellation is precisely
    the case where it does not — so the local is None and a correct push call
    would still have had nothing to push."""
    body = inspect.getsource(runner._run_session)
    i = body.index("except asyncio.CancelledError:")
    branch = body[i:body.index("except TimeoutError:", i)]
    assert '_registry.get(session_id)' in branch
    assert 'getattr(entry, "workspace", None)' in branch
    # …and the runner has to publish it while the session is still alive.
    assert "running.workspace = workspace" in inspect.getsource(runner._drive_agent)


def test_shutdown_waits_for_those_pushes():
    """Cancelling and returning is what it used to do, and it made the push
    above unreachable in practice: the loop stopped before the shielded
    coroutine could run."""
    body = inspect.getsource(runner.shutdown_all)
    assert "asyncio.wait" in body
    assert body.index("task.cancel()") < body.index("asyncio.wait")
    assert runner.SHUTDOWN_PUSH_SECONDS > 0


def test_the_container_is_given_time_to_finish_pushing():
    """Docker's default grace period is 10s. Waiting 25s inside a container
    that is SIGKILLed at 10 is a wait that only looks like one."""
    compose = (ROOT / "docker-compose.yml").read_text()
    i = compose.find("\n  api:")
    api = compose[i:compose.find("\n  web:", i)]
    assert "stop_grace_period:" in api, "the wait is decorative without this"
    grace = int(re.search(r"stop_grace_period:\s*(\d+)s", api).group(1))
    assert grace > runner.SHUTDOWN_PUSH_SECONDS, (
        f"grace {grace}s <= push wait {runner.SHUTDOWN_PUSH_SECONDS}s"
    )


# ─── coffee-break workflow ───────────────────────────────────────────


def test_a_finished_turn_is_announced_out_of_band():
    """The session page is the only place a finished turn appears, and nobody
    watches a page while an agent works — they close the tab.

    An earlier design answered that by ending the session once no SSE
    subscriber was left. That is backwards: it kills the conversation exactly
    when the person has stepped away, and stepping away is the workflow. The
    session stays; the notification travels.
    """
    body = inspect.getsource(runner._drive_agent)
    assert "_notify_turn_done(row, session_id, result_data)" in body
    # Per TURN, not per session: a parked session announces "your move" now,
    # not fifteen minutes later when the idle timeout fires.
    turn_end = body.index("_notify_turn_done")
    assert body.index("running.turns.get()") > turn_end


def test_the_announcement_uses_the_workspace_channels():
    """Same Google Chat / Slack / Discord fan-out everything else uses, and
    scoped to the workspace — a notification is a tenant-visible thing."""
    body = inspect.getsource(runner._notify_turn_done)
    assert "workspace_id=row.workspace_id" in body
    assert "from src.notifications.dispatch import notify" in body
    assert "link_url" in body, "no way back to the session from the message"


def test_a_broken_webhook_cannot_touch_the_conversation():
    body = inspect.getsource(runner._notify_turn_done)
    assert "except Exception" in body
    assert "asyncio.to_thread" in body, "a blocking POST on the event loop"


def test_nothing_is_announced_when_there_is_nothing_to_say():
    body = inspect.getsource(runner._notify_turn_done)
    assert "if not summary:" in body



# ─── attaching before the session exists ─────────────────────────────


def test_a_file_can_be_attached_before_the_session_starts():
    """The paperclip lived only inside a running session, which is the wrong
    moment: the thing people want to attach is the production log that made
    them open the session at all."""
    assert "async def stage_attachment(" in ROUTES
    assert '"/api/agent-sessions/staged-attachments"' in ROUTES


def test_staged_files_survive_the_startup_sweep():
    """`sweep_stale_workspaces()` deletes everything under agent_workspaces/ at
    startup. Staging inside it would throw away a queued session's attachments
    before it ever ran."""
    from src.agent.workspace import staging_root

    root = str(staging_root("abc"))
    assert "agent_staging" in root
    assert "agent_workspaces" not in root


def test_the_staging_id_cannot_be_a_path():
    """It is joined onto a directory. A caller-supplied id is accepted only in
    its exact minted shape."""
    i = ROUTES.find("async def stage_attachment(")
    body = ROUTES[i:i + 2500]
    assert "re.fullmatch" in body
    assert "Malformed staging id" in body


def test_staging_is_bounded():
    """A staging area with no ceiling is an upload endpoint with no ceiling,
    costing disk before any session exists to own it."""
    from src.api.routers.claude_code import MAX_STAGED_TOTAL_BYTES

    assert 0 < MAX_STAGED_TOTAL_BYTES <= 100 * 1024 * 1024
    i = ROUTES.find("async def stage_attachment(")
    # The whole handler, not a guessed window — the docstring alone is most of
    # 2 KB, and a fixed slice tests where the comment ends rather than what the
    # code does.
    body = ROUTES[i:ROUTES.find("\n@router", i) if ROUTES.find("\n@router", i) > 0
                  else len(ROUTES)]
    assert "MAX_STAGED_TOTAL_BYTES" in body


def test_staged_files_are_moved_in_and_named_in_the_prompt():
    """An attachment the model is not told about is a file sitting in a
    directory it has no reason to list."""
    body = inspect.getsource(runner._drive_agent)
    assert "adopt_staged" in body
    assert "Files attached with this request:" in body
