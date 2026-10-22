"""The built-in ``default`` workspace is never one side of a share.

Records of that workspace carry no tenant (``normalize_workspace_id`` reads
``"default"`` as "nobody"), which is why nothing that filters on the tenant, the
vector store included, can tell its repositories from anybody's. A share that has
it on either side is therefore refused when it is READ, not only when it is
created: the row can come from a route that forgot the rule, from an import, or
from an operator's hand, and the read is the last place left to say no.

Each test also lifts the rule for a moment and shows the repository then IS read,
so that a pass means the rule held it back and not that the setup never got there.
"""

from __future__ import annotations

import pytest

from src.access import shares
from tests.security.mcp_world import SHARED, WILD, WS_A, set_env
from tests.security.share_world import holder_token, make_share, register_in

LEGACY = "github_dco-legacy"


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    return mcp_world


def _readable(world, bearer: str, slug: str) -> bool:
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    _caller, access = caller_access([slug])
    return access[slug].researchable


def _lifted(monkeypatch) -> None:
    """The rule off, as if the read did not know about it."""
    monkeypatch.setattr(shares, "_is_a_side", lambda _workspace: True)


def _member_of_default(world, who: str = "mn") -> None:
    from src.db.models import Workspace, WorkspaceMember

    with world.session() as s:
        s.add(Workspace(id="default", name="Default", slug="default"))
        s.add(WorkspaceMember(workspace_id="default", user_id=world.users[who], role="member"))
        s.commit()


def test_a_repository_of_the_default_workspace_cannot_be_read_through_a_share(world, monkeypatch):
    register_in(world, "default", LEGACY)
    bearer = holder_token(world, [make_share(world, slug=LEGACY, owner="default")])[0]
    assert not _readable(world, bearer, LEGACY)

    _lifted(monkeypatch)
    assert _readable(world, bearer, LEGACY), "the setup must reach the repository without the rule"


def test_a_token_of_the_default_workspace_reads_no_share(world, monkeypatch):
    _member_of_default(world)
    share = make_share(world, grantee="default")
    bearer, view = holder_token(world, [share], workspace="default")
    assert view.workspace_id == "default" and view.shares == (share,)
    assert not _readable(world, bearer, SHARED)

    _lifted(monkeypatch)
    assert _readable(world, bearer, SHARED), "the setup must reach the repository without the rule"


def test_a_share_of_the_default_workspace_is_not_attachable_to_a_token(world):
    register_in(world, "default", LEGACY)
    share = make_share(world, slug=LEGACY, owner="default")
    with world.session() as s, pytest.raises(shares.ShareError, match=shares.NO_SUCH_SHARE):
        shares.check_attachable(s, [share], workspace_id=WS_A, user_id=world.users["mn"])


@pytest.mark.parametrize("workspace", ["default", " default ", "", "   ", None])
def test_a_workspace_that_is_no_tenant_is_not_a_side(workspace):
    assert shares._is_a_side(workspace) is False


@pytest.mark.parametrize("workspace", ["wsid-a", "ws-b", "Default", "defaults", "my-default"])
def test_any_other_workspace_is_a_side(workspace):
    assert shares._is_a_side(workspace) is True


def test_the_other_workspaces_still_share_with_each_other(world):
    bearer = holder_token(world, [make_share(world)], own=[WILD])[0]
    assert _readable(world, bearer, SHARED) and _readable(world, bearer, WILD)
