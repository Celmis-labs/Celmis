"""The sandbox has to be reachable BY THE AGENT, not only by tests.

For several commits it was not. The container existed, the per-uid isolation
worked and was verified against a live box, `src/agent/sandbox.py` spoke to it —
and nothing in `src/` ever called any of it. A grep for `sandbox.run(` outside
the module itself came back empty; the only importers were tests. A hardened
door in no wall.

The cause was one line away the whole time: `_DISALLOWED_TOOLS` denies Bash,
and no other execution tool existed, so the agent had no way to ask for
anything to be run. Every session was told, in its own system prompt, "do not
attempt to run shell commands".

So the wiring is what is pinned here — the tool exists, it is granted, it is
registered, the model is told about it, and Bash stays denied. Each of those
five can be undone on its own and four of them fail silently.
"""

from __future__ import annotations

import ast
import inspect
import io
import textwrap
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SRC = (ROOT / "src" / "agent" / "runner.py").read_text()
# The push deploy is gone — it kept a root key to production in the secrets of
# a now-public repository. Its work is in this script, which needs no
# credential anyone off that machine holds, so these guards point here.
DEPLOY = (ROOT / "scripts" / "deploy-on-server.sh").read_text()
DOCKERFILE = (ROOT / "Dockerfile.sandbox").read_text()


def _code(obj) -> str:
    """Source with comments and docstrings blanked, positions preserved.

    The comments in `runner.py` name Bash and the sandbox repeatedly in order
    to explain the design, so a plain grep proves nothing either way.
    """
    source = textwrap.dedent(inspect.getsource(obj))
    lines = source.splitlines(keepends=True)
    spans = [(t.start, t.end)
             for t in tokenize.generate_tokens(io.StringIO(source).readline)
             if t.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append(((first.lineno, first.col_offset),
                          (first.end_lineno, first.end_col_offset)))
    for (srow, scol), (erow, ecol) in spans:
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line.rstrip("\n"))
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "".join(lines)


# ─── the five links ──────────────────────────────────────────────────


def test_the_tool_exists():
    from src.agent.exec_tool import build_exec_server

    assert callable(build_exec_server)


def test_it_is_on_the_allow_list():
    from src.agent.runner import _ALLOWED_TOOLS

    assert "mcp__exec__run" in _ALLOWED_TOOLS


def test_it_is_registered_with_the_sdk():
    from src.agent import runner

    body = _code(runner._build_options)
    assert '"exec": build_exec_server(' in body


def test_the_model_is_told_it_can_run_things():
    """A tool nobody mentions is a tool nobody uses — and every session before
    this one was told the exact opposite in the same paragraph."""
    from src.agent import runner

    prompt = _code(runner._build_options)
    assert "mcp__exec__run" in prompt
    assert "Do not attempt to run shell commands" not in prompt, (
        "the old instruction is still there, and it contradicts the tool"
    )


def test_bash_is_still_denied():
    """Not an oversight — the design. Bash would run in the api container,
    beside /workspace/data/secrets/ and every tenant's git tokens, where `cat`
    is a cross-tenant compromise. The agent gets execution; it does not get it
    here."""
    from src.agent.runner import _DISALLOWED_TOOLS

    assert "Bash" in _DISALLOWED_TOOLS


# ─── what the tool actually does ─────────────────────────────────────


def test_the_workspace_is_closed_over_not_named_by_the_model():
    """A directory the model passes is a directory the model can get wrong.
    `build_exec_server` binds one session's root and the tool takes only a
    command."""
    from src.agent.exec_tool import build_exec_server

    params = list(inspect.signature(build_exec_server).parameters)
    # Both are supplied by the runner and closed over. What matters is that
    # neither is reachable from the model — the TOOL SCHEMA is the model's
    # whole vocabulary, and it names no path.
    assert params == ["workspace_root", "branch_name", "session_id"]
    body = _code(build_exec_server)
    # The braces AROUND the schema, not the last pair in the function — the
    # last one belongs to an f-string in the error path.
    i = body.index('"command": str')
    schema = body[body.rindex("{", 0, i):body.index("}", i) + 1]
    assert "path" not in schema.lower(), "the schema takes a path from the model"
    assert "workspace" not in schema.lower()


def test_it_runs_off_the_event_loop():
    """Packing a large checkout and waiting for a test suite are both blocking.
    On the loop they would stall the heartbeat and the SSE stream that the
    session page is reading."""
    from src.agent.exec_tool import build_exec_server

    assert "asyncio.to_thread" in _code(build_exec_server)


def test_a_missing_sandbox_tells_the_model_to_stop_trying():
    """Not an exception: a deployment without a sandbox keeps working with
    execution unavailable, and the model needs to know not to retry."""
    from src.agent.exec_tool import build_exec_server

    body = _code(build_exec_server)
    assert "if not sandbox.SANDBOX_TOKEN:" in body
    assert "No sandbox is configured" in body
    assert "could not perform" in body


def test_the_reply_is_bounded():
    """The sandbox clips for its own memory; this clips for the context
    window, which is a different budget."""
    from src.agent.exec_tool import MAX_REPLY_CHARS, build_exec_server

    assert 0 < MAX_REPLY_CHARS <= 32000
    assert "limit=MAX_REPLY_CHARS" in _code(build_exec_server)


def test_the_timeout_cannot_be_raised_past_the_sandbox_ceiling():
    from src.agent.exec_tool import MAX_TIMEOUT_SECONDS, build_exec_server

    assert MAX_TIMEOUT_SECONDS <= 900
    assert "min(timeout, MAX_TIMEOUT_SECONDS)" in _code(build_exec_server)


def test_the_description_says_edits_are_discarded():
    """The two surprises that would each cost a turn: "my edits are not there"
    and "whatever I wrote vanished"."""
    from src.agent.exec_tool import build_exec_server

    body = _code(build_exec_server)
    assert "DISCARDED" in body
    assert "edits you have already made" in body


# ─── it has to reach the server ──────────────────────────────────────


def test_the_deploy_fetches_the_sandbox_image():
    """The sandbox has to be named explicitly, whatever the deploy does to get
    images.

    `up -d` builds an image that does not exist and does NOT rebuild one that
    does — so the first deploy shipped a sandbox and every deploy after it kept
    that same image, silently, and changes to server.py never arrived. That was
    the original bug, and `$COMPOSE build sandbox` was the answer.

    The deploy no longer builds anything: images come from the registry, built
    once per tag. The hazard survives the change unaltered, because `up -d`
    will equally reuse an image whose tag it already has locally. Only the verb
    moved, from build to pull.
    """
    # Executable lines only. Picking the first line that MENTIONS the command
    # found a comment explaining the pull instead of the pull — the same
    # mistake this file's neighbours were just corrected for.
    lines = [
        line for line in DEPLOY.splitlines()
        if "$COMPOSE pull" in line and not line.strip().startswith("#")
    ]
    assert lines, "the deploy neither builds nor pulls"
    pull_line = lines[0]

    assert "sandbox" in pull_line, (
        "the sandbox is not named, so `up -d` will keep whatever is on disk"
    )
    assert "$COMPOSE build" not in DEPLOY, (
        "the production server is building images again"
    )
    # The one-at-a-time build with `docker builder prune -af` between images
    # used to be pinned here, on the reasoning that the box runs out of room
    # when two builds peak together. That reasoning was right and the remedy
    # was not: measured on the server, `prune -af` reclaims 0B there — with the
    # containerd image store the 11.6GB it reports is image layers, not build
    # cache — so the command ran three times a deploy and did nothing, while
    # costing every build its cache. An unchanged rebuild of `api` still took
    # 485 seconds and 4.2GB of a filesystem with 4.2GB free.
    #
    # Nothing pins it now because nothing builds there.
    runs = "\n".join(
        line for line in DEPLOY.splitlines() if not line.lstrip().startswith("#")
    )
    assert "docker builder prune -af" not in runs, (
        "a command measured to reclaim 0B on this server is back"
    )


def test_the_sandbox_carries_the_tools_a_failure_is_investigated_with():
    """A test fails and the next thing anyone does is search the tree or pull
    a field out of a JSON report. Without ripgrep the agent greps through
    node_modules and burns the turn."""
    for tool in ("ripgrep", "jq", "git", "make", "procps"):
        assert tool in DOCKERFILE, tool


def test_fd_is_reachable_by_its_usual_name():
    """Debian ships it as `fdfind`, and every instinct anybody has types `fd`."""
    assert "ln -sf" in DOCKERFILE and "/usr/local/bin/fd" in DOCKERFILE


def test_the_model_is_told_what_git_can_and_cannot_do_there():
    """The tenant's `.git` is still not shipped — it carries the remote URL,
    and a credentialed one would carry a token — but its absence used to break
    `git status`, `git diff` and pre-commit for a reason nothing to do with the
    code under test. A fresh repository is created in the sandbox instead.

    So the description has to be precise in BOTH directions. "git is broken"
    is now false and would stop the agent using tools that work; "git is fine"
    is also false, because there are no tags and `git describe` still cannot
    work. Either half missing costs a turn and usually a wrong conclusion.
    """
    from src.agent.exec_tool import build_exec_server

    body = _code(build_exec_server)
    assert "GIT WORKS" in body
    assert "no earlier commits" in body or "no history" in body.lower()
    assert "NOT A GIT REPOSITORY" not in body, "the old, now-false warning"

    # The two limitations fail DIFFERENTLY, and that was measured rather than
    # assumed. Without any repository setuptools_scm raised LookupError and
    # the build stopped; with the sandbox's fresh repo it returns
    # 0.1.dev1+g<sha> instead — so the build now SUCCEEDS with a wrong
    # version, and only a test asserting the version number notices. A quieter
    # trap than the old one, and worth naming precisely.
    assert "git describe" in body, "the hard failure is unstated"
    assert "setuptools_scm does NOT fail" in body, (
        "the quiet one is described as if it were the loud one"
    )
    assert "0.1.dev1" in body


def test_a_one_shot_caller_is_told_to_finish_rather_than_wait():
    """A session used to reach `done` when the agent stopped talking. It now
    parks for the idle timeout waiting for the next human turn, so a script
    that polls for `done` waits fifteen minutes for a push that used to take
    seconds. Measured on the deployed box; /finish takes it to `done` in about
    fifteen seconds."""
    from src.agent import runner

    src = inspect.getsource(runner)
    assert "POST /finish" in src
    assert "parks" in src.lower()
