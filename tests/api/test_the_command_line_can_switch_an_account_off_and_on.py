"""`analyzer auth deactivate EMAIL` and `analyzer auth reactivate EMAIL`.

The command-line twin of the administrator's buttons
(tests/api/test_the_administrator_can_switch_an_account_off_and_on.py), for the
operator who has the server and not a browser:

  * deactivating frees the seat and deletes nothing; reactivating needs a free
    seat under the licence configured HERE (the process mounts no app, so it
    reads the licence itself), and says the limit, not the head-count;
  * the master account, the seeded local user and the account holding the master
    address are never switched off;
  * both commands are idempotent and leave one audit row per real change,
    naming the person by id.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from src.users import User, seats

MASTER_EMAIL = "root@example.com"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.delenv("CELMIS_MASTER_KEY", raising=False)


@pytest.fixture
def users(tmp_path, monkeypatch):
    from src.users import UserStore

    store = UserStore(tmp_path / "users.db")
    monkeypatch.setattr("src.users.get_user_store", lambda: store)
    return store


@pytest.fixture
def audit(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr("src.security.audit.record_action", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture(autouse=True)
def _no_licence(monkeypatch, tmp_path):
    from tests.ee.licensing import isolate_license_store

    monkeypatch.delenv("CELMIS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("CELMIS_LICENSE_FILE", raising=False)
    isolate_license_store(monkeypatch, tmp_path)


def _run(*args: str):
    from src.cli import app

    return CliRunner().invoke(app, ["auth", *args])


def _people(users, count: int) -> None:
    for n in range(count):
        users.create(User(id=f"p-{n}", email=f"person{n}@example.com", name=f"Person {n}"))


def test_deactivating_switches_the_account_off_deletes_nothing_and_frees_the_seat(
        users, audit):
    _people(users, 5)

    result = _run("deactivate", "person0@example.com")

    assert result.exit_code == 0, result.output
    kept = users.get_by_id("p-0")
    assert kept is not None and kept.is_active is False and kept.name == "Person 0"
    assert seats.used_seats(users) == 4
    [row] = audit
    assert row["action"] == "auth.user_deactivated" and row["target"] == "p-0"
    assert row["detail"] == {"user_id": "p-0", "via": "cli"}
    assert "person0" not in repr(row)


def test_deactivating_twice_changes_nothing_the_second_time(users, audit):
    _people(users, 2)
    assert _run("deactivate", "person0@example.com").exit_code == 0
    again = _run("deactivate", "person0@example.com")
    assert again.exit_code == 0 and "already" in again.output
    assert len(audit) == 1


def test_reactivating_needs_a_free_seat_and_says_the_limit_not_the_headcount(users, audit):
    _people(users, 6)                  # one over: a licence lapsed
    seats.deactivate_person(users, "p-5")
    users.create(User(id="p-6", email="person6@example.com"))   # took her seat
    audit.clear()

    result = _run("reactivate", "person5@example.com")

    assert result.exit_code == 1
    assert "reached its limit of 5 people" in result.output
    assert users.get_by_id("p-5").is_active is False
    assert not audit

    seats.deactivate_person(users, "p-6")
    seats.deactivate_person(users, "p-4")
    done = _run("reactivate", "person5@example.com")
    assert done.exit_code == 0, done.output
    assert users.get_by_id("p-5").is_active is True
    [row] = audit
    assert row["action"] == "auth.user_reactivated" and row["detail"]["via"] == "cli"


def test_reactivating_reads_the_licence_configured_in_this_process(users, monkeypatch):
    """Without it the command line would hold a licensed installation to five."""
    from tests.ee.licensing import mint_test_license, trust_test_key

    _people(users, 6)
    seats.deactivate_person(users, "p-5")
    trust_test_key(monkeypatch)
    monkeypatch.setenv("CELMIS_LICENSE_KEY", mint_test_license(["analytics"], seats=9))
    users.create(User(id="p-6", email="person6@example.com"))   # five active + this = 6

    result = _run("reactivate", "person5@example.com")

    assert result.exit_code == 0, result.output
    assert users.get_by_id("p-5").is_active is True


def test_reactivating_an_active_account_says_so_and_changes_nothing(users, audit):
    _people(users, 1)
    result = _run("reactivate", "person0@example.com")
    assert result.exit_code == 0 and "already active" in result.output
    assert not audit


def test_an_address_nobody_has_is_an_error_either_way(users):
    for verb in ("deactivate", "reactivate"):
        result = _run(verb, "nobody@example.com")
        assert result.exit_code == 1 and "No user" in result.output


def test_the_platform_accounts_and_the_master_address_cannot_be_switched_off(users, audit):
    users.create(User(id="default", email="default@local", is_admin=True))
    users.create(User(id="master-admin", email="breakglass@example.com", is_admin=True))
    users.create(User(id="adopted", email=MASTER_EMAIL, is_admin=True))

    for email in ("default@local", "breakglass@example.com", MASTER_EMAIL):
        result = _run("deactivate", email)
        assert result.exit_code == 1, email
        assert "cannot be switched off" in result.output

    assert all(users.get_by_id(i).is_active for i in ("default", "master-admin", "adopted"))
    assert not audit
