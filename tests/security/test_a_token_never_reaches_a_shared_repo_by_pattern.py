"""No repository list, however it is spelled, reaches a repository another workspace shares.

A token's repository list is matched with globs against the receiving workspace's
own registry. A shared repository is not in that registry, and the ids of shares
live in a column beside the list, never in it, so there is no glob, and no entry
shaped like an id, that could be made to cover one. This is the property the
design rests on; these tests try to break it from every side:

  * every wildcarded spelling of the shared repository's slug and name, one by
    one and all together, grants nothing;
  * an entry that looks like a share (``share:<id>``, anything with a colon) is not
    a valid entry at all, and a bare id is only a slug that matches nothing;
  * the matcher is never handed a share id, and the share ids are not in the list.
"""

from __future__ import annotations

import pytest

from tests.security.mcp_world import (
    ALL_A,
    FOREIGN,
    SHARED,
    WILD,
    WS_A,
    WS_B,
    set_env,
)
from tests.security.share_world import holder_token, make_share

NAMES = (SHARED, "bco/shared")


def _spellings() -> list[str]:
    """Wildcarded forms of the slug and the repository name: each substring
    replaced by ``*``, each character by ``?``, each as a bracket class, and the
    prefixes and suffixes."""
    out: set[str] = set()
    for name in NAMES:
        out.add(name)
        for i in range(len(name) + 1):
            for j in range(i, len(name) + 1):
                out.add(name[:i] + "*" + name[j:])
        for i in range(len(name)):
            out.add(name[:i] + "?" + name[i + 1:])
            out.add(name[:i] + "[" + name[i] + "]" + name[i + 1:])
            out.add(name[:i] + "[a-z]" + name[i + 1:])
        out.update(f"{name[:i]}*" for i in range(1, len(name)))
        out.update(f"*{name[i:]}" for i in range(len(name)))
    out.update({"*", "**", "*/*", "*_*", "?*", "[a-z]*", "github_*", "github_b*", "b*", "*shared", "*/shared"})
    return sorted(p for p in out if p)


def _valid(patterns: list[str]) -> list[str]:
    from src.mcp_server import token_store

    ok = []
    for p in patterns:
        try:
            ok.extend(token_store.normalise_patterns([p]))
        except token_store.TokenError:
            continue
    return ok


SPELLINGS = _valid(_spellings())


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    return mcp_world


def _decision(patterns, slug=SHARED):
    from src.access.effective import Principal, effective_access

    return effective_access(Principal("u-mn", False, tuple(patterns)), WS_A, [slug])[slug]


def test_the_spellings_under_test_are_many_and_all_of_them_are_legal_entries():
    assert len(SPELLINGS) > 300
    assert {"*", "github_bco-*", "bco/*", "github_bco-shared", "bco/shared"} <= set(SPELLINGS)


def test_no_spelling_of_the_shared_repository_reads_it(world):
    make_share(world)
    reached = [p for p in SPELLINGS if _decision([p]).researchable]
    assert reached == []


def test_the_spellings_all_together_read_only_the_receivers_own_repositories(world):
    from src.access.effective import Principal, effective_access

    make_share(world)
    for start in range(0, len(SPELLINGS), 40):
        chunk = SPELLINGS[start:start + 40]
        got = effective_access(Principal("u-mn", False, tuple(chunk)), WS_A,
                               [*sorted(ALL_A), SHARED, FOREIGN])
        assert not got[SHARED].researchable and not got[FOREIGN].researchable


def test_a_share_on_another_repository_changes_no_spelling_into_a_way_to_this_one(world):
    """A token that holds a share of FOREIGN, and lists every spelling, still has
    no more of SHARED than anybody: each share names its own repository."""
    from src.access.effective import Principal, effective_access

    make_share(world)
    other = make_share(world, slug=FOREIGN)
    for start in range(0, len(SPELLINGS), 40):
        got = effective_access(
            Principal("u-mn", False, tuple(SPELLINGS[start:start + 40]), share_ids=(other,)),
            WS_A, [SHARED, FOREIGN])
        assert not got[SHARED].researchable
        assert got[FOREIGN].via_share == other


@pytest.mark.parametrize("entry", [
    "share:00000000-0000-4000-8000-000000000000", "share:*", "shared:x", "a:b", ":", "*:*",
    "github_bco-shared:read", "id:" + "0" * 36, "share/00000000-0000-4000-8000-000000000000/..",
])
def test_an_entry_shaped_like_a_share_is_not_a_repository_entry(entry):
    from src.mcp_server import token_store

    with pytest.raises(token_store.TokenError):
        token_store.normalise_patterns([entry])


def test_a_token_cannot_be_issued_or_changed_with_an_entry_shaped_like_a_share(world):
    from src.mcp_server import token_store

    s1 = make_share(world)
    for entry in (f"share:{s1}", f"{SHARED}:{s1}"):
        with pytest.raises(token_store.TokenError):
            world.issue("mn", [entry], kind="pat", profile="dev")
        _bearer, view = holder_token(world, [s1], own=[WILD])
        with world.session() as s, pytest.raises(token_store.TokenError):
            token_store.update_row(s, view.id, patterns=[entry])


def test_a_bare_share_id_listed_as_a_repository_is_only_a_name_that_matches_nothing(world):
    s1 = make_share(world)
    assert _decision([s1]).researchable is False
    assert _decision([s1, "*"]).via_share is None


def test_the_ids_of_shares_are_never_in_the_repository_list(world):
    s1 = make_share(world)
    _bearer, view = holder_token(world, [s1], own=[WILD])
    assert view.shares == (s1,) and view.patterns == (WILD,)
    assert s1 not in repr(world.row(view.id).repo_patterns)


def test_the_matcher_is_never_handed_a_share_id_or_the_shared_repository(world, monkeypatch):
    from src.access import effective
    from src.mcp_server.identity import caller_access

    s1 = make_share(world)
    bearer, _view = holder_token(world, [s1], own=["github_aco-*"])
    seen: list[tuple] = []
    real = effective.match_any

    def spy(patterns, *names):
        seen.append((tuple(patterns), names))
        return real(patterns, *names)

    monkeypatch.setattr(effective, "match_any", spy)
    world.as_(bearer)
    _caller, access = caller_access([*sorted(ALL_A), SHARED, FOREIGN])
    assert access[SHARED].via_share == s1, "the share did its work"
    assert seen, "the receiver's own repositories were matched"
    for patterns, names in seen:
        assert patterns == ("github_aco-*",)
        assert s1 not in patterns and all(SHARED not in n and "bco/" not in n for n in names)


def test_a_pattern_cannot_name_the_owners_workspace_either(world):
    make_share(world)
    for entry in (WS_B, f"{WS_B}/*", f"{WS_B}/{SHARED}", f"{WS_B}:{SHARED}"):
        assert not _decision(_valid([entry])).researchable, entry
