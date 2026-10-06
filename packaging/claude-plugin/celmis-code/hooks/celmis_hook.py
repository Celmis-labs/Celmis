#!/usr/bin/env python3
"""Hooks for the Celmis Claude Code plugin.

One stdlib-only script, three subcommands, run as ``python3 -I celmis_hook.py``:

``session``
    SessionStart. At most three lines of context: that the Celmis tools exist,
    which repository and commit this checkout is, and, only when the plugin is
    not configured, how to configure it. No network.

``pregrep``
    PreToolUse for Grep/Glob. When the pattern looks like an identifier, adds
    a nudge (never a denial) that the Celmis tools answer definition and
    caller questions across repositories. At most once per session.

``postmcp``
    PostToolUse for the Celmis tools: the dirty-guard. A Celmis answer is true
    at the commit it indexed; this checkout may differ. The hook reads the
    ``idx:`` line and the ``slug path:line`` hits of the answer, and when the
    answer is about the repository checked out here, names the hit files that
    differ locally so the agent reads them from disk instead.

Contract
--------
* Fail open. Whatever goes wrong (garbage on stdin, no git, an unknown sha, a
  timeout) the script prints nothing and exits 0. A hook must never be the
  reason a session stops.
* Tool output is data. It is parsed with the strict regexes below and never
  executed, never put through a shell. Paths from it reach git only as argv
  after ``--``, with ``--literal-pathspecs`` so ``*`` and ``:(...)`` stay
  literal.
* The regexes below are copies of ``src/mcp_server/dev_contract.py`` (a plugin
  cannot import the server). ``tests/plugin`` fails when they drift.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile

# --- copies of src/mcp_server/dev_contract.py (checked by tests/plugin) -----
DEV_TOOLS = ("repos", "find", "outline", "read_symbol", "refs", "grep", "map",
             "ask", "howto")
IDX_LINE_RE = r"^idx: (?P<entries>.+)$"
IDX_ENTRY_RE = (r"(?P<slug>[A-Za-z0-9._/-]+) (?P<branch>\S+)@(?P<sha>[0-9a-f]{7,40}) "
                r"(?P<age>\S+) (?P<state>fresh|STALE|unknown)")
HIT_RE = r"^(?P<slug>\S+) (?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?\b"
# ---------------------------------------------------------------------------

#: howto lists its slices as "1) path:12-31 symbol", the slug being on the
#: "howto <topic> <slug>" line. Local to the hook, not part of the contract.
HOWTO_HEAD_RE = r"^howto (?P<topic>\S+) (?P<slug>[A-Za-z0-9._/-]+)"
HOWTO_HIT_RE = r"^\s*\d+\) (?P<path>[^\s:]+):(?P<start>\d+)"

GIT_TIMEOUT_S = 1.5
MAX_PATHS = 60
MAX_TEXT = 200_000
#: Identifier-shaped Grep/Glob patterns: ``getUserById``, ``load_config``,
#: ``\bOrderService\b``. Not prose, not a path glob, not a regex with structure.
_IDENT_RE = re.compile(r"^(?:\\b)?[A-Za-z_][A-Za-z0-9_]{3,}(?:\\b)?$")
_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _json_out(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "additionalContext": text}})


# ---------------------------------------------------------------- git helpers


def _git(cwd: str, *args: str) -> str | None:
    """Run git with an argv list (no shell). None on any failure."""
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "--literal-pathspecs", "-C", cwd, *args],  # noqa: S607
            capture_output=True, timeout=GIT_TIMEOUT_S, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def _remote_candidates(remote: str) -> set[str]:
    """Every spelling of this checkout's repository an ``idx:`` slug may use.

    ``acme/shop`` and ``acme-shop`` for any host, and ``<provider>_acme-shop``
    where the server prefixes a provider. Lower-cased; compared exactly.
    """
    remote = remote.strip()
    if not remote:
        return set()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$",
                 remote)
    if m is None:
        m = re.match(r"^(?:[^@/]*@)?(?P<host>[^:/]+):(?P<path>.+)$", remote)
    if m is None:
        return set()
    host = m.group("host").lower()
    path = m.group("path").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return set()
    owner, name = "/".join(parts[:-1]), parts[-1]
    flat = f"{owner.replace('/', '-')}-{name}"
    out = {f"{owner}/{name}", flat}
    provider = {"github.com": "github", "www.github.com": "github",
                "gitlab.com": "gitlab", "bitbucket.org": "bitbucket"}.get(host)
    if provider:
        out.add(f"{provider}_{flat}")
    return {c.lower() for c in out}


def _checkout(cwd: str) -> dict | None:
    """The repository around ``cwd``: top, head, branch, remote candidates."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    top = top.strip()
    head = (_git(top, "rev-parse", "HEAD") or "").strip()
    branch = (_git(top, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    remote = _git(top, "remote", "get-url", "origin") or ""
    return {"top": top, "head": head, "branch": branch,
            "slugs": _remote_candidates(remote)}


# ------------------------------------------------------------------- parsing


def _collect_text(node, out: list[str], depth: int = 0) -> None:
    """All text of a tool_response, whatever envelope the client used."""
    if depth > 6 or sum(map(len, out)) > MAX_TEXT:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, out, depth + 1)
    elif isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        for key in ("content", "result", "output", "structuredContent"):
            if key in node:
                _collect_text(node[key], out, depth + 1)


def parse_idx(text: str) -> list[dict]:
    """The entries of the first ``idx:`` line, [] when there is none."""
    for line in text.splitlines():
        m = re.match(IDX_LINE_RE, line)
        if m is None:
            continue
        entries = []
        for chunk in m.group("entries").split(" · "):
            e = re.fullmatch(IDX_ENTRY_RE, chunk.strip())
            if e is not None:
                entries.append(e.groupdict())
        return entries
    return []


def parse_hits(text: str) -> dict[str, set[str]]:
    """slug -> paths named by ``slug path:line`` hits and howto slices."""
    hits: dict[str, set[str]] = {}
    howto_slug = ""
    for line in text.splitlines():
        h = re.match(HOWTO_HEAD_RE, line)
        if h is not None:
            howto_slug = h.group("slug")
            continue
        w = re.match(HOWTO_HIT_RE, line)
        if w is not None and howto_slug:
            hits.setdefault(howto_slug.lower(), set()).add(w.group("path"))
            continue
        m = re.match(HIT_RE, line)
        if m is not None:
            hits.setdefault(m.group("slug").lower(), set()).add(m.group("path"))
    return hits


def _safe_relpath(path: str) -> bool:
    if not path or path.startswith(("/", "-", "~")) or "\x00" in path:
        return False
    return ".." not in path.split("/")


def _tool_is_celmis(tool_name: str) -> bool:
    if not tool_name.startswith("mcp__"):
        return False
    short = tool_name.rsplit("__", 1)[-1]
    return "celmis" in tool_name.lower() and short in DEV_TOOLS


# --------------------------------------------------------------- subcommands


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def insecure_url(url: str) -> bool:
    """True when ``url`` would carry the bearer token in clear text: anything but
    https, except a server on this machine."""
    from urllib.parse import urlparse

    try:
        parts = urlparse(url.strip())
    except ValueError:
        return True
    if parts.scheme == "https":
        return False
    return not (parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS)


def cmd_session(event: dict) -> str:
    cwd = str(event.get("cwd") or os.getcwd())
    configured = bool(os.environ.get("CELMIS_URL")) and bool(os.environ.get("CELMIS_TOKEN"))
    lines: list[str] = []
    if configured and insecure_url(os.environ.get("CELMIS_URL", "")):
        configured = False
        lines.append("CELMIS_URL is not https, so the Celmis token would travel unencrypted: "
                     "its tools are not to be used until the URL is https (localhost is the "
                     "only exception). Use Grep/Read as usual and tell your administrator.")
    elif configured:
        lines.append("Celmis is connected (tools mcp__plugin_celmis-code_celmis__*): for "
                     "definitions, callers and other repositories use repos -> find -> "
                     "outline -> read_symbol -> refs; keep Grep for uncommitted code here.")
    else:
        lines.append("Celmis plugin installed but CELMIS_URL / CELMIS_TOKEN are not set in "
                     "this shell, so its tools will not connect: use Grep/Read as usual, or "
                     "ask your Celmis administrator for a token.")
    co = _checkout(cwd)
    if co and co["head"]:
        name = next((s for s in sorted(co["slugs"]) if "/" in s),
                    os.path.basename(co["top"]))
        status = _git(co["top"], "status", "--porcelain", "--untracked-files=no") or ""
        dirty = len([ln for ln in status.splitlines() if ln.strip()])
        lines.append(f"local checkout: {name} {co['branch']}@{co['head'][:8]}"
                     + (f", {dirty} tracked file(s) modified" if dirty else ""))
    return _json_out("SessionStart", "\n".join(lines[:3])) if lines else ""


def cmd_pregrep(event: dict) -> str:
    if not os.environ.get("CELMIS_TOKEN"):
        return ""
    if insecure_url(os.environ.get("CELMIS_URL", "")):
        return ""  # never nudge towards a server the token cannot safely reach
    tool_input = event.get("tool_input") or {}
    pattern = str(tool_input.get("pattern") or "").strip()
    if not _IDENT_RE.match(pattern):
        return ""
    marker = os.path.join(tempfile.gettempdir(),
                          "celmis-nudge-" + re.sub(r"[^A-Za-z0-9_-]", "", str(
                              event.get("session_id") or "none"))[:64])
    # O_EXCL | O_NOFOLLOW: a symlink planted at the predictable path is never followed
    # (it would truncate its target), and "already there" means "already nudged".
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(marker, flags, 0o600))
    except FileExistsError:
        return ""
    except OSError:
        pass  # without a marker the nudge is merely repeated; still fine
    return _json_out("PreToolUse",
                     f"'{pattern}' looks like a symbol name. If it may live in another "
                     "repository, or you want its callers, the Celmis tools find / refs "
                     "answer that in one call; Grep is right for this checkout's "
                     "uncommitted code.")


def stale_files(co: dict, sha: str, paths: set[str]) -> list[str] | None:
    """Hit paths that differ locally from ``sha``; None when ``sha`` is unknown here."""
    if not _HEX_RE.match(sha) or _git(co["top"], "cat-file", "-e", f"{sha}^{{commit}}") is None:
        return None
    safe = sorted(p for p in paths if _safe_relpath(p))[:MAX_PATHS]
    if not safe:
        return []
    changed: set[str] = set()
    diff = _git(co["top"], "diff", "--name-only", "-z", sha, "--", *safe)
    if diff:
        changed.update(p for p in diff.split("\0") if p)
    untracked = _git(co["top"], "ls-files", "--others", "--exclude-standard", "-z", "--", *safe)
    if untracked:
        changed.update(p for p in untracked.split("\0") if p)
    return sorted(changed & set(safe))


def cmd_postmcp(event: dict) -> str:
    if not _tool_is_celmis(str(event.get("tool_name") or "")):
        return ""
    texts: list[str] = []
    _collect_text(event.get("tool_response"), texts)
    text = "\n".join(texts)[:MAX_TEXT]
    entries = parse_idx(text)
    if not entries:
        return ""
    co = _checkout(str(event.get("cwd") or os.getcwd()))
    if not co or not co["slugs"]:
        return ""
    mine = next((e for e in entries if e["slug"].lower() in co["slugs"]), None)
    if mine is None:
        return ""
    paths = parse_hits(text).get(mine["slug"].lower(), set())
    if not paths:
        return ""
    stale = stale_files(co, mine["sha"], paths)
    if stale is None:
        return _json_out("PostToolUse",
                         f"index sha {mine['sha'][:8]} is not in your clone; line numbers "
                         "may differ from your files (git fetch, or read locally).")
    if not stale:
        return ""
    return _json_out("PostToolUse",
                     "[stale-locally] " + ", ".join(stale) + " - read locally")


COMMANDS = {"session": cmd_session, "pregrep": cmd_pregrep, "postmcp": cmd_postmcp}


def run(command: str, raw: str) -> str:
    """The hook's output for ``raw`` stdin. Never raises."""
    try:
        fn = COMMANDS.get(command)
        if fn is None:
            return ""
        try:
            event = json.loads(raw) if raw.strip() else {}
        except ValueError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        return fn(event) or ""
    except Exception:  # noqa: BLE001 - fail open, always
        return ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        raw = sys.stdin.read(MAX_TEXT * 2) if not sys.stdin.isatty() else ""
    except Exception:  # noqa: BLE001
        raw = ""
    out = run(argv[0] if argv else "", raw)
    if out:
        with contextlib.suppress(Exception):
            sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
