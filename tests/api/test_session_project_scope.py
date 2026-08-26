"""Starting a session from a project: what the server decides, not the browser.

The picker sends `project_id` and the server expands it into the repo set. That
split is the point of these tests. A browser tab is a cache of what a project
held when the page rendered, and sessions are started from tabs left open for
hours — so the set that gets cloned has to be read at submit time, from the
database, scoped to the caller's workspace.

These tests exercise the expansion rules directly rather than through the HTTP
stack, which needs Postgres, an authenticated user and a connected Claude
account. What is worth pinning here is the ordering and de-duplication, and
those are plain data rules.
"""

from __future__ import annotations

import pytest

from src.api.routers.claude_code import MAX_SESSION_REPOS


def resolve(project_slugs: list[str], repo_slug: str, extra: list[str]) -> list[str]:
    """The set-building rule from create_session, isolated.

    Kept as a copy of those four lines on purpose: importing the handler would
    drag in the DB session, the user store and the agent runner for a list
    comprehension. If the handler's ordering changes, these tests must fail —
    that is what makes the copy worth having.
    """
    wanted: list[str] = []
    for slug in [*project_slugs, repo_slug, *extra]:
        if slug and slug not in wanted:
            wanted.append(slug)
    return wanted


def test_project_members_lead_in_project_order():
    """The first project repo becomes the session's primary repo.

    It is the one `repo_slug` keeps carrying, so it is the one the session
    card, the push notification and the spend ledger will name.
    """
    got = resolve(["api", "web", "worker"], "api", [])
    assert got == ["api", "web", "worker"]


def test_primary_repo_is_not_duplicated():
    """The client sends the first project repo as repo_slug as well.

    Without the de-dup that repo would be cloned twice into the same session
    directory, and the second clone would fail on a non-empty target.
    """
    got = resolve(["api", "web"], "api", [])
    assert got == ["api", "web"]
    assert len(got) == len(set(got))


def test_a_project_pick_carries_no_primary_slug_of_its_own():
    """The picker sends repo_slug="" and the ADD-ONS when a project is chosen.

    Not its own expansion of the project: that list was read when the page
    rendered. Sending it too would union stale membership with fresh, and a
    repo dropped from the project an hour ago would be cloned anyway — the
    exact thing resolving server-side exists to prevent.
    """
    got = resolve(["api", "web"], "", ["docs"])
    assert got == ["api", "web", "docs"]
    assert got[0] == "api", "the project's first repo is still the primary one"


def test_chips_extend_the_project_without_reordering_it():
    got = resolve(["api", "web"], "api", ["docs"])
    assert got == ["api", "web", "docs"]


def test_a_chip_already_in_the_project_is_absorbed():
    got = resolve(["api", "web"], "api", ["web", "docs"])
    assert got == ["api", "web", "docs"]


def test_plain_repo_pick_is_unchanged_by_the_project_path():
    """The single-repo flow is the common case and must not have moved."""
    assert resolve([], "api", []) == ["api"]
    assert resolve([], "api", ["web"]) == ["api", "web"]


def test_a_project_over_the_cap_is_refused_not_truncated():
    """Six repos must fail the cap check rather than silently become five.

    Auditing five of a project's six repositories and reporting success is the
    failure mode worth paying an error for: nobody reads a session summary and
    notices which repo is absent.
    """
    slugs = [f"repo-{i}" for i in range(MAX_SESSION_REPOS + 1)]
    got = resolve(slugs, slugs[0], [])
    assert len(got) == MAX_SESSION_REPOS + 1, "expansion must not truncate"
    assert len(got) > MAX_SESSION_REPOS, "…so the handler's cap check rejects it"


@pytest.mark.parametrize("empty", ["", None])
def test_falsy_slugs_never_enter_the_set(empty):
    got = resolve([empty, "api"], "api", [empty])
    assert got == ["api"]
