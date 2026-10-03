"""`can_change` and `is_superadmin` — the rule, exhaustively, without HTTP.

The route-level tests (tests/api/test_roles_who_grants_what.py) prove each
path ASKS this function; this file proves what it ANSWERS, for every
(actor, actor role, current role, new role) combination — 5 actor kinds × 6
actor roles × 6 current × 6 new — against an independent statement of the
product owner's rule.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from src.users.roles import (
    DELEGABLE_ROLES,
    PRIVILEGED_ROLES,
    VALID_WORKSPACE_ROLES,
    can_change,
    grantable_roles,
    is_superadmin,
)

MASTER = "root@acme-corp.io"


@pytest.fixture(autouse=True)
def _master(monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER)


def _u(uid="u1", email="x@acme-corp.io", is_admin=False, is_active=True):
    return SimpleNamespace(id=uid, email=email, is_admin=is_admin, is_active=is_active)


SUPER = _u("master-admin", MASTER, is_admin=True)
ADOPTED_SUPER = _u("u-old", MASTER, is_admin=True)       # adopted by the master login
GLOBAL_ADMIN = _u("u-oidc", "ops@acme-corp.io", is_admin=True)
PLAIN = _u()
SIGNUP_WITH_MASTER_EMAIL = _u("u-squat", MASTER, is_admin=False)


@pytest.mark.parametrize("user, expected", [
    (SUPER, True),
    (ADOPTED_SUPER, True),
    (GLOBAL_ADMIN, False),
    (PLAIN, False),
    (SIGNUP_WITH_MASTER_EMAIL, False),
    (_u("master-admin", MASTER, is_admin=True, is_active=False), False),
    (_u("master-admin", "renamed@acme-corp.io", is_admin=True), True),
    (None, False),
])
def test_who_is_the_superadmin(user, expected):
    assert is_superadmin(user) is expected


def test_no_master_email_means_only_the_fixed_id(monkeypatch):
    monkeypatch.delenv("CELMIS_MASTER_EMAIL")
    assert is_superadmin(SUPER)
    assert not is_superadmin(ADOPTED_SUPER)


def _expected(actor, actor_role, current, new) -> bool:
    """The product owner's rule, restated independently of the implementation."""
    if new is not None and new not in VALID_WORKSPACE_ROLES:
        return False
    if actor is SUPER:
        return True
    if actor_role not in ("owner", "admin"):
        return False
    touched = {r for r in (current, new) if r is not None}
    return touched <= {"member", "viewer"}


ROLES = [None, *sorted(VALID_WORKSPACE_ROLES)]


@pytest.mark.parametrize("actor", [SUPER, GLOBAL_ADMIN, PLAIN], ids=["super", "global", "plain"])
def test_the_whole_matrix(actor):
    wrong = []
    for actor_role, current, new in itertools.product(ROLES, ROLES, ROLES + ["root"]):
        got = can_change(actor, actor_role, current, new)
        if got != _expected(actor, actor_role, current, new):
            wrong.append((actor_role, current, new, got))
    assert not wrong, wrong


def test_the_named_cases():
    # admin cannot demote the owner, another admin, or an editor
    for target in ("owner", "admin", "editor"):
        assert not can_change(PLAIN, "admin", target, "member")
        assert not can_change(PLAIN, "admin", target, None)
    # nor grant any of them
    for role in PRIVILEGED_ROLES:
        assert not can_change(PLAIN, "owner", None, role)
    # member <-> viewer, add, remove
    assert can_change(PLAIN, "admin", "member", "viewer")
    assert can_change(PLAIN, "owner", "viewer", "member")
    assert can_change(PLAIN, "admin", None, "viewer")
    assert can_change(PLAIN, "admin", "member", None)
    # editor manages nobody; a global admin is not a superadmin
    assert not can_change(PLAIN, "editor", "viewer", "member")
    assert not can_change(GLOBAL_ADMIN, None, "member", "admin")
    assert can_change(SUPER, None, "owner", "viewer")


def test_grantable_roles():
    assert grantable_roles(SUPER, None) == VALID_WORKSPACE_ROLES
    assert grantable_roles(PLAIN, "admin") == DELEGABLE_ROLES == {"member", "viewer"}
    assert grantable_roles(PLAIN, "owner") == DELEGABLE_ROLES
    for role in (None, "editor", "member", "viewer"):
        assert grantable_roles(PLAIN, role) == frozenset()
    assert grantable_roles(GLOBAL_ADMIN, None) == frozenset()


def test_the_web_copy_states_the_same_split():
    """web/lib/roles.ts decides which controls to draw; it must agree on which
    roles are the superadmin's. Read off the array literals, not comments."""
    import re
    from pathlib import Path

    ts = (Path(__file__).resolve().parents[2] / "web" / "lib" / "roles.ts").read_text()

    def array(name: str) -> set[str]:
        m = re.search(rf"export const {name}[^=]*=\s*\[([^\]]*)\]", ts)
        assert m is not None, name
        return set(re.findall(r'"([a-z]+)"', m.group(1)))

    assert array("PRIVILEGED_ROLES") == set(PRIVILEGED_ROLES)
    assert array("DELEGABLE_ROLES") == set(DELEGABLE_ROLES)
    from src.users.roles import TEAM_ROLES

    assert array("TEAM_ROLES") == set(TEAM_ROLES)


def test_nobody_can_sign_up_as_the_master_address(tmp_path):
    """The master login adopts an existing PASSWORD account holding the master
    address. A signup under it before the operator's first master login would
    be adopted — with the signer's password still working — as superadmin."""
    from fastapi import HTTPException

    from src.api.routers.auth import signup
    from src.api.schemas import SignupRequest
    from src.users.store import UserStore

    users = UserStore(tmp_path / "users.db")
    req = SignupRequest(email="ROOT@acme-corp.io", password="a-long-enough-pass-123!")
    with pytest.raises(HTTPException) as exc:
        signup(req, SimpleNamespace(headers={}, client=None), users)
    assert exc.value.status_code == 403
    assert users.get_by_email(MASTER) is None
