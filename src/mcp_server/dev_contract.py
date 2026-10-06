"""Frozen names and formats of the compact ``/mcp/dev`` profile.

The Claude Code plugin, its skill and its hooks parse what the dev tools print
and call them by these names, so a change here is a change to a published
contract: add, do not rename.

Line 1 of every dev-tool response, empty results and errors included, matches
:data:`IDX_LINE_RE`. It names the repositories the answer is about, the branch
and commit the index was built from, how old that is, and whether the remote
has moved since. Everything that follows (line numbers, bodies) refers to THAT
commit, not to whatever is checked out locally.
"""

from __future__ import annotations

from src.mcp_server.scopes import SCOPE_DEV  # one definition of the scope string

DEV_PATH = "/mcp/dev"
DEV_TOOLS = ("repos", "find", "outline", "read_symbol", "refs", "grep", "map", "ask", "howto")
HOWTO_TOPICS = ("db", "auth", "config", "http_client", "logging", "messaging", "cache")

IDX_LINE_RE = r"^idx: (?P<entries>.+)$"
#: One entry of the idx line; entries are joined by " · ".
IDX_ENTRY_RE = (
    r"(?P<slug>[A-Za-z0-9._/-]+) (?P<branch>\S+)@(?P<sha>[0-9a-f]{7,40}) "
    r"(?P<age>\S+) (?P<state>fresh|STALE|unknown)"
)
HIT_RE = r"^(?P<slug>\S+) (?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?\b"
MORE_RE = r"^… \+(?P<n>\d+) more · cursor=(?P<cursor>\S+)"

__all__ = [
    "DEV_PATH", "DEV_TOOLS", "HIT_RE", "HOWTO_TOPICS", "IDX_ENTRY_RE",
    "IDX_LINE_RE", "MORE_RE", "SCOPE_DEV",
]
