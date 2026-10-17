"""Seats (src/users/seats.py): who counts, and what a limit refuses.

The contract, from the product owner:

  * without a licence an installation holds at most five people; a licence
    lifts that (`seats`), and a licence without the claim is unlimited;
  * a limit refuses ADDING a person and nothing else. An installation already
    above it keeps every account working; deactivating one frees a seat;
  * the master (break-glass) account and the seeded local user are the
    platform's, not people: they take no seat and are never refused;
  * a refusal names the limit and never the head-count or the person: it is
    shown to somebody who is not signed in. The exact numbers are on the
    exception (for the audit row) and on /api/admin/seats;
  * counting is one SQL COUNT, and it counts the same people the Python
    predicate (`is_platform_account`) leaves in;
  * changing an existing account writes the one column that changes, and only
    if the row is still as it was read: a stale copy is never written back.

The single sign-on allowance is the enterprise code's
(tests/ee/test_the_sso_allowance.py); this file covers the hook it plugs
into. The flows that call this module are tested where they live (the sign-up,
the Google sign-in, the CLI: tests/api/test_a_community_installation_holds_
five_people.py).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from src.users import User, UserAuthMethod, UserNotFoundError, UserStore, seats

ISSUER = "https://sso.example.com/realms/celmis"


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.db")


@pytest.fixture(autouse=True)
def _generic_seats_only():
    """This file is about the generic count. The single sign-on allowance
    registers itself when the enterprise code is imported (anywhere in the
    session); set it aside here and put it back after."""
    saved = dict(seats._extensions)
    seats._extensions.clear()
    yield
    seats._extensions.clear()
    seats._extensions.update(saved)


def _person(n: int, *, sso: bool = False, active: bool = True) -> User:
    return User(
        id=f"p-{n}", email=f"person{n}@example.com", name=f"Person {n}",
        auth_method=UserAuthMethod.OIDC if sso else UserAuthMethod.PASSWORD,
        oidc_iss=ISSUER if sso else None, oidc_sub=f"kc-{n}" if sso else None,
        is_active=active,
    )


def _fill(store: UserStore, count: int, *, sso: int = 0) -> None:
    for n in range(count):
        store.create(_person(n, sso=n < sso))


@pytest.fixture
def extension():
    """A stand-in for a limit that lives in other code: refuses when more than
    `room` of the accounts it is asked about are bound to an identity provider."""
    asked: list[tuple[str, bool]] = []
    room = {"left": 2}

    def admit(store, user, written):
        asked.append((user.id, written))
        used = seats.count_people(store, oidc_only=True)
        if user.has_oidc and ((used > room["left"]) if written else (used >= room["left"])):
            raise seats.SeatLimitReached("sso_seats", used, room["left"])

    def report(store):
        return {"used": seats.count_people(store, oidc_only=True), "limit": room["left"]}

    seats.register_extension(seats.Extension("sso_seats", admit=admit, report=report))
    yield asked


# ─── the defaults ────────────────────────────────────────────────────


def test_without_a_licence_the_limit_is_five():
    limits = seats.current_limits()
    assert (limits.seats, limits.licensed) == (5, False)
    assert seats.FREE_SEATS == 5


def test_five_people_can_be_added_and_the_sixth_is_refused(store):
    for n in range(5):
        seats.create_person(store, _person(n))
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(5))
    assert (refused.value.kind, refused.value.used, refused.value.limit) == ("seats", 5, 5)
    assert store.get_by_id("p-5") is None, "a refused person was written anyway"
    assert seats.used_seats(store) == 5


def test_the_refusal_names_the_limit_and_never_the_headcount_or_the_person(store):
    _fill(store, 5)
    odd = User(id="odd", email="secret.name+tag@private.example", name="Zaphod Beeblebrox")
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, odd)
    expected = ("This Celmis has reached its limit of 5 people. "
                "An administrator can add a licence or deactivate an account.")
    assert str(refused.value) == expected
    for text in (refused.value.message, str(refused.value), repr(refused.value.detail)):
        assert "private.example" not in text and "Zaphod" not in text
    assert "in use" not in expected and "5 of" not in expected
    assert refused.value.detail == {"code": "seat_limit_reached", "message": expected}


def test_an_installation_far_over_the_limit_does_not_say_how_far(store):
    """A licence that lapsed on a company with many accounts: the stranger who
    asks to sign up is told the limit, not 143."""
    _fill(store, 12)
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(50))
    assert "12" not in refused.value.message
    assert refused.value.used == 12, "the operator's number is still on the exception"


def test_the_single_sign_on_refusal_has_its_own_code_and_words_and_no_headcount():
    over = seats.SeatLimitReached("sso_seats", 5, 5)
    assert over.detail == {
        "code": "sso_seat_limit_reached",
        "message": "Single sign-on is limited to 5 people without a licence."}
    licensed = seats.SeatLimitReached("sso_seats", 5, 5, licensed=True)
    assert licensed.message == ("Single sign-on is limited to 5 people by this "
                                "installation's licence.")


# ─── who counts ──────────────────────────────────────────────────────


def test_the_master_account_and_the_local_user_take_no_seat_and_are_never_refused(
        store, monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    _fill(store, 5)
    master = User(id="master-admin", email="root@example.com", is_admin=True)
    local = User(id="default", email="default@local", is_admin=True)
    seats.create_person(store, master)
    seats.create_person(store, local)
    assert store.get_by_id("master-admin") is not None
    assert store.get_by_id("default") is not None
    assert seats.used_seats(store) == 5


def test_a_password_account_the_master_login_adopted_is_still_the_master(
        store, monkeypatch):
    """The master login may adopt an existing password account holding the
    master address (it keeps its own id). It is_superadmin, so no seat."""
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    _fill(store, 4)
    store.create(User(id="adopted", email="root@example.com", is_admin=True))
    assert seats.used_seats(store) == 4


def test_the_sql_count_and_the_python_predicate_leave_in_the_same_people(store, monkeypatch):
    """`count_people` is one COUNT query; `is_platform_account` is the rule.
    Every kind of account that could differ is here."""
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "Root@Example.com")
    accounts = [
        User(id="a", email="a@example.com"),
        User(id="b", email="b@example.com", auth_method=UserAuthMethod.OIDC,
             oidc_iss=ISSUER, oidc_sub="kc-b"),
        User(id="c", email="c@example.com", google_sub="g-c"),
        User(id="d", email="d@example.com", is_active=False),
        User(id="e", email="e@example.com", is_admin=True),
        User(id="master-admin", email="whatever@example.com", is_admin=True),
        User(id="default", email="default@local", is_admin=True),
        User(id="adopted", email="root@example.com", is_admin=True),
        # The master address, but bound to Google / single sign-on: NOT the
        # master (see roles.is_superadmin), so it is a person.
        User(id="bound-g", email="bound-g@example.com", google_sub="g-1"),
    ]
    for u in accounts:
        store.create(u)
    bound = store.get_by_id("bound-g")
    bound.email = "root@example.com"  # a Google-bound account with the master address
    with store._connect() as conn:  # noqa: SLF001 — the unique email index forbids two rows
        conn.execute("DELETE FROM users WHERE id = 'adopted'")
    store.update(bound)

    rows = store.list(active_only=True)
    by_python = [u for u in rows if not seats.is_platform_account(u)]
    assert seats.count_people(store) == len(by_python)
    assert seats.count_people(store, oidc_only=True) == sum(1 for u in by_python if u.has_oidc)
    assert {u.id for u in by_python} == {"a", "b", "c", "e", "bound-g"}


def test_counting_does_not_load_the_table(store, monkeypatch):
    _fill(store, 5)

    def forbidden(*a, **k):
        raise AssertionError("the seat count listed every user")

    monkeypatch.setattr(store, "list", forbidden)
    assert seats.used_seats(store) == 5
    assert seats.seat_state(store)["seats"] == {"used": 5, "limit": 5}
    with pytest.raises(seats.SeatLimitReached):
        seats.create_person(store, _person(9))


def test_a_deactivated_account_takes_no_seat_and_deactivating_one_frees_it(store):
    _fill(store, 5)
    with pytest.raises(seats.SeatLimitReached):
        seats.create_person(store, _person(5))

    assert seats.deactivate_person(store, "p-0") is True

    assert seats.used_seats(store) == 4
    seats.create_person(store, _person(5))
    assert seats.used_seats(store) == 5


def test_an_account_created_already_inactive_takes_no_seat(store):
    _fill(store, 5)
    seats.create_person(store, _person(9, active=False))
    assert store.get_by_id("p-9") is not None
    assert seats.used_seats(store) == 5


# ─── nobody is locked out ────────────────────────────────────────────


def test_an_installation_above_the_limit_keeps_every_account_and_only_growth_stops(store):
    """Seven people when the limit became five."""
    _fill(store, 7)
    assert seats.used_seats(store) == 7
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(7))
    assert (refused.value.used, refused.value.limit) == (7, 5)
    assert [u.id for u in store.list(active_only=True)] == [f"p-{n}" for n in range(7)]
    # Nothing the module does to an existing, active account refuses it.
    assert seats.reactivate_person(store, store.get_by_id("p-3")).is_active


# ─── what a licence changes ──────────────────────────────────────────


def test_a_licence_without_a_seat_limit_is_unlimited(store):
    seats.set_licence_limits(None, True)
    _fill(store, 8)
    seats.create_person(store, _person(8))
    assert seats.used_seats(store) == 9
    assert seats.seat_state(store) == {
        "seats": {"used": 9, "limit": None},
        "licensed": True,
    }


def test_a_licence_with_a_seat_limit_sets_that_limit(store):
    seats.set_licence_limits(7, True)
    _fill(store, 6)
    seats.create_person(store, _person(6))
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(7))
    assert refused.value.limit == 7
    assert "7 people" in refused.value.message


def test_clearing_the_licence_returns_to_the_community_limit_and_disables_nobody(store):
    seats.set_licence_limits(None, True)
    _fill(store, 8)
    seats.clear_licence_limits()
    assert seats.current_limits() == seats.COMMUNITY_LIMITS
    assert seats.used_seats(store) == 8
    assert all(u.is_active for u in store.list(active_only=False))
    with pytest.raises(seats.SeatLimitReached):
        seats.create_person(store, _person(8))


def test_a_licence_that_lapses_while_the_process_runs_returns_to_the_community_limit():
    now = datetime.now(UTC)
    seats.set_licence_limits(None, True, expires_at=now + timedelta(hours=1))
    assert seats.current_limits(now).seats is None
    assert seats.current_limits(now + timedelta(hours=2)) == seats.COMMUNITY_LIMITS


@pytest.mark.parametrize("bad", [0, -1, True, "5", 2.5])
def test_a_limit_that_is_not_a_positive_integer_is_refused_at_registration(bad):
    with pytest.raises(ValueError):
        seats.set_licence_limits(bad, True)
    assert seats.current_limits() == seats.COMMUNITY_LIMITS


# ─── doing it under a lock ───────────────────────────────────────────


def test_a_creation_that_took_the_count_over_the_limit_is_undone(store, monkeypatch):
    """The window between the check and the write, in another process: the
    check passes, a second person arrives, and the re-count after the write
    sees six. The later one is removed again and refused."""
    _fill(store, 4)
    real_create = store.create

    def create_after_somebody_else_got_in(user):
        if user.id == "p-5":
            real_create(_person(50))
        return real_create(user)

    monkeypatch.setattr(store, "create", create_after_somebody_else_got_in)
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(5))
    assert refused.value.used == 5 and refused.value.limit == 5
    assert store.get_by_id("p-5") is None, "the creation that overshot was kept"
    assert store.get_by_id("p-50") is not None
    assert seats.used_seats(store) == 5


def test_many_threads_adding_people_at_once_never_pass_the_limit(store):
    outcomes: list[str] = []

    def add(n: int) -> None:
        try:
            seats.create_person(store, _person(n))
            outcomes.append("added")
        except seats.SeatLimitReached:
            outcomes.append("refused")

    threads = [threading.Thread(target=add, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("added") == 5 and outcomes.count("refused") == 35
    assert seats.used_seats(store) == 5


# ─── a limit that lives in other code ────────────────────────────────


def test_a_registered_extension_is_asked_before_and_after_the_write_and_can_refuse(
        store, extension):
    seats.set_licence_limits(None, True)
    _fill(store, 2, sso=2)
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(5, sso=True))
    assert refused.value.kind == "sso_seats"
    assert store.get_by_id("p-5") is None
    assert ("p-5", False) in extension

    seats.create_person(store, _person(6))  # not a single-sign-on person: not asked to refuse
    assert store.get_by_id("p-6") is not None


def test_an_extension_that_refuses_only_after_the_write_has_the_person_removed(
        store, extension, monkeypatch):
    seats.set_licence_limits(None, True)
    _fill(store, 1, sso=1)
    real_create = store.create

    def somebody_else_binds_first(user):
        if user.id == "p-8":
            real_create(_person(7, sso=True))
        return real_create(user)

    monkeypatch.setattr(store, "create", somebody_else_binds_first)
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.create_person(store, _person(8, sso=True))
    assert refused.value.kind == "sso_seats"
    assert store.get_by_id("p-8") is None, "the person that overshot was kept"
    assert store.get_by_id("p-7") is not None


def test_the_state_carries_one_block_per_extension(store, extension):
    _fill(store, 3, sso=2)
    state = seats.seat_state(store)
    assert state["sso_seats"] == {"used": 2, "limit": 2}
    assert state["seats"] == {"used": 3, "limit": 5}


def test_without_an_extension_there_is_no_single_sign_on_block(store):
    assert "sso_seats" not in seats.seat_state(store)


# ─── switching a deactivated account on again ────────────────────────


def test_reactivating_needs_a_free_seat(store):
    _fill(store, 5)
    store.create(_person(7, active=False))
    gone = store.get_by_id("p-7")
    with pytest.raises(seats.SeatLimitReached):
        seats.reactivate_person(store, gone)
    assert store.get_by_id("p-7").is_active is False

    seats.deactivate_person(store, "p-0")
    assert seats.reactivate_person(store, store.get_by_id("p-7")).is_active is True
    assert store.get_by_id("p-7").is_active is True
    assert seats.used_seats(store) == 5


def test_reactivating_asks_the_extensions_too(store, extension):
    seats.set_licence_limits(None, True)
    _fill(store, 2, sso=2)
    store.create(_person(7, sso=True, active=False))
    with pytest.raises(seats.SeatLimitReached) as refused:
        seats.reactivate_person(store, store.get_by_id("p-7"))
    assert refused.value.kind == "sso_seats"
    assert store.get_by_id("p-7").is_active is False


def test_reactivating_writes_only_is_active_and_reads_the_row_again(store):
    """The caller's copy is old: the account was renamed since. Switching it
    on must not put the old name back."""
    store.create(_person(1, active=False))
    stale = store.get_by_id("p-1")
    changed = store.get_by_id("p-1")
    changed.name = "Renamed meanwhile"
    store.update(changed)

    seats.reactivate_person(store, stale)

    row = store.get_by_id("p-1")
    assert row.is_active and row.name == "Renamed meanwhile"


def test_a_reactivation_that_took_the_count_over_the_limit_switches_only_is_active_back(
        store, monkeypatch):
    _fill(store, 4)
    store.create(_person(7, active=False))
    real_set_active = store.set_active
    calls: list[tuple] = []

    def set_active_after_somebody_else_got_in(user_id, active, *, expect=None):
        calls.append((user_id, active, expect))
        done = real_set_active(user_id, active, expect=expect)
        if user_id == "p-7" and active:
            store.create(_person(50))  # the sixth, a moment after the check
            renamed = store.get_by_id("p-7")
            renamed.name = "Renamed meanwhile"
            store.update(renamed)
        return done

    monkeypatch.setattr(store, "set_active", set_active_after_somebody_else_got_in)
    with pytest.raises(seats.SeatLimitReached):
        seats.reactivate_person(store, store.get_by_id("p-7"))
    row = store.get_by_id("p-7")
    assert row.is_active is False
    assert row.name == "Renamed meanwhile", "the rollback wrote back the whole stale row"
    assert calls[-1] == ("p-7", False, True)


def test_reactivating_an_account_that_is_gone_says_so(store):
    store.create(_person(1, active=False))
    ghost = store.get_by_id("p-1")
    store.delete("p-1")
    with pytest.raises(UserNotFoundError):
        seats.reactivate_person(store, ghost)


def test_deactivating_deletes_nothing_and_refuses_the_platform_accounts(store):
    _fill(store, 2)
    store.create(User(id="default", email="default@local", is_admin=True))
    assert seats.deactivate_person(store, "p-0") is True
    assert seats.deactivate_person(store, "p-0") is False
    kept = store.get_by_id("p-0")
    assert kept is not None and kept.email == "person0@example.com" and not kept.is_active
    with pytest.raises(ValueError):
        seats.deactivate_person(store, "default")
    assert store.get_by_id("default").is_active
    with pytest.raises(UserNotFoundError):
        seats.deactivate_person(store, "nobody")
