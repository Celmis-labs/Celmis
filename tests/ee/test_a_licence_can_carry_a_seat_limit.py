# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The `seats` and `sso_seats` claims of a licence.

The contract (src/ee/license.py, scripts/ee_mint_license.py):

  * both claims are optional positive integers; a licence WITHOUT `seats` is
    unlimited, so every licence issued before seats existed keeps working;
  * a licence never gives less than the free allowance: the limit in force is
    the larger of the claim and the community number;
  * a claim that is PRESENT but not a positive integer (zero, negative, a
    string, a float, a boolean, null, absurdly large) fails CLOSED: it is read
    as the community limit — never as unlimited — and logged at ERROR by claim
    name. The licence is still good otherwise: refusing it would take away
    what the customer paid for. The log never carries the token;
  * the summary the rest of the process sees carries both limits, never the
    token;
  * the mint script refuses the same values (an int, nothing that merely
    compares equal to one), and its ceiling is the same number the verifier
    uses;
  * applying a licence registers its limits with src/users/seats.py and the
    single sign-on allowance; removing it or letting it lapse returns the
    community limits.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest

import src.api  # noqa: F401  (the app first: src.ee.license_router imports it back)
from scripts import ee_mint_license
from src.ee import license as lic
from src.ee.sso import allowance
from src.users import seats
from tests.ee.licensing import TEST_KEY, mint_test_license, trust_test_key


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    trust_test_key(monkeypatch)


def _raw(**extra) -> str:
    """A licence signed with the test key and EXACTLY these extra claims — the
    mint script would refuse the values this file needs to try."""
    now = datetime.now(UTC)
    claims = {"iss": lic.ISSUER, "sub": "Test Customer Ltd", "features": ["sso"],
              "iat": int(now.timestamp()),
              "exp": int((now + timedelta(days=30)).timestamp()), **extra}
    return jwt.encode(claims, TEST_KEY, algorithm="EdDSA")


# ─── reading the claims ──────────────────────────────────────────────


def test_a_licence_without_the_claims_is_unlimited():
    parsed = lic.verify(mint_test_license(["sso"]))
    assert parsed.seats is None and parsed.sso_seats is None
    assert parsed.sso_allowance(5) is None


def test_a_licence_carries_the_numbers_it_was_minted_with():
    parsed = lic.verify(mint_test_license(["sso"], seats=40, sso_seats=12))
    assert (parsed.seats, parsed.sso_seats) == (40, 12)
    assert parsed.sso_allowance(5) == 12


def test_a_licence_never_gives_less_than_the_free_allowance():
    parsed = lic.verify(mint_test_license(["sso"], seats=2, sso_seats=1))
    assert (parsed.seats, parsed.sso_seats) == (seats.FREE_SEATS, allowance.FREE_SSO_SEATS)


def test_sso_seats_count_only_on_a_licence_that_grants_sso():
    parsed = lic.verify(mint_test_license(["analytics"], seats=40, sso_seats=12))
    assert parsed.sso_allowance(5) == 5


@pytest.mark.parametrize("bad", [0, -3, "5", "", 5.0, 2.5, True, False, None, [5], {"n": 5},
                                 lic.MAX_SEATS + 1, 10**12])
def test_a_claim_that_is_present_but_malformed_is_the_community_limit_not_unlimited(
        bad, caplog):
    """A mistyped limit must not be the way to unlimited seats."""
    with caplog.at_level(logging.ERROR, logger="src.ee.license"):
        parsed = lic.verify(_raw(seats=bad, sso_seats=bad))
    assert (parsed.seats, parsed.sso_seats) == (seats.FREE_SEATS, allowance.FREE_SSO_SEATS)
    assert parsed.grants("sso"), "a bad seat claim took the paid feature away"
    errors = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "claim=seats" in errors and "claim=sso_seats" in errors
    assert "eyJ" not in errors, "the log carries the token"


def test_only_an_absent_claim_is_unlimited():
    assert lic.verify(_raw()).seats is None
    assert lic.verify(_raw(seats=None)).seats == seats.FREE_SEATS


def test_the_ceiling_is_allowed_and_one_over_is_not():
    parsed = lic.verify(_raw(seats=lic.MAX_SEATS, sso_seats=lic.MAX_SEATS))
    assert (parsed.seats, parsed.sso_seats) == (lic.MAX_SEATS, lic.MAX_SEATS)
    assert lic.verify(_raw(seats=lic.MAX_SEATS + 1)).seats == seats.FREE_SEATS


def test_the_summary_carries_both_limits_and_never_the_token():
    token = mint_test_license(["sso"], seats=40, sso_seats=12)
    summary = lic.verify(token).summary()
    assert summary["seats"] == 40 and summary["sso_seats"] == 12
    assert token not in repr(summary)
    unlimited = lic.verify(mint_test_license(["sso"])).summary()
    assert unlimited["seats"] is None and unlimited["sso_seats"] is None


# ─── the mint script ─────────────────────────────────────────────────


def test_the_mint_script_and_the_verifier_agree_on_the_ceiling():
    assert ee_mint_license.MAX_SEATS == lic.MAX_SEATS


@pytest.mark.parametrize("name", ["seats", "sso_seats"])
@pytest.mark.parametrize("bad", [0, -1, True, False, 5.0, 5.5, "5", lic.MAX_SEATS + 1])
def test_the_mint_script_refuses_what_the_verifier_would_not_read_as_a_number(name, bad):
    with pytest.raises(ValueError, match=name.replace("_", "-")):
        ee_mint_license.mint(TEST_KEY, customer="Acme", features=["sso"], days=30,
                             **{name: bad})


def test_the_mint_script_adds_the_claims_only_when_asked():
    plain = jwt.decode(ee_mint_license.mint(TEST_KEY, customer="Acme", features=["sso"],
                                            days=30),
                       options={"verify_signature": False})
    assert "seats" not in plain and "sso_seats" not in plain
    with_seats = jwt.decode(ee_mint_license.mint(TEST_KEY, customer="Acme", features=["sso"],
                                                 days=30, seats=9, sso_seats=4),
                            options={"verify_signature": False})
    assert (with_seats["seats"], with_seats["sso_seats"]) == (9, 4)


def test_the_mint_command_line_takes_the_two_flags(tmp_path, capsys):
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = tmp_path / "k.pem"
    key.write_bytes(TEST_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    code = ee_mint_license.main(["--customer", "Acme", "--features", "sso", "--key", str(key),
                                 "--seats", "25", "--sso-seats", "10"])
    token = capsys.readouterr().out.strip()
    assert code == 0
    parsed = lic.verify(token)
    assert (parsed.seats, parsed.sso_seats) == (25, 10)

    assert ee_mint_license.main(["--customer", "Acme", "--features", "sso", "--key", str(key),
                                 "--seats", "0"]) == 2


# ─── applying and removing a licence ─────────────────────────────────


def _loaded(token: str | None):
    return lic.Loaded(license=lic.verify(token) if token else None,
                      source="env_key" if token else None)


def test_applying_a_licence_registers_its_limits_and_removing_it_clears_them():
    from fastapi import FastAPI

    from src.ee import apply_license

    app = FastAPI()
    apply_license(app, _loaded(mint_test_license(["sso"], seats=7, sso_seats=8)))
    now = seats.current_limits()
    assert (now.seats, now.licensed) == (7, True)
    assert allowance.effective() == (8, True)

    apply_license(app, _loaded(None))
    assert seats.current_limits() == seats.COMMUNITY_LIMITS
    assert allowance.effective() == (allowance.FREE_SSO_SEATS, False)


def test_a_licence_that_grants_nothing_about_seats_registers_unlimited_seats():
    from fastapi import FastAPI

    from src.ee import apply_license

    apply_license(FastAPI(), _loaded(mint_test_license(["analytics"])))
    now = seats.current_limits()
    assert now.seats is None, "a licence issued before seats existed must stay unlimited"
    assert allowance.effective() == (allowance.FREE_SSO_SEATS, False), \
        "analytics does not grant more single sign-on"


def test_a_licence_that_expires_while_the_process_runs_returns_the_community_limits():
    from fastapi import FastAPI

    from src.ee import apply_license

    apply_license(FastAPI(), _loaded(mint_test_license(["sso"], days=1)))
    assert seats.current_limits().seats is None
    assert allowance.effective() == (None, True)
    after = datetime.now(UTC) + timedelta(days=2)
    assert seats.current_limits(after) == seats.COMMUNITY_LIMITS
    assert allowance.effective(after) == (allowance.FREE_SSO_SEATS, False)


def test_an_unlicensed_start_has_the_community_limits():
    from fastapi import FastAPI

    from src.ee import apply_license

    apply_license(FastAPI(), _loaded(None))
    assert seats.current_limits() == seats.COMMUNITY_LIMITS
    assert allowance.effective() == (allowance.FREE_SSO_SEATS, False)


def test_a_process_without_an_app_publishes_the_configured_licence(tmp_path, monkeypatch):
    """The command line adds people but mounts nothing: it asks for the
    configured licence's limits through `publish_seat_limits`."""
    from src.ee import publish_seat_limits
    from tests.ee.licensing import isolate_license_store

    isolate_license_store(monkeypatch, tmp_path)
    env = {lic.ENV_KEY: mint_test_license(["sso"], seats=9)}
    assert publish_seat_limits(env) is not None
    assert (seats.current_limits().seats, seats.current_limits().licensed) == (9, True)
    assert allowance.effective() == (None, True)

    assert publish_seat_limits({}) is None
    assert seats.current_limits() == seats.COMMUNITY_LIMITS
