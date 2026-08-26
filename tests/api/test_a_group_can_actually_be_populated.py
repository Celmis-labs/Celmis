"""Creating a group with repos in it works, and a failure leaves nothing behind.

WHAT SHIPPED AND DID NOT WORK. `1c47f7a` added `/api/repos/groups` so cross-repo
drift could finally be reached, and every non-empty create returned HTTP 500:

    POST /api/repos/groups {"repos": []}                          → 201
    POST /api/repos/groups {"repos": ["not-a-real-repo"]}          → 422 (guard works)
    POST /api/repos/groups {"repos": ["github_owner-name"]}        → 500

`_resolve_in_workspace` normalised each identifier to the bare registry slug
and returned THAT; `RepoGroup.add_repo` then calls `parse_repo_url`, which
needs owner/name and raises "got 1 segments". Unconditional — the route could
create only empty groups, and drift needs a group with a sibling, so drift
stayed exactly as unreachable as before the route existed.

AND EACH FAILURE LEAKED A GROUP. `mgr.create` writes the file BEFORE
`workspace_id` is assigned, so a raising `add_repo` left a group owned by
"default": invisible to the tenant's list, 404 to its delete, and 422 "already
exists" on retry. The name was squatted and unreachable over HTTP.

The previous tests missed both because they called `mgr.create()` and
`group.add_repo("github:acme/api")` directly — with an identifier that already
parses — and never went through the router that builds one.

THE OBVIOUS REPAIR IS ALSO WRONG. Passing the bare full name parses, but
`parse_repo_url` defaults to the BITBUCKET provider, so `owner/name` yields
slug `owner-name`, which never equals the `github_owner-name` a review
carries. `_find_group_for_repo` would return None and drift would stay dead
with no error anywhere. Silent is worse than 500.
"""

from __future__ import annotations

import pytest

from src.api.auto_review import AutoReviewStore, RepoConfig
from src.groups.manager import GroupManager
from src.sync.git_providers import parse_repo_url

SLUG = "github_acme-payments"


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """The store and the group manager the router will reach for."""
    ar = AutoReviewStore(tmp_path / "ar.db")
    ar.upsert(RepoConfig(
        user_id="alice@example.com", repo_slug=SLUG, provider="github",
        full_name="acme/payments", url="https://github.com/acme/payments",
        workspace_id="ws-1",
    ))
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: ar)

    mgr = GroupManager()
    mgr.groups_dir = tmp_path / "groups"
    mgr.groups_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.groups.get_group_manager", lambda: mgr)
    monkeypatch.setattr("src.groups.manager.get_group_manager", lambda: mgr)
    # The singleton, cleared on both sides. `get_group_manager` caches
    # `_default_manager` on first use, and monkeypatching the FUNCTION does
    # not clear that cache — so a later test reaching the real function gets a
    # manager pointing at the real workspace directory, and writes group files
    # into it. This file and its neighbour leaked into each other exactly that
    # way: one passed alone and failed in company, which reads as flakiness
    # and gets re-run instead of read.
    monkeypatch.setattr("src.groups.manager._default_manager", mgr, raising=False)
    # See the neighbour file: `cross_repo_drift` binds `get_group_manager` at
    # module import time, so it has to be patched where it is USED.
    monkeypatch.setattr("src.review.cross_repo_drift.get_group_manager",
                        lambda: mgr, raising=False)
    yield mgr
    import src.groups.manager as gm
    gm._default_manager = None


# ─── the identifier the group stores ─────────────────────────────────


def test_the_stored_identifier_round_trips_to_the_registered_slug(wired):
    """The whole bug in one assertion: whatever a group stores must parse back
    to the slug a review carries, or drift silently never matches."""
    from src.api.routers.groups import _resolve_in_workspace

    stored = _resolve_in_workspace([SLUG], "ws-1")

    assert parse_repo_url(stored[0]).slug == SLUG


def test_the_bare_slug_is_not_what_gets_stored(wired):
    """It does not parse at all — that was the 500."""
    from src.api.routers.groups import _resolve_in_workspace

    assert _resolve_in_workspace([SLUG], "ws-1") != [SLUG]
    with pytest.raises(ValueError):
        parse_repo_url(SLUG)


def test_the_bare_full_name_would_resolve_to_the_wrong_slug():
    """Documents why the obvious repair is wrong: no provider prefix means
    bitbucket, and bitbucket's slug has no `github_`."""
    assert parse_repo_url("acme/payments").slug != SLUG


def test_an_unregistered_repo_is_still_refused(wired):
    from fastapi import HTTPException

    from src.api.routers.groups import _resolve_in_workspace

    with pytest.raises(HTTPException) as exc:
        _resolve_in_workspace(["github_not-registered"], "ws-1")

    assert exc.value.status_code == 422


def test_a_repo_registered_in_another_workspace_is_refused(wired):
    from fastapi import HTTPException

    from src.api.routers.groups import _resolve_in_workspace

    with pytest.raises(HTTPException):
        _resolve_in_workspace([SLUG], "ws-2")


# ─── the group the router builds ─────────────────────────────────────


def test_a_group_can_be_created_with_repos_in_it(wired):
    from src.api.routers.groups import GroupCreate, create_group

    user = type("U", (), {"email": "alice@example.com", "id": "u-1"})()
    out = create_group(GroupCreate(name="settlement", repos=[SLUG]),
                       user=user, workspace_id="ws-1")

    assert out.repos, "the group came back empty — this is the 500 shape"
    assert parse_repo_url(out.repos[0]).slug == SLUG


def test_the_group_is_findable_by_the_drift_detector(wired):
    """The end the whole feature exists for. Two members, so `others` is
    non-empty — a one-repo group never fires."""
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.api.routers.groups import GroupCreate, create_group
    from src.review.cross_repo_drift import _find_group_for_repo

    get_auto_review_store().upsert(RepoConfig(
        user_id="alice@example.com", repo_slug="github_acme-billing",
        provider="github", full_name="acme/billing",
        url="https://github.com/acme/billing", workspace_id="ws-1",
    ))
    user = type("U", (), {"email": "alice@example.com", "id": "u-1"})()
    create_group(GroupCreate(name="settlement",
                             repos=[SLUG, "github_acme-billing"]),
                 user=user, workspace_id="ws-1")

    found = _find_group_for_repo(SLUG, "ws-1")

    assert found is not None, "drift still cannot find its group"
    name, others = found
    assert name == "settlement"
    assert others == ["github_acme-billing"]


# ─── the leak ────────────────────────────────────────────────────────


def test_a_failed_create_leaves_no_group_behind(wired, monkeypatch):
    """`mgr.create` writes the file before the group is finished, so anything
    that raises afterwards squatted the name: invisible to list, 404 to
    delete, 422 to a retry."""
    from src.api.routers.groups import GroupCreate, create_group

    def boom(self, ident):
        raise ValueError("simulated parse failure")

    monkeypatch.setattr("src.groups.models.RepoGroup.add_repo", boom)
    user = type("U", (), {"email": "alice@example.com", "id": "u-1"})()

    with pytest.raises(ValueError):
        create_group(GroupCreate(name="doomed", repos=[SLUG]),
                     user=user, workspace_id="ws-1")

    assert wired.list() == [], "an orphan group survived the failure"
    assert not (wired.groups_dir / "doomed.yaml").exists()
