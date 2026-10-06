"""No planted secret reaches anything a client, an auditor or an operator can read.

Surfaces: every tool answer (including the raw MCP payload), the audit rows and
the captured server log. The canaries exist only on disk in a throwaway clone and
in memory here; the files that carry them are named in the world module.
"""

from __future__ import annotations

import pytest

from tests.e2e_local.client import McpClient
from tests.e2e_local.stack import Stack

pytestmark = pytest.mark.needs_lane("tools")


def _context(world, text: str) -> list[str]:
    """Where in the answer a canary sits, with the value itself cut out."""
    import base64

    out = []
    for label in world.leaks_in(text):
        value = world.canaries[label]
        for form, needle in (("plain", value), ("base64", base64.b64encode(value.encode()).decode())):
            at = text.find(needle)
            if at >= 0:
                out.append(f"{label}/{form}: ...{text[max(0, at - 120):at]}<value>"
                           f"{text[at + len(needle):at + len(needle) + 40]}...")
    return out


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("canary")) as st:
        assert st.world is not None
        client = McpClient(st.url, "/mcp/dev/", st.token("owner", repos=["*"]))
        shop = st.slug_of("acme/shop")
        outputs: list[tuple[str, str]] = []
        asked: list[str] = []

        def call(tool: str, args: dict | None = None) -> None:
            res = client.call(tool, args or {})
            outputs.append((tool, res.raw + res.text))
            asked.append(f"{tool} {sorted(args or {})} pattern={(args or {}).get('pattern', '')!r}")

        call("repos")
        for pattern in ("password", "secret", "BEGIN", "token", "dsn", "postgres", "AKIA",
                        "sk_", "api_key"):
            call("grep", {"pattern": pattern})
            call("grep", {"pattern": pattern, "repo": shop, "regex": False})
        call("grep", {"pattern": r"[A-Za-z0-9+/]{24,}={0,2}", "regex": True})
        for query in ("config", "password", "secret", "dsn", "legacy", "settings"):
            call("find", {"query": query})
        for slug in (r.slug for r in st.repos.values()):
            call("map", {"repo": slug})
            call("outline", {"repo": slug, "path": "."})
        call("read_symbol", {"repo": shop, "name": "legacy_dsn"})
        call("read_symbol", {"repo": shop, "name": "load_config"})
        call("find", {"query": ".env"})
        call("outline", {"repo": shop, "path": ".env"})
        call("read_symbol", {"repo": shop, "name": "DB_PASSWORD"})
        yield st, outputs, asked


def test_no_tool_answer_carries_a_canary_or_its_base64_form(run) -> None:
    st, outputs, asked = run
    leaks = [(asked[i], st.world.leaks_in(text), _context(st.world, text))
             for i, (_t, text) in enumerate(outputs) if st.world.leaks_in(text)]
    assert leaks == []


def test_the_audit_rows_carry_no_canary(run) -> None:
    st, *_ = run
    assert st.world.leaks_in(repr(st.audit_rows())) == []


def test_the_server_log_carries_no_canary(run) -> None:
    st, *_ = run
    assert st.world.leaks_in(st.log_text()) == []


def test_the_scan_is_not_vacuous(run) -> None:
    _, outputs, _asked = run
    assert len(outputs) > 30 and sum(len(t) for _, t in outputs) > 2000
