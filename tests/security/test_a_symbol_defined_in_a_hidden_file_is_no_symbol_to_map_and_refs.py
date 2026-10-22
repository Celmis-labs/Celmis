"""A name that only a hidden file defines does not show up in `map` or `refs`.

`find` and `read_symbol` already treat a symbol defined in a file the caller may not
list (the owner's deny rules, the share's own list, the floor under secret files) as
no symbol. Two tools did not:

  * `map` named the most used symbols under each directory from every file of the
    directory, hidden ones included, so `src/  top: helper, handler` gave away
    `helper` of a file that was hidden;
  * `refs` looked the name up in the whole graph, so a name defined only in a hidden
    file answered "callers of X: 0" and a name that is nowhere answered "no symbol":
    a probe for which names a hidden file (a key file, say) defines.

World: `tests/security/mcp_world.py` + `share_world.py` in both deployment modes. The
fixture repository: `src/app.py` defines `handler` and `load`, `src/util.py` defines
`helper`, `secrets/keys.py` (a secret file) defines `load_key` and `load`.
"""

from __future__ import annotations

import re

import pytest

from tests.security.mcp_world import SHARED, WILD, WS_B, set_env
from tests.security.share_world import holder_token, make_share


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


def _as_holder(world, **share) -> None:
    world.as_(holder_token(world, [make_share(world, **share)], own=[WILD])[0])


def _top(text: str) -> set[str]:
    """The names listed after `top:` on any line of a `map` answer."""
    return {n.strip() for m in re.findall(r"top: (.*)", text) for n in m.split(",")}


def _maps(path: str = "") -> set[str]:
    from src.mcp_server.dev_profile import tools_read

    return _top(tools_read.repo_map(SHARED, path=path))


def test_the_map_names_the_symbols_of_every_file_when_nothing_is_hidden(world):
    _as_holder(world)
    assert _maps() == {"handler", "helper"} and _maps("src") == {"handler", "helper"}


@pytest.mark.parametrize("hidden,gone,kept", [
    ("src/util.py", "helper", "handler"),
    ("src/app.py", "handler", "helper"),
])
def test_the_map_leaves_out_the_names_of_a_file_the_share_hides(world, hidden, gone, kept):
    _as_holder(world, deny_globs=[hidden])
    for path in ("", "src"):
        names = _maps(path)
        assert gone not in names and kept in names, (path, names)


def test_the_map_leaves_out_the_names_of_a_file_the_owner_hides(world):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id="rule-b", workspace_id=WS_B, team_id="team-b", repo_slug=SHARED,
                             visibility="code", deny_globs=["src/util.py"], allow_globs=[]))
        s.commit()
    _as_holder(world)
    assert _maps() == {"handler"}


def _refs(symbol: str, direction: str) -> str:
    from src.mcp_server.dev_profile import tools_refs

    return tools_refs.run(SHARED, symbol, direction=direction)


@pytest.mark.parametrize("direction", ["callers", "callees", "imports"])
def test_refs_answers_for_a_name_only_a_hidden_file_defines_as_for_a_name_nobody_defines(
        world, direction):
    _as_holder(world, deny_globs=["src/util.py"])
    hidden = _refs("helper", direction).replace("helper", "<name>")
    nowhere = _refs("zzz_nothing", direction).replace("zzz_nothing", "<name>")
    assert hidden == nowhere and "no symbol" in hidden


@pytest.mark.parametrize("direction", ["callers", "callees", "imports"])
def test_refs_answers_for_a_name_only_a_secret_file_defines_as_for_a_name_nobody_defines(
        world, direction):
    """No rule of anybody's: the floor under secret files is enough."""
    _as_holder(world)
    secret = _refs("load_key", direction).replace("load_key", "<name>")
    nowhere = _refs("zzz_nothing", direction).replace("zzz_nothing", "<name>")
    assert secret == nowhere and "no symbol" in secret


def test_refs_still_answers_for_a_name_that_a_visible_file_defines_too(world):
    """`load` is defined in `src/app.py` and in the secret file: the visible one counts."""
    _as_holder(world)
    out = _refs("load", "callers")
    assert "no symbol" not in out and 'callers of load' in out


def test_refs_answers_for_a_visible_symbol_as_before(world):
    _as_holder(world, deny_globs=["src/util.py"])
    out = _refs("handler", "callers")
    assert "no symbol" not in out and "callers of handler" in out
