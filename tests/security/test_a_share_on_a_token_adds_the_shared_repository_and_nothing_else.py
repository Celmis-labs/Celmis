"""A share on a token adds exactly one repository, whatever the token's own list reaches.

With a share on the token, the access check lets slugs the workspace does not
register through to the decision (the shared one is such a slug), where without a
share it refuses them up front. That is the one place a pattern like ``*`` or
``github_bco-*`` could start to reach the owner's other repositories, so every list
a token can carry is put through it here, with and without the share:

  * the readable set with the share is the set without it plus the shared repository;
  * the owner's other repository and a repository nobody has are never in it;
  * the same holds for a person who is an administrator of the receiving workspace.

World: `tests/security/mcp_world.py` + `share_world.py` in both deployment modes.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import ALL_A, FOREIGN, GHOST, SHARED, set_env
from tests.security.share_world import holder_token, make_share

EVERY = sorted(ALL_A | {FOREIGN, SHARED})
LISTS = [
    ["*"], ["github_*"], ["github_bco-*"], ["github_bco-shared"], ["bco/shared"], ["bco/*"],
    ["*shared*"], ["??????_bco-shared"], [SHARED, FOREIGN], [FOREIGN], [GHOST],
    ["github_bco-*", "github_aco-*"], ["*", FOREIGN], ["github_aco-app"],
]


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


def _readable(world, bearer: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    _caller, access = caller_access([*EVERY, GHOST])
    return {slug for slug, decision in access.items() if decision.researchable}


@pytest.mark.parametrize("own", LISTS, ids=lambda p: ",".join(p))
@pytest.mark.parametrize("who", ["mn", "admin", "su"])
def test_the_share_adds_the_shared_repository_to_what_the_list_reaches_and_nothing_more(
        world, own, who):
    without = _readable(world, world.issue(who, own, kind="pat", profile="dev")[0])
    share_id = make_share(world)
    with_share = _readable(world, holder_token(world, [share_id], own=own, who=who)[0])
    assert with_share == without | {SHARED}
    assert FOREIGN not in with_share and GHOST not in with_share


@pytest.mark.parametrize("own", LISTS, ids=lambda p: ",".join(p))
def test_a_list_that_reaches_the_owner_reads_nothing_of_it_once_the_share_has_ended(world, own):
    from tests.security.share_world import end_share

    share_id = make_share(world)
    bearer = holder_token(world, [share_id], own=own)[0]
    end_share(world, share_id)
    reached = _readable(world, bearer)
    assert SHARED not in reached and FOREIGN not in reached and GHOST not in reached
