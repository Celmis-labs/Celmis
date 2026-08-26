"""One repository, two spellings, and whichever the admin typed blinded half
the product.

`RepoTeamAccess.repo_slug` is free text. `PUT /teams/{id}/repos/{slug:path}`
stores whatever it is handed and nothing normalises, so the spelling is
decided by the person filling the box — and the box's own placeholder says
"owner/repo" (admin.teams.repoSlugPlaceholder). Meanwhile:

  * `POST /api/reviews/trigger` resolves the ref to "owner/repo" — it matches
    what the UI told the admin to type;
  * every path-param route — DELETE /api/repos/{slug}, the intel endpoints,
    the review-policy endpoints — carries the INDEXED slug
    "{provider}_{owner}-{name}", which is what registration writes.

So one enforcement family always missed. On the deployed instance both
spellings were granted through the API and both were stored, side by side, as
two rows for one repository: nothing anywhere objected.

Which spelling is "right" cannot be decided from the string — a bare
"owner/name" does not say which provider it is, and "owner-name" cannot be
un-flattened at all. The mapping comes from the workspace's own registered
repositories, which hold both, and the lookup accepts either. Rows written
before this keep working without a migration.

The deployment is single_tenant, where a missing grant falls OPEN — so this
was invisible in production and would have become a 403 on every guarded route
the day anyone switched to multi_tenant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.deps import repo_grant_candidates, slug_from_pr_ref

WS = "ws-alpha"
FULL = "celmis-codereviewer/celmis-e2e-probe"
INDEXED = "github_celmis-codereviewer-celmis-e2e-probe"


@pytest.fixture
def registered(monkeypatch):
    cfg = SimpleNamespace(repo_slug=INDEXED, full_name=FULL, provider="github")
    store = SimpleNamespace(list_for_workspace=lambda ws: [cfg] if ws == WS else [])
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: store)
    return store


def test_the_indexed_slug_reaches_a_grant_typed_as_owner_repo(registered):
    assert FULL in repo_grant_candidates(INDEXED, WS)


def test_owner_repo_reaches_a_grant_stored_as_the_indexed_slug(registered):
    assert INDEXED in repo_grant_candidates(FULL, WS)


def test_the_value_itself_always_comes_first(registered):
    assert repo_grant_candidates(INDEXED, WS)[0] == INDEXED


def test_an_unregistered_repo_keeps_its_literal_spelling(registered):
    assert repo_grant_candidates("github_other-thing", WS) == ["github_other-thing"]


def test_without_a_workspace_nothing_is_invented(registered):
    assert repo_grant_candidates(INDEXED, None) == [INDEXED]


def test_an_unreadable_registry_narrows_rather_than_denies(monkeypatch):
    """The registry failing must not turn into "no grants" — that decides a
    permission question on an infrastructure fault."""
    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", boom)

    assert repo_grant_candidates(INDEXED, WS) == [INDEXED]


def test_a_repo_of_another_workspace_does_not_map(registered):
    assert repo_grant_candidates(INDEXED, "ws-beta") == [INDEXED]


# ─── the ref parser ──────────────────────────────────────────────────


@pytest.mark.parametrize("ref,expected", [
    ("https://gitlab.com/acme/backend/payments/-/merge_requests/42",
     "acme/backend/payments"),
    ("gitlab:acme/backend/payments#42", "acme/backend/payments"),
    ("https://github.com/acme/frontend/pull/7", "acme/frontend"),
    ("github:acme/frontend#7", "acme/frontend"),
    ("https://bitbucket.org/acme/web/pull-requests/3", "acme/web"),
])
def test_the_whole_project_path_survives(ref, expected):
    assert slug_from_pr_ref(ref) == expected


def test_both_forms_of_one_subgroup_ref_agree():
    """They disagreed: the shorthand kept the full path, the URL was cut to
    two segments — so one repository had two permission keys."""
    url = slug_from_pr_ref(
        "https://gitlab.com/acme/backend/payments/-/merge_requests/42")
    short = slug_from_pr_ref("gitlab:acme/backend/payments#42")

    assert url == short
