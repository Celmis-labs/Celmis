"""`refs` follows a shared repository to the caller's own and back, and goes nowhere else.

For the callers of a symbol, `refs` scans a few other repositories for the word.
A repository shared with the caller's workspace is one of them, in both directions
(the callers of a shared symbol are looked for in the caller's own repositories, the
callers of an own symbol in the shared ones), and these are the limits that make
that safe:

  * the scan needs the share: without one, a repository of another workspace is not
    scanned and not mentioned, whatever its name;
  * what the share and the owner hide is not scanned either (the share's deny list,
    the owner's rules, the secret floor): a hit in a hidden file is not shown and
    its line is not quoted;
  * the owner's groups are the owner's: the other repositories of a group the shared
    repository is in are not looked into and not named, in either deployment mode;
  * the repositories scanned are capped, own and shared together, and the answer
    says that it stopped short;
  * the scan runs side by side, not one after the other.

World: `tests/security/mcp_world.py` + `share_world.py`; the caller's own repositories
are made real git clones here, because the scan is `git grep`.
"""

from __future__ import annotations

import threading

import pytest

from tests.security.mcp_world import (
    FOREIGN,
    GRANTED,
    RULED,
    SHARED,
    SHARED_MARKER,
    WILD,
    WS_B,
    set_env,
)
from tests.security.share_world import holder_token, make_share, real_clones

#: The word the scan looks for: `helper` is a symbol of every repository's graph, and
#: SHARED_FILES mentions it in `src/app.py` (`return helper()`).
WORD = "helper"
CALLER_FILE = "src/caller.py"
OWN_FILES = {
    CALLER_FILE: "def run():\n    return helper()\n",
    "src/other.py": "def other():\n    return 2\n",
}
OWNS = (WILD, GRANTED, RULED)
#: A second repository of the owner's, as real as SHARED (graph, clone, files).
SECOND = "github_bco-second"


@pytest.fixture(scope="module")
def own_clones(mcp_graphs):
    """The receiver's own repositories as real git clones that mention `helper`, and a
    second repository of the owner's that a share can name."""
    from src.config import Settings
    from tests.security.mcp_world import SHARED_FILES
    from tests.security.test_mcp_graph_obeys_access_rules_single_tenant import _build

    real_clones(mcp_graphs, OWNS, OWN_FILES)
    _build(Settings(workspace_dir=mcp_graphs), SECOND)
    real_clones(mcp_graphs, [SECOND], SHARED_FILES)
    return mcp_graphs


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch, own_clones):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


@pytest.fixture(autouse=True)
def _a_call_in_flight():
    """The call record is what the shared-text note and the call log are read from."""
    from src.mcp_server import callctx

    callctx.reset()
    yield
    callctx.reset()


def _refs(world, bearer, slug: str, **kw) -> str:
    from src.mcp_server import callctx
    from src.mcp_server.dev_profile import tools_refs

    world.as_(bearer)
    callctx.begin("refs")
    return tools_refs.run(slug, WORD, **kw)


def _holder(world, share_id: str, own=OWNS, who: str = "owner") -> str:
    """The owner of workspace A reads all of A, so what limits the answer is the share."""
    return holder_token(world, [share_id], own=list(own), who=who)[0]


# ─── both directions ─────────────────────────────────────────────────


def test_the_callers_of_a_shared_symbol_are_looked_for_in_the_callers_own_repositories(world):
    out = _refs(world, _holder(world, make_share(world)), SHARED)
    assert "text refs in sibling repos (heuristic)" in out
    for own in OWNS:
        assert f"{own} {CALLER_FILE}" in out and f"{own} 1 files" in out, own
    assert "text-ref" in out


def test_the_callers_of_an_own_symbol_are_looked_for_in_the_shared_repository(world):
    out = _refs(world, _holder(world, make_share(world)), WILD)
    assert f"{SHARED} src/app.py:" in out and "text-ref" in out
    assert f"{SHARED} 2 files" in out, "src/app.py and src/util.py both mention it"


def test_the_answer_marks_the_shared_text_as_data_in_both_directions(world):
    from src.mcp_server.dev_profile.emit import SHARED_NOTE

    bearer = _holder(world, make_share(world))
    assert SHARED_NOTE in _refs(world, bearer, SHARED)
    assert SHARED_NOTE in _refs(world, bearer, WILD)


def test_an_own_symbol_asked_with_cross_repo_off_never_leaves_the_repository(world):
    out = _refs(world, _holder(world, make_share(world)), WILD, cross_repo=False)
    assert SHARED not in out and "text-ref" not in out


def test_two_shared_repositories_are_not_scanned_for_each_other(world):
    """A shared repository is linked to the caller's own, never to another shared one."""
    from tests.security.share_world import register_in

    register_in(world, WS_B, SECOND)
    first = make_share(world)
    second = make_share(world, slug=SECOND)
    bearer = holder_token(world, [first, second], own=[], who="owner")[0]
    out = _refs(world, bearer, SHARED)
    assert SECOND not in out
    assert "text refs in sibling repos" not in out


# ─── without a share ─────────────────────────────────────────────────


def test_nothing_of_another_workspace_is_scanned_without_a_share(world):
    own_only = world.issue("owner", list(OWNS), kind="pat", profile="dev")[0]
    out = _refs(world, own_only, WILD)
    assert SHARED not in out and SHARED_MARKER not in out and FOREIGN not in out
    assert "text-ref" not in out


def test_a_share_held_by_somebody_else_scans_nothing_for_this_token(world):
    make_share(world)                                   # live, but on nobody's token
    out = _refs(world, world.issue("owner", list(OWNS), kind="pat", profile="dev")[0], WILD)
    assert SHARED not in out and "text-ref" not in out


def test_the_shared_repository_asked_for_without_a_share_answers_as_a_missing_one(world):
    from tests.security.mcp_world import GHOST

    bearer = world.issue("owner", list(OWNS), kind="pat", profile="dev")[0]
    asked = _refs(world, bearer, SHARED)
    ghost = _refs(world, bearer, GHOST)
    assert asked.replace(SHARED, "<slug>").split("similar:")[0] == \
        ghost.replace(GHOST, "<slug>").split("similar:")[0]


# ─── what the share and the owner hide ───────────────────────────────


def test_a_path_the_share_denies_is_not_scanned_and_not_quoted(world):
    bearer = _holder(world, make_share(world, deny_globs=["src/app.py"]))
    out = _refs(world, bearer, WILD)
    assert f"{SHARED} src/app.py" not in out and "return helper()" not in out.split(SHARED)[-1]
    assert SHARED_MARKER not in out


def test_a_share_that_denies_every_hit_leaves_no_trace_of_the_repository_in_the_answer(world):
    bearer = _holder(world, make_share(world, deny_globs=["src/**"]))
    out = _refs(world, bearer, WILD)
    assert SHARED not in out and "text-ref" not in out


def test_a_path_the_owner_denies_is_not_scanned(world):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id="rule-o", workspace_id=WS_B, team_id="team-b",
                             repo_slug=SHARED, visibility="code", deny_globs=["src/app.py"]))
        s.commit()
    out = _refs(world, _holder(world, make_share(world)), WILD)
    assert f"{SHARED} src/app.py" not in out


# ─── the owner's groups stay the owner's ─────────────────────────────


def _owner_group(tmp_path, monkeypatch, *repos: str) -> None:
    """A group of the OWNER's, in a directory of the test's own."""
    from src.config import Settings
    from src.groups.manager import GroupManager

    mgr = GroupManager(Settings(workspace_dir=tmp_path))
    monkeypatch.setattr("src.groups.manager._default_manager", mgr)
    group = mgr.create("owners-backend", workspace_id=WS_B)
    group.repos = [f"github:{slug.split('_', 1)[1].replace('-', '/', 1)}" for slug in repos]
    mgr.save(group)


def test_the_other_repositories_of_the_owners_group_are_not_scanned_or_named(
        world, tmp_path, monkeypatch):
    """The group holds the shared repository, another one of the owner's that nothing
    shares, and a third that is shared with the same caller by a share of its own."""
    from tests.security.share_world import register_in

    register_in(world, WS_B, SECOND)
    _owner_group(tmp_path, monkeypatch, SHARED, FOREIGN, SECOND)
    first, second = make_share(world), make_share(world, slug=SECOND)
    bearer = holder_token(world, [first, second], own=list(OWNS), who="owner")[0]
    out = _refs(world, bearer, SHARED)
    assert FOREIGN not in out and SECOND not in out
    assert f"{WILD} {CALLER_FILE}" in out, "the caller's own repositories are still scanned"


def test_a_group_of_the_receivers_that_names_a_shared_repository_scans_it_once(
        world, tmp_path, monkeypatch):
    """Not a state the routes allow, but a file on disk can be: the shared repository
    then reaches `refs` by the share, and counts once."""
    from src.config import Settings
    from src.groups.manager import GroupManager

    mgr = GroupManager(Settings(workspace_dir=tmp_path))
    monkeypatch.setattr("src.groups.manager._default_manager", mgr)
    group = mgr.create("receivers-group", workspace_id="wsid-a")
    group.repos = ["github:aco/wild", "github:bco/shared"]
    mgr.save(group)
    out = _refs(world, _holder(world, make_share(world)), WILD)
    assert out.count(f"{SHARED} 2 files") == 1


# ─── the cap, and side by side ───────────────────────────────────────


def test_the_scan_is_capped_and_the_answer_says_it_looked_in_part(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_refs

    monkeypatch.setattr(tools_refs, "MAX_SCANNED", 2)
    out = _refs(world, _holder(world, make_share(world)), SHARED)
    assert "text refs looked for in 2 of 3 repos; use grep with repo= for the rest" in out
    assert sum(f"{own} 1 files" in out for own in OWNS) == 2


def test_the_cap_counts_the_shared_repositories_with_the_rest(world, monkeypatch):
    from src.mcp_server.dev_profile import tools_refs

    monkeypatch.setattr(tools_refs, "MAX_SCANNED", 1)
    out = _refs(world, _holder(world, make_share(world), own=(WILD, GRANTED)), WILD)
    assert f"{SHARED} 2 files" in out, "the only repository linked to an own one is shared"
    assert "looked for in" not in out, "one linked repository, one slot: nothing was left out"


def test_the_repositories_are_scanned_side_by_side(world, monkeypatch):
    """Each scan waits for the other two. One after the other, the first would wait
    for ever; the barrier breaks, the repository is dropped, and the answer shows it."""
    from src.mcp_server.dev_profile import git_io

    barrier = threading.Barrier(len(OWNS), timeout=5)
    real = git_io.grep

    def meet(slug, *a, **kw):
        barrier.wait()
        return real(slug, *a, **kw)

    monkeypatch.setattr(git_io, "grep", meet)
    out = _refs(world, _holder(world, make_share(world)), SHARED)
    for own in OWNS:
        assert f"{own} 1 files" in out, own
