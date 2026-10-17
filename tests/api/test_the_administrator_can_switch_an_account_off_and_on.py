"""The administrator's deactivate and reactivate buttons, and what they end.

The contract (src/api/routers/admin_users.py, src/users/seats.py):

  * `POST /api/admin/users/{id}/deactivate` and `/reactivate` are the master
    account's, like the rest of the users page: anybody else gets a 403, a
    stranger a 401;
  * switching an account off ends every way in at once — the session token it
    already holds, a refresh, the password, Google — and deletes nothing: the
    account, its name and its memberships stay, and switching it on again
    brings them all back. Single sign-on, the MCP tokens and the pack tokens
    are tested where they live (tests/ee/test_the_sso_allowance.py,
    tests/security/test_a_deactivated_account_is_refused_on_every_mcp_token.py,
    tests/api/test_a_pack_token_publishes_one_pack_and_nothing_else.py);
  * the platform's own accounts (the break-glass account, the seeded local
    user) and the account that holds the master address are never switched
    off, by anybody;
  * switching an account on needs a free seat, exactly as a sign-up does: a
    403 `seat_limit_reached` naming the limit and not the head-count, with an
    audit row that does carry the counts;
  * each change leaves one audit row — `auth.user_deactivated` /
    `auth.user_reactivated` — naming the person by id and nothing else about
    them; repeating a change leaves none.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.users import User, seats

PASSWORD = "Vq7#mLz2-Rk9wTp4!"
MASTER_EMAIL = "root@example.com"
MASTER_KEY = "master-key-for-tests-only-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("CELMIS_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def users(tmp_path):
    from src.users import UserStore

    return UserStore(tmp_path / "users.db")


@pytest.fixture
def audit(monkeypatch):
    rows: list[dict] = []
    from src.api.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "record_action", lambda **kw: rows.append(kw))
    monkeypatch.setattr("src.security.audit.record_action", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def client(users, audit, monkeypatch):
    from src.api.deps import get_users
    from src.api.routers import admin_users
    from src.api.routers import auth as auth_router

    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(admin_users.router)
    app.dependency_overrides[get_users] = lambda: users
    return TestClient(app)


def _email(n: int) -> str:
    return f"person{n}@example.com"


def _signup(client, n: int):
    return client.post("/api/auth/signup", json={"email": _email(n), "password": PASSWORD})


def _login(client, n: int):
    return client.post("/api/auth/login", json={"email": _email(n), "password": PASSWORD})


def _person(client, n: int) -> dict:
    """Signs the person up; returns their bearer header."""
    r = _signup(client, n)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _master(client) -> dict:
    r = client.post("/api/auth/login", json={"email": MASTER_EMAIL, "password": MASTER_KEY})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _id(users, n: int) -> str:
    return users.get_by_email(_email(n)).id


def _off(client, master, uid: str):
    return client.post(f"/api/admin/users/{uid}/deactivate", headers=master)


def _on(client, master, uid: str):
    return client.post(f"/api/admin/users/{uid}/reactivate", headers=master)


def _google_says(monkeypatch, *, sub: str, email: str):
    from src.api.routers import auth as auth_router

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")
    payload = {"aud": "g-client", "iss": "https://accounts.google.com", "sub": sub,
               "email": email, "email_verified": "true"}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(auth_router, "build_client", lambda **k: _Client())


# ─── who may press the buttons ───────────────────────────────────────


def test_only_the_master_account_may_switch_an_account_off_or_on(client, users):
    alice, bob = _person(client, 1), _person(client, 2)
    target = _id(users, 2)
    for path in ("deactivate", "reactivate"):
        by_person = client.post(f"/api/admin/users/{target}/{path}", headers=alice)
        assert by_person.status_code == 403, f"a plain person pressed {path}"
        by_stranger = client.post(f"/api/admin/users/{target}/{path}")
        assert by_stranger.status_code == 401
    assert users.get_by_id(target).is_active
    assert client.get("/api/auth/me", headers=bob).status_code == 200


def test_a_global_admin_who_is_not_the_master_account_may_not_press_them_either(
        client, users):
    admin = _person(client, 1)
    promoted = users.get_by_email(_email(1))
    promoted.is_admin = True
    users.update(promoted)
    _person(client, 2)
    r = client.post(f"/api/admin/users/{_id(users, 2)}/deactivate", headers=admin)
    assert r.status_code == 403


# ─── what switching off ends, and what it keeps ──────────────────────


def test_switching_an_account_off_ends_every_way_in_and_switching_it_on_restores_them(
        client, users, monkeypatch):
    alice, bob = _person(client, 1), _person(client, 2)
    master = _master(client)
    _google_says(monkeypatch, sub="g-alice", email=_email(1))
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 200
    assert users.get_by_google_sub("g-alice") is not None
    uid = _id(users, 1)

    off = _off(client, master, uid)
    assert (off.status_code, off.json()) == (200, {"id": uid, "is_active": False})

    # the session she already holds
    assert client.get("/api/auth/me", headers=alice).status_code == 401
    assert client.post("/api/auth/refresh", headers=alice).status_code == 401
    # the password
    assert _login(client, 1).status_code == 401
    # Google
    google = client.post("/api/auth/google", json={"id_token": "x"})
    assert google.status_code == 403 and google.json()["detail"] == "Account is disabled"
    assert "access_token" not in google.text
    # and nobody else is affected
    assert client.get("/api/auth/me", headers=bob).status_code == 200

    on = _on(client, master, uid)
    assert (on.status_code, on.json()) == (200, {"id": uid, "is_active": True})
    assert client.get("/api/auth/me", headers=alice).status_code == 200
    assert _login(client, 1).status_code == 200
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 200


def test_switching_an_account_off_deletes_nothing(client, users):
    _person(client, 1)
    master = _master(client)
    before = users.get_by_email(_email(1))

    assert _off(client, master, before.id).status_code == 200

    kept = users.get_by_id(before.id)
    assert kept is not None and kept.is_active is False
    assert (kept.email, kept.name, kept.password_hash) == (
        before.email, before.name, before.password_hash)


def test_an_account_that_is_already_off_stays_off_and_leaves_no_second_audit_row(
        client, users, audit):
    _person(client, 1)
    master = _master(client)
    uid = _id(users, 1)
    audit.clear()

    assert _off(client, master, uid).status_code == 200
    assert _off(client, master, uid).status_code == 200

    rows = [r for r in audit if r["action"] == "auth.user_deactivated"]
    assert len(rows) == 1


def test_an_account_that_is_already_on_is_not_audited_when_switched_on(
        client, users, audit):
    _person(client, 1)
    master = _master(client)
    audit.clear()
    r = _on(client, master, _id(users, 1))
    assert (r.status_code, r.json()["is_active"]) == (200, True)
    assert not [x for x in audit if x["action"] == "auth.user_reactivated"]


def test_an_account_that_does_not_exist_is_a_404_either_way(client):
    master = _master(client)
    assert _off(client, master, "nobody").status_code == 404
    assert _on(client, master, "nobody").status_code == 404


# ─── who is never switched off ───────────────────────────────────────


def test_the_platform_accounts_are_not_there_to_be_switched_off(client, users):
    master = _master(client)
    users.create(User(id="default", email="default@local", is_admin=True))
    for uid in ("default", "master-admin"):
        assert _off(client, master, uid).status_code == 404, uid
        assert _on(client, master, uid).status_code == 404, uid
    assert users.get_by_id("default").is_active
    assert users.get_by_id("master-admin").is_active


def test_the_account_holding_the_master_address_cannot_be_switched_off_even_under_another_id(
        client, users):
    """The master login adopts a password account that holds the master address
    and keeps its id, so the id alone does not name it."""
    users.create(User(id="adopted", email=MASTER_EMAIL, is_admin=True))
    master = _master(client)
    assert users.get_by_id("adopted") is not None

    r = _off(client, master, "adopted")

    assert r.status_code == 409 and r.json()["detail"] == "This account cannot be deactivated"
    assert users.get_by_id("adopted").is_active


# ─── switching on needs a seat ───────────────────────────────────────


def test_switching_an_account_on_is_refused_when_the_installation_is_full(
        client, users, audit):
    for n in range(5):
        _person(client, n)
    master = _master(client)
    assert _off(client, master, _id(users, 0)).status_code == 200
    assert _signup(client, 5).status_code == 200            # takes the seat she left
    audit.clear()

    refused = _on(client, master, _id(users, 0))

    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "code": "seat_limit_reached",
        "message": "This Celmis has reached its limit of 5 people. "
                   "An administrator can add a licence or deactivate an account."}
    assert users.get_by_id(_id(users, 0)).is_active is False
    [row] = [r for r in audit if r["action"] == "auth.seat_limit_refused"]
    assert row["target"] == "reactivate" and row["actor"] == MASTER_EMAIL
    assert row["detail"] == {"kind": "seats", "used": 5, "limit": 5}
    assert not [r for r in audit if r["action"] == "auth.user_reactivated"]

    assert _off(client, master, _id(users, 5)).status_code == 200
    assert _on(client, master, _id(users, 0)).status_code == 200
    assert seats.used_seats(users) == 5


def test_switching_an_account_off_frees_the_seat_for_the_next_sign_up(client, users):
    for n in range(5):
        _person(client, n)
    assert _signup(client, 5).status_code == 403
    master = _master(client)

    _off(client, master, _id(users, 0))

    assert _signup(client, 5).status_code == 200


# ─── the audit rows ──────────────────────────────────────────────────


def test_each_change_leaves_one_audit_row_that_names_the_person_by_id_only(
        client, users, audit):
    _person(client, 1)
    master = _master(client)
    uid = _id(users, 1)
    audit.clear()

    _off(client, master, uid)
    _on(client, master, uid)

    [off] = [r for r in audit if r["action"] == "auth.user_deactivated"]
    [on] = [r for r in audit if r["action"] == "auth.user_reactivated"]
    for row, via in ((off, "admin_users"), (on, "admin_users")):
        assert row["target"] == uid
        assert row["detail"] == {"user_id": uid, "via": via}
        assert row["actor"] == MASTER_EMAIL and row["actor_id"] == "master-admin"
        assert _email(1) not in repr(row), "the person's address is in the audit row"
