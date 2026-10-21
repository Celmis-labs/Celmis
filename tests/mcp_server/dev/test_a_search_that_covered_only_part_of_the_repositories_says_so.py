"""A tool that cut the repositories it searched says how many, never silently.

The contract (docs/MCP_DEV.md "Limits per token", docs/mcp-access.md):

  * one call searches at most `MAX_REPOS_PER_CALL` (40) repositories; when a
    token reaches more, `find` prints
    `searched 40 of 90 repos; pass repo= to target one` - the same line `grep`
    prints for its own cut - and `search_symbols`, which has no `repo=`, prints
    `searched 40 of 90 repos; pass project_id= to narrow` (not "or a narrower
    query": the first 40 are searched whatever the query), because a search over
    40 of 90 repositories that says nothing reads as "no other repository has it";
    when `project_id=` WAS given and the project itself holds more than 40, advising
    `project_id=` again is no way out, so it prints
    `searched 40 of 90 repos in this project; narrow the project`;
  * the cut is made AFTER the repositories the caller cannot research are
    dropped: they never use up the cap, and the count in the line is of the
    repositories the caller can search, so it names and counts nothing hidden;
  * `refs` says how many sibling repositories it looked in for text mentions,
    `brief` says how much of `repos=` it read, and `generate_client` says how
    many sibling repositories it looked in;
  * under the cap, or with `repo=`, there is no such line.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

from src.mcp_server import http_app
from src.mcp_server.dev_contract import HIT_RE
from src.mcp_server.dev_profile import common, tools_find, tools_grep, tools_refs
from src.mcp_server.dev_profile import tools_skills as ts
from tests.mcp_server.dev.conftest import BILLING, SHOP

LINE = "searched {} of {} repos; pass repo= to target one"
#: `search_symbols` has no `repo=` parameter; its way out is `project_id=`.
SYMBOLS_LINE = "searched {} of {} repos; pass project_id= to narrow"
#: ...and when the caller already named the project, which is itself over the cap.
PROJECT_LINE = "searched {} of {} repos in this project; narrow the project"


def _many(n: int) -> dict[str, str]:
    """`n` readable repository names that exist only as names (no clone, no graph)."""
    return {f"github_acme-svc{i:03d}": "code" for i in range(n)}


# ─── find ────────────────────────────────────────────────────────────


def test_find_over_more_repos_than_the_cap_says_it_searched_only_the_cap(as_caller, monkeypatch):
    as_caller(_many(45))
    seen: list[list[str]] = []
    monkeypatch.setattr(tools_find, "search",
                        lambda scope, slugs, q, **kw: seen.append(list(slugs)) or [])

    out = tools_find.run("create_order")

    assert [len(s) for s in seen] == [common.MAX_REPOS_PER_CALL]
    assert LINE.format(40, 45) in out.splitlines()


@pytest.mark.parametrize("readable", [3, 40])
def test_find_at_or_under_the_cap_has_no_such_line(as_caller, monkeypatch, readable):
    as_caller(_many(readable))
    monkeypatch.setattr(tools_find, "search", lambda scope, slugs, q, **kw: [])

    assert "searched" not in tools_find.run("create_order")


def test_find_with_repo_has_no_such_line_however_many_repos_are_readable(as_caller, monkeypatch):
    as_caller({**_many(45), SHOP: "code"})
    monkeypatch.setattr(tools_find, "search", lambda scope, slugs, q, **kw: [])

    assert "searched" not in tools_find.run("create_order", repo=SHOP)


def test_find_that_cuts_a_real_search_still_answers_from_the_repos_it_kept(as_caller, world, monkeypatch):
    """With the cap at 1 the first readable repository answers and the line says 1 of 2."""
    monkeypatch.setattr(common, "MAX_REPOS_PER_CALL", 1)

    out = tools_find.run("create")

    assert LINE.format(1, 2) in out.splitlines()
    hits = [ln for ln in out.split("\n")[1:] if re.match(HIT_RE, ln)]
    assert hits and all(h.startswith(BILLING) for h in hits)      # the repositories sort billing, shop


def test_grep_prints_the_same_line_for_its_own_cut(as_caller, world, monkeypatch):
    monkeypatch.setattr(tools_grep, "MAX_REPOS", 1)

    assert LINE.format(1, 2) in tools_grep.run("create_order").splitlines()


def test_the_cap_the_documentation_names_is_forty():
    assert common.MAX_REPOS_PER_CALL == 40
    assert common.cap_repos([f"r{i}" for i in range(40)]) == ([f"r{i}" for i in range(40)], "")
    kept, line = common.cap_repos([f"r{i}" for i in range(41)])
    assert len(kept) == 40 and kept[0] == "r0" and line == LINE.format(40, 41)


# ─── search_symbols (the full server and the REST search) ────────────


@pytest.fixture
def symbols(monkeypatch, world, freshness, shared_graphs):
    """`symbols(readable=[...], hidden=[...])`: who the full server lets search what.
    Every repository, whatever its name, answers from the shop graph."""
    from src.access import RepoAccessDecision
    from src.mcp_server import identity
    from src.mcp_server.dev_profile import access

    @contextmanager
    def shop_graph(_slug):
        yield shared_graphs(SHOP)

    def apply(readable: list[str], hidden: list[str] = ()):
        every = [*hidden, *readable]            # the denied ones come first on purpose
        monkeypatch.setattr(access, "indexed_slugs", lambda: every)
        monkeypatch.setattr(common, "open_store", shop_graph)
        monkeypatch.setattr(identity, "caller_access", lambda repos: (None, {
            r: RepoAccessDecision.full(r) if r in readable else RepoAccessDecision.denied(r)
            for r in repos}))

    return apply


def test_search_symbols_over_more_repos_than_the_cap_says_so_and_counts_only_what_it_may_search(symbols):
    readable = [*_many(42)]
    hidden = [f"github_acme-private{i}" for i in range(3)]
    symbols(readable, hidden)

    out = http_app._search_symbols_impl(None, "create_order", None, 5)

    assert out["note"] == SYMBOLS_LINE.format(40, 42), "42 searchable repos, not the 45 that exist"
    assert out["matches"], "the search itself still ran"
    assert not any(h in str(out) for h in hidden)


def test_search_symbols_in_a_project_over_the_cap_does_not_advise_the_project_id_again(symbols, monkeypatch):
    """`project_id=` was passed; "pass project_id= to narrow" would send the caller in a circle."""
    readable = [*_many(42)]
    hidden = [f"github_acme-private{i}" for i in range(3)]
    symbols(readable, hidden)
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: [*hidden, *readable])

    out = http_app._search_symbols_impl("proj-1", "create_order", None, 5)

    assert out["note"] == PROJECT_LINE.format(40, 42), "42 searchable repos of the project, not the 45"
    assert "project_id" not in out["note"]
    assert out["matches"], "the search itself still ran"
    assert not any(h in str(out) for h in hidden)


def test_search_symbols_in_a_project_within_the_cap_has_no_such_line(symbols, monkeypatch):
    readable = [*_many(40)]
    symbols(readable)
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: readable)

    assert "note" not in http_app._search_symbols_impl("proj-1", "create_order", None, 5)


def test_repos_the_caller_cannot_search_do_not_use_up_the_cap(symbols):
    """Ninety repositories exist and the caller may search two: the 88 that sort first must not
    take the 40 places, or the two real answers are cut away and the search finds nothing."""
    symbols([BILLING, SHOP], hidden=[f"github_acme-private{i:02d}" for i in range(88)])

    out = http_app._search_symbols_impl(None, "create_order", None, 5)

    assert {m["repo_slug"] for m in out["matches"]} == {BILLING, SHOP}
    assert "note" not in out


# ─── refs, brief, generate_client ────────────────────────────────────


def test_refs_says_how_many_sibling_repos_it_left_out(as_caller, world, monkeypatch):
    monkeypatch.setattr(tools_refs, "_siblings",
                        lambda scope, slug: [BILLING, *(f"github_acme-sib{i}" for i in range(6))])

    out = tools_refs.run(SHOP, "create_order")

    assert "text refs looked for in 5 of 7 sibling repos; use grep with repo= for the rest" in out


def test_refs_with_few_siblings_says_nothing_about_a_cut(as_caller, world, monkeypatch):
    monkeypatch.setattr(tools_refs, "_siblings", lambda scope, slug: [BILLING])

    assert "sibling repos; use grep" not in tools_refs.run(SHOP, "create_order")


async def test_brief_says_when_repos_names_more_than_it_reads(pack_world):
    pack_world.as_caller("owner")
    names = ",".join(f"r{i}" for i in range(25))

    text = pack_world.call(ts.run_brief, "invoicing routine", names, tool="brief")[0]

    assert "repos= keeps the first 20 of 25 names; ask again with fewer to see pointers to the rest" \
        in text.splitlines()


async def test_brief_says_when_repos_was_longer_than_it_reads(pack_world):
    pack_world.as_caller("owner")
    text = pack_world.call(ts.run_brief, "invoicing routine", "a" * 450, tool="brief")[0]

    assert f"repos= is read up to {ts.MAX_REPOS_ARG} characters; the rest was ignored" in text.splitlines()


@pytest.mark.parametrize("names", ["", "r1,r2", ",".join(f"r{i}" for i in range(20))])
async def test_brief_with_a_short_repos_list_says_nothing_about_a_cut(pack_world, names):
    pack_world.as_caller("owner")

    text = pack_world.call(ts.run_brief, "invoicing routine", names, tool="brief")[0]

    assert "repos= " not in text


def test_generate_client_counts_the_sibling_repos_it_may_search_and_not_the_hidden_ones(monkeypatch):
    from src.access import RepoAccessDecision
    from src.mcp_server import identity
    from src.ownership import builder

    monkeypatch.setattr(builder, "load_snapshot", lambda slug: None)       # no database here

    target = "github_acme-api"
    consumers = [f"github_acme-web{i:02d}" for i in range(11)]
    hidden = [f"github_acme-private{i:02d}" for i in range(30)]       # sort first, never searchable
    searched: list[str] = []
    monkeypatch.setattr(http_app, "_get_api_surface_impl",
                        lambda slug, path_glob=None: {"endpoints": [{"path": "/orders", "handler": "h"}]})
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: [*hidden, target, *consumers])
    monkeypatch.setattr(http_app, "_legacy_callers", lambda handler, slug: searched.append(slug) or [])
    monkeypatch.setattr(identity, "caller_access", lambda repos: (None, {
        r: RepoAccessDecision.denied(r) if r in hidden else RepoAccessDecision.full(r) for r in repos}))

    out = http_app._bootstrap_client_impl(
        project_id="p", target_repo_slug=target, target_endpoint=None, language="python")

    assert searched == consumers[:http_app.SIBLING_REPOS_SEARCHED]
    assert "Usage examples were searched in 8 of 11 sibling repos of the project." in out["note"]
    assert not any(h in str(out) for h in hidden)


def test_generate_client_with_few_siblings_keeps_its_note_as_it_was(monkeypatch):
    from src.access import RepoAccessDecision
    from src.mcp_server import identity
    from src.ownership import builder

    monkeypatch.setattr(builder, "load_snapshot", lambda slug: None)       # no database here

    monkeypatch.setattr(http_app, "_get_api_surface_impl",
                        lambda slug, path_glob=None: {"endpoints": [{"path": "/orders", "handler": "h"}]})
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: ["a", "b", "c"])
    monkeypatch.setattr(http_app, "_legacy_callers", lambda handler, slug: [])
    monkeypatch.setattr(identity, "caller_access",
                        lambda repos: (None, {r: RepoAccessDecision.full(r) for r in repos}))

    out = http_app._bootstrap_client_impl(
        project_id="p", target_repo_slug="a", target_endpoint=None, language="python")

    assert "sibling repos of the project" not in out["note"]
