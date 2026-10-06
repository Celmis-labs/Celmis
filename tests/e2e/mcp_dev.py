"""In-container E2E for the developer MCP profile (``/mcp/dev/``).

A contract smoke against the LIVE stack: it needs no fixture repositories. The
full gold scenarios run on an in-process stack (``scripts/dev_mcp_e2e.py --spawn``
or ``tests/e2e_local``); against a live deployment point that script at the URL:

    CELMIS_TOKEN=... python scripts/dev_mcp_e2e.py --url https://celmis.example.com

The suite needs a REAL issued token (the superadmin mints it for a person with an explicit
repository list): export it as ``E2E_MCP_TOKEN`` (or ``CELMIS_TOKEN``). Self-signed
legacy JWTs are refused by ``/mcp/dev/``, so this script never mints its own. Without a
token the authenticated checks are skipped (``RESULT: SKIP``; a failure under
``CELMIS_E2E_STRICT=1``).

Checks here: the endpoint exists and refuses anonymous callers, ``tools/list``
is the nine contract tools, ``repos`` leads with the freshness (``idx:``) line,
and a call for a repository that does not exist answers like any other refusal.

Skips (exit 0, ``RESULT: SKIP``) while the endpoint is not deployed; with
``CELMIS_E2E_STRICT=1`` that is a failure instead.
"""
import json
import os
import re
import sys

import httpx

from tests.e2e_local.client import McpClient, McpHttpError

BASE = os.environ.get("E2E_API_BASE", "http://api:8000").rstrip("/")
PATH = "/mcp/dev/"
STRICT = os.environ.get("CELMIS_E2E_STRICT") == "1"
TOOLS = {"repos", "find", "outline", "read_symbol", "refs", "grep", "map", "ask", "howto"}
CORE = TOOLS - {"howto"}  # howto ships with the howto lane
IDX = re.compile(r"^idx: .+", re.M)

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


probe = httpx.post(BASE + PATH, json={}, timeout=30,
                   headers={"Accept": "application/json, text/event-stream"})
if probe.status_code == 404:
    print(f"RESULT: SKIP - {PATH} is not deployed")
    sys.exit(1 if STRICT else 0)
check(probe.status_code == 401, f"anonymous call is refused (got {probe.status_code})")

try:
    McpClient(BASE, PATH, "not-a-token").call("repos")
    check(False, "a forged token is refused")
except McpHttpError as exc:
    check(exc.status in (401, 403), f"a forged token is refused ({exc.status})")

token = os.environ.get("E2E_MCP_TOKEN") or os.environ.get("CELMIS_TOKEN")
if not token:
    print("no E2E_MCP_TOKEN / CELMIS_TOKEN: the authenticated checks are skipped")
    if failures:
        print(f"RESULT: FAIL ({len(failures)})")
        sys.exit(1)
    print("RESULT: SKIP - no issued token for the authenticated checks")
    sys.exit(1 if STRICT else 0)

client = McpClient(BASE, PATH, token)
try:
    tools = client.list_tools()
except McpHttpError as exc:
    print(f"RESULT: FAIL - the issued token was refused ({exc.status})")
    sys.exit(1)

names = {t["name"] for t in tools}
check(names >= CORE and names <= TOOLS, f"tools/list is the contract set ({sorted(names)})")
size = len(json.dumps(tools, separators=(",", ":")))
check(size < 8000, f"tools/list stays small ({size} chars)")

res = client.call("repos")
check(not res.is_error, "repos answers")
check(bool(IDX.match(res.text)), "repos leads with the idx: freshness line")

ghost = client.call("grep", {"pattern": "x", "repo": "github_nobody-nothing"})
check(ghost.is_error or "nobody-nothing" not in ghost.text.replace("github_nobody-nothing", ""),
      "an unknown repository is a plain refusal")

print("RESULT: ALL_PASS" if not failures else f"RESULT: FAIL ({len(failures)})")
sys.exit(1 if failures else 0)
