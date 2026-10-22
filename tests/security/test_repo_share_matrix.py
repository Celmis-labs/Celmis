"""A repository another workspace shares is readable by exactly the people holding it.

Workspace B (the owner) shares ``SHARED`` with workspace A (the receiver). The
rows below (T1 to T18 of the sharing design) put a credential, or a state of the
share, through the real identity resolver and the real dev tools, in both
deployment modes, and assert two things each time:

  * the readable set is exact: ``SHARED`` is in it for a holder and only for one;
  * for everybody else, ``SHARED`` answers like a repository that does not exist,
    and no listing names it.

A share is the owner's consent, not a grant: it works only when the superadmin
has put its id on a read-only developer token, and then only while every condition
still holds on the call (the 30 s token cache holds the list of ids and nothing
else). Anything missing, odd or unreadable is "no access".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.security.mcp_world import (
    ALL_A,
    FOREIGN,
    GHOST,
    GRANTED,
    RULED,
    SHARED,
    SHARED_MARKER,
    WILD,
    WS_A,
    WS_B,
    past,
    set_env,
)
from tests.security.share_world import (
    change_share,
    end_share,
    holder_token,
    make_share,
    register_in,
    tamper_token,
    unregister,
)

EVERY = sorted(ALL_A | {FOREIGN, SHARED})


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    """The world, in one of the two deployment modes."""
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


def _decisions(world, bearer: str | None):
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    return caller_access([*EVERY, GHOST])


def _readable(world, bearer: str | None) -> set[str]:
    _caller, access = _decisions(world, bearer)
    return {s for s, d in access.items() if d.researchable}


def _holder(world, share_id: str, **kw):
    return holder_token(world, [share_id], **kw)[0]


# ─── T1: a holder reads it, beside what the token lists for itself ───


def test_a_token_that_lists_a_repository_and_a_share_reads_both(world):
    s1 = make_share(world)
    assert _readable(world, _holder(world, s1, own=[WILD])) == {WILD, SHARED}


def test_what_a_holder_reads_is_code_of_the_owners_repository_by_explicit_rule(world):
    s1 = make_share(world)
    _caller, access = _decisions(world, _holder(world, s1))
    dec = access[SHARED]
    assert dec.code_visible and dec.researchable
    assert dec.via_share == s1 and dec.data_workspace == WS_B
    assert dec.open_default is False and dec.rules, "never the 'no rule, so everything' reading"
    assert dec.ask_ok is False, "asking is off unless the owner allowed it"


def test_a_holders_own_repositories_are_the_receivers_and_say_so(world):
    s1 = make_share(world)
    _caller, access = _decisions(world, _holder(world, s1, own=[WILD]))
    own = access[WILD]
    assert own.via_share is None and own.data_workspace is None and own.ask_ok is True


def test_a_share_on_a_token_leaves_what_its_repository_list_matches_as_it_was(world):
    s1 = make_share(world)
    assert _readable(world, _holder(world, s1, own=["github_aco-*"])) == ALL_A | {SHARED}
    assert _readable(world, _holder(world, s1, own=["github_aco-app"])) == {GRANTED, SHARED}


# ─── T2, T3: no pattern reaches it ───────────────────────────────────


@pytest.mark.parametrize("own", [
    ["*"], ["github_*"], ["github_bco-*"], ["github_bco-shared"], ["bco/shared"], ["bco/*"],
    ["*shared*"], ["??????_bco-shared"], [SHARED, FOREIGN],
], ids=lambda p: ",".join(p))
def test_no_repository_list_reaches_a_shared_repository_without_the_share(world, own):
    make_share(world)
    bearer = world.issue("mn", own, kind="pat", profile="dev")[0]
    reached = _readable(world, bearer)
    assert SHARED not in reached and FOREIGN not in reached


def test_a_star_token_reads_all_of_its_own_workspace_and_nothing_of_the_owners(world):
    make_share(world)
    assert _readable(world, world.issue("mn", ["*"], kind="pat", profile="dev")[0]) == ALL_A


def test_the_same_share_on_another_persons_token_does_not_help_this_one(world):
    s1 = make_share(world)
    _holder(world, s1, who="mg")
    assert SHARED not in _readable(world, world.issue("mn", ["*"], kind="pat", profile="dev")[0])


# ─── T4: a token may list nothing of its own ─────────────────────────


def test_a_token_that_lists_only_a_share_reads_that_and_nothing_of_its_own(world):
    s1 = make_share(world)
    bearer, view = holder_token(world, [s1])
    assert view.patterns == () and view.shares == (s1,)
    assert _readable(world, bearer) == {SHARED}


# ─── T5: it ends ─────────────────────────────────────────────────────


def test_an_expired_share_reads_like_no_share_and_the_tokens_own_list_still_works(world):
    s1 = make_share(world, expires_at=past(0.01))
    assert _readable(world, _holder(world, s1, own=[WILD])) == {WILD}


def test_the_share_ends_when_the_clock_passes_it_not_a_cache_interval_later(world, monkeypatch):
    from src.access import shares

    s1 = make_share(world, days=1)
    bearer = _holder(world, s1, own=[WILD])
    assert _readable(world, bearer) == {WILD, SHARED}
    monkeypatch.setattr(shares, "_now", lambda: datetime.now(UTC) + timedelta(days=2))
    assert _readable(world, bearer) == {WILD}


def test_a_share_is_over_at_the_very_moment_it_ends(world, monkeypatch):
    from src.access import shares

    end = datetime.now(UTC) + timedelta(hours=1)
    s1 = make_share(world, expires_at=end)
    bearer = _holder(world, s1)
    monkeypatch.setattr(shares, "_now", lambda: end - timedelta(seconds=1))
    assert _readable(world, bearer) == {SHARED}
    monkeypatch.setattr(shares, "_now", lambda: end)
    assert _readable(world, bearer) == set()


# ─── T6: revoking applies on the next call ───────────────────────────


def test_a_revoked_share_is_refused_on_the_next_call_though_the_token_is_cached(world):
    s1 = make_share(world)
    bearer = _holder(world, s1, own=[WILD])
    assert _readable(world, bearer) == {WILD, SHARED}      # the token is cached now
    end_share(world, s1)                                    # ...and nothing clears it
    assert _readable(world, bearer) == {WILD}


def test_revoking_through_the_service_applies_on_the_next_call(world):
    from src.access import shares

    s1 = make_share(world)
    bearer = _holder(world, s1)
    assert _readable(world, bearer) == {SHARED}
    with world.session() as s:
        assert shares.revoke_share(s, s1, by=world.users["su"], reason="superadmin") is True
        s.commit()
    assert _readable(world, bearer) == set()


# ─── T7, T8: a share is for one workspace and not for everybody ──────


def test_a_share_for_another_workspace_is_refused(world):
    s1 = make_share(world, grantee="wsid-c")
    assert _readable(world, _holder(world, s1, own=[WILD])) == {WILD}


def test_a_share_pointed_at_another_workspace_after_the_token_was_made_is_refused_next_call(
        world):
    """The token carries ids; the row decides. Re-pointing the share to another
    workspace takes it away from a token in this one on the next call."""
    s1 = make_share(world)
    bearer = _holder(world, s1)
    assert _readable(world, bearer) == {SHARED}
    change_share(world, s1, grantee_workspace_id="wsid-c")
    assert _readable(world, bearer) == set()


def test_a_person_the_owner_switched_off_is_refused_and_the_others_are_not(world):
    s1 = make_share(world, blocked=[world.users["mn"]])
    assert _readable(world, _holder(world, s1, who="mn", own=[WILD])) == {WILD}
    assert _readable(world, _holder(world, s1, who="mg")) == {SHARED}


def test_switching_a_person_off_applies_on_their_next_call(world):
    s1 = make_share(world)
    bearer = _holder(world, s1, who="mn")
    assert _readable(world, bearer) == {SHARED}
    change_share(world, s1, blocked_user_ids=[world.users["mn"]])
    assert _readable(world, bearer) == set()


# ─── T9: it is the repository that was shared ────────────────────────


def test_a_repository_the_owner_removed_is_gone_for_the_holder(world):
    s1 = make_share(world)
    bearer = _holder(world, s1)
    assert _readable(world, bearer) == {SHARED}
    unregister(WS_B, SHARED)
    assert _readable(world, bearer) == set()


def test_a_slug_registered_again_for_another_repository_is_not_the_one_that_was_shared(world):
    s1 = make_share(world)
    bearer = _holder(world, s1)
    unregister(WS_B, SHARED)
    register_in(world, WS_B, SHARED, full_name="bco/something-else")
    assert _readable(world, bearer) == set()


def test_the_same_name_on_another_provider_is_another_repository(world):
    s1 = make_share(world)
    bearer = _holder(world, s1)
    unregister(WS_B, SHARED)
    register_in(world, WS_B, SHARED, provider="gitlab")
    assert _readable(world, bearer) == set()


def test_a_provider_that_spells_the_name_in_another_case_is_still_the_same_repository(world):
    s1 = make_share(world)
    bearer = _holder(world, s1)
    unregister(WS_B, SHARED)
    register_in(world, WS_B, SHARED, full_name="BCO/Shared")
    assert _readable(world, bearer) == {SHARED}


def test_removing_the_repository_revokes_its_shares_and_registering_it_again_starts_from_nothing(
        world):
    from src.access import shares

    s1 = make_share(world)
    bearer = _holder(world, s1)
    with world.session() as s:
        assert shares.revoke_for_repo(s, WS_B, SHARED, by=world.users["owner"]) == [s1]
        s.commit()
    unregister(WS_B, SHARED)
    register_in(world, WS_B, SHARED)                        # the very same repository
    assert _readable(world, bearer) == set()
    assert shares.status_of(_row(world, s1)) == "revoked"


def _row(world, share_id):
    from tests.security.share_world import share_row

    return share_row(world, share_id)


# ─── T10: a slug two workspaces registered is nobody's ───────────────


def test_a_repository_the_receiver_registered_under_the_same_slug_is_not_read_through_the_share(
        world):
    s1 = make_share(world)
    register_in(world, WS_A, SHARED)
    bearer = _holder(world, s1, own=[WILD])
    _caller, access = _decisions(world, bearer)
    assert access[SHARED].via_share is None, "the share is not what grants it, in any mode"
    assert {s for s, d in access.items() if d.researchable} == {WILD}


def test_a_pattern_over_such_a_slug_reads_it_as_the_receivers_own_only_where_that_was_always_so(
        world):
    s1 = make_share(world)
    register_in(world, WS_A, SHARED)
    _caller, access = _decisions(world, _holder(world, s1, own=["github_bco-*"]))
    if world.mode == "multi_tenant":
        assert not access[SHARED].researchable, "bound to two workspaces: nobody's"
    else:
        assert access[SHARED].via_share is None, "the pre-existing reading of the receiver's own repo"


# ─── T11: only a developer token reads a share ───────────────────────


@pytest.mark.parametrize("kind", ["cli", "oauth_grant", "self", "internal"])
def test_a_share_written_into_any_other_kind_of_token_is_ignored(world, kind):
    s1 = make_share(world)
    bearer, view = world.issue("mn", [WILD], kind=kind)
    baseline = _readable(world, bearer)
    tamper_token(world, view.id, shared_repos=[s1])
    assert _readable(world, bearer) == baseline
    assert SHARED not in baseline


def test_a_share_on_a_token_with_no_row_behind_it_cannot_exist(world):
    make_share(world)
    assert SHARED not in _readable(world, world.legacy_token("owner"))


@pytest.mark.parametrize("kind", ["cli", "oauth_grant", "self", "internal"])
def test_a_token_of_another_kind_cannot_be_issued_with_a_share(world, kind):
    from src.mcp_server import token_store

    s1 = make_share(world)
    with pytest.raises(token_store.TokenError):
        holder_token(world, [s1], own=[WILD], kind=kind)


# ─── T12: nor a full or a writing token ──────────────────────────────


def test_a_share_written_into_a_full_profile_token_is_ignored(world):
    s1 = make_share(world)
    bearer, view = world.issue("mn", [WILD], kind="pat", profile="full")
    tamper_token(world, view.id, shared_repos=[s1])
    assert _readable(world, bearer) == {WILD}


def test_a_share_written_into_a_token_that_can_write_is_ignored(world):
    s1 = make_share(world)
    bearer, view = holder_token(world, [s1], own=[WILD])
    assert _readable(world, bearer) == {WILD, SHARED}
    tamper_token(world, view.id, allow_write=True)
    assert _readable(world, bearer) == {WILD}


def test_a_developer_token_cannot_be_issued_with_writing_and_a_share(world):
    from src.mcp_server import token_store

    s1 = make_share(world)
    with pytest.raises(token_store.TokenError):
        holder_token(world, [s1], own=[WILD], write=True)
    with pytest.raises(token_store.TokenError):
        holder_token(world, [s1], own=[WILD], profile="full")


# ─── T13: the full server does not honour a developer token ──────────


def test_a_developer_token_with_a_share_is_refused_by_the_full_servers_tools(world):
    from src.mcp_server.scopes import ScopeError

    s1 = make_share(world)
    world.as_(_holder(world, s1, own=[WILD]))
    tools = world.tools()
    for call in (lambda: tools["find_symbol"](name="handler", repo_slug=SHARED),
                 lambda: tools["list_repos"](),
                 lambda: tools["query_graph"](cypher="MATCH (s:Symbol) RETURN s.name",
                                              repo_slug=SHARED)):
        with pytest.raises(ScopeError):
            call()


# ─── T14: the receiver's own people do not get it by being important ──


@pytest.mark.parametrize("who", ["su", "owner", "admin", "mg", "mr", "mn"])
def test_nobody_in_the_receiver_reads_it_by_role_or_by_a_self_service_token(world, who):
    make_share(world)
    assert SHARED not in _readable(world, world.self_token(who)[0])


@pytest.mark.parametrize("who", ["su", "owner", "admin"])
def test_an_administrators_developer_token_without_the_share_does_not_reach_it(world, who):
    make_share(world)
    assert _readable(world, world.issue(who, ["*"], kind="pat", profile="dev")[0]) == ALL_A


@pytest.mark.parametrize("who", ["su", "owner", "admin", "mg", "mn"])
def test_a_session_principal_has_no_decision_for_it_by_role(world, who):
    from src.access.effective import Principal, effective_access

    make_share(world)
    dec = effective_access(
        Principal(world.users[who], who == "su", None), WS_A, [SHARED])[SHARED]
    assert not dec.researchable and dec.via_share is None


# ─── T15: leaving the workspace ends it, and coming back does not revive it ──


def test_a_person_who_left_the_receiving_workspace_reads_nothing_through_it(world):
    from src.db.models import WorkspaceMember

    s1 = make_share(world)
    bearer = _holder(world, s1, own=[WILD])
    assert _readable(world, bearer) == {WILD, SHARED}
    with world.session() as s:
        s.delete(s.get(WorkspaceMember, (WS_A, world.users["mn"])))
        s.commit()
    caller, access = _decisions(world, bearer)
    assert caller.refused
    assert not any(d.researchable for d in access.values())


def test_a_person_who_left_and_came_back_has_the_old_token_dead_and_the_share_with_it(world):
    from src.db.models import WorkspaceMember
    from src.mcp_server import token_store

    s1 = make_share(world)
    bearer, view = holder_token(world, [s1], own=[WILD])
    with world.session() as s:
        token_store.revoke_held(s, by="membership_removed:test", user_id=world.users["mn"],
                                workspace_ids=[WS_A])
        s.delete(s.get(WorkspaceMember, (WS_A, world.users["mn"])))
        s.commit()
    token_store.invalidate(view.id)
    with world.session() as s:
        s.add(WorkspaceMember(workspace_id=WS_A, user_id=world.users["mn"], role="member"))
        s.commit()
    assert _readable(world, bearer) == set()


# ─── T16: places ─────────────────────────────────────────────────────


def test_a_share_has_places_for_the_people_it_allows_and_no_more(world):
    from src.access import shares

    s1 = make_share(world, max_people=2)
    _holder(world, s1, who="mn")
    _holder(world, s1, who="mg")
    with world.session() as s:
        assert shares.has_room(s, s1, world.users["mn"]), "somebody who holds it already"
        assert not shares.has_room(s, s1, world.users["mr"]), "a third person"
        assert set(shares.holders(s, s1)) == {world.users["mn"], world.users["mg"]}


def test_one_person_with_two_tokens_takes_one_place(world):
    from src.access import shares

    s1 = make_share(world, max_people=1)
    _holder(world, s1, who="mn")
    _holder(world, s1, who="mn")
    with world.session() as s:
        assert len(shares.holders(s, s1)) == 1
        assert shares.has_room(s, s1, world.users["mn"])
        assert not shares.has_room(s, s1, world.users["mg"])


def test_only_a_token_the_share_can_work_on_takes_a_place(world):
    from src.access import shares

    s1 = make_share(world, max_people=2)
    from src.mcp_server import token_store

    _a, revoked = holder_token(world, [s1], who="mn")
    _b, other_kind = holder_token(world, [s1], who="mg")
    _c, writing = holder_token(world, [s1], who="mr")
    with world.session() as s:
        assert len(shares.holders(s, s1)) == 3, "the limit is checked when a token is added"
    with world.session() as s:
        token_store.revoke(s, revoked.id, world.users["su"])
    tamper_token(world, writing.id, allow_write=True)
    tamper_token(world, other_kind.id, kind="cli")
    with world.session() as s:
        assert shares.holders(s, s1) == {}
        assert shares.has_room(s, s1, world.users["mn"])


def test_a_share_cannot_be_narrowed_below_the_people_who_hold_it(world):
    from src.access import shares

    s1 = make_share(world, max_people=3)
    _holder(world, s1, who="mn")
    _holder(world, s1, who="mg")
    with world.session() as s, pytest.raises(shares.ShareError):
        shares.narrow(s, s1, max_people=1)
    with world.session() as s:
        assert shares.narrow(s, s1, max_people=2)["max_people"] == {"before": 3, "after": 2}


# ─── T17: what a token may be given ──────────────────────────────────


def test_a_share_of_another_workspace_a_revoked_one_and_a_made_up_one_all_read_no_such_share(
        world):
    from src.access import shares

    mine = make_share(world)
    theirs = make_share(world, grantee="wsid-c")
    revoked = make_share(world, slug=FOREIGN)
    end_share(world, revoked)
    given_by_us = make_share(world, slug=GRANTED, owner=WS_A, grantee=WS_B)
    expired = make_share(world, slug=RULED, owner=WS_A, grantee=WS_B, expires_at=past(1))
    messages = set()
    for bad in (theirs, revoked, given_by_us, expired, "00000000-0000-4000-8000-000000000000"):
        with world.session() as s, pytest.raises(shares.ShareError) as err:
            shares.check_attachable(s, [mine, bad], workspace_id=WS_A, user_id=world.users["mn"])
        messages.add(str(err.value))
    with world.session() as s, pytest.raises(shares.ShareError) as err:
        shares.check_attachable(s, ["not-an-id"], workspace_id=WS_A, user_id=world.users["mn"])
    messages.add(str(err.value))
    assert messages == {shares.NO_SUCH_SHARE}


def test_a_share_is_not_attachable_for_a_person_the_owner_switched_off(world):
    from src.access import shares

    s1 = make_share(world, blocked=[world.users["mn"]])
    with world.session() as s:
        with pytest.raises(shares.ShareError):
            shares.check_attachable(s, [s1], workspace_id=WS_A, user_id=world.users["mn"])
        assert shares.check_attachable(
            s, [s1, s1], workspace_id=WS_A, user_id=world.users["mg"]) == [s1]


def test_a_repository_the_receiver_has_too_is_not_attachable(world):
    from src.access import shares

    s1 = make_share(world)
    register_in(world, WS_A, SHARED)
    with world.session() as s, pytest.raises(shares.ShareError):
        shares.check_attachable(s, [s1], workspace_id=WS_A, user_id=world.users["mn"])


def test_no_share_is_attachable_and_nothing_is_the_empty_list(world):
    from src.access import shares

    with world.session() as s:
        assert shares.check_attachable(
            s, [], workspace_id=WS_A, user_id=world.users["mn"]) == []


def test_changing_the_shares_of_a_token_replaces_the_list_and_takes_effect_at_once(world):
    from src.mcp_server import token_store

    s1 = make_share(world)
    s2 = make_share(world, slug=FOREIGN)
    bearer, view = holder_token(world, [s1], own=[WILD])
    assert _readable(world, bearer) == {WILD, SHARED}
    with world.session() as s:
        token_store.update_row(s, view.id, shares=[s2])
    assert _readable(world, bearer) == {WILD, FOREIGN}     # the list is replaced, not added to
    with world.session() as s:
        token_store.update_row(s, view.id, shares=[s1])
    assert _readable(world, bearer) == {WILD, SHARED}
    with world.session() as s:
        token_store.update_row(s, view.id, shares=[])
    assert _readable(world, bearer) == {WILD}


def test_a_token_cannot_be_left_with_nothing_to_read(world):
    from src.mcp_server import token_store

    s1 = make_share(world)
    _bearer, only_a_share = holder_token(world, [s1])
    with world.session() as s, pytest.raises(token_store.TokenError):
        token_store.update_row(s, only_a_share.id, shares=[])
    _bearer, only_a_repo = world.issue("mn", [WILD], kind="pat", profile="dev")
    with world.session() as s, pytest.raises(token_store.TokenError):
        token_store.update_row(s, only_a_repo.id, patterns=[])
    _bearer, both = holder_token(world, [s1], own=[WILD])
    with world.session() as s:                              # either may go while the other stays
        token_store.update_row(s, both.id, patterns=[])
    with world.session() as s:
        token_store.update_row(s, both.id, patterns=[WILD], shares=[])


# ─── T18: an empty list is "nothing of my own" ───────────────────────


def test_a_token_without_a_repository_or_a_share_cannot_be_issued(world):
    from src.mcp_server import token_store

    with pytest.raises(token_store.TokenError):
        world.issue("mn", [], kind="pat", profile="dev")


def test_a_token_whose_list_is_empty_in_the_database_reads_nothing_at_all(world):
    bearer, view = world.issue("mn", [WILD], kind="pat", profile="dev")
    tamper_token(world, view.id, repo_patterns=[])
    assert _readable(world, bearer) == set()


def test_a_holder_whose_share_ended_is_left_with_nothing_rather_than_with_everything(world):
    s1 = make_share(world)
    bearer = _holder(world, s1)
    end_share(world, s1)
    assert _readable(world, bearer) == set()


def test_a_share_listed_on_a_token_with_a_corrupt_column_is_not_a_share(world):
    s1 = make_share(world)
    bearer, view = holder_token(world, [s1], own=[WILD])
    for junk in ("not a list", [42, None, "x"], {"id": s1}, [[s1]]):
        tamper_token(world, view.id, shared_repos=junk)
        assert _readable(world, bearer) == {WILD}, junk


# ─── the dev tools: only the holder gets an answer ───────────────────


def _answers(slug: str) -> dict[str, str]:
    from src.mcp_server.dev_profile import tools_find, tools_grep, tools_read, tools_refs
    from src.mcp_server.howto import run_howto

    return {
        "find": tools_find.run("handler", repo=slug),
        "outline": tools_read.outline(slug, "src/app.py"),
        "read": tools_read.read_symbol(slug, "handler"),
        "refs": tools_refs.run(slug, "helper"),
        "grep": tools_grep.run(SHARED_MARKER, repo=slug),
        "map": tools_read.repo_map(slug),
        "howto": run_howto("config", slug),
    }


def _blank(text: str, slug: str) -> str:
    """The answer with the asked slug blanked, and the suggestions in one order:
    `similar:` ranks the caller's OWN repositories by likeness to the typed name,
    so two different names rank the same set differently."""
    def ordered(m: re.Match) -> str:
        return "similar: " + ", ".join(sorted(m.group(1).split(", ")))

    return re.sub(r"similar: (.*)", ordered, text.replace(slug, "<slug>"))


def test_the_holder_gets_an_answer_from_every_dev_tool(world):
    from src.mcp_server.dev_profile.access import NOT_ACCESSIBLE

    s1 = make_share(world)
    world.as_(_holder(world, s1, own=[WILD]))
    for tool, text in _answers(SHARED).items():
        assert NOT_ACCESSIBLE not in text, tool
        assert SHARED in text, tool
    assert SHARED_MARKER in _answers(SHARED)["read"]


def test_the_holders_repository_list_names_it_beside_their_own(world):
    from src.mcp_server.dev_profile import tools_repos

    s1 = make_share(world)
    world.as_(_holder(world, s1, own=[WILD]))
    listed = tools_repos.run()
    assert WILD in listed and SHARED in listed
    assert GRANTED not in listed and FOREIGN not in listed


def _non_holders(world) -> dict[str, str]:
    """Credentials that must not read SHARED even though a live share exists."""
    live = make_share(world, blocked=[world.users["mr"]])
    out = {
        "pat-star": world.issue("mn", ["*"], kind="pat", profile="dev")[0],
        "pat-glob-reaching-into-b": world.issue("mg", ["github_*"], kind="pat",
                                                profile="dev")[0],
        "pat-names-the-slug": world.issue("owner", [SHARED], kind="pat", profile="dev")[0],
        "self-su": world.self_token("su", profile="dev")[0],
        "self-owner": world.self_token("owner", profile="dev")[0],
        "pat-admin": world.issue("admin", ["*"], kind="pat", profile="dev")[0],
        "holder-of-another-share": holder_token(world, [make_share(world, slug=FOREIGN)],
                                                own=[WILD], who="mr")[0],
        "holder-switched-off": holder_token(world, [live], who="mr")[0],
    }
    elsewhere = make_share(world, slug=SHARED, grantee="wsid-c")
    out["holder-of-a-share-for-another-workspace"] = holder_token(
        world, [elsewhere], own=[WILD], who="admin")[0]
    return out


def test_everybody_else_gets_the_answer_for_a_repository_that_does_not_exist(world):
    for label, bearer in _non_holders(world).items():
        world.as_(bearer)
        ghost = {k: _blank(v, GHOST) for k, v in _answers(GHOST).items()}
        shared = {k: _blank(v, SHARED) for k, v in _answers(SHARED).items()}
        assert shared == ghost, label


def test_no_listing_of_anybody_else_names_the_shared_repository(world):
    from src.mcp_server.dev_profile import tools_find, tools_repos

    for label, bearer in _non_holders(world).items():
        world.as_(bearer)
        assert SHARED not in tools_repos.run(), label
        # `similar:` suggests only what the caller can read.
        assert SHARED not in tools_find.run("x", repo="github_bco-shard"), label


def test_an_ended_share_leaves_the_holder_with_the_ghost_answer(world):
    s1 = make_share(world)
    bearer = _holder(world, s1, own=[WILD])
    world.as_(bearer)
    assert SHARED_MARKER in _answers(SHARED)["read"]
    end_share(world, s1)
    ghost = {k: _blank(v, GHOST) for k, v in _answers(GHOST).items()}
    assert {k: _blank(v, SHARED) for k, v in _answers(SHARED).items()} == ghost
