"""Structural guards for the web app's browser security (the behaviour itself is
in web/lib/csp.test.ts and web/lib/federated-logout.test.ts).

  * the proxy sets a per-response CSP and passes the nonce on to the layout;
  * the one inline script carries that nonce;
  * the framework banner is off and the static headers are configured;
  * the session callback never copies the refresh credential or the id_token
    out to the object that /api/auth/session returns to page scripts;
  * the session is held to a day.
"""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"


def _read(rel: str) -> str:
    return (WEB / rel).read_text()


def test_the_proxy_sets_the_csp_and_hands_the_nonce_to_the_layout():
    proxy = _read("proxy.ts")
    assert "buildCsp(" in proxy and "makeNonce()" in proxy
    assert 'requestHeaders.set("x-nonce", nonce)' in proxy
    assert "res.headers.set(csp[0], csp[1])" in proxy
    assert "CELMIS_CSP" in proxy
    assert "AUTH_OIDC_ISSUER" in proxy


def test_the_inline_theme_script_carries_the_nonce_and_is_the_only_inline_script():
    layout = _read("app/layout.tsx")
    assert re.search(r"<script nonce=\{nonce\} dangerouslySetInnerHTML", layout)
    hits = [
        str(p.relative_to(WEB))
        for ext in ("tsx", "ts")
        for p in WEB.glob(f"**/*.{ext}")
        if "node_modules" not in p.parts and ".next" not in p.parts
        and "dangerouslySetInnerHTML" in p.read_text()
        and "test" not in p.name
    ]
    assert hits == ["app/layout.tsx"], hits


def test_the_framework_banner_is_off_and_the_static_headers_are_configured():
    cfg = _read("next.config.ts")
    assert "poweredByHeader: false" in cfg
    assert "STATIC_SECURITY_HEADERS" in cfg


def test_the_session_object_never_carries_the_refresh_credential():
    auth = _read("auth.ts")
    session_cb = auth[auth.index("session: async ({ session, token })"):]
    session_cb = session_cb[: session_cb.index("return session;")]
    assert "refreshToken" not in session_cb
    assert "idToken" not in session_cb
    assert "refreshToken" not in _read("types/next-auth.d.ts").split("interface Session")[1].split("}\n}")[0]


def test_the_session_is_held_to_a_day_and_slides():
    auth = _read("auth.ts")
    assert "maxAge: sessionMaxAgeSeconds()" in auth
    assert "30 * 24 * 60 * 60" not in auth
    assert "Math.min(hours, 24)" in _read("lib/auth-options.ts")


def test_sign_out_goes_through_the_provider_when_there_is_one():
    shell = _read("components/app-shell.tsx")
    assert "/api/auth/federated-logout" in shell
    assert 'signOut({ redirect: false })' in shell
