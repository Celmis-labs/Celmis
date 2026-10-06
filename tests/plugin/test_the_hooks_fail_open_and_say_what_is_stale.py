"""The plugin hooks: output shape, the dirty-guard, injection safety, fail-open.

Real git, real subprocesses where the contract is about the process (exit 0,
nothing on stdout), in-process calls where it is about the logic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.plugin import contract as C

_spec = importlib.util.spec_from_file_location("celmis_hook_under_test", C.HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

PLUGIN_TOOL = C.PLUGIN_PREFIX + "find"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A clone of acme/shop with two committed files; returns (path, indexed sha)."""
    repo = tmp_path / "shop"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/shop.git")
    (repo / "app").mkdir()
    (repo / "app" / "orders.py").write_text("def create_order():\n    pass\n")
    (repo / "app" / "users.py").write_text("def get_user():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _answer(sha: str, hits: list[str], slug: str = "github_acme-shop",
            state: str = "fresh") -> str:
    lines = [f"idx: {slug} develop@{sha[:8]} 2h {state}"]
    lines += [f"{slug} {h} function x()" for h in hits]
    return "\n".join(lines)


def _post(cwd: Path, text, tool: str = PLUGIN_TOOL) -> str:
    event = {"tool_name": tool, "cwd": str(cwd), "tool_input": {},
             "tool_response": [{"type": "text", "text": text}] if isinstance(text, str) else text}
    return hook.run("postmcp", json.dumps(event))


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


# ─── the dirty-guard ─────────────────────────────────────────────────


def test_an_unchanged_checkout_produces_no_output(checkout) -> None:
    repo, sha = checkout
    assert _post(repo, _answer(sha, ["app/orders.py:1-2"])) == ""


def test_a_locally_modified_hit_file_is_reported_stale(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("def create_order():\n    return 1\n")
    out = _post(repo, _answer(sha, ["app/orders.py:1-2", "app/users.py:1-2"]))
    ctx = _context(out)
    assert ctx == "[stale-locally] app/orders.py - read locally"
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_a_staged_or_committed_change_after_the_indexed_sha_counts_too(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "users.py").write_text("def get_user():\n    return 2\n")
    _git(repo, "commit", "-q", "-am", "later")
    assert "app/users.py" in _context(_post(repo, _answer(sha, ["app/users.py:1"])))


def test_a_hit_file_that_exists_only_locally_is_stale(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "new.py").write_text("x = 1\n")
    assert "app/new.py" in _context(_post(repo, _answer(sha, ["app/new.py:1"])))


def test_an_index_sha_missing_from_the_clone_is_said_plainly(checkout) -> None:
    repo, _ = checkout
    out = _post(repo, _answer("deadbeefcafe", ["app/orders.py:1"]))
    assert "deadbeef is not in your clone" in _context(out)


def test_an_answer_about_another_repository_is_left_alone(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    assert _post(repo, _answer(sha, ["app/orders.py:1"], slug="github_acme-billing")) == ""


@pytest.mark.parametrize("slug", ["acme/shop", "acme-shop", "github_acme-shop", "GitHub_Acme-Shop"])
def test_every_spelling_of_the_slug_matches_the_local_remote(checkout, slug) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    assert "app/orders.py" in _context(_post(repo, _answer(sha, ["app/orders.py:1"], slug=slug)))


@pytest.mark.parametrize("remote", ["git@github.com:acme/shop.git",
                                    "ssh://git@github.com/acme/shop",
                                    "https://user@bitbucket.org/acme/shop.git"])
def test_ssh_and_https_remotes_resolve_to_the_same_repository(checkout, remote) -> None:
    repo, sha = checkout
    _git(repo, "remote", "set-url", "origin", remote)
    (repo / "app" / "orders.py").write_text("changed\n")
    assert "app/orders.py" in _context(_post(repo, _answer(sha, ["app/orders.py:1"],
                                                           slug="acme/shop")))


def test_a_state_of_stale_on_the_idx_line_does_not_stop_the_comparison(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    assert _post(repo, _answer(sha, ["app/orders.py:1"], state="STALE")) != ""


def test_howto_slices_are_checked_like_hits(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    text = (f"idx: github_acme-shop develop@{sha[:8]} 2h fresh\n"
            "howto db github_acme-shop\n1) app/orders.py:1-2 create_order\n")
    assert "app/orders.py" in _context(_post(repo, text))


@pytest.mark.parametrize("shape", ["list", "content", "string", "nested"])
def test_every_tool_response_envelope_is_understood(checkout, shape) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    text = _answer(sha, ["app/orders.py:1"])
    response = {"list": [{"type": "text", "text": text}],
                "content": {"content": [{"type": "text", "text": text}]},
                "string": text,
                "nested": {"result": {"content": [{"type": "text", "text": text}]}}}[shape]
    assert "app/orders.py" in _context(_post(repo, response))


def test_other_tools_and_non_celmis_servers_are_ignored(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    text = _answer(sha, ["app/orders.py:1"])
    assert _post(repo, text, tool="Grep") == ""
    assert _post(repo, text, tool="mcp__gitnexus__query") == ""
    assert _post(repo, text, tool="mcp__celmis__create_review") == ""


def test_the_manual_mcp_server_name_works_too(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    out = _post(repo, _answer(sha, ["app/orders.py:1"]), tool="mcp__celmis__find")
    assert "app/orders.py" in _context(out)


# ─── injection safety ────────────────────────────────────────────────


def test_shell_metacharacters_in_paths_are_passed_literally(checkout, tmp_path) -> None:
    repo, sha = checkout
    nasty = "a;b$(touch${IFS}PWNED)`id`.py"
    (repo / nasty).write_text("x\n")
    marker = repo / "PWNED"
    out = _post(repo, _answer(sha, [f"{nasty}:1", "$(touch${IFS}PWNED):3", "x;touch${IFS}PWNED:2"]))
    assert not marker.exists()
    assert not (Path.cwd() / "PWNED").exists()
    assert _context(out) == f"[stale-locally] {nasty} - read locally"


def test_glob_and_magic_pathspecs_in_a_hit_do_not_expand(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    for path in ("*.py", "app/*.py", ":(glob)**/*.py", ":/", "app/"):
        assert _post(repo, _answer(sha, [f"{path}:1"])) == "", path


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "-rf", "~/x.py", "a/../../b"])
def test_unsafe_relative_paths_are_never_given_to_git(checkout, path) -> None:
    repo, sha = checkout
    assert hook._safe_relpath(path) is False
    assert _post(repo, _answer(sha, [f"{path}:1"])) == ""


def test_a_hostile_idx_line_cannot_smuggle_arguments(checkout) -> None:
    repo, _ = checkout
    for sha in ("--output=/tmp/x", "HEAD;id", "not-hex-at-all"):
        text = f"idx: github_acme-shop develop@{sha} 2h fresh\ngithub_acme-shop app/orders.py:1"
        assert _post(repo, text) == ""


# ─── fail-open ───────────────────────────────────────────────────────


def _run_process(sub: str, stdin: str, cwd: Path, env_extra: dict | None = None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-I", str(C.HOOK), sub], input=stdin,
                          capture_output=True, text=True, cwd=cwd, env=env, timeout=20)


@pytest.mark.parametrize("sub", ["session", "pregrep", "postmcp", "nonsense", ""])
@pytest.mark.parametrize("stdin", ["", "not json", "[1,2,3]", "null", '{"tool_name": 5}',
                                   "\x00\xff garbage", '{"cwd": "/does/not/exist"}'])
def test_garbage_on_stdin_exits_zero(sub, stdin, tmp_path) -> None:
    done = _run_process(sub, stdin, tmp_path)
    assert done.returncode == 0
    if sub in {"pregrep", "postmcp", "nonsense", ""}:
        assert done.stdout == ""


def test_without_git_on_the_path_every_hook_still_exits_zero(checkout, tmp_path) -> None:
    repo, sha = checkout
    event = json.dumps({"tool_name": PLUGIN_TOOL, "cwd": str(repo),
                        "tool_response": _answer(sha, ["app/orders.py:1"])})
    done = _run_process("postmcp", event, repo, {"PATH": str(tmp_path)})
    assert done.returncode == 0 and done.stdout == ""


def test_a_failing_git_is_swallowed(checkout, monkeypatch) -> None:
    repo, sha = checkout

    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(hook.subprocess, "run", boom)
    assert _post(repo, _answer(sha, ["app/orders.py:1"])) == ""
    assert hook.run("session", json.dumps({"cwd": str(repo)})) != ""


def test_an_exception_inside_a_command_is_swallowed(monkeypatch) -> None:
    monkeypatch.setitem(hook.COMMANDS, "postmcp", lambda e: 1 / 0)
    assert hook.run("postmcp", "{}") == ""


def test_the_process_prints_the_json_when_there_is_something_to_say(checkout) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    event = json.dumps({"tool_name": PLUGIN_TOOL, "cwd": str(repo),
                        "tool_response": _answer(sha, ["app/orders.py:1"])})
    done = _run_process("postmcp", event, repo)
    assert done.returncode == 0
    assert "[stale-locally] app/orders.py" in json.loads(done.stdout)["hookSpecificOutput"][
        "additionalContext"]


# ─── session start and the grep nudge ────────────────────────────────

_CONFIGURED = {"CELMIS_URL": "https://celmis.example.com", "CELMIS_TOKEN": "x" * 24}


def test_session_start_prints_at_most_three_lines(checkout, monkeypatch) -> None:
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    for k, v in _CONFIGURED.items():
        monkeypatch.setenv(k, v)
    ctx = _context(hook.run("session", json.dumps({"cwd": str(repo)})))
    lines = ctx.splitlines()
    assert 2 <= len(lines) <= 3
    assert f"develop@{sha[:8]}" in ctx and "1 tracked file(s) modified" in ctx
    assert "github_acme-shop" in ctx or "acme/shop" in ctx


def test_session_start_tells_an_unconfigured_user_how_to_configure(checkout, monkeypatch) -> None:
    repo, _ = checkout
    monkeypatch.delenv("CELMIS_URL", raising=False)
    monkeypatch.delenv("CELMIS_TOKEN", raising=False)
    ctx = _context(hook.run("session", json.dumps({"cwd": str(repo)})))
    assert "CELMIS_URL" in ctx and "CELMIS_TOKEN" in ctx and len(ctx.splitlines()) <= 3


def test_session_start_outside_a_repository_still_answers(tmp_path, monkeypatch) -> None:
    for k, v in _CONFIGURED.items():
        monkeypatch.setenv(k, v)
    ctx = _context(hook.run("session", json.dumps({"cwd": str(tmp_path)})))
    assert len(ctx.splitlines()) == 1


def _pre(pattern: str, session: str) -> str:
    return hook.run("pregrep", json.dumps({"tool_name": "Grep", "session_id": session,
                                           "tool_input": {"pattern": pattern}}))


def test_the_grep_nudge_fires_once_per_session_and_never_denies(monkeypatch) -> None:
    for k, v in _CONFIGURED.items():
        monkeypatch.setenv(k, v)
    session = uuid.uuid4().hex
    first = json.loads(_pre("OrderService", session))
    assert first["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in first["hookSpecificOutput"]
    assert "decision" not in first
    assert _pre("create_order", session) == ""
    assert _pre("create_order", uuid.uuid4().hex) != ""


def test_a_symlink_planted_at_the_nudge_marker_never_truncates_its_target(tmp_path, monkeypatch) -> None:
    import tempfile

    for k, v in _CONFIGURED.items():
        monkeypatch.setenv(k, v)
    session = uuid.uuid4().hex
    victim = tmp_path / "precious.txt"
    victim.write_text("keep me")
    marker = Path(tempfile.gettempdir()) / f"celmis-nudge-{session}"
    marker.symlink_to(victim)
    try:
        _pre("OrderService", session)  # the answer is not the point: it must not write through
        assert victim.read_text() == "keep me"
    finally:
        marker.unlink(missing_ok=True)


@pytest.mark.parametrize("pattern", ["TODO fix", "a.*b", "def \\w+", "x", "foo|bar", "**/*.py",
                                     "how does the login flow work", ""])
def test_the_grep_nudge_ignores_things_that_are_not_identifiers(pattern, monkeypatch) -> None:
    for k, v in _CONFIGURED.items():
        monkeypatch.setenv(k, v)
    assert _pre(pattern, uuid.uuid4().hex) == ""


def test_the_grep_nudge_is_silent_without_a_token(monkeypatch) -> None:
    monkeypatch.delenv("CELMIS_TOKEN", raising=False)
    assert _pre("OrderService", uuid.uuid4().hex) == ""


# ─── speed ───────────────────────────────────────────────────────────


def test_the_dirty_guard_logic_is_fast_once_the_interpreter_is_up(checkout) -> None:
    """In-process: the logic alone (no interpreter start-up, no process spawn)."""
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    text = _answer(sha, [f"app/orders.py:{n}" for n in range(1, 30)])
    event = json.dumps({"tool_name": PLUGIN_TOOL, "cwd": str(repo), "tool_response": text})
    hook.run("postmcp", event)  # warm the page cache
    samples = []
    for _ in range(25):
        t0 = time.perf_counter()
        hook.run("postmcp", event)
        samples.append(time.perf_counter() - t0)
    samples.sort()
    assert samples[len(samples) // 2] < 0.100  # the median, so one slow tick cannot fail CI


def test_the_dirty_guard_process_finishes_far_inside_the_hook_timeout(checkout) -> None:
    """End to end, as Claude Code runs it: ``python -I`` start-up, imports, the git calls.

    hooks.json gives the hook 5 s; the ceiling here is a fifth of that, generous
    enough for a loaded CI box and still a regression guard against a slow path.
    """
    repo, sha = checkout
    (repo / "app" / "orders.py").write_text("changed\n")
    text = _answer(sha, [f"app/orders.py:{n}" for n in range(1, 30)])
    event = json.dumps({"tool_name": PLUGIN_TOOL, "cwd": str(repo), "tool_response": text})
    _run_process("postmcp", event, repo)  # warm up
    samples = []
    for _ in range(8):
        t0 = time.perf_counter()
        done = _run_process("postmcp", event, repo)
        samples.append(time.perf_counter() - t0)
        assert done.returncode == 0
    samples.sort()
    assert samples[-2] < 1.0, samples  # the second slowest of eight: p~90, tolerant of one spike
