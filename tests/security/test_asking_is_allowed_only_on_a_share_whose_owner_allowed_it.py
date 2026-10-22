"""`ask` sends code to a model, so it has its own switch (C8) on top of reading.

A share has `allow_ask`, off unless the owner turns it on at creation. The decision
carries it as `ask_ok` (everything that is the caller's own says True), and
`shares.ask_targets` is the subset of the live shares that may be used by `ask`:
those that hold (C2 to C6) and allow it. The automation actor carries the ids of
the token's shares so that `ask` can read them from the token; the repository list
it carries is the token's own and never has a share in it.
"""

from __future__ import annotations

from src.access import shares
from tests.security.mcp_world import SHARED, WILD, WS_A
from tests.security.share_world import end_share, holder_token, make_share


def _targets(world, share_id, user="mn", **kw):
    with world.session() as s:
        return shares.ask_targets(s, share_ids=[share_id], workspace_id=WS_A,
                                  user_id=world.users[user], **kw)


def test_a_share_is_closed_to_asking_unless_the_owner_opened_it(mcp_world):
    s1 = make_share(mcp_world)
    assert _targets(mcp_world, s1) == {}


def test_a_share_the_owner_opened_is_a_target_and_says_so_on_its_decision(mcp_world):
    s1 = make_share(mcp_world, allow_ask=True)
    got = _targets(mcp_world, s1)
    assert set(got) == {SHARED} and got[SHARED].share_id == s1
    assert got[SHARED].decision().ask_ok is True


def test_a_target_is_still_subject_to_every_condition_of_reading(mcp_world):
    s1 = make_share(mcp_world, allow_ask=True, blocked=[mcp_world.users["mn"]])
    assert _targets(mcp_world, s1) == {}
    assert set(_targets(mcp_world, s1, user="mg")) == {SHARED}
    end_share(mcp_world, s1)
    assert _targets(mcp_world, s1, user="mg") == {}


def test_targets_can_be_limited_to_the_repositories_asked_about(mcp_world):
    s1 = make_share(mcp_world, allow_ask=True)
    assert set(_targets(mcp_world, s1, slugs=[SHARED])) == {SHARED}
    assert _targets(mcp_world, s1, slugs=[WILD]) == {}
    assert _targets(mcp_world, s1, slugs=[]) == {}


def test_switching_asking_off_applies_on_the_next_decision(mcp_world):
    from src.mcp_server.identity import caller_access

    s1 = make_share(mcp_world, allow_ask=True)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    assert caller_access([SHARED])[1][SHARED].ask_ok is True
    with mcp_world.session() as s:
        shares.narrow(s, s1, allow_ask=False)
        s.commit()
    assert caller_access([SHARED])[1][SHARED].ask_ok is False


def test_everything_the_caller_owns_may_be_asked_about_as_before(mcp_world):
    from src.mcp_server.identity import caller_access

    bearer, _view = holder_token(mcp_world, [make_share(mcp_world)], own=[WILD])
    mcp_world.as_(bearer)
    assert caller_access([WILD])[1][WILD].ask_ok is True


def test_the_ask_actor_carries_the_shares_of_the_token_and_a_list_without_them(mcp_world):
    from src.mcp_server.dev_profile import tools_ask

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    actor = tools_ask._actor()
    assert actor.token_shares == (s1,)
    assert tuple(actor.token_filter) == (WILD,)
    assert s1 not in repr(actor.token_filter)


def test_the_ask_actor_of_a_token_without_shares_carries_none(mcp_world):
    from src.mcp_server.dev_profile import tools_ask

    bearer, _view = mcp_world.issue("mn", [WILD], kind="pat", profile="dev")
    mcp_world.as_(bearer)
    assert tools_ask._actor().token_shares == ()


def test_the_ask_actor_of_a_token_a_share_cannot_work_on_carries_none(mcp_world):
    from src.mcp_server.dev_profile import tools_ask
    from tests.security.share_world import tamper_token

    s1 = make_share(mcp_world)
    bearer, view = mcp_world.issue("mn", [WILD], kind="cli", profile="dev")
    tamper_token(mcp_world, view.id, shared_repos=[s1])
    mcp_world.as_(bearer)
    assert tools_ask._actor().token_shares == ()
