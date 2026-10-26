"""The bundled proxies say which prefix they peeled off.

`handle_path /backend/*` strips the prefix before the API sees the request, so
the API cannot know it was there. It has to: the MCP sign-in documents and the
401 challenge print the API's own public address, and `https://host/mcp/dev`
is not that address (the web app owns the host root), `https://host/backend/mcp/dev`
is. Each Caddyfile therefore tells the API with `X-Forwarded-Prefix`, and the
separate API domain of the Hetzner layout drops one a client may have sent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CADDYFILES = ["deploy/Caddyfile", "deploy/hetzner/Caddyfile", "deploy/oracle/Caddyfile.http"]


def _blocks(text: str, opener: str) -> list[str]:
    """The bodies of every `<opener> { ... }` block (brace-matched)."""
    found = []
    for m in re.finditer(rf"^[ \t]*{re.escape(opener)}[ \t]*\{{", text, re.M):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        found.append(text[m.end():i - 1])
    return found


@pytest.mark.parametrize("path", CADDYFILES)
def test_the_prefixed_route_to_the_api_names_its_prefix(path):
    text = (ROOT / path).read_text()
    (handle,) = _blocks(text, "handle_path /backend/*")
    (proxy,) = _blocks(handle, "reverse_proxy api:8000")
    assert re.search(r"^\s*header_up\s+X-Forwarded-Prefix\s+/backend\s*$", proxy, re.M), path


@pytest.mark.parametrize("path", CADDYFILES)
def test_the_web_app_keeps_the_host_root(path):
    """The prefix is peeled off for the API only; the root still belongs to the web app."""
    text = (ROOT / path).read_text()
    assert "handle_path /backend/*" in text
    assert re.search(r"^\s*reverse_proxy web:3000\s*$", text, re.M)
    assert "X-Forwarded-Prefix" not in "".join(_blocks(text, "reverse_proxy web:3000"))


def test_a_separate_api_domain_drops_a_prefix_a_client_sent():
    text = (ROOT / "deploy/hetzner/Caddyfile").read_text()
    (api_block,) = _blocks(text, "{$API_DOMAIN}")
    (proxy,) = _blocks(api_block, "reverse_proxy api:8000")
    assert re.search(r"^\s*header_up\s+-X-Forwarded-Prefix\s*$", proxy, re.M)
    assert "/backend" not in proxy
