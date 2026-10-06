"""The frozen names the plugin, skill and hooks depend on.

The server module ``src/mcp_server/dev_contract.py`` is the source of truth.
It lands with the integrator's contract commit; until it is on this branch the
values below (the contract as frozen in the plan) stand in for it, so the
drift guards are live from the first day rather than skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "packaging" / "claude-plugin" / "celmis-code"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
HOOK = PLUGIN / "hooks" / "celmis_hook.py"

try:  # pragma: no cover - which branch runs depends on integration state
    from src.mcp_server import dev_contract as _c

    DEV_PATH = _c.DEV_PATH
    DEV_TOOLS = tuple(_c.DEV_TOOLS)
    HOWTO_TOPICS = tuple(_c.HOWTO_TOPICS)
    IDX_LINE_RE = _c.IDX_LINE_RE
    IDX_ENTRY_RE = _c.IDX_ENTRY_RE
    HIT_RE = _c.HIT_RE
    MORE_RE = _c.MORE_RE
    FROM_SERVER = True
except ImportError:  # pragma: no cover
    DEV_PATH = "/mcp/dev"
    DEV_TOOLS = ("repos", "find", "outline", "read_symbol", "refs", "grep", "map",
                 "ask", "howto")
    HOWTO_TOPICS = ("db", "auth", "config", "http_client", "logging", "messaging", "cache")
    IDX_LINE_RE = r"^idx: (?P<entries>.+)$"
    IDX_ENTRY_RE = (r"(?P<slug>[A-Za-z0-9._/-]+) (?P<branch>\S+)@(?P<sha>[0-9a-f]{7,40}) "
                    r"(?P<age>\S+) (?P<state>fresh|STALE|unknown)")
    HIT_RE = r"^(?P<slug>\S+) (?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?\b"
    MORE_RE = r"^… \+(?P<n>\d+) more · cursor=(?P<cursor>\S+)"
    FROM_SERVER = False

PLUGIN_PREFIX = "mcp__plugin_celmis-code_celmis__"

#: Names that must never appear in the public repository. Assembled from
#: fragments so that this file itself passes the guard it implements.
FORBIDDEN = re.compile("|".join(["vi" + "yar", "vp" + "2d"]), re.IGNORECASE)


def forbidden_hits(paths) -> list[str]:
    """``path:line`` for every line under ``paths`` that names the company."""
    hits: list[str] = []
    for base in paths:
        base = Path(base)
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for f in files:
            if "__pycache__" in f.parts or f.suffix in {".pyc", ".png"}:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if FORBIDDEN.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}")
    return hits
