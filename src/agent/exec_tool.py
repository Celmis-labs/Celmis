"""The agent's way to run something — the door into the sandbox.

Everything else was already here. The sandbox container runs a tenant's own
build and test commands away from the api's Fernet key and credential store,
each job under its own uid so concurrent tenants cannot read each other, and
`src/agent/sandbox.py` speaks to it. What was missing was any way for the AGENT
to ask: it has Read, Edit and Write and no execution tool at all, so the whole
thing was reachable only from tests. A hardened door in no wall.

This is the wall. One in-process MCP tool, `mcp__exec__run`, which:

  * packs the session's CURRENT workspace — the agent's edits included, because
    the point is "does the change I just made pass" —
  * posts it to the sandbox with the command,
  * hands back the exit code and clipped output as text.

`Bash` stays denied, and that is the whole design rather than an oversight.
Bash would run in the api container, next to `/workspace/data/secrets/`, where
`cat` is a cross-tenant credential compromise. The agent gets execution and
does not get it HERE.

The copy is one-way. Whatever the command writes stays in the sandbox and is
thrown away; edits are made by the agent's own tools in the api workspace,
where they can be reviewed and pushed. So "run the tests" answers a question,
it does not smuggle a change back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Longer than a unit test suite and shorter than the turn clock, so a command
#: that hangs is reported as a hang rather than taking the turn down with it.
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900

#: What comes back to the model. The sandbox clips its own output first; this
#: is the second ceiling, on the context window rather than on memory.
MAX_REPLY_CHARS = 12000


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def build_exec_server(workspace_root: Path, branch_name: str = "work",
                      session_id: str = ""):
    """An MCP server exposing `run`, bound to one session's workspace.

    Bound, not parameterised: the directory is closed over rather than taken as
    an argument, so there is no path for the model to name and therefore no
    path for it to name the wrong one.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "run",
        # The description is the whole interface — it is what the model reads
        # when deciding whether this is the tool it wants. Say where it runs,
        # since "my edits are not there" and "the network is missing" are the
        # two surprises that would otherwise cost a turn each.
        "Run a shell command against a COPY of the current working tree, in an "
        "isolated sandbox container. Use it to run tests, linters, builds and "
        "type checks — this is the only way to execute anything.\n\n"
        "YOU ARE ALREADY IN THE REPOSITORY ROOT. Do not cd anywhere first — "
        "run `pytest`, not `cd /some/path && pytest`. The absolute paths your "
        "Read and Edit tools use do NOT exist here: the sandbox unpacks the "
        "tree at its own location, so a command starting with `cd "
        "/workspace/...` fails with 'No such file or directory' and costs you "
        "a turn. Use paths relative to the repository root.\n\n"
        "The copy includes edits you have already made. It has bash, python, "
        "node/npm, ripgrep, jq and outbound internet, so installing "
        "dependencies works. Anything the command writes is DISCARDED — make "
        "changes with Edit/Write, then run this to check them. Returns the "
        "exit code with stdout and stderr.\n\n"
        "GIT WORKS, WITH NO HISTORY. The tenant's own .git is not shipped — it "
        "holds the remote URL, and a credentialed one would carry a token — so "
        "a fresh repository is created there instead: one commit, on the real "
        "branch name, no remote. `git status`, `git diff`, `git rev-parse` and "
        "pre-commit all work, and `git diff` shows what YOUR COMMAND changed, "
        "which is what a formatter or codegen check is usually asking — the "
        "`tool --write . && git diff --exit-code` idiom works. Note `git diff` "
        "does NOT show your own earlier edits: they are already in that "
        "baseline commit.\n\n"
        "There are no tags and no earlier commits, and the two consequences "
        "differ. `git describe` fails outright ('No names found'). "
        "setuptools_scm does NOT fail — it returns a placeholder like "
        "0.1.dev1+g<sha>, so a build succeeds with a wrong version and only a "
        "test that asserts the version number notices. If you see a version "
        "assertion fail here, that is this, not your change. Pushing happens "
        "outside the sandbox and is handled for you.\n\n"
        "A TEST RUNNER MAY NOT BE INSTALLED. `python -m pytest` can answer 'No "
        "module named pytest' even when the project's tests are written for "
        "it — the image ships an interpreter, not the project's dev "
        "dependencies. Install and run in ONE call ("
        "`pip install pytest -q && python -m pytest -q`) rather than "
        "discovering it over two.\n\n"
        "Installing dependencies is fine and gets a longer clock, but it costs "
        "more of the shared sandbox, so combine steps in one call "
        "(`pip install -r requirements.txt && pytest`) instead of making the "
        "install its own round trip.",
        {
            "command": str,
            "timeout": int,
        },
    )
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        from src.agent import sandbox

        command = str(args.get("command") or "").strip()
        if not command:
            return _text("No command given.")
        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        timeout = max(1, min(timeout, MAX_TIMEOUT_SECONDS))

        if not sandbox.SANDBOX_TOKEN:
            # Deliberately not an exception: a deployment without a sandbox
            # keeps working with execution unavailable, and the model needs to
            # know to stop trying rather than to retry the same call.
            return _text(
                "No sandbox is configured on this deployment, so commands "
                "cannot be run. Continue without executing anything, and say "
                "in your summary which checks you could not perform."
            )

        logger.info("agent_exec cmd=%s timeout=%ss", command[:120], timeout)
        # Packing and posting are blocking and can take tens of seconds on a
        # large checkout; off the event loop so heartbeats and the SSE stream
        # keep flowing while a test suite runs.
        started = time.monotonic()
        result = await asyncio.to_thread(
            sandbox.run, workspace_root, command,
            timeout=timeout, branch=branch_name or "work")

        # Recorded whether it passed or failed. A commit that lists only the
        # green runs is a worse record than one that lists none: it reads as
        # proof while hiding the attempt that did not work.
        if session_id:
            from src.agent.runner import record_exec
            record_exec(session_id, command, result.exit_code,
                        ok=result.succeeded, elapsed=time.monotonic() - started)

        if not result.ok:
            return _text(f"The command could not be run: {result.error}")
        return _text(result.as_text(limit=MAX_REPLY_CHARS))

    return create_sdk_mcp_server(name="exec", version="1.0.0", tools=[run])


__all__ = ["build_exec_server", "DEFAULT_TIMEOUT_SECONDS", "MAX_REPLY_CHARS"]
