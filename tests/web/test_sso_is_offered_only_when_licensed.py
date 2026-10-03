"""The SSO button appears only when the sign-in it starts can finish.

Two independent halves have to be present: the web side configured for OIDC
(AUTH_OIDC_*, read by NextAuth) and the API's `sso` capability — the
endpoint the provider's callback posts to, mounted only under a licence that
grants "sso" (src/ee/sso). Either alone is a button that dead-ends.

The decision is made on the server (web/app/login/page.tsx and the invite
landing page, through web/lib/sso-offer.ts), so it is read
from the source here — with comments stripped, because the page explains the
rule at length in a comment and a test must not pass on the explanation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"


def _code(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


@pytest.mark.parametrize("page_path", [
    ("app", "login", "page.tsx"),
    # The invite landing page offers the same sign-in buttons, by the same rule.
    ("app", "invite", "[token]", "page.tsx"),
])
def test_the_page_needs_both_the_config_and_the_capability(page_path) -> None:
    page = _code(WEB.joinpath(*page_path))
    assert re.search(r"oidcConfigured\(\) && \(await apiOffersSso\(\)\)", page)
    assert 'import { apiOffersSso } from "@/lib/sso-offer"' in page


def test_the_capability_check_fails_closed() -> None:
    """The check both pages share (web/lib/sso-offer.ts)."""
    lib = _code(WEB / "lib" / "sso-offer.ts")
    assert "features?.sso?.available === true" in lib
    assert "/api/capabilities" in lib
    assert 'cache: "no-store"' in lib
    # Any failure hides the button rather than throwing the login page away.
    assert re.search(r"catch \{\s*return false;", lib)


def test_the_form_no_longer_decides_sso_from_nextauth_alone() -> None:
    """NextAuth's provider list says what the WEB is configured for; it was
    the only input before, which offered SSO to a community API."""
    form = _code(WEB / "app" / "login" / "login-form.tsx")
    assert "providers?.oidc" not in form
    assert "ssoName: string | null" in form
    assert 'from "@/ee/sso/sso-button"' in form


def test_the_provider_and_the_exchange_live_behind_the_boundary() -> None:
    auth = _code(WEB / "auth.ts")
    assert 'from "@/ee/sso/oidc-provider"' in auth
    assert "...oidcProviders()" in auth
    assert "next-auth/providers/keycloak" not in auth
    provider = _code(WEB / "ee" / "sso" / "oidc-provider.ts")
    assert 'OIDC_EXCHANGE_PATH = "/api/auth/oidc"' in provider
    assert "if (!oidcConfigured()) return [];" in provider


def test_the_exchange_path_is_the_one_the_api_mounts() -> None:
    from src.ee.sso.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/auth/oidc" in paths
