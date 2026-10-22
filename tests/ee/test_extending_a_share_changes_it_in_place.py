# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Extending a share: a later end, other hidden paths, questions, people, in place.

``POST /api/sharing-grants/{id}/extend`` changes a RUNNING share without making a new
one, so the tokens that carry its id keep working and nobody is issued anything again.
It is the licensed half of what the free ``PATCH /api/sharing/shares/{id}`` refuses to
do (that route only narrows). Every part is optional, at least one is needed, and a
request is all or nothing. The owner's audit trail gets the before and after of what
changed.

World: `tests/api/sharing_world.py` under a licence that grants ``sharing``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.rbac_world import B_REPO
from tests.api.sharing_world import (
    HOLDER,
    WS_A,
    WS_B,
    actions,
    add_share,
    audit_rows,
    share_row,
)
from tests.ee.sharing_support import extend

ENDED = "This share has ended. Share the repository again."
CHANGED = "The repository is no longer the one that was shared. Share it again."


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hold(w, share_id: str, who: str = HOLDER) -> str:
    """A live developer token of ``who`` (a person of A) that carries the share."""
    from src.mcp_server import token_store
    from tests.api.rbac_world import A_REPO

    _token, view = token_store.mint(
        kind="pat", workspace_id=WS_A, user_id=w.uid(who), issued_by="master-admin",
        label="t", patterns=[A_REPO], profile="dev", expires_in_days=30, shared=[share_id])
    return view.id


async def _narrow(w, share_id: str, body: dict):
    return await w.client.patch(f"/api/sharing/shares/{share_id}", json=body,
                                headers=w.h("admin_b", "ws-b"))


# ─── the end ─────────────────────────────────────────────────────────


async def test_a_later_end_is_counted_from_now_and_the_share_keeps_its_id(gw):
    share_id = add_share(gw, days=5)

    r = await extend(gw, share_id, {"days": 60})

    assert r.status_code == 200, r.text
    assert r.json()["share_id"] == share_id and r.json()["status"] == "active"
    left = _aware(share_row(gw, share_id).expires_at) - datetime.now(UTC)
    assert timedelta(days=60) - timedelta(minutes=1) <= left <= timedelta(days=60)


@pytest.mark.parametrize("days", [1, 4])
async def test_an_end_that_is_not_later_than_the_present_one_is_refused(gw, days):
    """Ending a share sooner is narrowing it, which is free and has its own route."""
    share_id = add_share(gw, days=5)
    before = share_row(gw, share_id).expires_at

    r = await extend(gw, share_id, {"days": days})

    assert r.status_code == 422, r.text
    assert "not later" in r.json()["detail"]
    assert share_row(gw, share_id).expires_at == before


@pytest.mark.parametrize("days", [0, -1, 91, 10_000, "60", 60.5, True])
async def test_an_end_outside_one_day_and_the_ceiling_is_refused(gw, days):
    share_id = add_share(gw, days=5)

    r = await extend(gw, share_id, {"days": days, "max_people": 6})

    assert r.status_code == 422, (days, r.text)
    assert share_row(gw, share_id).max_people == 5


async def test_the_ceiling_is_the_one_the_operator_set_for_tokens(gw, monkeypatch):
    from src.config import get_settings

    share_id = add_share(gw, days=2)
    monkeypatch.setenv("CELMIS_MCP_TOKEN_MAX_DAYS", "10")
    get_settings.cache_clear()

    assert (await extend(gw, share_id, {"days": 11})).status_code == 422
    assert (await extend(gw, share_id, {"days": 10})).status_code == 200


async def test_the_ceiling_counts_from_now_so_a_share_can_be_extended_again_and_again(gw):
    share_id = add_share(gw, days=89)

    assert (await extend(gw, share_id, {"days": 90})).status_code == 200
    # Pretend the first stretch is nearly over: still extendable, not stuck at the
    # ceiling of the day it was made.
    from sqlalchemy.orm import Session

    from src.db.models import RepoShare

    with Session(gw.engine) as s:
        s.get(RepoShare, share_id).expires_at = datetime.now(UTC) + timedelta(days=1)
        s.commit()
    assert (await extend(gw, share_id, {"days": 90})).status_code == 200


# ─── the hidden paths ────────────────────────────────────────────────


async def test_the_list_of_hidden_paths_is_replaced_not_added_to(gw):
    share_id = add_share(gw, deny=["old/**", "keep/**"])

    r = await extend(gw, share_id, {"deny_globs": ["keep/**", "/new/**"]})

    assert r.status_code == 200, r.text
    assert share_row(gw, share_id).deny_globs == ["keep/**", "new/**"]
    assert r.json()["deny_globs"] == ["keep/**", "new/**"]


async def test_an_empty_list_hides_nothing_of_the_shares_own(gw):
    share_id = add_share(gw, deny=["old/**"])

    assert (await extend(gw, share_id, {"deny_globs": []})).status_code == 200

    assert share_row(gw, share_id).deny_globs == []


async def test_the_secret_floor_is_not_in_the_list_and_stays_whatever_it_says(gw):
    """Emptying the share's own list opens nothing the floor hides: the decision still
    hides key files, and the owner's rules for the repository are not touched."""
    from src.access.effective import Principal, effective_access

    share_id = add_share(gw, deny=["old/**"])
    await extend(gw, share_id, {"deny_globs": []})

    got = effective_access(
        Principal(user_id=gw.uid(HOLDER), repo_patterns=(), share_ids=(share_id,)),
        WS_A, [B_REPO])[B_REPO]
    assert got.path_visible("src/app.py")
    assert got.path_visible("old/readme.md"), "the share's own list is empty now"
    assert not got.path_visible(".env")
    assert not got.path_visible("keys/server.pem")


async def test_the_decision_on_the_next_call_follows_the_new_list_with_no_token_change(gw):
    from src.access.effective import Principal, effective_access

    share_id = add_share(gw, deny=["old/**"])
    token_id = _hold(gw, share_id)

    def seen(path: str) -> bool:
        return effective_access(
            Principal(user_id=gw.uid(HOLDER), repo_patterns=(), share_ids=(share_id,)),
            WS_A, [B_REPO])[B_REPO].path_visible(path)

    assert not seen("old/a.py") and seen("new/a.py")
    await extend(gw, share_id, {"deny_globs": ["new/**"]})
    assert seen("old/a.py") and not seen("new/a.py")
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    with Session(gw.engine) as s:
        assert s.get(McpToken, token_id).shared_repos == [share_id], "the token was not reissued"


@pytest.mark.parametrize("globs", [["a" * 201], ["bad\x00glob"], [f"d{n}/**" for n in range(101)],
                                   [7], "config/**"])
async def test_hidden_paths_that_cannot_be_kept_are_refused_and_the_share_is_left_alone(gw, globs):
    share_id = add_share(gw, deny=["old/**"])

    r = await extend(gw, share_id, {"deny_globs": globs, "allow_ask": True})

    assert r.status_code == 422, r.text
    row = share_row(gw, share_id)
    assert row.deny_globs == ["old/**"] and row.allow_ask is False


# ─── questions, people ───────────────────────────────────────────────


async def test_questions_can_be_switched_on_and_off(gw):
    share_id = add_share(gw)

    assert (await extend(gw, share_id, {"allow_ask": True})).json()["allow_ask"] is True
    assert share_row(gw, share_id).allow_ask is True
    assert (await extend(gw, share_id, {"allow_ask": False})).json()["allow_ask"] is False


@pytest.mark.parametrize("value", ["yes", 1, "true", 0])
async def test_questions_are_a_yes_or_a_no_and_nothing_else(gw, value):
    share_id = add_share(gw)

    assert (await extend(gw, share_id, {"allow_ask": value})).status_code == 422
    assert share_row(gw, share_id).allow_ask is False


async def test_the_ceiling_on_people_can_go_up_to_twenty_five(gw):
    share_id = add_share(gw, people=5)

    assert (await extend(gw, share_id, {"max_people": 25})).status_code == 200
    assert share_row(gw, share_id).max_people == 25
    assert (await extend(gw, share_id, {"max_people": 26})).status_code == 422
    assert (await extend(gw, share_id, {"max_people": 0})).status_code == 422
    assert share_row(gw, share_id).max_people == 25


async def test_the_ceiling_cannot_go_below_the_people_who_hold_a_token(gw):
    share_id = add_share(gw, people=5)
    _hold(gw, share_id, "member_a")
    _hold(gw, share_id, "viewer_a")

    r = await extend(gw, share_id, {"max_people": 1})

    assert r.status_code == 422 and "block some first" in r.json()["detail"]
    assert share_row(gw, share_id).max_people == 5
    assert (await extend(gw, share_id, {"max_people": 2})).status_code == 200


# ─── switching a person back on ──────────────────────────────────────


async def test_a_person_the_owner_switched_off_can_be_switched_back_on(gw):
    share_id = add_share(gw)
    me, other = gw.uid("member_a"), gw.uid("viewer_a")
    assert (await _narrow(gw, share_id, {"block": [me, other]})).status_code == 200

    r = await extend(gw, share_id, {"unblock": [me]})

    assert r.status_code == 200, r.text
    assert share_row(gw, share_id).blocked_user_ids == [other]
    assert [p["user_id"] for p in r.json()["blocked"]] == [other]


async def test_a_person_who_was_not_switched_off_changes_nothing_and_writes_nothing(gw):
    share_id = add_share(gw, blocked=[gw.uid("viewer_a")])

    r = await extend(gw, share_id, {"unblock": [gw.uid("member_a"), "nobody-at-all"]})

    assert r.status_code == 200
    assert share_row(gw, share_id).blocked_user_ids == [gw.uid("viewer_a")]
    assert actions(gw, "sharing.extended") == []


async def test_switching_back_on_cannot_take_the_holders_above_the_ceiling(gw):
    """One place, held by someone else: the person who was switched off held a token
    while they were; with them back on, two would read through one place. The
    request is refused whole."""
    share_id = add_share(gw, people=1)
    me = gw.uid("member_a")
    _hold(gw, share_id, "member_a")
    assert (await _narrow(gw, share_id, {"block": [me]})).status_code == 200
    _hold(gw, share_id, "viewer_a")

    r = await extend(gw, share_id, {"unblock": [me], "allow_ask": True})

    assert r.status_code == 422, r.text
    row = share_row(gw, share_id)
    assert row.blocked_user_ids == [me] and row.allow_ask is False


@pytest.mark.parametrize("people", [[7], ["x" * 201], ["a\x00b"], "member"])
async def test_something_that_is_not_a_list_of_user_ids_is_refused(gw, people):
    share_id = add_share(gw, blocked=["u1"])

    r = await extend(gw, share_id, {"unblock": people})

    assert r.status_code == 422, r.text
    assert share_row(gw, share_id).blocked_user_ids == ["u1"]


# ─── all or nothing ──────────────────────────────────────────────────


async def test_one_bad_part_changes_none_of_the_others(gw):
    share_id = add_share(gw, days=5, deny=["old/**"])
    before = share_row(gw, share_id)

    r = await extend(gw, share_id, {"days": 60, "deny_globs": ["new/**"], "allow_ask": True,
                                    "max_people": 99})

    assert r.status_code == 422
    after = share_row(gw, share_id)
    assert (after.expires_at, after.deny_globs, after.allow_ask, after.max_people) == (
        before.expires_at, ["old/**"], False, 5)
    assert actions(gw, "sharing.extended") == []


async def test_every_part_at_once(gw):
    share_id = add_share(gw, days=5, deny=["old/**"], blocked=[gw.uid("viewer_a")])

    r = await extend(gw, share_id, {"days": 30, "deny_globs": [], "allow_ask": True,
                                    "max_people": 8, "unblock": [gw.uid("viewer_a")]})

    assert r.status_code == 200, r.text
    row = share_row(gw, share_id)
    assert (row.deny_globs, row.allow_ask, row.max_people, row.blocked_user_ids) == (
        [], True, 8, [])
    (entry,) = audit_rows(gw, "sharing.extended")
    assert set(entry["detail"]["changes"]) == {
        "expires_at", "deny_globs", "allow_ask", "max_people", "blocked_user_ids"}


# ─── nothing to do, and what may not be sent ─────────────────────────


@pytest.mark.parametrize("body", [{}, {"unblock": []}, {"deny_globs": None}])
async def test_a_request_that_asks_for_nothing_is_refused(gw, body):
    share_id = add_share(gw)

    r = await extend(gw, share_id, body)

    assert r.status_code == 422 and r.json()["detail"] == "Nothing to change.", r.text
    assert actions(gw, "sharing.extended") == []


async def test_asking_for_what_the_share_already_is_changes_and_writes_nothing(gw):
    share_id = add_share(gw, deny=["a/**"], people=5)

    r = await extend(gw, share_id, {"deny_globs": ["a/**"], "allow_ask": False, "max_people": 5})

    assert r.status_code == 200
    assert actions(gw, "sharing.extended") == []


@pytest.mark.parametrize("extra", [
    {"expires_at": "2099-01-01T00:00:00Z"}, {"block": ["u"]}, {"add_deny": ["x/**"]},
    {"repo_slug": "other"}, {"workspace": "ws-c"}, {"revoked_at": None},
])
async def test_a_field_the_request_does_not_define_is_refused_not_ignored(gw, extra):
    share_id = add_share(gw)

    r = await extend(gw, share_id, {"allow_ask": True, **extra})

    assert r.status_code == 422, r.text
    assert share_row(gw, share_id).allow_ask is False


# ─── a share that has ended ──────────────────────────────────────────


@pytest.mark.parametrize("how", ["revoked", "lapsed"])
async def test_a_share_that_has_ended_is_shared_again_not_extended(gw, how):
    share_id = add_share(gw, revoked=how == "revoked", days=-1 if how == "lapsed" else 30)

    r = await extend(gw, share_id, {"days": 60})

    assert r.status_code == 409 and r.json()["detail"] == ENDED
    assert share_row(gw, share_id).expires_at is not None
    assert actions(gw, "sharing.extended") == []


async def test_a_share_that_ended_with_its_repository_is_not_brought_back(gw):
    """The repository was removed: the share was ended by `remove_repo`. Extending it
    would be the way to revive it, which is exactly what ending it on removal prevents."""
    share_id = add_share(gw)
    from sqlalchemy.orm import Session

    from src.access import shares as repo_shares

    with Session(gw.engine) as s:
        repo_shares.revoke_for_repo(s, WS_B, B_REPO)
        s.commit()

    assert (await extend(gw, share_id, {"days": 60})).status_code == 409


async def test_a_damaged_share_is_revoked_and_made_again_not_extended(gw):
    share_id = add_share(gw)
    from sqlalchemy.orm import Session

    from src.db.models import RepoShare

    with Session(gw.engine) as s:
        s.get(RepoShare, share_id).deny_globs = "not-a-list"
        s.commit()

    r = await extend(gw, share_id, {"deny_globs": []})

    assert r.status_code == 422 and "damaged" in r.json()["detail"]
    assert share_row(gw, share_id).deny_globs == "not-a-list"


async def test_a_share_whose_repository_is_no_longer_the_one_shared_is_not_extended(gw):
    """The share names a provider and a name. If the slug now points at another
    repository (or at none) the reading refuses the share already; extending it would
    only write down a consent for a repository the owner never agreed to."""
    from sqlalchemy.orm import Session

    from src.db.models import RepoShare

    share_id = add_share(gw, days=5)
    with Session(gw.engine) as s:
        s.get(RepoShare, share_id).repo_full_name = "another-owner/another-repo"
        s.commit()
    before = share_row(gw, share_id)

    r = await extend(gw, share_id, {"days": 60, "allow_ask": True})

    assert r.status_code == 409 and r.json()["detail"] == CHANGED, r.text
    after = share_row(gw, share_id)
    assert (after.expires_at, after.allow_ask) == (before.expires_at, before.allow_ask)
    assert actions(gw, "sharing.extended") == []


async def test_a_refused_extension_leaves_the_row_alone_even_if_the_caller_commits(gw):
    """The service is called by a router that rolls back, but also by scripts and tests
    that may not: the part that fails last (people above the ceiling) must not leave the
    parts staged before it behind."""
    from sqlalchemy.orm import Session

    from src.access.shares import ShareError
    from src.ee.sharing import service

    share_id = add_share(gw, people=1, days=5)
    me = gw.uid("member_a")
    _hold(gw, share_id, "member_a")
    assert (await _narrow(gw, share_id, {"block": [me]})).status_code == 200
    _hold(gw, share_id, "viewer_a")
    before = share_row(gw, share_id)

    with Session(gw.engine) as s:
        with pytest.raises(ShareError, match="fewer places"):
            service.extend_share(s, share_id, days=60, allow_ask=True, deny_globs=["new/**"],
                                 unblock=[me])
        s.commit()

    after = share_row(gw, share_id)
    assert (after.expires_at, after.deny_globs, after.allow_ask, after.blocked_user_ids,
            after.max_people) == (before.expires_at, before.deny_globs, False, [me], 1)


# ─── the audit ───────────────────────────────────────────────────────


async def test_the_owners_trail_gets_the_before_and_after_of_what_changed(gw):
    share_id = add_share(gw, days=5, deny=["old/**"])
    before = share_row(gw, share_id)

    await extend(gw, share_id, {"days": 40, "deny_globs": ["new/**"], "allow_ask": True})

    assert actions(gw, "sharing.") == [("sharing.extended", WS_B, share_id)]
    (entry,) = audit_rows(gw, "sharing.extended")
    assert entry["actor_id"] == gw.uid("admin_b") and entry["ip"]
    changes = entry["detail"]["changes"]
    assert set(changes) == {"expires_at", "deny_globs", "allow_ask"}
    assert changes["deny_globs"] == {"before": ["old/**"], "after": ["new/**"]}
    assert changes["allow_ask"] == {"before": False, "after": True}
    assert datetime.fromisoformat(changes["expires_at"]["before"]) == _aware(before.expires_at)
    assert datetime.fromisoformat(changes["expires_at"]["after"]) > _aware(before.expires_at)
    assert not [a for a in gw.audit if a.get("workspace_id") == WS_A], \
        "the receiving workspace's trail has no row for it"


async def test_the_superadmin_can_extend_and_the_trail_names_them(gw):
    share_id = add_share(gw)

    r = await extend(gw, share_id, {"allow_ask": True}, who="su", ws="ws-a")

    assert r.status_code == 200
    (entry,) = audit_rows(gw, "sharing.extended")
    assert entry["actor_id"] == gw.uid("su") and entry["workspace_id"] == WS_B
