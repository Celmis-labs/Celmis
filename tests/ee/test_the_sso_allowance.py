# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Single sign-on without a licence: five people, and a licence lifts it.

The contract (LICENSE_EE section 2, src/ee/sso/allowance.py, src/ee/__init__.py):

  * without a licence, `POST /api/auth/oidc` is there and works for at most
    five people; the sixth gets a 403 `sso_seat_limit_reached`;
  * a person is added only if the installation also has a free SEAT, and that
    refusal (`seat_limit_reached`) comes first;
  * binding an existing local account to single sign-on takes one place in the
    allowance and no new seat; refused (nothing written) when they are used up;
  * a licence that grants `sso` lifts the limit: unlimited without `sso_seats`,
    N with it (never below five). A licence that does not grant `sso` leaves
    the allowance;
  * nobody is ever switched off: people who signed in before keep signing in
    when the licence is removed or lapses, over the limit as they may be;
  * the refusal names the limit and never the head-count or the person, and
    writes one audit row that does carry the counts;
  * the check is in a covered file (src/ee/sso/allowance.py), reached by every
    flow that makes an account an SSO person — including reactivating one — and
    it re-reads the account under the lock, so a stale copy is never written
    back.

The identity provider is stubbed (its token checks are tested in
tests/ee/test_oidc_sign_in.py): what is under test is what happens AFTER it
has spoken.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ee import license as lic
from src.ee.sso import allowance, oidc
from src.users import User, UserAuthMethod, seats
from tests.ee.licensing import isolate_license_store, mint_test_license, trust_test_key

ISSUER = "https://sso.example.com/realms/celmis"


@pytest.fixture
def users(tmp_path):
    from src.users import UserStore

    return UserStore(tmp_path / "users.db")


@pytest.fixture
def audit(monkeypatch):
    rows: list[dict] = []
    from src.api.routers import auth as auth_router
    from src.ee.sso import router as sso_router

    monkeypatch.setattr(auth_router, "record_action", lambda **k: rows.append(k))
    monkeypatch.setattr(sso_router, "record_action", lambda **k: rows.append(k))
    return rows


def _idp(monkeypatch):
    """The identity provider says whatever `says[...]` holds."""
    cfg = oidc.OidcConfig(issuer=ISSUER, client_id="celmis-web")
    says: dict = {}
    monkeypatch.setattr(oidc, "oidc_config", lambda: cfg)
    monkeypatch.setattr(oidc, "verify_id_token", lambda _t, _c: dict(says))
    return says


def _person_says(says: dict, n: int, *, email: str | None = None, verified: bool = True) -> None:
    says.clear()
    says.update({"sub": f"kc-{n}", "email": email or f"person{n}@example.com",
                 "email_verified": verified, "name": f"Person {n}"})


def _app(users, monkeypatch, tmp_path, *, features=("sso",), **claims):
    from src.api.deps import get_users
    from src.api.routers import auth as auth_router
    from src.ee import mount_enterprise

    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace", lambda *a, **k: None)
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)
    trust_test_key(monkeypatch)
    isolate_license_store(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(auth_router.router)
    env = {lic.ENV_KEY: mint_test_license(features, **claims)} if features else {}
    mount_enterprise(app, env=env)
    app.dependency_overrides[get_users] = lambda: users
    return app


def _sign_in(client, says, n: int, **kw):
    _person_says(says, n, **kw)
    return client.post("/api/auth/oidc", json={"id_token": "x"})


@pytest.fixture
def idp(monkeypatch):
    return _idp(monkeypatch)


def _local(users, n: int) -> User:
    user = User(id=f"local-{n}", email=f"person{n}@example.com",
                auth_method=UserAuthMethod.PASSWORD)
    users.create(user)
    return user


# ─── without a licence ───────────────────────────────────────────────


def test_without_a_licence_five_people_sign_in_through_sso_and_the_sixth_is_refused(
        users, idp, monkeypatch, tmp_path, audit):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n}"
    audit.clear()

    refused = _sign_in(c, idp, 5)

    assert refused.status_code == 403
    # Five people, five seats: the seat limit is reported first.
    assert refused.json()["detail"]["code"] == "seat_limit_reached"
    assert users.get_by_email("person5@example.com") is None
    [row] = [r for r in audit if r["action"] == "auth.seat_limit_refused"]
    assert row["target"] == "sso" and "person5" not in repr(row)


def test_the_sso_allowance_is_refused_with_its_own_code_when_seats_are_free(
        users, idp, monkeypatch, tmp_path):
    """Seats lifted by a licence that does not grant sso: five SSO people use
    up the allowance, a sixth is refused with the SSO code."""
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("analytics",), seats=20))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200
    refused = _sign_in(c, idp, 5)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "code": "sso_seat_limit_reached",
        "message": "Single sign-on is limited to 5 people without a licence.",
    }
    assert users.get_by_email("person5@example.com") is None
    assert seats.used_seats(users) == 5


def test_people_who_signed_in_before_keep_signing_in_at_the_limit(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200
    assert _sign_in(c, idp, 5).status_code == 403
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n} was locked out"


# ─── seats and the allowance together ────────────────────────────────


def test_a_full_installation_refuses_a_new_sso_person_with_the_seat_code(
        users, idp, monkeypatch, tmp_path):
    for n in range(5):
        _local(users, n)
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    refused = _sign_in(c, idp, 9)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "seat_limit_reached"


def test_the_master_address_cannot_use_sso_and_takes_no_seat(
        users, idp, monkeypatch, tmp_path):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    r = _sign_in(c, idp, 1, email="root@example.com")
    assert r.status_code == 403
    assert r.json()["detail"] == "This account cannot use single sign-on"
    assert seats.used_seats(users) == 0


# ─── binding an existing local account ───────────────────────────────


def test_binding_a_local_account_takes_an_sso_seat_and_no_new_seat(
        users, idp, monkeypatch, tmp_path):
    for n in range(5):
        _local(users, n)
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    r = _sign_in(c, idp, 2)  # same address as local-2, verified
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-2").id == "local-2"
    assert seats.used_seats(users) == 5 and allowance.used(users) == 1


def test_binding_is_refused_when_the_allowance_is_used_up_and_changes_nothing(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("analytics",), seats=20))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200
    _local(users, 8)

    refused = _sign_in(c, idp, 8)

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "sso_seat_limit_reached"
    assert users.get_by_oidc(ISSUER, "kc-8") is None
    assert users.get_by_id("local-8").oidc_sub is None
    assert users.get_by_id("local-8").auth_method == UserAuthMethod.PASSWORD


# ─── what a licence changes ──────────────────────────────────────────


def test_a_licence_that_grants_sso_without_a_limit_is_unlimited(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("sso",)))
    for n in range(9):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n}"
    assert allowance.used(users) == 9


def test_a_licence_with_sso_seats_sets_that_limit_and_says_it_is_the_licence(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("sso",), sso_seats=6))
    for n in range(6):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n}"
    refused = _sign_in(c, idp, 6)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "code": "sso_seat_limit_reached",
        "message": "Single sign-on is limited to 6 people by this installation's licence.",
    }


def test_a_licence_never_gives_single_sign_on_fewer_people_than_the_free_allowance(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("sso",), sso_seats=2))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n}"
    assert _sign_in(c, idp, 5).status_code == 403


def test_a_licence_with_seats_limits_the_installation_to_them(
        users, idp, monkeypatch, tmp_path):
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("sso",), seats=7))
    for n in range(7):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n}"
    refused = _sign_in(c, idp, 7)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "code": "seat_limit_reached",
        "message": "This Celmis has reached its limit of 7 people. "
                   "An administrator can add a licence or deactivate an account.",
    }


def test_a_licence_for_other_features_leaves_the_sso_allowance_where_it_was(
        users, idp, monkeypatch, tmp_path):
    """`sso_seats` on a licence that does not grant sso is not a grant of sso."""
    c = TestClient(_app(users, monkeypatch, tmp_path, features=("analytics",),
                        seats=50, sso_seats=40))
    for n in range(5):
        assert _sign_in(c, idp, n).status_code == 200
    assert _sign_in(c, idp, 5).status_code == 403


# ─── nobody is switched off ──────────────────────────────────────────


def test_removing_the_licence_returns_to_five_and_signs_nobody_out(
        users, idp, monkeypatch, tmp_path):
    from src.ee import reload_license, runtime

    app = _app(users, monkeypatch, tmp_path, features=("sso",))
    c = TestClient(app)
    for n in range(8):
        assert _sign_in(c, idp, n).status_code == 200

    runtime(app).env = {}
    reload_license(app)
    assert seats.current_limits() == seats.COMMUNITY_LIMITS

    for n in range(8):
        assert _sign_in(c, idp, n).status_code == 200, f"person {n} was locked out"
    assert _sign_in(c, idp, 8).status_code == 403
    assert all(u.is_active for u in users.list(active_only=False))


def test_applying_a_licence_registers_its_limits_and_removing_it_clears_them(
        users, monkeypatch, tmp_path):
    from src.ee import reload_license, runtime

    app = _app(users, monkeypatch, tmp_path, features=("sso",), seats=7, sso_seats=3)
    limits = seats.current_limits()
    assert (limits.seats, limits.licensed) == (7, True)
    assert allowance.effective() == (5, True), "3 is below the free allowance of 5"

    runtime(app).env = {}
    reload_license(app)
    assert seats.current_limits() == seats.COMMUNITY_LIMITS
    assert allowance.effective() == (allowance.FREE_SSO_SEATS, False)


# ─── the check is a covered file's, and every door asks it ───────────


def test_the_single_sign_on_count_and_refusal_live_in_a_covered_file():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    covered = (root / "src/ee/sso/allowance.py").read_text(encoding="utf-8")
    assert covered.startswith("# Celmis Enterprise Edition. Licensed under LICENSE_EE")
    assert "FREE_SSO_SEATS = 5" in covered and "SeatLimitReached(\"sso_seats\"" in covered
    agpl = (root / "src/users/seats.py").read_text(encoding="utf-8")
    for leaked in ("FREE_SSO_SEATS", "def used_sso_seats", "def bind_sso",
                   "def check_can_use_sso", "sso_seats="):
        assert leaked not in agpl, f"{leaked} is back in the AGPL seat code"


def test_reactivating_an_sso_account_asks_the_allowance_too(users, monkeypatch, tmp_path):
    """A second door: switching a deactivated single-sign-on person on again
    would otherwise pass the limit that signing in new people may not."""
    _app(users, monkeypatch, tmp_path, features=("analytics",), seats=20)
    for n in range(5):
        users.create(User(id=f"s-{n}", email=f"person{n}@example.com",
                          auth_method=UserAuthMethod.OIDC, oidc_iss=ISSUER, oidc_sub=f"kc-{n}"))
    users.create(User(id="s-9", email="person9@example.com", auth_method=UserAuthMethod.OIDC,
                      oidc_iss=ISSUER, oidc_sub="kc-9", is_active=False))

    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.reactivate_person(users, users.get_by_id("s-9"))
    assert refused.value.kind == "sso_seats"
    assert users.get_by_id("s-9").is_active is False

    assert seats.deactivate_person(users, "s-0")
    assert seats.reactivate_person(users, users.get_by_id("s-9")).is_active
    assert allowance.used(users) == 5


def test_a_licence_that_lapses_returns_the_allowance_without_switching_anybody_off(
        users, monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    _app(users, monkeypatch, tmp_path, features=("sso",))
    for n in range(8):
        users.create(User(id=f"s-{n}", email=f"person{n}@example.com",
                          auth_method=UserAuthMethod.OIDC, oidc_iss=ISSUER, oidc_sub=f"kc-{n}"))
    assert allowance.effective() == (None, True)
    later = datetime.now(UTC) + timedelta(days=400)
    assert allowance.effective(later) == (5, False)
    assert allowance.used(users) == 8 and all(u.is_active for u in users.list())


def test_a_person_is_bound_by_a_fresh_read_and_only_the_identity_columns_are_written(
        users, monkeypatch, tmp_path):
    _app(users, monkeypatch, tmp_path, features=("sso",))
    _local(users, 1)
    # The caller read the account; before the bind runs, somebody renames it.
    renamed = users.get_by_id("local-1")
    renamed.name = "Renamed meanwhile"
    users.update(renamed)

    bound = allowance.bind(users, "local-1", issuer=ISSUER, subject="kc-1")

    row = users.get_by_id("local-1")
    assert (row.oidc_iss, row.oidc_sub) == (ISSUER, "kc-1")
    assert row.name == "Renamed meanwhile", "the bind wrote back a stale copy of the row"
    assert bound.name == "Renamed meanwhile"


def test_an_account_switched_off_or_erased_before_the_bind_is_not_put_back(
        users, monkeypatch, tmp_path):
    """The stale-write window: the route read the account, then it was erased
    (GDPR erase deactivates it, renames it and clears its identity)."""
    _app(users, monkeypatch, tmp_path, features=("sso",))
    _local(users, 1)
    gone = users.get_by_id("local-1")
    gone.is_active = False
    gone.email = "deleted-local-1@erased.local"
    users.update(gone)

    with pytest.raises(allowance.AccountUnavailable):
        allowance.bind(users, "local-1", issuer=ISSUER, subject="kc-1")
    row = users.get_by_id("local-1")
    assert row.is_active is False and row.email == "deleted-local-1@erased.local"
    assert row.oidc_sub is None

    users.delete("local-1")
    with pytest.raises(allowance.AccountUnavailable):
        allowance.bind(users, "local-1", issuer=ISSUER, subject="kc-1")


def test_an_account_bound_by_a_concurrent_login_keeps_the_first_identity(
        users, monkeypatch, tmp_path):
    """Two first sign-ins for one local account with different subjects: the
    second sees the account already bound and does not overwrite the first."""
    _app(users, monkeypatch, tmp_path, features=("sso",))
    _local(users, 1)
    allowance.bind(users, "local-1", issuer=ISSUER, subject="kc-first")

    with pytest.raises(allowance.AlreadyBound):
        allowance.bind(users, "local-1", issuer=ISSUER, subject="kc-second")
    assert users.get_by_id("local-1").oidc_sub == "kc-first"


def test_a_bind_that_took_the_count_over_the_limit_is_undone_without_touching_the_rest(
        users, monkeypatch, tmp_path):
    _app(users, monkeypatch, tmp_path, features=("analytics",), seats=20)
    for n in range(4):
        users.create(User(id=f"s-{n}", email=f"s{n}@example.com", auth_method=UserAuthMethod.OIDC,
                          oidc_iss=ISSUER, oidc_sub=f"kc-s{n}"))
    _local(users, 8)
    real_admit = allowance.admit
    state = {"calls": 0}

    def admit_then_somebody_else_binds(store, user, written):
        real_admit(store, user, written)
        state["calls"] += 1
        if state["calls"] == 1:  # between the check and the write, in another process
            users.create(User(id="s-9", email="s9@example.com", auth_method=UserAuthMethod.OIDC,
                              oidc_iss=ISSUER, oidc_sub="kc-s9"))

    monkeypatch.setattr(allowance, "admit", admit_then_somebody_else_binds)
    with pytest.raises(seats.SeatLimitReached) as refused:
        allowance.bind(users, "local-8", issuer=ISSUER, subject="kc-8")
    assert refused.value.kind == "sso_seats"
    row = users.get_by_id("local-8")
    assert row.oidc_sub is None and row.is_active and row.email == "person8@example.com"
    assert allowance.used(users) == 5


def test_a_platform_account_cannot_be_bound_to_single_sign_on(users):
    users.create(User(id="default", email="default@local", is_admin=True))
    with pytest.raises(ValueError):
        allowance.bind(users, "default", issuer=ISSUER, subject="x")


# ─── a switched-off person ───────────────────────────────────────────


def test_a_deactivated_sso_person_is_refused_at_the_callback_until_switched_on_again(
        users, idp, monkeypatch, tmp_path):
    """The administrator's deactivate button ends single sign-on too: the
    identity provider still vouches for the person, Celmis does not let them in."""
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))
    assert _sign_in(c, idp, 1).status_code == 200
    person = users.get_by_email("person1@example.com")

    assert seats.deactivate_person(users, person.id) is True
    refused = _sign_in(c, idp, 1)
    assert refused.status_code == 403 and refused.json()["detail"] == "Account is disabled"
    assert "access_token" not in refused.text

    seats.reactivate_person(users, users.get_by_id(person.id))
    assert _sign_in(c, idp, 1).status_code == 200


def test_a_deactivated_account_is_not_bound_to_single_sign_on_by_a_stranger_with_its_address(
        users, idp, monkeypatch, tmp_path):
    _local(users, 3)
    seats.deactivate_person(users, "local-3")
    c = TestClient(_app(users, monkeypatch, tmp_path, features=()))

    refused = _sign_in(c, idp, 3)

    assert refused.status_code == 403 and refused.json()["detail"] == "Account is disabled"
    assert users.get_by_id("local-3").oidc_sub is None
