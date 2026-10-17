"""A Celmis without a licence holds five people; the sixth is refused, nobody else is.

The contract (src/users/seats.py):

  * the sign-up, the Google sign-in and `celmis auth google` each ADD a person,
    and each refuses the sixth with a 403 whose body is
    `{"code": "seat_limit_reached", "message": ...}` — the LIMIT, never the
    head-count and never a name: the caller is not signed in;
  * the refusal writes nothing, and leaves one audit row that carries the
    exact counts and the flow but not the address that was turned away;
  * the master account takes no seat, and deactivating an account frees one;
  * an installation that is already above the limit keeps every account
    signing in, and linking Google to an account that already exists takes no
    new seat;
  * `/api/admin/seats` is for global admins; `/api/invites/seats` is for the
    people who may invite into a TEAM workspace (not into their own personal
    one, which everybody owns) and answers a bare yes or no.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from src.users import seats
from tests.api.rbac_world import world

PASSWORD = "Vq7#mLz2-Rk9wTp4!"
MASTER_EMAIL = "root@example.com"
MASTER_KEY = "master-key-for-tests-only-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)
    monkeypatch.delenv("CELMIS_MASTER_EMAIL", raising=False)
    monkeypatch.delenv("CELMIS_MASTER_KEY", raising=False)


@pytest.fixture
def users(tmp_path):
    from src.users import UserStore

    return UserStore(tmp_path / "users.db")


@pytest.fixture
def audit(monkeypatch):
    rows: list[dict] = []

    def record(**kw):
        rows.append(kw)

    from src.api.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "record_action", record)
    return rows


@pytest.fixture
def client(users, audit, monkeypatch):
    from src.api.deps import get_users
    from src.api.routers import auth as auth_router

    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[get_users] = lambda: users
    return TestClient(app)


def _signup(client, n: int):
    return client.post("/api/auth/signup", json={"email": f"person{n}@example.com",
                                                 "password": PASSWORD})


def _fill(client, count: int) -> None:
    for n in range(count):
        assert _signup(client, n).status_code == 200


def _google_says(monkeypatch, **claims):
    from src.api.routers import auth as auth_router

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")
    payload = {"aud": "g-client", "iss": "https://accounts.google.com", "sub": "g-1",
               "email": "guest@example.com", "email_verified": "true", **claims}

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


# ─── the sign-up ─────────────────────────────────────────────────────


def test_five_people_can_sign_up_and_the_sixth_is_refused(client, users):
    _fill(client, 5)
    refused = _signup(client, 5)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "code": "seat_limit_reached",
        "message": "This Celmis has reached its limit of 5 people. "
                   "An administrator can add a licence or deactivate an account.",
    }
    assert users.get_by_email("person5@example.com") is None


def test_the_refusal_does_not_say_who_was_turned_away(client):
    _fill(client, 5)
    refused = _signup(client, 5)
    assert "person5" not in refused.text and "example.com" not in refused.text


def test_the_refusal_does_not_say_how_many_accounts_there_are(client):
    """The stranger who asks to sign up is told the limit, not the head-count."""
    seats.set_licence_limits(None, True)
    _fill(client, 8)
    seats.clear_licence_limits()
    refused = _signup(client, 8)
    assert refused.status_code == 403
    assert "8" not in refused.json()["detail"]["message"]
    assert refused.json()["detail"]["message"].startswith(
        "This Celmis has reached its limit of 5 people.")


def test_a_refused_sign_up_leaves_an_audit_row_with_counts_and_no_address(client, audit):
    _fill(client, 5)
    audit.clear()
    _signup(client, 5)
    [row] = [r for r in audit if r["action"] == "auth.seat_limit_refused"]
    assert row["target"] == "signup"
    assert row["detail"] == {"kind": "seats", "used": 5, "limit": 5}
    assert row["error"] == "seat_limit_reached"
    assert "person5" not in repr(row) and "example.com" not in repr(row)


def test_an_address_that_is_already_registered_is_still_a_conflict_not_a_seat_error(client):
    _fill(client, 5)
    again = _signup(client, 2)
    assert again.status_code == 409


def test_deactivating_an_account_frees_its_seat_for_the_next_sign_up(client, users):
    _fill(client, 5)
    assert _signup(client, 5).status_code == 403
    leaving = users.get_by_email("person0@example.com")
    leaving.is_active = False
    users.update(leaving)
    assert _signup(client, 5).status_code == 200


def test_the_master_account_takes_no_seat(client, users, monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("CELMIS_MASTER_KEY", MASTER_KEY)
    _fill(client, 5)
    r = client.post("/api/auth/login", json={"email": MASTER_EMAIL, "password": MASTER_KEY})
    assert r.status_code == 200, r.text
    assert users.get_by_email(MASTER_EMAIL).id == "master-admin"
    assert seats.used_seats(users) == 5


def test_an_installation_above_the_limit_keeps_everybody_signing_in(client, users):
    _fill(client, 5)
    seats.set_licence_limits(None, True)
    for n in range(5, 8):
        assert _signup(client, n).status_code == 200
    seats.clear_licence_limits()

    assert seats.used_seats(users) == 8
    for n in range(8):
        r = client.post("/api/auth/login",
                        json={"email": f"person{n}@example.com", "password": PASSWORD})
        assert r.status_code == 200, f"person {n} was locked out"
    assert _signup(client, 8).status_code == 403


def test_a_licence_with_more_seats_lets_the_installation_grow_to_them(client):
    seats.set_licence_limits(7, True)
    _fill(client, 7)
    refused = _signup(client, 7)
    assert refused.status_code == 403
    assert "limit of 7 people" in refused.json()["detail"]["message"]
    assert "7 of 7" not in refused.text


def test_thirty_people_signing_up_at_once_over_http_leave_five(client, users):
    """The window between the check and the write, through the real route."""
    import threading

    codes: list[int] = []

    def go(n: int) -> None:
        codes.append(_signup(client, n).status_code)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(set(codes)) == [200, 403], codes
    assert codes.count(200) == 5 and seats.used_seats(users) == 5


# ─── the Google sign-in ──────────────────────────────────────────────


def test_a_google_sign_in_that_would_be_the_sixth_person_is_refused(
        client, users, monkeypatch, audit):
    _fill(client, 5)
    audit.clear()
    _google_says(monkeypatch)
    r = client.post("/api/auth/google", json={"id_token": "x"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "seat_limit_reached"
    assert users.get_by_google_sub("g-1") is None
    [row] = [x for x in audit if x["action"] == "auth.seat_limit_refused"]
    assert row["target"] == "google" and "guest" not in repr(row)


def test_the_first_google_sign_ins_create_people_until_the_seats_are_full(
        client, users, monkeypatch):
    _fill(client, 4)
    _google_says(monkeypatch)
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 200
    assert users.get_by_google_sub("g-1") is not None
    _google_says(monkeypatch, sub="g-2", email="other@example.com")
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 403


def test_a_google_person_who_already_has_an_account_signs_in_at_the_limit(
        client, users, monkeypatch):
    _fill(client, 4)
    _google_says(monkeypatch)
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 200
    assert seats.used_seats(users) == 5
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 200


def test_linking_google_to_an_account_that_exists_takes_no_new_seat(
        client, users, monkeypatch):
    _fill(client, 5)
    _google_says(monkeypatch, email="person3@example.com", sub="g-3")
    r = client.post("/api/auth/google", json={"id_token": "x"})
    assert r.status_code == 200, r.text
    assert users.get_by_google_sub("g-3").email == "person3@example.com"
    assert seats.used_seats(users) == 5


# ─── `celmis auth google` ────────────────────────────────────────────


def test_the_command_line_sign_in_asks_the_seats_too(users, monkeypatch):
    """There is no flag to skip it."""
    from src.cli import app
    from src.users import User
    from src.users.google_oauth import GoogleUserInfo

    for n in range(5):
        users.create(User(id=f"p-{n}", email=f"person{n}@example.com"))
    monkeypatch.setattr("src.users.get_user_store", lambda: users)
    monkeypatch.setattr(
        "src.users.google_oauth.run_google_oauth_flow",
        lambda **kw: GoogleUserInfo(sub="g-cli", email="cli@example.com",
                                    name="Cli", email_verified=True))

    result = CliRunner().invoke(app, ["auth", "google", "--no-browser"])

    assert result.exit_code == 1
    assert "reached its limit of 5 people" in result.output
    assert users.get_by_email("cli@example.com") is None


def test_the_command_line_takes_its_limits_from_the_licence_and_from_nothing_else(
        users, monkeypatch, tmp_path):
    """The command line reads the limits from the licence it can read, as the
    API does, not from whatever a previous caller in the process registered."""
    from src.cli import app

    _cli_world(users, monkeypatch, tmp_path, people=5)
    seats.set_licence_limits(None, True)

    result = CliRunner().invoke(app, ["auth", "google", "--no-browser"])

    assert result.exit_code == 1, result.output
    assert users.get_by_email("cli@example.com") is None


def _cli_world(users, monkeypatch, tmp_path, *, people: int):
    """A command-line process: the user store, a Google identity, and an empty
    licence slot — no app, nothing mounted."""
    from src.users import User
    from src.users.google_oauth import GoogleUserInfo
    from tests.ee.licensing import isolate_license_store, trust_test_key

    for n in range(people):
        users.create(User(id=f"p-{n}", email=f"person{n}@example.com"))
    monkeypatch.setattr("src.users.get_user_store", lambda: users)
    monkeypatch.setattr(
        "src.users.google_oauth.run_google_oauth_flow",
        lambda **kw: GoogleUserInfo(sub="g-cli", email="cli@example.com",
                                    name="Cli", email_verified=True))
    isolate_license_store(monkeypatch, tmp_path)
    trust_test_key(monkeypatch)


def test_the_command_line_reads_the_configured_licence_before_it_asks_the_seats(
        users, monkeypatch, tmp_path):
    """A process that mounts no app has registered nothing: without reading the
    licence it would hold every installation to five people, a licensed one too.
    A real licence is minted and configured the way an operator does."""
    from src.cli import app
    from tests.ee.licensing import mint_test_license

    _cli_world(users, monkeypatch, tmp_path, people=7)
    monkeypatch.setenv("CELMIS_LICENSE_KEY", mint_test_license(["analytics"], seats=9))

    result = CliRunner().invoke(app, ["auth", "google", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert users.get_by_email("cli@example.com") is not None
    assert seats.current_limits().seats == 9


def test_the_command_line_still_refuses_the_person_who_passes_the_licences_limit(
        users, monkeypatch, tmp_path):
    from src.cli import app
    from tests.ee.licensing import mint_test_license

    _cli_world(users, monkeypatch, tmp_path, people=7)
    monkeypatch.setenv("CELMIS_LICENSE_KEY", mint_test_license(["analytics"], seats=7))

    result = CliRunner().invoke(app, ["auth", "google", "--no-browser"])

    assert result.exit_code == 1
    assert "limit of 7 people" in result.output
    assert users.get_by_email("cli@example.com") is None


def test_the_command_line_does_not_sign_in_a_deactivated_account(users, monkeypatch, tmp_path):
    from src.cli import app
    from src.users import User

    _cli_world(users, monkeypatch, tmp_path, people=1)
    users.create(User(id="gone", email="cli@example.com", is_active=False))

    result = CliRunner().invoke(app, ["auth", "google", "--no-browser"])

    assert result.exit_code == 1
    assert users.get_by_email("cli@example.com").is_active is False


# ─── the two read-only routes ────────────────────────────────────────


def _seat_routes():
    from src.api.routers import seats as seats_router

    return (seats_router.router,)


async def test_the_admin_card_reports_used_and_limit_and_is_for_global_admins_only(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes(),
                     licensed=False) as w:
        r = await w.client.get("/api/admin/seats", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        body = r.json()
        # The cast is eleven people (the master takes no seat): over the limit,
        # and reported as it is.
        assert body["seats"]["limit"] == 5 and body["seats"]["used"] == 11
        assert body["sso_seats"] == {"used": 0, "limit": 5}
        assert body["licensed"] is False
        assert (body["free_seats"], body["free_sso_seats"]) == (5, 5)

        for who in ("admin_a", "owner_a", "member_a", "loner"):
            denied = await w.client.get("/api/admin/seats", headers=w.h(who))
            assert denied.status_code == 403, f"{who} read the installation's seat count"


async def test_the_admin_card_reports_an_unlimited_licence_as_no_limit(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes()) as w:
        body = (await w.client.get("/api/admin/seats", headers=w.h("gadmin"))).json()
        assert body["seats"]["limit"] is None
        # A licence without the `sso` feature gives single sign-on nothing: it
        # keeps the free allowance.
        assert body["sso_seats"]["limit"] == 5
        assert body["licensed"] is True


async def test_the_admin_card_has_no_single_sign_on_line_in_a_build_without_it(
        tmp_path, monkeypatch):
    """The community build has no `src.ee`: nothing registers the allowance."""
    monkeypatch.setattr(seats, "_extensions", {})
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes()) as w:
        body = (await w.client.get("/api/admin/seats", headers=w.h("gadmin"))).json()
        assert body["sso_seats"] is None and body["free_sso_seats"] is None
        assert body["seats"]["limit"] is None


async def test_the_invite_dialog_learns_only_whether_there_is_a_free_seat(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes(),
                     licensed=False) as w:
        r = await w.client.get("/api/invites/seats", headers=w.h("admin_a", "ws-a"))
        assert r.status_code == 200, r.text
        assert r.json() == {"has_free_seat": False}, "eleven people on a five-seat Celmis"

        seats.set_licence_limits(12, True)
        assert (await w.client.get("/api/invites/seats", headers=w.h("admin_a", "ws-a"))
                ).json() == {"has_free_seat": True}

        seats.set_licence_limits(None, True)
        assert (await w.client.get("/api/invites/seats", headers=w.h("admin_a", "ws-a"))
                ).json() == {"has_free_seat": True}


async def test_only_somebody_who_may_invite_into_a_team_workspace_may_ask_about_free_seats(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes()) as w:
        for who in ("member_a", "viewer_a", "editor_a"):
            denied = await w.client.get("/api/invites/seats", headers=w.h(who, "ws-a"))
            assert denied.status_code == 403, f"{who} may ask about seats of ws-a"
        for who in ("owner_a", "admin_a", "gadmin"):
            ok = await w.client.get("/api/invites/seats", headers=w.h(who, "ws-a"))
            assert ok.status_code == 200, f"{who}: {ok.text}"


async def test_somebody_with_no_team_workspace_cannot_ask_whether_the_installation_is_full(
        tmp_path, monkeypatch):
    """Everybody owns a personal workspace, so `workspace admin` alone would have
    let any signed-in person probe the head-count. Somebody who belongs to no
    team workspace lands in their own, and is refused there."""
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes(),
                     licensed=False) as w:
        denied = await w.client.get("/api/invites/seats", headers=w.h("loner"))
        assert denied.status_code == 403
        assert "free" not in denied.text and "has_free_seat" not in denied.text
        # Asking for somebody's personal workspace by name does not help: the
        # caller is a member of nothing there, and falls back to their own.
        again = await w.client.get("/api/invites/seats", headers=w.h("loner", "u-u-loner"))
        assert again.status_code == 403


async def test_a_personal_workspace_is_refused_even_to_a_global_admin_who_names_it(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes()) as w:
        # The loner's personal workspace is created the first time they ask.
        assert (await w.client.get("/api/invites/seats", headers=w.h("loner"))).status_code == 403
        r = await w.client.get("/api/invites/seats", headers=w.h("gadmin", "u-u-loner"))
        assert r.status_code == 403, "nobody is invited into a personal workspace"
        assert (await w.client.get("/api/invites/seats", headers=w.h("gadmin", "ws-a"))
                ).status_code == 200


async def test_a_team_workspace_whose_slug_merely_starts_with_u_dash_is_a_team_workspace(
        tmp_path, monkeypatch):
    from src.db.models import Workspace

    async with world(tmp_path, monkeypatch, extra_routers=_seat_routes()) as w:
        async with w.factory() as s:
            s.add(Workspace(id="wsid-u", name="U Team", slug="u-team", description=""))
            await s.commit()
        r = await w.client.get("/api/invites/seats", headers=w.h("gadmin", "u-team"))
        assert r.status_code == 200, r.text


async def test_inviting_an_existing_account_adds_no_person_on_a_full_installation(
        tmp_path, monkeypatch):
    """Joining a workspace adds nobody: the account already holds its seat."""
    async with world(tmp_path, monkeypatch, licensed=False) as w:
        assert seats.used_seats() > seats.FREE_SEATS
        before = seats.used_seats()
        r = await w.client.post("/api/invites", headers=w.h("su", "ws-a"),
                                json={"email": "loner@acme-corp.io", "role": "member"})
        assert r.status_code == 201, r.text
        assert r.json()["added_directly"] is True
        assert await w.role("loner", "ws-a") == "member"
        assert seats.used_seats() == before


async def test_an_invite_link_to_a_stranger_is_still_issued_and_creates_no_account(
        tmp_path, monkeypatch):
    """The link is issued (the warning is the web's); no account exists until
    the stranger signs up, and THAT is where the seat is asked."""
    async with world(tmp_path, monkeypatch, licensed=False) as w:
        before = seats.used_seats()
        r = await w.client.post("/api/invites", headers=w.h("admin_a", "ws-a"),
                                json={"email": "stranger@outside.io", "role": "member"})
        assert r.status_code == 201, r.text
        assert r.json()["added_directly"] is False
        assert seats.used_seats() == before
