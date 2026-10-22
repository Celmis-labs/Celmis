"""Raw Cypher cannot be filtered by path, so it is never run on another workspace's repository.

`query_graph` takes a query a caller wrote and returns whatever rows it yields.
A row has no fixed shape (a path may come back under any alias, inside a list or
a symbol id), so the tool is allowed only where there is nothing to hide
(`tenancy.unrestricted`). A shared repository always has something to hide: the
owner's rules for it, the share's own list and the floor under every secret file.
So its decision is never "unrestricted", whatever the lists say, and the tool
answers for it exactly as for a repository that does not exist.

The developer profile has no raw-Cypher tool, and the full server demands
``read:graph`` of a token that a developer token does not carry. The defence
tested here is the one behind those: a token that somehow carries both.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import GHOST, SHARED, WILD, WS_A, WS_B, set_env
from tests.security.share_world import holder_token, make_share, tamper_token

CYPHER = "MATCH (s:Symbol) RETURN s.name AS name, s.file AS file"


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    return mcp_world


def _with_graph_scope(world, view):
    """The bearer of a developer token whose row and signature also carry
    ``read:graph``: a token no code of ours issues."""
    from src.mcp_server import token_store

    tamper_token(world, view.id, scopes=[*view.scopes, "read:graph"])
    return token_store.sign(world.row(view.id))


def _answer(world, slug):
    try:
        return repr(world.tools()["query_graph"](cypher=CYPHER, repo_slug=slug))
    except Exception as exc:  # noqa: BLE001 — a refusal is an answer
        return f"{type(exc).__name__}: {exc}"


def test_a_shared_repository_is_never_unrestricted_even_with_nothing_hidden():
    from datetime import UTC, datetime

    from src.access.shares import SharedGrant
    from src.mcp_server import tenancy

    grant = SharedGrant(
        share_id="00000000-0000-4000-8000-000000000001", repo_slug=SHARED,
        owner_workspace_id=WS_B, grantee_workspace_id=WS_A, deny_globs=(),
        allow_ask=True, expires_at=datetime.now(UTC))
    dec = grant.decision()
    assert dec.code_visible and not dec.deny_globs and not dec.open_default
    assert tenancy.unrestricted(dec) is False


def test_the_same_decision_without_a_share_is_unrestricted_so_the_refusal_is_the_shares():
    from src.access.resolver import RepoAccessDecision
    from src.mcp_server import tenancy

    assert tenancy.unrestricted(RepoAccessDecision.full(WILD)) is True
    assert tenancy.unrestricted(None) is False


def test_the_decision_a_holder_gets_is_never_unrestricted(world):
    from src.mcp_server import tenancy
    from src.mcp_server.identity import caller_access

    s1 = make_share(world)
    bearer, _view = holder_token(world, [s1], own=[WILD])
    world.as_(bearer)
    _caller, access = caller_access([WILD, SHARED])
    assert tenancy.unrestricted(access[WILD]) is True      # their own repository, as ever
    assert tenancy.unrestricted(access[SHARED]) is False


def test_a_token_that_carries_graph_scope_and_a_share_cannot_run_cypher_on_the_shared_repo(
        world):
    s1 = make_share(world)
    _bearer, view = holder_token(world, [s1], own=[WILD])
    world.as_(_with_graph_scope(world, view))
    ghost = _answer(world, GHOST).replace(GHOST, "<slug>")
    shared = _answer(world, SHARED).replace(SHARED, "<slug>")
    assert shared == ghost
    refusal = world.tools()["query_graph"](cypher=CYPHER, repo_slug=SHARED)
    assert refusal["ok"] is False and refusal["rows"] == []


def test_that_same_token_still_runs_cypher_on_its_own_repository(world):
    """So the refusal above is the shared repository's, not a broken tool."""
    s1 = make_share(world)
    _bearer, view = holder_token(world, [s1], own=[WILD])
    world.as_(_with_graph_scope(world, view))
    assert "handler" in _answer(world, WILD)


def test_no_alias_or_aggregate_gets_a_row_out_of_a_shared_repository(world):
    s1 = make_share(world)
    _bearer, view = holder_token(world, [s1], own=[WILD])
    world.as_(_with_graph_scope(world, view))
    tools = world.tools()
    ghost = repr(tools["query_graph"](cypher="MATCH (s) RETURN count(*) AS n", repo_slug=GHOST))
    for cypher in (
        "MATCH (s) RETURN count(*) AS n",
        "MATCH (s:Symbol) RETURN s.signature AS sig, s.docstring AS doc",
        "MATCH (s:Symbol) RETURN collect(s.file) AS files",
        "MATCH (s:Symbol) WHERE s.file STARTS WITH 'secrets/' RETURN s.name AS x",
    ):
        got = repr(tools["query_graph"](cypher=cypher, repo_slug=SHARED))
        assert got.replace(SHARED, "<slug>") == ghost.replace(GHOST, "<slug>"), cypher
