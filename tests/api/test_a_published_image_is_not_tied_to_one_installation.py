"""The first image built for the registry broke production, and would have
broken every user who ever pulled it.

`NEXT_PUBLIC_*` is substituted into the client bundle at BUILD time. The web
Dockerfile defaulted it to `http://localhost:8000`, and `docker-compose.yml`
passed the deployment's own absolute URL — which worked, because every build
happened on the machine that would run it.

The moment an image is published, that stops being true. Built once and pulled
by somebody else, the bundle tells their browser to call THEIR localhost:8000.
Verified the hard way: the first registry-built `web` reached production and
every data page answered "Failed to fetch" while the API was perfectly healthy.

So the browser side now defaults to the RELATIVE `/backend`, which the reverse
proxy maps to FastAPI on the same origin. The address belongs to wherever the
app is running, not to wherever it was built — one image serves any hostname,
which is the only thing that makes publishing one worth doing.

The server side of Next keeps an absolute URL: a relative path has nothing to
be relative to outside a browser.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"
API_TS = WEB / "lib/api.ts"
DOCKERFILE = WEB / "Dockerfile"


def _api_base_expr() -> str:
    src = API_TS.read_text(encoding="utf-8")
    m = re.search(r"export const API_BASE =(.*?);", src, re.S)
    assert m, "API_BASE is gone"
    return m.group(1)


def test_the_browser_falls_back_to_a_relative_path():
    assert '"/backend"' in _api_base_expr()


def test_the_browser_never_falls_back_to_localhost():
    """A published bundle that names localhost is a bundle that only works on
    the machine that built it."""
    # The ternary's browser branch is everything after the last `:`.
    browser = _api_base_expr().rsplit(":", 1)[-1]

    assert "localhost" not in browser, browser


def test_the_server_side_keeps_an_absolute_url():
    """Outside a browser there is no origin for a relative path to resolve
    against."""
    expr = _api_base_expr()

    assert "API_BASE_INTERNAL" in expr
    assert "http://localhost:8000" in expr, (
        "the server side lost its absolute fallback"
    )


def test_an_explicit_setting_still_wins():
    """Development runs Next on :3000 and FastAPI on :8000 with nothing between
    them; that setup has to remain possible."""
    expr = _api_base_expr()

    assert "NEXT_PUBLIC_API_BASE" in expr


def test_the_fallback_treats_an_empty_string_as_unset():
    """The build arg is empty by default, and Next substitutes it literally —
    so the browser branch sees "" and must fall through. `??` would keep the
    empty string and produce requests to a bare path."""
    browser = _api_base_expr().rsplit(":", 1)[-1]

    assert "||" in browser, "an empty NEXT_PUBLIC_API_BASE would win over /backend"


def test_the_image_bakes_nothing_by_default():
    src = DOCKERFILE.read_text(encoding="utf-8")

    assert re.search(r"^ARG NEXT_PUBLIC_API_BASE=\s*$", src, re.M), (
        "the Dockerfile still bakes a default URL into every published image"
    )


def test_compose_does_not_pass_a_build_arg_it_no_longer_builds_with():
    root = WEB.parent
    import yaml

    doc = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "build" not in doc["services"]["web"]


@pytest.mark.parametrize("path", ["/backend/api/capabilities", "/backend/healthz"])
def test_the_relative_path_matches_what_the_proxy_serves(path):
    """`/backend` is not a guess: it is the prefix the deployed reverse proxy
    already maps to FastAPI, and every operational check in this repository
    uses it."""
    caddy = [p for p in (WEB.parent / "deploy").rglob("*")
             if p.is_file() and "caddyfile" in p.name.lower()]
    assert caddy, "no Caddyfile found — the proxy config moved"
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in caddy)

    assert "handle_path /backend/*" in text, (
        "the proxy no longer serves the API under /backend"
    )
