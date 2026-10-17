"""A full installation says so, in the person's language, where they are.

The contract (src/users/seats.py is the rule; this is what the web does with
it):

  * a sign-in that would add a person beyond a seat limit is answered with a
    code; auth.ts carries the CODE to /login (a redirect cannot carry a
    sentence) and the form says it in the viewer's language, as a notice that
    stays and a toast;
  * the codes the web knows are exactly the codes the API sends;
  * a password sign-up refused for want of a seat is told apart from a taken
    address (a CredentialsSignin with the code), and a log-in is never refused;
  * the refusal the person turned away reads names no head-count: the messages
    carry no placeholder at all;
  * /admin/health shows used and limit for seats and, in a build that has
    single sign-on, for single sign-on; "unlimited" when a licence has no
    limit, and the community rule when there is none;
  * the workspace invite form warns when no free seat is left, because then only
    people who already have an account can join;
  * the users page switches an account off and on, after asking, and says in
    the viewer's language when there is no seat to switch one on;
  * the new strings exist in all sixteen languages with their placeholders; the
    two the product owner reads (English, Ukrainian) are real text.

There is no TS test runner in web/, so the source is read, with comments
removed — a test must not pass on the explanation a file gives of its rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.users import seats

WEB = Path(__file__).resolve().parents[2] / "web"
MESSAGES = WEB / "lib" / "i18n" / "messages"

NEW_KEYS = {
    "auth.seatLimit.seats": set(),
    "auth.seatLimit.sso": set(),
    "seats.title": set(),
    "seats.seatsLabel": set(),
    "seats.ssoLabel": set(),
    "seats.usedOfLimit": {"used", "limit"},
    "seats.usedUnlimited": {"used"},
    "seats.communityNote": {"seats"},
    "seats.overNote": set(),
    "seats.fullNote": set(),
    "seats.loadFailed": set(),
    "invite.noFreeSeat": set(),
    "admin.users.deactivate": set(),
    "admin.users.reactivate": set(),
    "admin.users.deactivateConfirm": {"email"},
    "admin.users.reactivateConfirm": {"email"},
    "admin.users.deactivated": set(),
    "admin.users.reactivated": set(),
    "admin.users.activeHint": set(),
    "admin.users.inactiveHint": set(),
    "admin.users.noFreeSeat": set(),
}


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


def _messages(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))


# ─── the codes ───────────────────────────────────────────────────────


def test_the_web_knows_exactly_the_codes_the_api_sends() -> None:
    lib = _code(WEB / "lib" / "seats.ts")
    m = re.search(r"SEAT_LIMIT_CODES = \[([^\]]*)\] as const", lib)
    assert m, "lib/seats.ts no longer lists the refusal codes"
    assert set(re.findall(r'"([a-z_]+)"', m.group(1))) == {seats.CODE_SEATS, seats.CODE_SSO_SEATS}


def test_every_code_has_a_message_in_every_language() -> None:
    lib = _code(WEB / "lib" / "seats.ts")
    keys = dict(re.findall(r'(\w+): "([\w.]+)"', lib.split("SEAT_LIMIT_MESSAGE_KEY")[1]))
    assert set(keys) == {seats.CODE_SEATS, seats.CODE_SSO_SEATS}
    for locale in [p.stem for p in MESSAGES.glob("*.json")]:
        for key in keys.values():
            assert _messages(locale).get(key), f"{locale} has no {key}"


# ─── the sign-in ─────────────────────────────────────────────────────


def test_a_refused_sign_up_carries_its_code_and_is_not_a_taken_address() -> None:
    auth = _code(WEB / "auth.ts")
    assert re.search(r"class SeatLimitSignIn extends CredentialsSignin", auth)
    assert re.search(r"this\.code = code;", auth)
    authorize = auth[auth.index("authorize: async"):auth.index("process.env.GOOGLE_CLIENT_ID &&")]
    assert re.search(r"seatLimitCodeOf\(err\.body\)", authorize)
    assert re.search(r"throw new SeatLimitSignIn\(full\)", authorize)
    # A wrong password stays a plain failure.
    assert re.search(r"return null;", authorize)


def test_a_refused_oauth_sign_in_redirects_with_the_code() -> None:
    auth = _code(WEB / "auth.ts")
    assert re.search(r"return `/login\?error=\$\{code\}`;", auth)
    sign_in = auth[auth.index("signIn: async"):auth.index("jwt: async")]
    assert re.search(r"seatLimitCodeOf\(err\.body\)", sign_in)
    assert re.search(r"full \? seatLimitUrl\(full\) : SSO_REJECTED_URL", sign_in)


def test_the_login_form_says_it_as_a_notice_and_a_toast() -> None:
    form = _code(WEB / "app" / "login" / "login-form.tsx")
    assert 'from "@/lib/seats"' in form
    # From the redirect (?error=<code>) and from the sign-up form's answer.
    assert re.search(r"isSeatLimitCode\(authError\)", form)
    assert re.search(r"isSeatLimitCode\(res\.code\)", form)
    assert re.search(r"toast\.error\(t\(SEAT_LIMIT_MESSAGE_KEY\[res\.code\]\)\)", form)
    # A notice that stays, announced to assistive technology.
    assert re.search(r'role="alert"[^<]*<Callout tone="warning">\{t\(SEAT_LIMIT_MESSAGE_KEY\[seatLimit\]\)', form)
    # Not a generic "single sign-on failed" for a full installation.
    assert re.search(r"if \(isSeatLimitCode\(authError\)\)", form)


def test_a_log_in_is_never_told_the_installation_is_full() -> None:
    """Logging in adds nobody. Only the sign-up path may carry the code, and
    the API sends it only for a new account; the form has no 'full' branch for
    an ordinary wrong password."""
    form = _code(WEB / "app" / "login" / "login-form.tsx")
    wrong = re.search(
        r'mode === "signup" \? t\("login\.signupFailed"\) : t\("login\.invalidCredentials"\)', form)
    assert wrong, "the ordinary failure message changed"


# ─── the admin card and the invite form ──────────────────────────────


def test_the_health_page_shows_the_seats_card_to_admins() -> None:
    page = _code(WEB / "app" / "(app)" / "admin" / "health" / "page.tsx")
    assert "<SeatsCard />" in page
    assert "<AdminGate>" in page, "the seats card must stay behind the admin gate"


def test_the_seats_card_reads_the_admin_endpoint_and_names_unlimited() -> None:
    card = _code(WEB / "components" / "seats-card.tsx")
    assert '"/api/admin/seats"' in card
    assert 't("seats.usedUnlimited"' in card and 't("seats.usedOfLimit"' in card
    assert 't("seats.communityNote", { seats: s.free_seats })' in card
    assert "!s.licensed" in card, "the community rule is shown only without a licence"
    # It counts people; it never lists them.
    assert "email" not in card


def test_the_seats_card_has_no_single_sign_on_line_in_a_build_without_it() -> None:
    card = _code(WEB / "components" / "seats-card.tsx")
    assert re.search(r"\{s\.sso_seats && <CountRow label=\{t\(\"seats\.ssoLabel\"\)\}", card)
    lib = _code(WEB / "lib" / "seats.ts")
    assert re.search(r"sso_seats: SeatCount \| null;", lib)
    assert re.search(r"free_sso_seats: number \| null;", lib)
    # The warnings look at the single sign-on count only when there is one.
    assert "anyOver(s)" in card and "anyFull(s)" in card


def test_a_refusal_shown_to_somebody_not_signed_in_names_no_headcount() -> None:
    for locale in [p.stem for p in MESSAGES.glob("*.json")]:
        for key in ("auth.seatLimit.seats", "auth.seatLimit.sso"):
            text = _messages(locale)[key]
            assert not re.search(r"\{|\d", text), f"{locale}: {key} carries a number or placeholder"


def test_the_users_page_switches_an_account_off_and_on_after_asking() -> None:
    page = _code(WEB / "app" / "(app)" / "admin" / "users" / "page.tsx")
    assert "adminUsersApi.deactivate(token!, user.id)" in page
    assert "adminUsersApi.reactivate(token!, user.id)" in page
    # Asked first, and the switch-off is the destructive-looking one.
    assert re.search(r"const ok = await confirm\(\{.*?danger: user\.is_active", page, re.S)
    assert "if (ok) toggle.mutate(!user.is_active)" in page
    # The seat card and the list are read again after a change.
    assert "queryKey: SEATS_KEY" in page and 'queryKey: ["admin-users"]' in page
    # No seat to switch on: said in the viewer's language, not the API's English.
    assert re.search(r"seatLimitCodeOf\(e\.body\)\s*\?\s*t\(\"admin\.users\.noFreeSeat\"\)", page)
    api = _code(WEB / "lib" / "api.ts")
    assert "/deactivate`" in api and "/reactivate`" in api


def test_the_invite_form_warns_when_no_seat_is_free() -> None:
    page = _code(WEB / "app" / "(app)" / "admin" / "workspaces" / "page.tsx")
    assert "invitesApi.freeSeat(" in page
    assert re.search(r"has_free_seat === false", page)
    assert re.search(r'<Callout tone="warning">\{t\("invite\.noFreeSeat"\)\}</Callout>', page)
    api = _code(WEB / "lib" / "api.ts")
    assert '"/api/invites/seats"' in api


# ─── the strings ─────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", sorted(p.stem for p in MESSAGES.glob("*.json")))
def test_the_new_strings_exist_in_every_language_with_their_placeholders(locale: str) -> None:
    msgs = _messages(locale)
    for key, placeholders in NEW_KEYS.items():
        assert key in msgs, f"{locale}: {key} missing"
        assert set(re.findall(r"\{(\w+)\}", msgs[key])) == placeholders, f"{locale}: {key}"


def test_english_and_ukrainian_are_real_text() -> None:
    en, uk = _messages("en"), _messages("uk")
    for key in NEW_KEYS:
        if key == "seats.ssoLabel":
            continue  # "SSO" is the same word in both
        assert en[key] != uk[key], f"{key} is not translated to Ukrainian"
        assert re.search("[а-яіїєґ]", uk[key].lower()), f"{key} has no Cyrillic"


def test_the_community_rule_says_what_the_product_owner_decided() -> None:
    assert _messages("en")["seats.communityNote"] == (
        "Community edition: up to {seats} people; a licence lifts the limit.")
