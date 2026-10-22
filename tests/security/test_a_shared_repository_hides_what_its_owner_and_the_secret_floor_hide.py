"""A shared repository shows less than it holds: three lists of hidden paths, one decision.

What is hidden from a holder is the union of

  * every deny glob of the OWNER's rules for the repository (whatever team the
    rule is for: what the owner hides from anybody short of a rule that says
    otherwise, a share does not show either),
  * the share's own list (the owner can add to it later, never remove), and
  * the floor under every secret file, which no list can lift.

The owner's allow-lists and visibility levels are not applied: the share is the
explicit permission, and it is the same reading a token gets for its own
repositories. The union is read from the database on every call, so a rule the
owner adds hides the path from the next call on, with no new token and no new share.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import SHARED, SHARED_MARKER, WILD, WS_A, WS_B, set_env
from tests.security.share_world import holder_token, make_share


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    return mcp_world


def _owner_rule(world, *, deny=(), allow=(), visibility="code", rule_id="rule-b", team="team-b"):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id=rule_id, workspace_id=WS_B, team_id=team, repo_slug=SHARED,
                             visibility=visibility, deny_globs=list(deny),
                             allow_globs=list(allow)))
        s.commit()


def _decision(world, bearer):
    from src.mcp_server.identity import caller_access

    world.as_(bearer)
    return caller_access([SHARED])[1][SHARED]


def _holder(world, **share):
    return holder_token(world, [make_share(world, **share)], own=[WILD])[0]


def test_an_owner_without_rules_hides_only_the_floor_from_a_holder(world):
    dec = _decision(world, _holder(world))
    assert dec.path_visible("src/app.py") and dec.path_visible("config/settings.yaml")


@pytest.mark.parametrize("path", [
    ".env", "config/.env.production", "deploy/server.key", "id_rsa", "a/.aws/credentials",
    "secrets/keys.py", "certs/server.pem", "terraform.tfstate",
])
def test_a_secret_file_is_hidden_whatever_the_lists_say(world, path):
    dec = _decision(world, _holder(world))
    assert not dec.path_visible(path)


def test_the_floor_holds_even_for_a_rule_that_allows_exactly_that_path(world):
    _owner_rule(world, allow=[".env", "secrets/**"])
    dec = _decision(world, _holder(world))
    assert not dec.path_visible(".env") and not dec.path_visible("secrets/keys.py")


def test_what_the_owner_denies_is_hidden_from_the_holder(world):
    _owner_rule(world, deny=["config/**"])
    dec = _decision(world, _holder(world))
    assert not dec.path_visible("config/settings.yaml") and dec.path_visible("src/app.py")


def test_a_deny_of_a_rule_for_any_team_counts(world):
    _owner_rule(world, deny=["config/**"], rule_id="rule-1", team="team-1")
    _owner_rule(world, deny=["src/util.py"], rule_id="rule-2", team="team-2")
    dec = _decision(world, _holder(world))
    assert not dec.path_visible("config/settings.yaml") and not dec.path_visible("src/util.py")
    assert dec.path_visible("src/app.py")


def test_what_the_share_itself_denies_is_hidden_from_the_holder(world):
    dec = _decision(world, _holder(world, deny_globs=["src/util.py"]))
    assert not dec.path_visible("src/util.py") and dec.path_visible("src/app.py")


def test_the_owners_list_and_the_shares_list_are_added_together(world):
    _owner_rule(world, deny=["config/**"])
    dec = _decision(world, _holder(world, deny_globs=["src/util.py"]))
    assert set(dec.deny_globs) == {"config/**", "src/util.py"}
    assert not dec.path_visible("config/settings.yaml") and not dec.path_visible("src/util.py")


def test_the_owners_allow_list_and_visibility_do_not_narrow_a_share(world):
    _owner_rule(world, allow=["docs/**"], visibility="metadata")
    dec = _decision(world, _holder(world))
    assert dec.code_visible and dec.path_visible("src/app.py")


def test_a_rule_the_owner_adds_applies_from_the_next_call_with_no_new_token(world):
    bearer = _holder(world)
    assert _decision(world, bearer).path_visible("config/settings.yaml")
    _owner_rule(world, deny=["config/**"])
    assert not _decision(world, bearer).path_visible("config/settings.yaml")


def test_a_rule_of_the_receivers_for_a_repository_of_the_same_name_does_not_apply(world):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id="rule-a", workspace_id=WS_A, team_id="team-g", repo_slug=SHARED,
                             visibility="code", deny_globs=["src/**"]))
        s.commit()
    assert _decision(world, _holder(world)).path_visible("src/app.py")


def test_a_share_gives_the_one_repository_and_not_the_ones_next_to_it(world):
    from src.mcp_server.identity import caller_access
    from tests.security.mcp_world import FOREIGN

    world.as_(_holder(world))
    access = caller_access([SHARED, FOREIGN])[1]
    assert access[SHARED].researchable and not access[FOREIGN].researchable


# ─── and the tools that read files obey it ───────────────────────────


def test_the_dev_tools_show_a_holder_the_code_and_not_a_secret_file(world):
    from src.mcp_server.dev_profile import tools_grep, tools_read

    world.as_(_holder(world))
    assert SHARED_MARKER in tools_grep.run(SHARED_MARKER, repo=SHARED)
    assert "PLACEHOLDER_SETTING" not in tools_grep.run("PLACEHOLDER_SETTING", repo=SHARED)
    assert "not-a-real-value" not in tools_grep.run("not-a-real-value", repo=SHARED)
    assert ".env" not in tools_read.repo_map(SHARED)
    assert "load_key" not in tools_read.outline(SHARED, "secrets/keys.py")


def test_a_symbol_in_a_secret_file_is_not_found_by_a_holder(world):
    from src.mcp_server.dev_profile import tools_find, tools_read

    world.as_(_holder(world))
    found = tools_find.run("load_key", repo=SHARED)
    nothing = tools_find.run("no_such_name", repo=SHARED)
    assert found.replace("load_key", "<q>") == nothing.replace("no_such_name", "<q>")
    assert "secrets/keys.py" not in found
    assert "return 3" not in tools_read.read_symbol(SHARED, "load_key")


def test_a_path_the_owner_denies_is_not_read_by_the_dev_tools(world):
    from src.mcp_server.dev_profile import tools_grep, tools_read

    bearer = _holder(world)
    world.as_(bearer)
    assert "helper" in tools_read.read_symbol(SHARED, "helper")
    _owner_rule(world, deny=["src/util.py"])
    assert "return 2" not in tools_read.read_symbol(SHARED, "helper")
    assert "def helper" not in tools_grep.run("def helper", repo=SHARED)
    assert "src/util.py" not in tools_read.outline(SHARED, "src/")
