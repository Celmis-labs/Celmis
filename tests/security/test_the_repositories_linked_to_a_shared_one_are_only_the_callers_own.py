"""`refs` looks across a shared repository and the caller's own, and across nothing else.

`shares.linked_repos(scope, slug)` is the list of repositories `refs` scans for the
other side of `slug`. From a shared repository: the repositories of the caller's own
that the caller reads in full. From one of the caller's own: the shared ones the
caller reads. Never the owner's other repositories or groups (they are not in the
caller's decisions at all), never one the caller reads only as metadata, never the
repository itself, and never more than the cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from src.access import shares
from src.access.resolver import RepoAccessDecision, _RuleView
from tests.security.mcp_world import ALL_A, GRANTED, RULED, SHARED, WILD, WS_A, WS_B

OWN = [f"github_aco-r{i:02d}" for i in range(30)]
LATER = [f"github_bco-s{i:02d}" for i in range(30)]


def _shared(slug: str) -> RepoAccessDecision:
    return shares.SharedGrant(
        share_id="00000000-0000-4000-8000-000000000001", repo_slug=slug,
        owner_workspace_id=WS_B, grantee_workspace_id=WS_A, deny_globs=(),
        allow_ask=False, expires_at=datetime.now(UTC)).decision()


def _metadata(slug: str) -> RepoAccessDecision:
    return RepoAccessDecision(repo_slug=slug, visibility="metadata", open_default=False,
                              rules=(_RuleView("metadata", (), (), ()),))


def _scope(decisions: dict) -> SimpleNamespace:
    return SimpleNamespace(decisions=decisions)


def test_from_a_shared_repository_the_callers_own_ones_are_linked():
    scope = _scope({SHARED: _shared(SHARED), WILD: RepoAccessDecision.full(WILD),
                    GRANTED: RepoAccessDecision.full(GRANTED)})
    assert shares.linked_repos(scope, SHARED) == sorted([WILD, GRANTED])


def test_from_an_own_repository_the_shared_ones_are_linked_and_the_other_own_ones_are_not():
    scope = _scope({SHARED: _shared(SHARED), WILD: RepoAccessDecision.full(WILD),
                    GRANTED: RepoAccessDecision.full(GRANTED)})
    assert shares.linked_repos(scope, WILD) == [SHARED]


def test_two_shared_repositories_are_not_linked_to_each_other():
    scope = _scope({SHARED: _shared(SHARED), "github_bco-other": _shared("github_bco-other"),
                    WILD: RepoAccessDecision.full(WILD)})
    assert shares.linked_repos(scope, SHARED) == [WILD]


def test_a_repository_the_caller_reads_only_as_metadata_is_not_linked():
    scope = _scope({SHARED: _shared(SHARED), WILD: _metadata(WILD),
                    GRANTED: RepoAccessDecision.full(GRANTED)})
    assert shares.linked_repos(scope, SHARED) == [GRANTED]


def test_a_repository_the_caller_cannot_read_has_nothing_linked():
    scope = _scope({WILD: RepoAccessDecision.full(WILD), RULED: RepoAccessDecision.denied(RULED)})
    assert shares.linked_repos(scope, SHARED) == []
    assert shares.linked_repos(scope, RULED) == []
    assert shares.linked_repos(_scope({SHARED: _metadata(SHARED), WILD: RepoAccessDecision.full(WILD)}),
                               SHARED) == []


def test_a_scope_without_decisions_links_nothing():
    assert shares.linked_repos(SimpleNamespace(), SHARED) == []
    assert shares.linked_repos(_scope({}), SHARED) == []
    assert shares.linked_repos(_scope(None), SHARED) == []


def test_the_list_is_sorted_and_capped():
    scope = _scope({SHARED: _shared(SHARED), **{s: RepoAccessDecision.full(s) for s in OWN}})
    got = shares.linked_repos(scope, SHARED)
    assert got == sorted(OWN)[:shares.MAX_LINKED] and len(got) == shares.MAX_LINKED
    assert shares.linked_repos(scope, SHARED, limit=3) == sorted(OWN)[:3]
    assert shares.linked_repos(scope, SHARED, limit=0) == []
    assert shares.linked_repos(scope, SHARED, limit=-5) == []


def test_from_an_own_repository_the_shared_ones_are_capped_too():
    scope = _scope({WILD: RepoAccessDecision.full(WILD), **{s: _shared(s) for s in LATER}})
    assert len(shares.linked_repos(scope, WILD)) == shares.MAX_LINKED


def test_what_a_real_holders_scope_links_is_decided_by_the_caller_and_not_the_owner(mcp_world):
    from src.access.effective import mcp_scope
    from tests.security.share_world import holder_token, make_share

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=["github_aco-*"])
    mcp_world.as_(bearer)
    scope = mcp_scope()
    assert set(scope.decisions) >= ALL_A | {SHARED}
    assert shares.linked_repos(scope, SHARED) == sorted(ALL_A)
    assert shares.linked_repos(scope, WILD) == [SHARED]
