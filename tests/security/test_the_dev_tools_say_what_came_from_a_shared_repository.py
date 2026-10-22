"""The developer tools tell the reader which text came from another workspace.

A repository another workspace shares is written by people the reader does not
work with, and an agent that reads a sentence in it may obey it. So (src/mcp_server/
dev_profile, spec 5.1):

  * `repos` marks a shared repository with `shared` after its level, and never an
    own one;
  * every answer of a call that read through a share carries one line after the
    freshness header that says the text is data, not instructions; the line names no
    repository, and an answer of a call that read through no share has none;
  * a search over every repository (`find`, `grep`) puts the shared ones first, so
    that the cut to a fixed number of repositories falls on the caller's own;
  * a search over every repository logs the shares it went through by id, even where
    the access check logs nothing (more than twenty candidates).

World: `tests/security/mcp_world.py` + `share_world.py` in both deployment modes.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import GRANTED, SHARED, SHARED_MARKER, WILD, set_env
from tests.security.share_world import holder_token, make_share, real_clones

#: A text the receiver's own repositories hold too, so a cut that fell on them is a
#: search that found something else.
OWN_FILES = {"src/app.py": "def handler():\n    return 1\n"}


@pytest.fixture(scope="module")
def own_clones(mcp_graphs):
    real_clones(mcp_graphs, (WILD, GRANTED), OWN_FILES)
    return mcp_graphs


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch, own_clones):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


@pytest.fixture(autouse=True)
def _no_call_in_flight():
    from src.mcp_server import callctx

    callctx.reset()
    yield
    callctx.reset()


def _call(world, bearer: str, tool: str = "find"):
    """The caller is `bearer`, and a call record is open, as the server opens one."""
    from src.mcp_server import callctx

    world.as_(bearer)
    return callctx.begin(tool)


def _holder(world, share_id: str) -> str:
    return holder_token(world, [share_id], own=[WILD, GRANTED], who="owner")[0]


def _own_only(world) -> str:
    return world.issue("owner", [WILD, GRANTED], kind="pat", profile="dev")[0]


def _answers(slug: str) -> dict[str, str]:
    from src.mcp_server.dev_profile import (
        tools_find,
        tools_grep,
        tools_read,
        tools_refs,
        tools_repos,
    )

    return {
        "repos": tools_repos.run(),
        "find": tools_find.run("handler", repo=slug),
        "outline": tools_read.outline(slug, "src/app.py"),
        "read": tools_read.read_symbol(slug, "handler"),
        "refs": tools_refs.run(slug, "handler"),
        "grep": tools_grep.run("handler", repo=slug),
        "map": tools_read.repo_map(slug),
    }


# ─── the listing ─────────────────────────────────────────────────────


def test_the_listing_marks_the_shared_repository_and_only_that_one(world):
    from src.mcp_server.dev_profile import tools_repos

    _call(world, _holder(world, make_share(world)), "repos")
    lines = {ln.split()[0]: ln for ln in tools_repos.run().splitlines()
             if ln.startswith("github_")}
    assert lines[SHARED].endswith(" code shared")
    for own in (WILD, GRANTED):
        assert lines[own].endswith(" code") and "shared" not in lines[own], own


def test_a_listing_with_no_share_has_no_marker_and_no_note(world):
    from src.mcp_server.dev_profile import tools_repos
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    _call(world, _own_only(world), "repos")
    out = tools_repos.run()
    assert "shared" not in out and SHARED_NOTE not in out and SHARED not in out


# ─── the note ────────────────────────────────────────────────────────


def test_every_answer_that_read_a_shared_repository_carries_the_note_right_after_the_header(world):
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    _call(world, _holder(world, make_share(world)))
    for tool, text in _answers(SHARED).items():
        lines = text.splitlines()
        assert lines[0].startswith("idx:"), tool
        assert lines[1] == SHARED_NOTE, tool
        assert text.count(SHARED_NOTE) == 1, tool


def test_the_note_names_no_repository():
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    assert "github_" not in SHARED_NOTE and "workspace" not in SHARED_NOTE.replace(
        "another workspace", "")


def test_an_answer_of_a_call_with_no_share_has_no_note(world):
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    _call(world, _own_only(world))
    for tool, text in _answers(WILD).items():
        assert SHARED_NOTE not in text, tool


def test_an_answer_with_no_call_in_flight_has_no_note_and_no_error(world):
    """A tool run in process, with nothing recording the call, is not made to fail."""
    from src.mcp_server import callctx
    from src.mcp_server.dev_profile import tools_find
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    world.as_(_holder(world, make_share(world)))
    assert callctx.current() is None
    assert SHARED_NOTE not in tools_find.run("handler", repo=WILD)


# ─── the cut falls on the caller's own ───────────────────────────────


def test_a_grep_over_every_repository_cut_short_keeps_the_shared_one(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_grep

    monkeypatch.setattr(tools_grep, "MAX_REPOS", 1)
    _call(world, _holder(world, make_share(world)), "grep")
    out = tools_grep.run(SHARED_MARKER)
    assert f"{SHARED} src/app.py" in out and SHARED_MARKER in out
    assert "searched 1 of 3 repos" in out, "and it says that it stopped short"


def test_a_find_over_every_repository_cut_short_keeps_the_shared_one(world, monkeypatch):
    from src.mcp_server.dev_profile import common, tools_find

    monkeypatch.setattr(common, "MAX_REPOS_PER_CALL", 1)
    _call(world, _holder(world, make_share(world)))
    out = tools_find.run("handler")
    assert f"{SHARED} src/app.py" in out
    assert WILD not in out and GRANTED not in out
    assert "searched 1 of 3 repos" in out


def test_the_order_is_the_shared_repositories_then_the_rest_each_by_name(world):
    from src.mcp_server.dev_profile import access

    _call(world, _holder(world, make_share(world)))
    scope = access.mcp_scope()
    assert scope.slugs() == sorted([WILD, GRANTED, SHARED])
    assert scope.slugs(shared_first=True) == [SHARED, *sorted([WILD, GRANTED])]
    assert scope.slugs(need="code", shared_first=True)[0] == SHARED
    assert scope.is_shared(SHARED) and not scope.is_shared(WILD)


def test_a_caller_with_no_share_sees_the_order_it_always_had(world):
    from src.mcp_server.dev_profile import access

    _call(world, _own_only(world))
    scope = access.mcp_scope()
    assert scope.slugs(shared_first=True) == scope.slugs() == sorted([WILD, GRANTED])


# ─── the call log ────────────────────────────────────────────────────


def _many_candidates(monkeypatch) -> None:
    """More than twenty repositories to decide on: the access check then logs
    nothing, because it cannot tell which of them the call went on to read."""
    from src.mcp_server.dev_profile import access

    ghosts = [f"github_nobody-r{i:02d}" for i in range(25)]
    monkeypatch.setattr(access, "indexed_slugs", lambda: [SHARED, WILD, GRANTED, *ghosts])


def test_a_search_over_every_repository_logs_the_share_by_id_when_the_access_check_did_not(
        world, monkeypatch):
    from src.mcp_server.dev_profile import access, common

    _many_candidates(monkeypatch)
    share_id = make_share(world)
    rec = _call(world, _holder(world, share_id))
    scope = access.mcp_scope()
    assert rec.shares == set() and rec.repos == set(), "more than twenty: nothing noted yet"
    common.pick_repos(scope, "")
    assert rec.shares == {share_id}
    assert SHARED not in repr(rec.repos), "by id: the log belongs to the receiving workspace"


def test_a_grep_logs_the_share_it_went_through(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_grep

    _many_candidates(monkeypatch)
    share_id = make_share(world)
    rec = _call(world, _holder(world, share_id), "grep")
    tools_grep.run(SHARED_MARKER)
    assert rec.shares == {share_id} and SHARED not in repr(rec.repos)


def test_a_search_cut_to_one_repository_logs_the_shared_one_it_kept(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_grep

    _many_candidates(monkeypatch)
    monkeypatch.setattr(tools_grep, "MAX_REPOS", 1)
    share_id = make_share(world)
    rec = _call(world, _holder(world, share_id), "grep")
    out = tools_grep.run("handler")
    assert f"{SHARED} src/app.py" in out and WILD not in out
    assert rec.shares == {share_id}


def test_a_search_with_no_share_logs_no_share(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_grep

    _many_candidates(monkeypatch)
    make_share(world)                                   # live, on nobody's token
    rec = _call(world, _own_only(world), "grep")
    tools_grep.run("handler")
    assert rec.shares == set()


# ─── a share is logged when a call reads it, not when a call could ────


def test_a_call_that_reads_only_an_own_repository_logs_no_share_though_the_token_has_one(world):
    """The owner's view of who read the share is made from these rows. A person who
    holds a share and goes on working in their own repositories read nothing of it, and
    no tool that lists or reads an own repository says otherwise."""
    from src.mcp_server.dev_profile import tools_read, tools_repos

    share_id = make_share(world)
    holder = _holder(world, share_id)
    for tool, run in (
        ("repos", lambda: tools_repos.run()),
        ("outline", lambda: tools_read.outline(WILD, "src/app.py")),
        ("read_symbol", lambda: tools_read.read_symbol(WILD, "handler")),
        ("map", lambda: tools_read.repo_map(WILD)),
    ):
        rec = _call(world, holder, tool)
        run()
        assert rec.shares == set(), tool


def test_the_listing_says_so_though_it_logs_no_read(world):
    """`repos` names the shared repository, so its answer carries the note; but it
    reads nothing of it, and a listing is not a row in the owner's view."""
    from src.mcp_server.dev_profile import tools_repos
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    rec = _call(world, _holder(world, make_share(world)), "repos")
    assert SHARED_NOTE in tools_repos.run()
    assert rec.shares == set()


def test_a_call_that_reads_the_shared_repository_logs_its_share(world):
    from src.mcp_server.dev_profile import tools_read

    share_id = make_share(world)
    rec = _call(world, _holder(world, share_id), "outline")
    tools_read.outline(SHARED, "src/app.py")
    assert rec.shares == {share_id} and SHARED not in repr(rec.repos)
