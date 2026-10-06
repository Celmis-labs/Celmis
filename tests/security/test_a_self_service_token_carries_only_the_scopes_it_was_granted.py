"""A self-service token asking for one write scope carries exactly that scope.

`_granted_scopes` checks the role per requested scope; the token minted from
the result used to carry every write scope the product has. The actions still
checked roles, so nothing was reachable that was not allowed - but the scope
list on the token, and in the response, said more than was granted.
"""

from __future__ import annotations

from src.mcp_server import token_store
from src.mcp_server.token_store import WRITE_SCOPES


def test_one_requested_write_scope_is_the_only_one_on_the_token(mcp_world):
    token, view = token_store.mint(
        kind="self", workspace_id="ws-a", user_id=mcp_world.users["owner"],
        issued_by=mcp_world.users["owner"], label="t", patterns=["*"],
        allow_write=True, profile="full", expires_in_days=7,
        write_scopes=["write:reviews"])
    carried = [s for s in view.scopes if s.startswith("write:")]
    assert carried == ["write:reviews"], carried
    assert token


def test_a_write_token_without_a_narrowed_list_still_carries_every_write_scope(mcp_world):
    _token, view = mcp_world.issue("owner", ["*"], kind="cli", write=True)
    assert {s for s in view.scopes if s.startswith("write:")} == set(WRITE_SCOPES)


def test_narrowing_writing_off_drops_them_and_keeping_it_on_keeps_the_granted_ones(mcp_world):
    _token, view = token_store.mint(
        kind="self", workspace_id="ws-a", user_id=mcp_world.users["owner"],
        issued_by=mcp_world.users["owner"], label="t", patterns=["*"],
        allow_write=True, profile="full", expires_in_days=7,
        write_scopes=["write:reviews"])
    with mcp_world.session() as s:
        row = token_store.update_row(s, view.id, allow_write=True)
        assert [x for x in row.scopes if x.startswith("write:")] == ["write:reviews"]
        row = token_store.update_row(s, view.id, allow_write=False)
        assert not [x for x in row.scopes if x.startswith("write:")]
