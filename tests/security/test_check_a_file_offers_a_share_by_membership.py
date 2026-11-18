"""A shared configuration in the web tab, opened by membership of the receiving workspace.

Workspace A has five members (owner, admin, mg, mr, mn); workspace B owns SHARED
and shares it with A. Membership alone offers SHARED in Check a file while the
share's ceiling on people covers every member the owner has not switched off; a
bigger workspace keeps the token rule. Everything the share itself decides
(live, still the owner's repository, the person not switched off) still applies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from src.onec import review
from tests.security.mcp_world import SHARED, WS_A
from tests.security.share_world import change_share, end_share, make_share

MEMBERS_OF_A = 5


def _opened(world, who: str) -> list[str]:
    with Session(world.engine) as s:
        return review._member_shares(s, world.users[who], WS_A)


def test_a_member_is_offered_a_share_whose_ceiling_covers_the_workspace(mcp_world):
    share = make_share(mcp_world, max_people=MEMBERS_OF_A)
    assert _opened(mcp_world, "mn") == [share]
    decisions = review._shared(mcp_world.users["mn"], WS_A, {})
    assert set(decisions) == {SHARED}
    assert decisions[SHARED].via_share


def test_a_workspace_bigger_than_the_ceiling_keeps_the_token_rule(mcp_world):
    make_share(mcp_world, max_people=MEMBERS_OF_A - 1)
    assert _opened(mcp_world, "mn") == []
    assert review._shared(mcp_world.users["mn"], WS_A, {}) == {}


def test_people_the_owner_switched_off_do_not_count_and_read_nothing(mcp_world):
    share = make_share(mcp_world, max_people=MEMBERS_OF_A - 1, blocked=[mcp_world.users["mr"]])
    assert _opened(mcp_world, "mn") == [share]
    assert set(review._shared(mcp_world.users["mn"], WS_A, {})) == {SHARED}
    assert review._shared(mcp_world.users["mr"], WS_A, {}) == {}


def test_somebody_outside_the_receiving_workspace_is_offered_nothing(mcp_world):
    make_share(mcp_world, max_people=25)
    assert _opened(mcp_world, "outsider") == []
    assert review._shared(mcp_world.users["outsider"], WS_A, {}) == {}


def test_an_ended_or_expired_share_is_offered_to_nobody(mcp_world):
    ended = make_share(mcp_world)
    end_share(mcp_world, ended)
    assert _opened(mcp_world, "mn") == []
    expired = make_share(mcp_world)
    change_share(mcp_world, expired, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert _opened(mcp_world, "mn") == []


def test_a_list_of_blocked_people_that_cannot_be_read_opens_nothing(mcp_world):
    share = make_share(mcp_world)
    change_share(mcp_world, share, blocked_user_ids="everyone")
    assert _opened(mcp_world, "mn") == []
