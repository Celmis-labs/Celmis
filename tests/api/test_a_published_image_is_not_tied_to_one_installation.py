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


def _caddyfiles():
    return sorted(p for p in (WEB.parent / "deploy").rglob("*")
                  if p.is_file() and "caddyfile" in p.name.lower())


def _directives(path) -> str:
    """The file without its comments.

    A comment explaining why /backend matters is not a route. This test used
    to search the text, and the file that needed the route the most is the one
    whose comment would have satisfied the search.
    """
    return "\n".join(line for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if not line.lstrip().startswith("#"))


def test_there_is_a_proxy_config_to_check():
    assert _caddyfiles(), "no Caddyfile found — the proxy config moved"


@pytest.mark.parametrize("caddyfile", _caddyfiles(), ids=lambda p: str(p.relative_to(p.parents[1])))
def test_every_proxy_that_serves_the_app_serves_backend_too(caddyfile):
    """`/backend` is not a guess: it is the prefix the published bundle calls.

    EACH FILE, not all of them joined. This read every Caddyfile into one
    string and asked whether the prefix appeared ANYWHERE, so one correct file
    covered for the rest — and deploy/hetzner/Caddyfile, which had no such
    route at all, passed. A browser loading the published image there asked
    APP_DOMAIN/backend/... and got a 404 from Next for every call, on a stack
    that was otherwise healthy.

    Conditioned on serving the app: a proxy that only fronts the API has
    nothing to answer for.
    """
    text = _directives(caddyfile)
    if "reverse_proxy web:3000" not in text:
        pytest.skip("does not serve the web app")
    assert "handle_path /backend/*" in text, (
        f"{caddyfile.name} serves the web app and does not route /backend to "
        f"the API. The published image is built with an empty "
        f"NEXT_PUBLIC_API_BASE, so its bundle calls the relative /backend — "
        f"every API call from a browser 404s here."
    )
