"""Sign-in failures reach the person, and a new workspace starts clean.

  * When the API refuses a Google / SSO id_token, Auth.js must not finish the
    OAuth callback as a success: the exchange runs in the `signIn` callback,
    which answers with a /login?error= redirect, so the login form shows
    auth.sso.failed instead of the proxy silently bouncing to /login.
  * The form's error toast carries a fixed id, so a lazily loaded dictionary
    or a language switch (both give `t` a new identity) updates it instead of
    stacking a second one.
  * Accepting an invite is a workspace switch: the agent's session is
    forgotten and the page fully reloads, like the workspace switcher.

There is no TS test runner in web/, so the source is read — with comments
removed TOKEN-wise (strings are kept intact), because each of these files
explains the rule in a comment and a test must not pass on the explanation.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def _code(path: Path) -> str:
    """Drop // and /* */ comments, keeping string and template literals."""
    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _block(code: str, start: str) -> str:
    """The brace-balanced body of the arrow function that starts at `start`."""
    at = code.index(start)
    open_at = code.index("{", code.index("=>", at))
    depth = 0
    for k in range(open_at, len(code)):
        depth += {"{": 1, "}": -1}.get(code[k], 0)
        if depth == 0:
            return code[open_at:k + 1]
    raise AssertionError(f"unbalanced block after {start!r}")


def test_the_comment_stripper_keeps_strings() -> None:
    sample = WEB / "auth.ts"
    code = _code(sample)
    assert '"/api/auth/google"' in code
    assert "Exchange a provider id_token" not in code


def test_a_refused_exchange_redirects_with_an_error() -> None:
    code = _code(WEB / "auth.ts")
    m = re.search(r'const SSO_REJECTED_URL = "([^"]+)"', code)
    assert m and m.group(1).startswith("/login?") and "error=" in m.group(1)

    sign_in = _block(code, "signIn: async")
    assert "exchangeIdToken(" in sign_in
    assert re.search(r"catch\s*\{\s*return SSO_REJECTED_URL;\s*\}", sign_in)
    # No id_token at all is a refusal too, not a silent success.
    assert re.search(r"if \(!account\?\.id_token\) return SSO_REJECTED_URL;", sign_in)


def test_the_jwt_callback_no_longer_swallows_a_refusal() -> None:
    """The old path: exchange in `jwt`, `delete token.celmisToken` on error,
    and Auth.js redirected to the callbackUrl as if all was well."""
    code = _code(WEB / "auth.ts")
    jwt_cb = _block(code, "jwt: async")
    assert "exchangeIdToken(" not in jwt_cb
    assert "delete token.celmisToken" not in jwt_cb


def test_the_login_page_is_reachable_signed_out() -> None:
    """The redirect target must not itself bounce a signed-out visitor."""
    proxy = _code(WEB / "proxy.ts")
    assert re.search(r'AUTH_PATHS = new Set\(\[\s*"/login"', proxy)


def test_the_error_toast_is_one_toast() -> None:
    form = _code(WEB / "app" / "login" / "login-form.tsx")
    assert re.search(
        r'toast\.error\(t\("auth\.sso\.failed"\),\s*\{\s*id:\s*SSO_ERROR_TOAST_ID\s*\}\)',
        form)
    assert re.search(r'const SSO_ERROR_TOAST_ID = "[^"]+"', form)


def test_the_login_page_forgets_the_agent_session() -> None:
    """Reached after an expiry too, where no sign-out button ran."""
    form = _code(WEB / "app" / "login" / "login-form.tsx")
    assert re.search(r"useEffect\(\(\) => \{\s*forgetAgentSession\(\);\s*\}, \[\]\)", form)


def test_accepting_an_invite_switches_workspace_like_the_switcher() -> None:
    # The client half of the invite page (page.tsx is its server wrapper).
    page = _code(WEB / "app" / "invite" / "[token]" / "invite-view.tsx")
    accept = _block(page, "const accept = async")
    cookie = accept.index("x-workspace=")
    forget = accept.index("forgetAgentSession();")
    reload = accept.index('window.location.assign("/dashboard")')
    assert cookie < forget < reload
    assert "router.push" not in accept
