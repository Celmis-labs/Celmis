"""Giving less than a share gives is free in every build; giving more is the licensed part.

`shares.narrow` is what an owner's PATCH calls: more hidden paths, an earlier end,
`ask` switched off, fewer places (never fewer than the people who can still read
through it: somebody switched off holds no place), one more person switched off. Anything that gives more raises `WideningError`, which says
what it needs. A change stages and does not commit, returns `{field: {before,
after}}` for the audit, and holds from the next call: the rows are read on every
call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.access import shares
from tests.security.mcp_world import SHARED, WILD
from tests.security.share_world import end_share, holder_token, make_share, share_row


@pytest.fixture
def share(mcp_world):
    return make_share(mcp_world, deny_globs=["docs/**"], allow_ask=True, max_people=3,
                      days=10)


def _narrow(world, share_id, **kw):
    with world.session() as s:
        changes = shares.narrow(s, share_id, **kw)
        s.commit()
    return changes


# ─── what narrowing may do ───────────────────────────────────────────


def test_more_hidden_paths_are_added_once_and_reported(mcp_world, share):
    changes = _narrow(mcp_world, share, add_deny=["config/**", "docs/**", " config/** "])
    assert changes == {"deny_globs": {"before": ["docs/**"], "after": ["docs/**", "config/**"]}}
    assert share_row(mcp_world, share).deny_globs == ["docs/**", "config/**"]


def test_nothing_new_to_hide_is_no_change(mcp_world, share):
    assert _narrow(mcp_world, share, add_deny=["docs/**"]) == {}
    assert _narrow(mcp_world, share) == {}


def test_an_earlier_end_is_a_change_and_the_same_end_is_not(mcp_world, share):
    row = share_row(mcp_world, share)
    sooner = datetime.now(UTC) + timedelta(days=2)
    changes = _narrow(mcp_world, share, expires_at=sooner)
    assert set(changes) == {"expires_at"}
    assert changes["expires_at"]["after"] == sooner
    assert changes["expires_at"]["before"].replace(tzinfo=UTC) == row.expires_at.replace(tzinfo=UTC)
    assert _narrow(mcp_world, share, expires_at=sooner) == {}


def test_asking_can_be_switched_off(mcp_world, share):
    assert _narrow(mcp_world, share, allow_ask=False) == {
        "allow_ask": {"before": True, "after": False}}
    assert share_row(mcp_world, share).allow_ask is False


def test_fewer_places_down_to_the_people_who_hold_it(mcp_world, share):
    holder_token(mcp_world, [share], who="mn")
    holder_token(mcp_world, [share], who="mg")
    assert _narrow(mcp_world, share, max_people=2) == {"max_people": {"before": 3, "after": 2}}
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, max_people=1)


def test_a_person_switched_off_holds_no_place(mcp_world, share):
    """Their token still exists but reads nothing, so it must not keep a place in
    `max_people` (the owner could not lower the ceiling, and the place could not go
    to somebody who can read)."""
    mn, mg = mcp_world.users["mn"], mcp_world.users["mg"]
    holder_token(mcp_world, [share], who="mn")
    holder_token(mcp_world, [share], who="mg")
    with mcp_world.session() as s:
        assert set(shares.holders(s, share)) == {mn, mg}
    _narrow(mcp_world, share, block=[mn])
    with mcp_world.session() as s:
        assert set(shares.holders(s, share)) == {mg}
        assert shares.has_room(s, share, mcp_world.users["mr"])
        assert shares.has_room(s, share, mg)


def test_the_ceiling_goes_down_to_the_people_left_once_somebody_is_switched_off(mcp_world, share):
    mn = mcp_world.users["mn"]
    holder_token(mcp_world, [share], who="mn")
    holder_token(mcp_world, [share], who="mg")
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, max_people=1)
    _narrow(mcp_world, share, block=[mn])
    assert _narrow(mcp_world, share, max_people=1) == {"max_people": {"before": 3, "after": 1}}


def test_switching_somebody_off_and_lowering_the_ceiling_can_be_one_request(mcp_world, share):
    mn = mcp_world.users["mn"]
    holder_token(mcp_world, [share], who="mn")
    holder_token(mcp_world, [share], who="mg")
    changes = _narrow(mcp_world, share, max_people=1, block=[mn])
    assert set(changes) == {"max_people", "blocked_user_ids"}
    row = share_row(mcp_world, share)
    assert row.max_people == 1 and row.blocked_user_ids == [mn]


def test_switching_off_somebody_who_holds_nothing_frees_nothing(mcp_world, share):
    """The relief is only for a person who really held a place: blocking a stranger
    does not let the ceiling drop below the people who still read."""
    holder_token(mcp_world, [share], who="mn")
    holder_token(mcp_world, [share], who="mg")
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, max_people=1, block=[mcp_world.users["mr"]])
    row = share_row(mcp_world, share)
    assert row.max_people == 3 and row.blocked_user_ids == []


def test_a_list_of_switched_off_people_that_cannot_be_read_counts_everybody(mcp_world, share):
    """Damage in the column is not a licence to free places: the person stays counted."""
    from tests.security.share_world import change_share

    holder_token(mcp_world, [share], who="mn")
    change_share(mcp_world, share, blocked_user_ids="not-a-list")
    with mcp_world.session() as s:
        assert set(shares.holders(s, share)) == {mcp_world.users["mn"]}


def test_a_person_can_be_switched_off_and_only_once(mcp_world, share):
    first = _narrow(mcp_world, share, block=["u-mn"])
    assert first == {"blocked_user_ids": {"before": [], "after": ["u-mn"]}}
    assert _narrow(mcp_world, share, block=["u-mn", 7, None, ""]) == {}
    assert share_row(mcp_world, share).blocked_user_ids == ["u-mn"]


def test_every_change_at_once_is_reported_together(mcp_world, share):
    changes = _narrow(mcp_world, share, add_deny=["x/**"], allow_ask=False, max_people=2,
                      block=["u-mr"], expires_at=datetime.now(UTC) + timedelta(days=1))
    assert set(changes) == {"deny_globs", "allow_ask", "max_people", "blocked_user_ids",
                            "expires_at"}


# ─── what it may not do ──────────────────────────────────────────────


def test_a_later_end_is_widening(mcp_world, share):
    with mcp_world.session() as s, pytest.raises(shares.WideningError) as err:
        shares.narrow(s, share, expires_at=datetime.now(UTC) + timedelta(days=11))
    assert str(err.value) == shares.WIDENING


def test_switching_asking_on_is_widening(mcp_world):
    off = make_share(mcp_world, allow_ask=False)
    with mcp_world.session() as s, pytest.raises(shares.WideningError):
        shares.narrow(s, off, allow_ask=True)


def test_more_places_are_widening(mcp_world, share):
    with mcp_world.session() as s, pytest.raises(shares.WideningError):
        shares.narrow(s, share, max_people=4)


def test_a_widening_among_narrowings_changes_nothing(mcp_world, share):
    """Staged, not committed: the caller rolls back, and nothing of it holds."""
    with mcp_world.session() as s, pytest.raises(shares.WideningError):
        shares.narrow(s, share, add_deny=["x/**"], allow_ask=False, max_people=5)
        s.commit()
    row = share_row(mcp_world, share)
    assert row.deny_globs == ["docs/**"] and row.allow_ask is True and row.max_people == 3


def test_an_end_in_the_past_is_a_revocation_not_a_narrowing(mcp_world, share):
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, expires_at=datetime.now(UTC) - timedelta(minutes=1))


@pytest.mark.parametrize("people", [0, 26, -3, "many", [3]])
def test_a_number_of_places_outside_one_to_twenty_five_is_refused(mcp_world, share, people):
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, max_people=people)


def test_nothing_can_be_narrowed_after_the_share_has_ended(mcp_world, share):
    end_share(mcp_world, share)
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, add_deny=["x/**"])


@pytest.mark.parametrize("share_id", ["not-an-id", "00000000-0000-4000-8000-000000000000", "", None])
def test_a_share_that_is_not_there_cannot_be_narrowed(mcp_world, share_id):
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share_id, add_deny=["x/**"])


def test_the_limits_on_hidden_paths_and_switched_off_people_hold(mcp_world, share):
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, add_deny=[f"d{i}/**" for i in range(shares.MAX_DENY_GLOBS)])
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, block=[f"u-{i}" for i in range(shares.MAX_BLOCKED + 1)])
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, share, block=["u-\x00x"])


# ─── a narrowing is staged, and holds from the next call ─────────────


def test_a_narrowing_that_is_rolled_back_leaves_the_share_as_it_was(mcp_world, share):
    with mcp_world.session() as s:
        shares.narrow(s, share, add_deny=["x/**"], allow_ask=False)
        s.rollback()
    row = share_row(mcp_world, share)
    assert row.deny_globs == ["docs/**"] and row.allow_ask is True


def test_a_narrowing_holds_on_the_holders_next_call(mcp_world, share):
    from src.mcp_server.identity import caller_access

    bearer, _view = holder_token(mcp_world, [share], own=[WILD])
    mcp_world.as_(bearer)
    before = caller_access([SHARED])[1][SHARED]
    assert before.ask_ok and before.path_visible("config/settings.yaml")

    _narrow(mcp_world, share, add_deny=["config/**"], allow_ask=False)
    after = caller_access([SHARED])[1][SHARED]
    assert not after.ask_ok and not after.path_visible("config/settings.yaml")
    assert after.path_visible("src/app.py")

    _narrow(mcp_world, share, block=[mcp_world.users["mn"]])
    assert not caller_access([SHARED])[1][SHARED].researchable

