"""Enrolling a member must not be a way to reach a stranger's account.

Three endpoints were individually reasonable and together were an account
takeover, reachable from a fresh signup:

  GET  /api/users/directory                        — every active account on
       the installation, to any signed-in caller: a tenant roster, and the ids
       needed for the next step.
  PUT  /api/workspaces/{ws}/members/{user_id}      — wrote whatever id it was
       given into the caller's own workspace. No existence check, no consent.
  POST /api/workspaces/{ws}/members/{user_id}/reset-link
                                                   — treats membership as
       authority to mint a live password-reset link, refusing only when the
       target is a global admin.

So: sign up, harvest ids, enrol a victim into your own workspace, mint their
reset link, set their password. Everything but a global admin was takeable.

The fix is at step two — an id may only name someone the caller already shares
a workspace with — plus scoping the directory to the same set. These tests pin
the boundary rule itself, which is plain set logic, rather than standing up
Postgres and the auth stack.
"""

from __future__ import annotations

import pytest


def may_enrol(
    caller_id: str,
    target_id: str,
    memberships: dict[str, set[str]],
    *,
    caller_is_global_admin: bool = False,
    target_exists: bool = True,
    target_active: bool = True,
    platform_ids: frozenset[str] = frozenset({"default", "master-admin"}),
) -> bool:
    """The rule from upsert_member, isolated.

    `memberships` maps workspace_id -> set of member user ids.
    """
    if not target_exists or not target_active or target_id in platform_ids:
        return False
    if caller_is_global_admin:
        return True
    peers = {caller_id}
    for members in memberships.values():
        if caller_id in members:
            peers |= members
    return target_id in peers


def test_a_stranger_cannot_be_enrolled():
    """The takeover's first step, refused.

    `attacker` owns a workspace of their own; `victim` works elsewhere. They
    share nothing, so the id is not a handle the attacker may use.
    """
    memberships = {"ws-attacker": {"attacker"}, "ws-acme": {"victim", "colleague"}}
    assert not may_enrol("attacker", "victim", memberships)


def test_a_colleague_can_be_enrolled():
    """The legitimate case this must not break.

    Two people already in one workspace; an admin of a second workspace they
    also own may add the colleague to it without a round trip through email.
    """
    memberships = {"ws-acme": {"admin", "colleague"}, "ws-side": {"admin"}}
    assert may_enrol("admin", "colleague", memberships)


def test_platform_accounts_are_never_enrollable():
    """Not cosmetic: these are the two accounts with is_admin set by seed.

    They also carry no password hash, so a reset link against one is a way to
    give it a password that did not exist before.
    """
    memberships = {"ws": {"admin", "default", "master-admin"}}
    for account in ("default", "master-admin"):
        assert not may_enrol("admin", account, memberships), account


def test_an_id_that_names_nobody_is_refused():
    """The old handler wrote unverified ids, leaving membership rows pointing
    at accounts that never existed — WorkspaceMember.user_id has no foreign
    key, so nothing else would have caught it."""
    assert not may_enrol("admin", "ghost", {"ws": {"admin"}}, target_exists=False)


def test_a_deactivated_account_is_refused():
    assert not may_enrol("admin", "gone", {"ws": {"admin", "gone"}}, target_active=False)


def test_a_global_admin_still_manages_anyone():
    """Platform administration is a real role and keeps working — except for
    the platform's own accounts, which stay excluded for everyone."""
    memberships = {"ws-acme": {"victim"}}
    assert may_enrol("root", "victim", memberships, caller_is_global_admin=True)
    assert not may_enrol("root", "default", memberships, caller_is_global_admin=True)


@pytest.mark.parametrize("caller", ["loner", "newcomer"])
def test_a_user_with_no_shared_workspace_can_reach_only_themselves(caller):
    """A fresh signup gets a personal workspace and nothing else. The set of
    ids they may name is exactly {themselves}."""
    memberships = {f"ws-{caller}": {caller}, "ws-acme": {"a", "b", "c"}}
    for other in ("a", "b", "c"):
        assert not may_enrol(caller, other, memberships)
    assert may_enrol(caller, caller, memberships)
