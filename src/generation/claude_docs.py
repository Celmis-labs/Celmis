"""Documentation written by an agent that can only look things up.

The api engine gets one prompt with the code already assembled and answers in
one turn. That is the right shape for review — a diff is a bounded input and
the answer is a fixed JSON — and the wrong shape for a module PRD, which is an
unbounded question. To write one honestly you follow the imports, look at who
calls the thing, check what the tests assert, and only then write. That is a
loop, and a single-shot call cannot do it.

WHAT THIS AGENT CAN AND CANNOT DO

It gets the Celmis MCP tools and nothing else. Bash, Read, Write, Edit, Grep
and Glob are all denied, so it cannot open a file, cannot run a command, and
cannot wander outside the indexed repositories. Everything it learns comes from
a query against the graph Celmis already built: which symbols exist, who
consumes them, what the public surface is, what is deprecated, who owns it.

That restriction is the feature, not a safety afterthought. Documentation is
the surface where a model has the most room to invent, because a PRD is
interpretation by nature — there is no `evidence_kind` that separates a proven
claim from a plausible-sounding one, the way there is for a lock-file drift.
The defence is not a sterner prompt. It is leaving the model no way to answer
except by looking something up, and recording what it looked at.

WHAT IT COSTS

A vault build over a large repository is dozens of documents. On the api engine
that is dozens of billed calls; on a subscription token it is flat. For one
pull request the difference is noise. For fifty documents it is the reason the
choice exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from src.generation.engines import ENGINE_CLAUDE_CODE, DocResult, compose_prompt

logger = logging.getLogger(__name__)

#: Enough turns to follow a dependency chain and come back, not enough to
#: wander. Review uses a similar ceiling for the same reason.
_MAX_TURNS = 24


def _session_timeout() -> float:
    """The whole session's deadline, from the settings rather than from here.

    A number without a name is one nobody can raise on the day a monorepo's
    biggest module needs the turns."""
    from src.config import get_settings
    return float(get_settings().generation_agent_timeout_seconds)


_RESEARCH_BRIEF = """\
You are writing documentation for a codebase you can inspect but not open.

You have the mcp__celmis__* tools and nothing else — no shell, no file reads,
no grep. Use them: search_symbols to find what exists, get_api_surface for the
public shape, find_consumers for who depends on it, get_architecture for how
the pieces fit, list_deprecations for what is on the way out.

Rules that matter more than style:
- Every statement about behaviour must come from something you looked up. If
  you did not check it, do not write it.
- Where you could not determine something, write that you could not. "Not
  determined from the code" is a useful sentence; a confident guess is not.
- Never infer behaviour from a name. `validateOrder` may not validate.
- Cite the file and line you learned it from wherever the tools give you one.

Produce the document and nothing else — no preamble, no "here is the
documentation", no closing summary.
"""


def claude_docs_available(workspace_id: str = "default",
                          user_id: str | None = None) -> tuple[bool, str]:
    """Can this workspace run the agent engine, and if not, why not.

    Returns a reason rather than a bare False so the caller can say something
    useful. A vault build that silently produced api-engine documents when the
    user picked the agent would be the worst of both.
    """
    try:
        from claude_agent_sdk import ClaudeSDKClient  # noqa: F401
    except ImportError:
        return False, "claude_agent_sdk is not installed"

    if _auth_env(workspace_id, user_id) is None:
        return False, ("no Claude subscription connected and no Anthropic API "
                       "key for this workspace")
    return True, ""


def _auth_env(workspace_id: str, user_id: str | None = None) -> dict | None:
    """Subscription token first, workspace Anthropic key as fallback.

    Same order as the review engine — a workspace that connected Claude Code
    once should not have to connect it again for documentation.
    """
    if user_id:
        try:
            from src.agent.connection import resolve_connection

            conn = resolve_connection(user_id, workspace_id)
            if conn is not None:
                return dict(conn.env)
        except Exception as exc:  # noqa: BLE001
            logger.debug("claude_docs_connection_failed err=%s", exc)
    try:
        from src.llm.keys import resolve_api_key

        return {"ANTHROPIC_API_KEY": resolve_api_key(
            "anthropic", workspace_id=workspace_id)}
    except Exception:  # noqa: BLE001
        return None


class ClaudeDocsEngine:
    """One document per agent session."""

    name = ENGINE_CLAUDE_CODE

    def __init__(self, workspace_id: str = "default",
                 user_id: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.user_id = user_id

    def generate(
        self, *, prompt: str, system_instruction: str,
        code_context: str | None, metadata_context: dict[str, Any] | None,
        operation: str, repo: str | None, module: str | None = None,
        temperature: float | None = None, max_output_tokens: int | None = None,
    ) -> DocResult:
        # The code still travels redacted. The agent can look more up on its
        # own, but what we hand it goes through the same control the api engine
        # uses — an engine choice must not be a way around redaction.
        full_prompt, _stats = compose_prompt(
            prompt, code_context, metadata_context, operation=operation)

        task = (
            f"{_RESEARCH_BRIEF}\n\n"
            f"Repository: {repo or 'unknown'}"
            + (f"\nModule: {module}" if module else "")
            + f"\n\n{full_prompt}"
        )
        # Ask whether a loop is running rather than catching the RuntimeError
        # that says so. `except RuntimeError` around the whole session caught
        # every failure the session itself raises — an empty document, a
        # refused run, missing auth — and re-ran the entire agent to fail the
        # same way twice, at twice the cost and twice the wall clock.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop here: this is the CLI or a sync caller.
            return asyncio.run(self._run(task, system_instruction, operation, repo))

        # Inside a loop already — the queue worker is async — so the session
        # gets its own thread with its own loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                self._run(task, system_instruction, operation, repo),
            ).result()

    async def _run(self, task: str, system_instruction: str,
                   operation: str, repo: str | None) -> DocResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )

        from src.agent.runner import _mint_mcp_token

        auth = _auth_env(self.workspace_id, self.user_id)
        if auth is None:
            raise RuntimeError("no Claude auth for this workspace")

        api_port = os.environ.get("PORT", "8000")
        text_parts: list[str] = []
        tools_used: list[str] = []

        with tempfile.TemporaryDirectory(prefix="claude-docs-") as home:
            options = ClaudeAgentOptions(
                cwd=home,
                env={
                    "HOME": home,
                    "CLAUDE_CONFIG_DIR": f"{home}/.claude",
                    "DISABLE_TELEMETRY": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    **auth,
                },
                system_prompt=system_instruction,
                allowed_tools=["mcp__celmis__*", "mcp__celmis"],
                # The same denial list the review engine uses, for the same two
                # reasons: the agent must research through the index rather
                # than the filesystem, and Agent/Task would spawn a BACKGROUND
                # subagent whose answer arrives in a turn this loop never
                # reads — the document would end at "let me look into that".
                disallowed_tools=["Bash", "Read", "Write", "Edit", "Grep",
                                  "Glob", "WebFetch", "WebSearch", "Agent",
                                  "Task"],
                permission_mode="acceptEdits",
                max_turns=_MAX_TURNS,
                mcp_servers={
                    "celmis": {
                        "type": "http",
                        # Trailing slash: without it Starlette answers 307, and
                        # a redirected POST is not something the MCP
                        # streamable-HTTP client is guaranteed to follow.
                        "url": f"http://localhost:{api_port}/mcp/",
                        "headers": {
                            "Authorization":
                                f"Bearer {_mint_mcp_token(self.user_id or 'system')}",
                        },
                    },
                },
            )
            # A DEADLINE ON THE WHOLE SESSION. There was none: the loop below
            # waits for the next message, and a session that stops producing
            # them waits forever. `max_turns` does not help — turns are only
            # counted when they arrive.
            #
            # It matters more since the job lease began renewing itself. A
            # hung build used to lose its lease after ten minutes and a
            # sibling worker took the row; now the heartbeat keeps saying "a
            # worker is alive on this", which is true, and is why the row
            # would never come back — the worker is alive, waiting on a
            # session that will not answer, and the slot is gone until the
            # container restarts.
            #
            # `TimeoutError` propagates: the generator above records the
            # module as failed and the build carries on with the next one,
            # which is what every other failure on this path already does.
            async with asyncio.timeout(_session_timeout()), \
                    ClaudeSDKClient(options=options) as client:
                await client.query(task)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                            else:
                                name = getattr(block, "name", "")
                                if name:
                                    tools_used.append(name)
                    elif isinstance(message, ResultMessage):
                        # is_error is the SDK saying the session failed — a
                        # refusal, a tool loop that ran out of turns, an auth
                        # problem. Without this the loop simply stopped and the
                        # caller got text="" as a normal result, which the vault
                        # writer then stored as a finished document.
                        if getattr(message, "is_error", False):
                            raise RuntimeError(
                                "claude agent session failed: "
                                + str(getattr(message, "result", "") or
                                      "no detail from the SDK")[:300])
                        break

        text = "".join(text_parts).strip()
        if not text:
            # An empty document is not a document. Returning it lets the writer
            # store a note that looks generated, and resume-mode then pins that
            # emptiness across every later commit because the hash matches.
            raise RuntimeError(
                "claude agent produced no text (tools called: "
                f"{len(tools_used)})")
        logger.info("claude_docs_done op=%s repo=%s tools=%d chars=%d",
                    operation, repo, len(tools_used), len(text))
        return DocResult(
            text=text,
            engine=ENGINE_CLAUDE_CODE,
            # Kept because it is the evidence the agent looked things up. A
            # document produced with zero tool calls was written from the
            # prompt alone, which is exactly what this engine exists to avoid.
            tools_used=tools_used,
        )


__all__ = ["ClaudeDocsEngine", "claude_docs_available"]
