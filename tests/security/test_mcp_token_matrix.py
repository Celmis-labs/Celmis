"""The MCP tools answer for exactly the repositories a credential is entitled to.

Credentials x principals x repository states, through the real identity
resolver and the real graph tools:

  * the allowed set is exact, per tool;
  * a denied slug is not named anywhere in a serialized answer;
  * the answer for a denied slug is the answer for one that does not exist;
  * a token's repo list grants by itself (that is what an administrator issues
    it for), but only inside its own workspace's registry, and a rule's deny
    globs still subtract.

The matrix is the contract of docs/mcp-access.md. A tool that skips the filter
fails here by name.
"""

from __future__ import annotations

import logging

import pytest

from tests.security.mcp_world import (
    ALL_A,
    FOREIGN,
    GHOST,
    GRANTED,
    HANDLER,
    HELPER,
    RULED,
    WILD,
    set_env,
)

EVERY_SLUG = sorted(ALL_A | {FOREIGN})

#: credential id -> (how to get the bearer, the repositories it may read)
CREDENTIALS = {
    "self-su": (lambda w: w.self_token("su")[0], ALL_A),
    "self-owner": (lambda w: w.self_token("owner")[0], ALL_A),
    "self-admin": (lambda w: w.self_token("admin")[0], ALL_A),
    "self-granted-member": (lambda w: w.self_token("mg")[0], {GRANTED}),
    "self-ruled-member": (lambda w: w.self_token("mr")[0], {RULED}),
    "self-teamless-member": (lambda w: w.self_token("mn")[0], set()),
    "self-outsider": (lambda w: w.self_token("outsider")[0], set()),
    "pat-one-slug": (lambda w: w.issue("mn", [WILD])[0], {WILD}),
    "pat-two-slugs": (lambda w: w.issue("mn", [GRANTED, RULED])[0], {GRANTED, RULED}),
    "pat-glob": (lambda w: w.issue("mn", ["github_aco-*"])[0], ALL_A),
    "pat-star": (lambda w: w.issue("mn", ["*"], kind="pat")[0], ALL_A),
    "pat-glob-reaching-into-b": (lambda w: w.issue("mg", ["github_*"])[0], ALL_A),
    "pat-names-only-b": (lambda w: w.issue("owner", [FOREIGN])[0], set()),
    "pat-glob-matching-nothing": (lambda w: w.issue("owner", ["zzz-*"])[0], set()),
    "oauth-with-grant": (lambda w: w.issue("mr", [GRANTED], kind="oauth_grant")[0], {GRANTED}),
    "legacy-token": (lambda w: w.legacy_token("owner"), set()),
}
IDS = list(CREDENTIALS)


def _try(fn, **kw):
    """The tool's answer, or what it raised — both are 'the answer'."""
    try:
        return fn(**kw)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _echo(answer, slug: str) -> str:
    """The answer with the asked slug blanked, so two slugs compare alike."""
    return repr(answer).replace(slug, "<slug>")


def _found(tool: str, answer) -> bool:  # noqa: ANN001
    if answer is None or isinstance(answer, str):   # nothing, or the refusal text
        return False
    if tool == "find_symbol":
        return bool(answer.get("matches"))
    if tool == "get_symbol":
        return bool(answer)
    if tool == "find_callers":
        return bool(answer.get("callers"))
    if tool == "find_callees":
        return bool(answer.get("callees"))
    if tool == "query_graph":
        return bool(answer.get("rows"))
    raise AssertionError(tool)


def _call(tools: dict, tool: str, slug: str):
    fn = tools[tool]
    if tool == "find_symbol":
        return _try(fn, name="handler", repo_slug=slug)
    if tool == "get_symbol":
        return _try(fn, symbol_id=HANDLER, repo_slug=slug)
    if tool == "find_callers":
        return _try(fn, symbol_id=HELPER, repo_slug=slug)
    if tool == "find_callees":
        return _try(fn, symbol_id=HANDLER, repo_slug=slug)
    return _try(fn, cypher="MATCH (s:Symbol) RETURN s.name AS name, s.file AS file",
                repo_slug=slug)


GRAPH_TOOLS = ("find_symbol", "get_symbol", "find_callers", "find_callees")

#: Opening a graph starts an embedded database (a second or more), so the
#: tool-by-tool checks run on credentials that exercise a different rule each;
#: the exact allowed set for every credential is asserted below on the
#: decision itself, which is what every tool asks.
REPRESENTATIVE = ["self-ruled-member", "pat-one-slug", "oauth-with-grant", "self-admin"]


@pytest.mark.parametrize("cred", IDS)
def test_the_decision_is_exact_for_every_credential(mcp_world, cred):
    from src.mcp_server.identity import caller_access

    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    _caller, access = caller_access([*EVERY_SLUG, GHOST])
    readable = {s for s, d in access.items() if d.researchable}
    assert readable == allowed
    assert {s for s, d in access.items() if d.code_visible} <= allowed
    # a denied repository is the same decision as one that does not exist
    ghost = access[GHOST].to_dict()
    for slug in set(EVERY_SLUG) - allowed:
        assert {**access[slug].to_dict(), "repo_slug": ""} == {**ghost, "repo_slug": ""}


@pytest.mark.parametrize("cred", IDS)
def test_find_symbol_reaches_exactly_the_allowed_repositories(mcp_world, cred):
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    tools = mcp_world.tools()
    reached = {s for s in EVERY_SLUG if _found("find_symbol", _call(tools, "find_symbol", s))}
    assert reached == allowed


@pytest.mark.parametrize("cred", REPRESENTATIVE)
@pytest.mark.parametrize("tool", GRAPH_TOOLS[1:])
def test_every_graph_tool_reaches_exactly_the_allowed_repositories(mcp_world, cred, tool):
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    tools = mcp_world.tools()
    reached = {s for s in EVERY_SLUG if _found(tool, _call(tools, tool, s))}
    assert reached == allowed


@pytest.mark.parametrize("cred", REPRESENTATIVE)
def test_raw_cypher_never_reaches_beyond_the_allowed_set(mcp_world, cred):
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    tools = mcp_world.tools()
    reached = {s for s in EVERY_SLUG if _found("query_graph", _call(tools, "query_graph", s))}
    assert reached <= allowed, sorted(reached - allowed)


@pytest.mark.parametrize("cred", IDS)
def test_a_denied_slug_answers_like_one_that_does_not_exist(mcp_world, cred):
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    tools = mcp_world.tools()
    for tool in (*GRAPH_TOOLS, "query_graph"):
        ghost = _echo(_call(tools, tool, GHOST), GHOST)
        for slug in EVERY_SLUG:
            if slug in allowed:
                continue
            assert _echo(_call(tools, tool, slug), slug) == ghost, (tool, slug)


@pytest.mark.parametrize("cred", IDS)
def test_the_repo_list_names_exactly_the_allowed_repositories(mcp_world, cred):
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    answer = _try(mcp_world.tools()["list_repos"])     # a refused credential: the refusal
    listed = {r["slug"] for r in answer["repos"]} if isinstance(answer, dict) else set()
    assert listed == allowed
    text = repr(answer)
    assert not [s for s in EVERY_SLUG if s not in allowed and s in text], (
        "a slug the credential may not read is named in the list")


@pytest.mark.parametrize("cred", IDS)
def test_a_denied_slug_is_named_nowhere_in_an_answer_that_was_not_asked_about_it(
        mcp_world, cred):
    """Group and listing tools answer for the whole workspace: they must not
    carry a repository the credential cannot read, in any field."""
    make, allowed = CREDENTIALS[cred]
    mcp_world.as_(make(mcp_world))
    tools = mcp_world.tools()
    for tool in ("list_repos", "list_groups"):
        text = repr(_try(tools[tool]))
        assert not [s for s in EVERY_SLUG if s not in allowed and s in text], tool


def test_a_token_repo_list_still_loses_the_paths_a_rule_denies(mcp_world):
    token, _ = mcp_world.issue("mn", [RULED])
    mcp_world.as_(token)
    tools = mcp_world.tools()
    assert tools["find_symbol"](name="handler", repo_slug=RULED)["matches"]
    assert tools["find_symbol"](name="load_key", repo_slug=RULED)["matches"] == []
    assert tools["get_symbol"](symbol_id="secrets/keys.py::load_key",
                               repo_slug=RULED) is None


def test_a_refused_credential_says_why_and_reads_nothing(mcp_world):
    from src.mcp_server.identity import LEGACY_REFUSAL, resolve_caller

    mcp_world.as_(mcp_world.legacy_token("owner"))
    caller = resolve_caller()
    assert caller.refused == LEGACY_REFUSAL
    assert not caller.is_admin and not caller.allow_write


# ─── the policy knob and the deployment mode ─────────────────────────


def _readable_by(world, who: str) -> set[str]:
    world.as_(world.self_token(who)[0])
    answer = world.tools()["list_repos"]()
    return {r["slug"] for r in answer["repos"]}


@pytest.mark.parametrize("mode", ["single_tenant", "multi_tenant"])
def test_a_member_with_no_rule_sees_no_unruled_repo_in_either_mode(
        mcp_world, monkeypatch, mode):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=mode)
    assert _readable_by(mcp_world, "mn") == set()
    assert _readable_by(mcp_world, "mg") == {GRANTED}


@pytest.mark.parametrize("mode", ["single_tenant", "multi_tenant"])
@pytest.mark.parametrize("who", ["su", "owner", "admin"])
def test_admins_owners_and_the_superadmin_still_see_every_repo(
        mcp_world, monkeypatch, mode, who):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=mode)
    assert _readable_by(mcp_world, who) == ALL_A


def test_open_restores_the_old_reading_in_single_tenant(mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE="single_tenant",
            CELMIS_UNRULED_REPO_ACCESS="open")
    seen = _readable_by(mcp_world, "mn")
    assert WILD in seen and GRANTED in seen, "no rule means open again"
    assert RULED not in seen, "a rule that excludes the member still does"


def test_open_is_ignored_with_a_warning_in_multi_tenant(mcp_world, monkeypatch, caplog):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE="multi_tenant",
            CELMIS_UNRULED_REPO_ACCESS="open")
    with caplog.at_level(logging.WARNING):
        assert _readable_by(mcp_world, "mn") == set()
    assert "unruled_repo_open_ignored" in caplog.text


def test_a_misspelt_policy_is_the_closed_one(mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE="single_tenant",
            CELMIS_UNRULED_REPO_ACCESS="opne")
    assert _readable_by(mcp_world, "mn") == set()
