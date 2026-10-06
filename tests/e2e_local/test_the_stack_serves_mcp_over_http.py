"""The in-process stack itself: real app, real indexing, real MCP client over HTTP.

These run on any tree (they use only the existing ``/mcp/`` endpoint), so a
broken harness is caught here and not mistaken for a failing lane.
"""

from __future__ import annotations

import pytest

from tests.e2e_local.client import McpClient, McpHttpError
from tests.e2e_local.stack import Stack


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("stack")) as st:
        yield st


def test_the_endpoint_lists_tools_for_a_signed_in_caller(stack) -> None:
    tools = McpClient(stack.url, "/mcp/", stack.token("owner", profile="full")).list_tools()
    names = {t["name"] for t in tools}
    assert "list_accessible_repos" in names and len(names) >= 10


def test_no_credentials_means_401_and_an_expired_token_is_refused(stack) -> None:
    assert McpClient(stack.url, "/mcp/", None).initialize_status() == 401
    with pytest.raises(McpHttpError) as exc:
        McpClient(stack.url, "/mcp/", stack.expired_token("owner")).list_tools()
    assert exc.value.status == 401


def test_every_repository_of_the_world_is_cloned_and_indexed_at_its_head(stack) -> None:
    from src.repos import index_state

    assert set(stack.repos) == {"acme/shop", "acme/billing", "acme/gateway"}
    for logical, repo in stack.repos.items():
        assert repo.clone.is_dir() and (repo.clone / ".git").exists(), logical
        state = index_state.read_index_state(repo.slug)
        assert state is not None and state.last_indexed_sha == repo.head, logical


def test_a_signed_in_member_sees_the_repositories_through_the_existing_tool(stack) -> None:
    res = McpClient(stack.url, "/mcp/", stack.token("su", profile="full")).call("list_accessible_repos")
    assert not res.is_error
    for repo in stack.repos.values():
        assert repo.slug in res.raw


def test_a_caller_of_another_workspace_gets_a_different_answer(stack) -> None:
    mine = McpClient(stack.url, "/mcp/", stack.token("owner", profile="full")).call("list_accessible_repos")
    theirs = McpClient(stack.url, "/mcp/", stack.token("other", workspace="ws-b", profile="full")) \
        .call("list_accessible_repos")
    assert not theirs.is_error
    for repo in stack.repos.values():
        assert repo.slug in mine.raw
        assert repo.slug not in theirs.raw


def test_indexing_the_planted_secrets_leaves_no_value_in_the_logs(stack) -> None:
    assert stack.world is not None
    assert stack.world.leaks_in(stack.log_text()) == []


def test_a_pushed_commit_moves_the_clone_and_a_reindex_moves_the_recorded_sha(stack) -> None:
    repo = stack.repos["acme/shop"]
    before = repo.head
    new = stack.push_commit("acme/shop", "app/extra.py", "def extra() -> int:\n    return 1\n",
                            "add extra")
    assert new != before
    stack.reindex(repo.slug)
    from src.repos import index_state

    state = index_state.read_index_state(repo.slug)
    assert state is not None and state.last_indexed_sha == new
