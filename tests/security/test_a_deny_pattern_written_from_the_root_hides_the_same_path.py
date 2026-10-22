"""A path the owner hides stays hidden however the pattern is spelled from the root.

The matcher reads a pattern against paths that have no leading ``/`` or ``./``, so
a pattern written the way a ``.gitignore`` is (``/src/util.py``, ``./src/util.py``)
matched nothing: the owner saw no error, believed the file hidden, and the holder
read it. A pattern that ended in ``/`` after a wildcard (``src/**/``) matched only
paths that end in ``/``, that is no file. Both the share's own list and the
owner's rules go through :func:`src.access.shares.anchor_glob` on every call, so a
row written before the rule was known, or by hand, is read the same way.

World: `tests/security/mcp_world.py` + `share_world.py` in both deployment modes.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import SHARED, SHARED_MARKER, WILD, WS_B, set_env
from tests.security.share_world import holder_token, make_share

#: The same file, `src/util.py` (it defines `helper`), spelled every way an owner might.
SPELLINGS = ["src/util.py", "/src/util.py", "./src/util.py", "//src/util.py", "/./src/util.py",
             "src/util.py/", "/src/", "./src/**", "src/**/", "/src/**/", "src\\util.py"]


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


def _read_as_holder(world, **share):
    from src.mcp_server.dev_profile import tools_find, tools_grep, tools_read

    world.as_(holder_token(world, [make_share(world, **share)], own=[WILD])[0])
    return {
        "outline": tools_read.outline(SHARED, "src/util.py"),
        "read": tools_read.read_symbol(SHARED, "helper"),
        "grep": tools_grep.run("return 2", repo=SHARED),
        "find": tools_find.run("helper", repo=SHARED),
    }


def _shows_util(answers: dict[str, str]) -> list[str]:
    return [tool for tool, text in answers.items()
            if "return 2" in text or "1 symbols" in text or "function def helper" in text]


def test_the_control_reads_the_file_when_nothing_hides_it(world):
    assert sorted(_shows_util(_read_as_holder(world))) == ["find", "grep", "outline", "read"]


@pytest.mark.parametrize("pattern", SPELLINGS)
def test_every_spelling_of_the_path_in_the_share_hides_it_from_every_tool(world, pattern):
    assert _shows_util(_read_as_holder(world, deny_globs=[pattern])) == []


@pytest.mark.parametrize("pattern", ["/src/util.py", "./src/util.py", "src/**/"])
def test_every_spelling_in_a_rule_of_the_owner_hides_it_too(world, pattern):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id="rule-b", workspace_id=WS_B, team_id="team-b", repo_slug=SHARED,
                             visibility="code", deny_globs=[pattern], allow_globs=[]))
        s.commit()
    assert _shows_util(_read_as_holder(world)) == []


def test_hiding_one_file_leaves_the_others_readable(world):
    """The point of anchoring is exactness: `src/app.py` is still there."""
    answers = _read_as_holder(world, deny_globs=["/src/util.py"])
    from src.mcp_server.dev_profile import tools_grep

    assert SHARED_MARKER in tools_grep.run(SHARED_MARKER, repo=SHARED)
    assert _shows_util(answers) == []


@pytest.mark.parametrize("pattern", ["/", "./", ".", "//", "/./"])
def test_a_pattern_that_is_only_the_root_hides_the_whole_repository(world, pattern):
    from src.mcp_server.dev_profile import tools_grep

    _read_as_holder(world, deny_globs=[pattern])
    assert SHARED_MARKER not in tools_grep.run(SHARED_MARKER, repo=SHARED)


@pytest.mark.parametrize("raw,written", [
    ("/src/util.py", "src/util.py"), ("./src/util.py", "src/util.py"),
    ("//a//b", "a//b"), ("/./a", "a"), ("src/**/", "src/**"), ("src/*/", "src/*/**"),
    ("a/b/", "a/b/"), ("src\\x", "src/x"), ("**/.env*", "**/.env*"), (".env", ".env"),
    (".hidden/**", ".hidden/**"), ("/", "**"), ("./", "**"), (".", "**"),
])
def test_how_a_pattern_is_written_down(raw, written):
    from src.access.shares import anchor_glob

    assert anchor_glob(raw) == written


@pytest.mark.parametrize("raw", ["/src/util.py", "src/**/", "./a/b", "a/b/", "*", "**/x", "/", ".env"])
def test_a_pattern_written_down_is_written_down_the_same_way_again(raw):
    from src.access.shares import anchor_glob

    assert anchor_glob(anchor_glob(raw)) == anchor_glob(raw)
