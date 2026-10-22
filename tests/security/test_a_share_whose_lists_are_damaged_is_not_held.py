"""A share whose lists cannot be read as lists of strings is refused, not read as empty.

A share carries two lists the owner wrote: the paths it hides and the people it
switched off. The owner's own rules for the repository add a third. Read
leniently, damage in any of them opens the repository wider than the owner meant:
a string becomes its characters (hiding and blocking nothing), a number or a
boolean becomes an empty list. The design's rule (I1) is that a value the code does
not know refuses, so such a share is not held, for anybody, until the owner ends it
and shares again.

Every test first shows the same repository IS read with a well-formed list, so a
pass is the damage being refused and not a setup that never got there.
"""

from __future__ import annotations

import pytest

from src.access import shares
from tests.security.mcp_world import SHARED, WS_A, WS_B, set_env
from tests.security.share_world import change_share, holder_token, make_share, share_row

#: What a list column may hold when something other than our code wrote it.
DAMAGE = [
    pytest.param("u-mn", id="a-string"),
    pytest.param({"u-mn": 1}, id="an-object"),
    pytest.param(7, id="a-number"),
    pytest.param(True, id="a-boolean"),
    pytest.param([None], id="a-null-in-a-list"),
    pytest.param([["u-mn"]], id="a-list-in-a-list"),
    pytest.param([1], id="a-number-in-a-list"),
]


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    return mcp_world


def _decision(world, bearer: str):
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    _caller, access = caller_access([SHARED])
    return access[SHARED]


@pytest.mark.parametrize("damaged", DAMAGE)
def test_a_list_of_people_that_is_not_a_list_of_strings_refuses_the_share(world, damaged):
    share = make_share(world)
    bearer = holder_token(world, [share])[0]
    assert _decision(world, bearer).researchable

    change_share(world, share, blocked_user_ids=damaged)
    assert not _decision(world, bearer).researchable


@pytest.mark.parametrize("damaged", [*DAMAGE, pytest.param("config/**", id="a-glob-as-text")])
def test_a_list_of_hidden_paths_that_is_not_a_list_of_strings_refuses_the_share(world, damaged):
    share = make_share(world, deny_globs=["config/**"])
    bearer = holder_token(world, [share])[0]
    assert _decision(world, bearer).researchable

    change_share(world, share, deny_globs=damaged)
    assert not _decision(world, bearer).researchable


@pytest.mark.parametrize("damaged", ["secrets/**", {"secrets/**": 1}, 7, [None], [["x"]]])
def test_an_owner_rule_whose_hidden_paths_are_damaged_refuses_the_share(world, damaged):
    from src.db.models import RepoAccessRule

    share = make_share(world)
    bearer = holder_token(world, [share])[0]
    with world.session() as s:
        s.add(RepoAccessRule(id="rule-owner", workspace_id=WS_B, team_id="team-b",
                             repo_slug=SHARED, visibility="code", deny_globs=["secrets/**"]))
        s.commit()
    assert _decision(world, bearer).researchable
    assert "secrets/**" in _decision(world, bearer).deny_globs

    with world.session() as s:
        s.get(RepoAccessRule, "rule-owner").deny_globs = damaged
        s.commit()
    assert not _decision(world, bearer).researchable


def test_a_damaged_share_is_refused_for_everybody_and_not_only_the_person_it_names(world):
    share = make_share(world, max_people=5)
    one = holder_token(world, [share], who="mn")[0]
    other = holder_token(world, [share], who="mg")[0]
    change_share(world, share, blocked_user_ids="u-mn")
    assert not _decision(world, one).researchable
    assert not _decision(world, other).researchable


def test_a_missing_list_reads_as_an_empty_one_and_the_share_is_held(world):
    share = make_share(world)
    bearer = holder_token(world, [share])[0]
    change_share(world, share, blocked_user_ids=None, deny_globs=None)
    assert _decision(world, bearer).researchable


def test_a_damaged_share_cannot_be_attached_to_a_token(world):
    share = make_share(world)
    change_share(world, share, blocked_user_ids="u-mn")
    with world.session() as s, pytest.raises(shares.ShareError, match=shares.NO_SUCH_SHARE):
        shares.check_attachable(s, [share], workspace_id=WS_A, user_id=world.users["mn"])


@pytest.mark.parametrize("field", ["deny_globs", "blocked_user_ids"])
def test_narrowing_a_damaged_share_does_not_mend_it_into_a_held_one(world, field):
    share = make_share(world)
    bearer = holder_token(world, [share])[0]
    change_share(world, share, **{field: "u-mn"})
    assert not _decision(world, bearer).researchable

    with world.session() as s, pytest.raises(shares.ShareError, match="damaged"):
        shares.narrow(s, share, add_deny=["docs/**"], block=["u-mg"])
        s.commit()
    assert getattr(share_row(world, share), field) == "u-mn", "the row is left as it was"
    assert not _decision(world, bearer).researchable


def test_a_damaged_share_can_still_be_revoked(world):
    share = make_share(world)
    change_share(world, share, blocked_user_ids="u-mn")
    with world.session() as s:
        assert shares.revoke_share(s, share, by="owner-admin") is True
        s.commit()
    assert share_row(world, share).revoked_at is not None
