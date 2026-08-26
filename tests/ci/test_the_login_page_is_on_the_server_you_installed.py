"""A fresh install must not send its users to the installer's own laptop.

Pressing anything on a brand-new installation at http://your-server/ answered
`307 → http://localhost:3000/login`. The API was healthy, every page was
served, and the site was unusable: the browser was told to go find the login
page on the machine of whoever was looking at it.

The cause is one line in `.env.example`, copied verbatim into every `.env` by
`init-env.sh`:

    NEXTAUTH_URL=http://localhost:3000

`web/auth.ts` already sets `trustHost: true`, which exists precisely so a
self-hosted app derives its own address from the request it is answering. An
explicitly-set NEXTAUTH_URL overrides it. So the example file defeated the
setting that was put there to solve this, and the compose file re-applied the
same default even for an operator who deleted the line.

THE RULE these tests hold: a shipped example must not answer a question only
the installation can answer. The address a user reaches you at is that kind of
question, and every one of these variables already has a correct answer that
derives from the request — a relative `/backend`, a trusted Host header. A
wrong hard-coded value is worse than no value, because no value falls through
to the working default and a wrong one silently wins over it.

The defect hid because it only fires when logged out. Every screenshot in
every earlier run was taken with a session cookie already set.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

#: Variables that name an address the installation is reached at. Each has a
#: correct derive-from-the-request default in the code; a value here overrides
#: it. CELMIS_CORS_ORIGINS is deliberately absent: it names *other* origins
#: allowed to call the API, which is not the same question.
ADDRESS_VARS = ("NEXTAUTH_URL", "NEXT_PUBLIC_API_BASE", "PUBLIC_BASE_URL")

#: A machine only the installer has.
OWN_MACHINE = re.compile(r"\blocalhost\b|\b127\.0\.0\.1\b|\b0\.0\.0\.0\b")


def _assignments(text: str) -> dict[str, str]:
    """The variables a dotenv file actually sets.

    Commented-out lines are documentation, not configuration, and dotenv
    files have no inline comments — so a `#` only counts at the start.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


@pytest.fixture(scope="module")
def example() -> dict[str, str]:
    return _assignments((ROOT / ".env.example").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


# ── the example file ────────────────────────────────────────────────

@pytest.mark.parametrize("var", ADDRESS_VARS)
def test_the_example_does_not_hand_out_the_installers_own_machine(example, var):
    value = example.get(var)
    if value is None:
        return  # unset is the whole point — the code default applies
    assert not OWN_MACHINE.search(value), (
        f"{var}={value} in .env.example. init-env.sh copies this file verbatim, "
        f"so every installation is configured to point at whoever installed it."
    )


def test_the_example_still_mentions_them(example):
    """Removing the value must not remove the knob.

    An operator with a fixed public URL needs to know these exist; deleting
    the lines outright trades one silent failure for another.
    """
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ADDRESS_VARS:
        assert var in text, f"{var} vanished from .env.example instead of being emptied"


def test_the_example_says_what_happens_when_they_are_unset(example):
    """...and says it where the reader is standing when the question arises.

    Scoped to the address section rather than the whole file: the word
    appearing somewhere in 400 lines of dotenv is not the reader being told.
    """
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines)
              if ln.startswith("#") and "Addresses" in ln]
    assert starts, "the address block lost its heading"
    start = starts[0]
    ends = [i for i in range(start + 1, len(lines))
            if lines[i].startswith("# \u2500\u2500")]
    body = "\n".join(lines[start:ends[0] if ends else len(lines)]).lower()
    assert "unset" in body or "blank" in body, (
        "the address section never tells the reader that leaving these empty "
        "is the choice that works"
    )


def test_the_one_case_that_does_need_a_fixed_address_is_named(example):
    """Blank is right for sign-in by password and wrong for Google.

    Google only redirects back to a URI registered with it in advance, so
    there the address is fixed by definition. An operator who reads "leave it
    unset" and enables Google gets a login that dead-ends; the exception has
    to sit next to the rule, not in another file.
    """
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    if "GOOGLE_CLIENT_ID" not in text:
        pytest.skip("no Google provider shipped")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("#") and "Addresses" in ln)
    ends = [i for i in range(start + 1, len(lines))
            if lines[i].startswith("# \u2500\u2500")]
    body = "\n".join(lines[start:ends[0] if ends else len(lines)]).lower()
    assert "google" in body, (
        "the address section says to leave these blank without naming the "
        "provider for which blank does not work"
    )


# ── the compose file ────────────────────────────────────────────────

def _env_block(compose: dict, service: str) -> dict:
    return compose["services"][service].get("environment") or {}


@pytest.mark.parametrize("service,var", [("web", "NEXTAUTH_URL"),
                                         ("web", "NEXT_PUBLIC_API_BASE"),
                                         ("api", "PUBLIC_BASE_URL")])
def test_compose_does_not_re_apply_the_default_itself(compose, service, var):
    """Emptying .env.example is not enough while compose hard-codes the same value.

    `VAR: "${VAR:-http://localhost:3000}"` sets the variable in the container
    even for an operator who deleted the line — the default is applied by the
    file they did not edit.
    """
    block = _env_block(compose, service)
    if var not in block:
        return
    value = block[var]
    if value is None:
        return  # pass-through: set only when the operator sets it
    assert not OWN_MACHINE.search(str(value)), (
        f"docker-compose.yml sets {service}.{var}={value!r}; an operator who "
        f"clears their .env still gets this."
    )


def test_nextauth_url_is_pass_through_not_defaulted(compose):
    """The specific line that broke the install.

    A null value in a compose `environment:` mapping means *pass the variable
    through when it is defined, and do not set it at all when it is not* —
    verified against docker compose with --env-file, which is what the deploy
    script uses. That is the only form which lets trustHost do its job while
    still honouring an operator's explicit choice.
    """
    block = _env_block(compose, "web")
    assert "NEXTAUTH_URL" in block, "the knob must remain reachable"
    assert block["NEXTAUTH_URL"] is None, (
        f"expected pass-through (a bare `NEXTAUTH_URL:`), got {block['NEXTAUTH_URL']!r}"
    )


# ── the setting the fix depends on ──────────────────────────────────

def test_auth_still_trusts_the_host_it_was_asked_at():
    """The compose fix is only correct while this is true.

    Unsetting NEXTAUTH_URL without trustHost leaves NextAuth with no base URL
    at all. These two changes are one change.
    """
    auth = (ROOT / "web" / "auth.ts").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in auth.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    assert re.search(r"trustHost\s*:\s*true", body), (
        "web/auth.ts no longer trusts the request host; clearing NEXTAUTH_URL "
        "would leave the app with no way to know its own address"
    )


def test_the_browser_bundle_still_falls_back_to_a_relative_path():
    """The other half of one-image-serves-any-host.

    An absolute API base baked into the client bundle points every visitor's
    browser at their own machine. This fallback is what the empty
    NEXT_PUBLIC_API_BASE lands on.
    """
    api_ts = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in api_ts.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    assert '"/backend"' in body, "the relative default is gone from web/lib/api.ts"


# ── the shape of the failure, stated once ───────────────────────────

def test_no_service_hands_a_browser_an_address_only_the_server_knows(compose):
    """A sweep, so the next variable of this kind is caught on arrival.

    Anything named NEXT_PUBLIC_* is substituted into the client bundle and is
    read by a browser that is not on this machine.
    """
    offenders = []
    for name, svc in (compose.get("services") or {}).items():
        for var, value in (svc.get("environment") or {}).items():
            if not var.startswith("NEXT_PUBLIC_") or value is None:
                continue
            if OWN_MACHINE.search(str(value)):
                offenders.append(f"{name}.{var}={value}")
    assert not offenders, (
        "these reach a browser somewhere else and name this machine: "
        + ", ".join(offenders)
    )
