"""`/.well-known/oauth-*` on the web host is data for a program, never a login page.

An MCP client that cannot follow the metadata link of a 401 looks for the OAuth
discovery documents at the HOST ROOT, and the host root is the web app's. The
sign-in guard (`proxy.ts`) used to answer those requests with `307 /login`; the
client followed it, got HTML, and reported `JSON Parse error: Unrecognized token '<'`.

Two things hold it closed, and each is pinned here because losing either brings
the symptom back without any test noticing:

* the guard's matcher leaves `/.well-known` alone, and
* a route handler under `app/.well-known` answers it, at run time (a build-time
  `rewrites()` entry would tie one published image to one installation).

The behaviour of the handler itself is in `web/lib/well-known*.test.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
ROUTE = WEB / "app" / ".well-known" / "[...path]" / "route.ts"
BARE_ROUTE = WEB / "app" / ".well-known" / "route.ts"


def _matcher() -> str:
    text = (WEB / "proxy.ts").read_text()
    m = re.search(r"matcher:\s*\[\s*\"((?:[^\"\\]|\\.)*)\"", text, re.S)
    assert m, "proxy.ts has no matcher"
    return m.group(1).replace("\\\\", "\\")


def _guard_would_run_on(path: str) -> bool:
    # Next compiles the matcher with path-to-regexp; the pattern here is a plain
    # `/((?!a|b|c).*)`, so the same lookahead semantics in `re` decide it.
    return re.fullmatch(_matcher(), path) is not None


def test_the_sign_in_guard_does_not_run_on_the_well_known_paths():
    for path in ("/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-protected-resource/backend/mcp/dev",
                 "/.well-known/oauth-authorization-server"):
        assert not _guard_would_run_on(path), path


def test_the_sign_in_guard_still_runs_on_the_app_itself():
    for path in ("/", "/settings/mcp", "/codebase", "/review"):
        assert _guard_would_run_on(path), path


def test_a_route_handler_answers_the_well_known_paths():
    assert ROUTE.is_file()
    text = ROUTE.read_text()
    assert "export async function GET" in text and "export async function HEAD" in text


def test_the_bare_well_known_path_gets_the_json_404_too():
    """The catch-all next door needs one segment; without its own handler Next answers
    `/.well-known` with its HTML 404 page."""
    assert BARE_ROUTE.is_file()
    text = BARE_ROUTE.read_text()
    assert "export async function GET" in text and "export async function HEAD" in text
    assert "[...path]/route" in text, "it must answer through the same handler, not a second copy"
    assert "text/html" not in text


def test_the_handler_reads_the_api_address_at_run_time_not_at_build_time():
    text = ROUTE.read_text()
    assert 'export const dynamic = "force-dynamic"' in text
    assert "API_BASE" in text
    config = "\n".join(p.read_text() for p in (WEB / "next.config.ts", WEB / "next.config.mjs",
                                               WEB / "next.config.js") if p.exists())
    assert "well-known" not in config, "a rewrites() entry is resolved at build time"


def test_the_handler_never_redirects_and_never_answers_html():
    text = ROUTE.read_text()
    assert "redirect" not in text.lower()
    assert "text/html" not in text
    assert text.count('"content-type": "application/json"') >= 3


def test_the_handler_forwards_what_the_api_needs_to_name_its_public_address():
    text = ROUTE.read_text()
    for header in ("x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix"):
        assert header in text


def test_only_the_oauth_documents_are_proxied():
    text = (WEB / "lib" / "well-known.ts").read_text()
    assert '["oauth-protected-resource", "oauth-authorization-server"]' in text
