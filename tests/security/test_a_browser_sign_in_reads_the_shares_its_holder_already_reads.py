"""A sign-in through the browser reads the shares its holder already reads.

Signing in to MCP through the browser mints a ``self`` token: the person's own
access, ``*``. A shared repository was never part of that, so a member of a workspace
that has nothing of its own but a share saw "no repositories you can read" while the
token the superadmin had issued them, unused, would have read the share.

A read-only ``self`` token now carries, on every call, the shares its holder reads
with their own rights there and nothing else:

  * the shares on the person's own live developer ``pat`` in that workspace (the
    superadmin already gave them a place under the share's ceiling);
  * the shares whose ceiling on people covers every member the owner has not
    switched off (the rule "Check a file" has used since 3.1.39).

Everything else about a share still holds on the call (C3-C7): switched-off people,
the end of the share, the repository still the owner's.
"""

from __future__ import annotations

from tests.security.mcp_world import SHARED, WILD
from tests.security.share_world import end_share, holder_token, make_share


def _readable(world, bearer: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    _caller, access = caller_access([SHARED, WILD])
    return {s for s, d in access.items() if d.researchable}


def _revoke(world, token_id: str) -> None:
    from src.mcp_server import token_store

    with world.session() as s:
        token_store.revoke(s, token_id, by=world.users["su"])
        s.commit()
    token_store.invalidate(token_id)


# ─── the token the superadmin issued ────────────────────────────────


def test_a_member_whose_developer_token_holds_the_share_reads_it_signed_in_through_the_browser(mcp_world):
    s1 = make_share(mcp_world, max_people=2)  # two places, five members: membership opens nothing
    holder_token(mcp_world, [s1], own=["*"], who="mn")
    assert SHARED in _readable(mcp_world, mcp_world.self_token("mn")[0])
    assert SHARED not in _readable(mcp_world, mcp_world.self_token("mg")[0])


def test_the_share_goes_with_the_developer_token_on_the_next_call(mcp_world):
    s1 = make_share(mcp_world, max_people=2)
    _bearer, pat = holder_token(mcp_world, [s1], own=["*"], who="mn")
    browser = mcp_world.self_token("mn")[0]
    assert SHARED in _readable(mcp_world, browser)
    _revoke(mcp_world, pat.id)
    assert SHARED not in _readable(mcp_world, browser)


def test_another_persons_developer_token_does_not_help(mcp_world):
    s1 = make_share(mcp_world, max_people=2)
    holder_token(mcp_world, [s1], own=["*"], who="mg")
    assert SHARED not in _readable(mcp_world, mcp_world.self_token("mn")[0])


# ─── membership, when the ceiling covers everybody ──────────────────


def test_every_member_reads_it_when_its_ceiling_covers_every_member(mcp_world):
    make_share(mcp_world, max_people=5)  # the five members of A
    for who in ("owner", "admin", "mg", "mr", "mn"):
        assert SHARED in _readable(mcp_world, mcp_world.self_token(who)[0]), who


def test_a_superadmin_who_is_not_a_member_does_not_read_it(mcp_world):
    make_share(mcp_world, max_people=25)
    assert SHARED not in _readable(mcp_world, mcp_world.self_token("su")[0])


def test_a_person_the_owner_switched_off_does_not_and_the_others_still_do(mcp_world):
    make_share(mcp_world, max_people=4, blocked=[mcp_world.users["mr"]])
    assert SHARED not in _readable(mcp_world, mcp_world.self_token("mr")[0])
    assert SHARED in _readable(mcp_world, mcp_world.self_token("mn")[0])


def test_an_ended_share_is_refused_on_the_next_call(mcp_world):
    s1 = make_share(mcp_world, max_people=5)
    browser = mcp_world.self_token("mn")[0]
    assert SHARED in _readable(mcp_world, browser)
    end_share(mcp_world, s1)
    assert SHARED not in _readable(mcp_world, browser)


# ─── what it does not change ────────────────────────────────────────


def test_a_self_token_that_can_write_reads_no_share(mcp_world):
    s1 = make_share(mcp_world, max_people=5)
    holder_token(mcp_world, [s1], own=["*"], who="mn")
    assert SHARED not in _readable(mcp_world, mcp_world.self_token("mn", write=True)[0])


def test_the_repository_list_names_it(mcp_world):
    from src.mcp_server.dev_profile import tools_repos

    make_share(mcp_world, max_people=5)
    mcp_world.as_(mcp_world.self_token("mn", profile="dev")[0])
    assert SHARED in tools_repos.run()
