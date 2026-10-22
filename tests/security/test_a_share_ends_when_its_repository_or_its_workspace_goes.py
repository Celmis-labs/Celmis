"""A share ends with the thing it depended on, in the transaction that ends that thing.

Removing a repository ends every live share of it; deleting a workspace ends every
share it gives and every share it receives; an owner or a superadmin can end one
share. The rows stay as history (who ended it, when, and why), the unique index lets
a new share of the same repository start from nothing, and none of these helpers
commits: the caller commits them with whatever made them necessary.

The identity check on the call (is it still the repository that was shared) is
the second lock; ending the rows is the first, and the only one that survives a
repository registered again under the same name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.access import shares
from tests.security.mcp_world import ALL_A, FOREIGN, GRANTED, SHARED, WS_A, WS_B, past
from tests.security.share_world import holder_token, make_share, share_row

THIRD = "wsid-c"


def _commit(world, fn, *args, **kw):
    with world.session() as s:
        out = fn(s, *args, **kw)
        s.commit()
    return out


def test_one_share_is_ended_once_and_remembers_who_and_why(mcp_world):
    s1 = make_share(mcp_world)
    assert _commit(mcp_world, shares.revoke_share, s1, by="u-owner") is True
    row = share_row(mcp_world, s1)
    assert row.revoked_at is not None and row.revoked_by == "u-owner"
    assert row.revoke_reason == "owner"
    assert _commit(mcp_world, shares.revoke_share, s1, by="u-other") is False
    assert share_row(mcp_world, s1).revoked_by == "u-owner", "the first ending is the record"


def test_a_share_that_is_not_there_is_not_ended(mcp_world):
    for share_id in ("not-an-id", "00000000-0000-4000-8000-000000000000", ""):
        assert _commit(mcp_world, shares.revoke_share, share_id, by="u") is False


def test_a_reason_nobody_knows_is_refused(mcp_world):
    s1 = make_share(mcp_world)
    with mcp_world.session() as s, pytest.raises(shares.ShareError):
        shares.revoke_share(s, s1, by="u", reason="because")
    assert share_row(mcp_world, s1).revoked_at is None


def test_the_name_of_whoever_ended_it_is_kept_short(mcp_world):
    s1 = make_share(mcp_world)
    _commit(mcp_world, shares.revoke_share, s1, by="x" * 500)
    assert len(share_row(mcp_world, s1).revoked_by) == 200


def test_ending_a_repository_ends_its_shares_to_everybody_and_no_others(mcp_world):
    to_a = make_share(mcp_world)
    to_c = make_share(mcp_world, grantee=THIRD)
    other_repo = make_share(mcp_world, slug=FOREIGN)
    other_owner = make_share(mcp_world, slug=GRANTED, owner=WS_A, grantee=WS_B)
    ended = _commit(mcp_world, shares.revoke_for_repo, WS_B, SHARED, by="u-owner")
    assert sorted(ended) == sorted([to_a, to_c])
    assert share_row(mcp_world, to_a).revoke_reason == "repo_removed"
    assert share_row(mcp_world, other_repo).revoked_at is None
    assert share_row(mcp_world, other_owner).revoked_at is None
    assert _commit(mcp_world, shares.revoke_for_repo, WS_B, SHARED) == []


def test_a_purge_can_say_so(mcp_world):
    s1 = make_share(mcp_world)
    _commit(mcp_world, shares.revoke_for_repo, WS_B, SHARED, reason="workspace_deleted")
    assert share_row(mcp_world, s1).revoke_reason == "workspace_deleted"


def test_deleting_a_workspace_ends_what_it_gives_and_what_it_receives(mcp_world):
    gives = make_share(mcp_world)                                    # B -> A
    receives = make_share(mcp_world, slug=GRANTED, owner=WS_A, grantee=WS_B)   # A -> B
    unrelated = make_share(mcp_world, grantee=THIRD)                 # B -> C
    ended = _commit(mcp_world, shares.revoke_for_workspace, WS_A, by="u-su")
    assert sorted(ended) == sorted([gives, receives])
    assert share_row(mcp_world, gives).revoke_reason == "workspace_deleted"
    assert share_row(mcp_world, unrelated).revoked_at is None


def test_the_helpers_stage_and_do_not_commit(mcp_world):
    s1 = make_share(mcp_world)
    with mcp_world.session() as s:
        assert shares.revoke_for_repo(s, WS_B, SHARED) == [s1]
        s.rollback()
    assert share_row(mcp_world, s1).revoked_at is None


def test_a_history_row_does_not_stop_a_new_share_of_the_same_repository(mcp_world):
    first = make_share(mcp_world)
    _commit(mcp_world, shares.revoke_share, first, by="u")
    second = make_share(mcp_world)
    assert second != first and share_row(mcp_world, second).revoked_at is None


def test_a_token_that_carried_the_ended_share_reads_nothing_through_it_and_a_new_share_is_not_its(
        mcp_world):
    from src.mcp_server.identity import caller_access

    first = make_share(mcp_world)
    bearer, view = holder_token(mcp_world, [first])
    _commit(mcp_world, shares.revoke_for_repo, WS_B, SHARED)
    second = make_share(mcp_world)                                   # the owner shares it again
    mcp_world.as_(bearer)
    assert not caller_access([SHARED])[1][SHARED].researchable
    assert second not in mcp_world.row(view.id).shared_repos


def test_the_status_of_a_share_is_what_its_rows_say(mcp_world):
    now = datetime.now(UTC)
    live = share_row(mcp_world, make_share(mcp_world))
    lapsed = share_row(mcp_world, make_share(mcp_world, slug=FOREIGN, expires_at=past(1)))
    ended = make_share(mcp_world, slug=GRANTED, owner=WS_A, grantee=WS_B)
    _commit(mcp_world, shares.revoke_share, ended, by="u")
    assert shares.status_of(live, now) == "active"
    assert shares.status_of(lapsed, now) == "expired"
    assert shares.status_of(share_row(mcp_world, ended), now) == "revoked"
    assert shares.status_of(live, now + timedelta(days=31)) == "expired"


def test_a_share_that_lapsed_and_was_revoked_is_revoked(mcp_world):
    s1 = make_share(mcp_world, expires_at=past(1))
    _commit(mcp_world, shares.revoke_share, s1, by="u", reason="expired")
    assert shares.status_of(share_row(mcp_world, s1)) == "revoked"


def test_a_token_page_describes_each_share_by_who_gives_it_and_when_it_ends(mcp_world):
    s1 = make_share(mcp_world, days=5)
    elsewhere = make_share(mcp_world, grantee=THIRD)
    with mcp_world.session() as s:
        [one] = shares.describe(s, [s1], workspace_id=WS_A)
        assert set(one) == {"share_id", "slug", "owner_name", "expires_at", "status"}
        assert one["owner_name"] == "Bravo" and one["slug"] == SHARED and one["status"] == "active"
        assert shares.describe(s, [elsewhere], workspace_id=WS_A) == [], "not this workspace's"
        assert shares.describe(s, ["junk", "00000000-0000-4000-8000-000000000000"]) == []
        assert shares.describe(s, []) == []
        assert [d["share_id"] for d in shares.describe(s, [elsewhere, s1])] == [elsewhere, s1]


def test_ids_that_are_not_ids_are_dropped_wherever_they_come_from():
    good = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    assert shares.clean_ids([good, good, "x", 5, None, good.upper()]) == [good]
    assert shares.clean_ids("not a list") == [] and shares.clean_ids(None) == []
    assert shares.clean_ids({"k": good}) == [] and shares.clean_ids(5) == []
    assert shares.is_share_id(good) and not shares.is_share_id(good + "\n")
    assert not shares.is_share_id(f"share:{good}") and not shares.is_share_id(None)


def test_what_a_workspace_can_still_read_after_it_stops_giving_is_its_own_only(mcp_world):
    from src.mcp_server.identity import caller_access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=["github_aco-*"])
    _commit(mcp_world, shares.revoke_for_workspace, WS_B, by="u")
    mcp_world.as_(bearer)
    got = caller_access([*sorted(ALL_A), SHARED])[1]
    assert {s for s, d in got.items() if d.researchable} == ALL_A
