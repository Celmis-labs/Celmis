"""A self-hosted install is private, so no search engine should list it.

Three things together keep it out of the results:

* every page carries ``X-Robots-Tag: noindex`` (web/lib/csp.ts, sent by the
  proxy) and a robots meta tag (web/app/layout.tsx), and the API sends the same
  header (src/api/edge.py, tested in tests/api);
* robots.txt is served as a file, not bounced to /login by the sign-in guard;
* robots.txt does NOT disallow crawling. A crawler that may not fetch a page
  never reads its noindex, and an already indexed page would stay listed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
NOINDEX = "noindex, nofollow, noarchive"


def _matcher() -> str:
    text = (WEB / "proxy.ts").read_text()
    m = re.search(r"matcher:\s*\[\s*\"((?:[^\"\\]|\\.)*)\"", text, re.S)
    assert m, "proxy.ts has no matcher"
    return m.group(1).replace("\\\\", "\\")


def test_robots_txt_is_not_redirected_to_the_sign_in_page():
    assert re.fullmatch(_matcher(), "/robots.txt") is None
    assert re.fullmatch(_matcher(), "/dashboard") is not None


def test_robots_txt_exists_and_leaves_crawling_allowed():
    text = (WEB / "public" / "robots.txt").read_text()
    rules = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert "User-agent: *" in rules
    assert not any(re.fullmatch(r"Disallow:\s*/\S*", r) for r in rules), rules


def test_every_page_says_noindex_in_its_headers_and_its_markup():
    csp = (WEB / "lib" / "csp.ts").read_text()
    assert f'["X-Robots-Tag", "{NOINDEX}"]' in csp
    layout = (WEB / "app" / "layout.tsx").read_text()
    assert re.search(r"robots:\s*\{\s*index:\s*false,\s*follow:\s*false", layout)


def test_the_api_and_the_web_app_send_the_same_value():
    from src.api.edge import ROBOTS_TAG

    assert ROBOTS_TAG == NOINDEX
