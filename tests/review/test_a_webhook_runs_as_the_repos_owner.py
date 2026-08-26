"""A webhook delivery runs under the repo owner's credential, and only if asked.

TWO DEFECTS, ONE ROOT. `_dispatch_review` knew which workspace a repo belonged
to and nothing else about it, so it filled the rest in with guesses.

  1. It hardcoded `user_id="default"` in the queue payload. `resolve_auth`
     looks for a personal Claude credential keyed by user id, falling back to
     `ws:{workspace_id}`; "default" matches neither. Every webhook-triggered
     review on a workspace whose Claude account is connected personally — the
     default in the UI — failed in 0.06 seconds and posted "this pull request
     has NOT been reviewed" onto the PR. Measured on production: the same pull
     request, triggered manually two minutes later, completed in 18.6s and
     produced two correctly anchored inline findings. The credential was
     always there; only the automatic path could not see it.

  2. It never read `enabled`. The binding row survives the auto-review toggle —
     that is what the toggle toggles — so a webhook left installed after
     somebody switched auto-review off kept spending model budget on every
     pull request.

The fix is one lookup: `config_for_repo` returns the row the webhook needed all
along, and the dispatcher reads the owner and the switch off it.
"""

from __future__ import annotations

import pytest

from src.api.auto_review import AutoReviewStore, RepoConfig


@pytest.fixture()
def store(tmp_path) -> AutoReviewStore:
    return AutoReviewStore(tmp_path / "auto_review.db")


def cfg(**kw) -> RepoConfig:
    base = dict(
        user_id="alice@example.com",
        repo_slug="github_acme-payments",
        provider="github",
        full_name="acme/payments",
        url="https://github.com/acme/payments",
        workspace_id="ws-1",
        enabled=True,
        mode="webhook",
    )
    base.update(kw)
    return RepoConfig(**base)


def test_the_config_carries_the_owner_not_a_placeholder(store):
    store.upsert(cfg())

    found = store.config_for_repo("github", "acme/payments")

    assert found is not None
    assert found.user_id == "alice@example.com"
    assert found.workspace_id == "ws-1"


def test_an_unknown_repo_has_no_config(store):
    assert store.config_for_repo("github", "acme/nothing-here") is None


def test_a_repo_in_two_workspaces_fails_closed(store):
    """Same rule as `workspace_for_repo`: an ambiguous repo must not run under
    a guessed tenant's keys."""
    store.upsert(cfg(user_id="alice@example.com", workspace_id="ws-1"))
    store.upsert(cfg(user_id="bob@example.com", workspace_id="ws-2"))

    assert store.config_for_repo("github", "acme/payments") is None


def test_an_enabled_row_wins_over_a_disabled_sibling(store):
    """Two members of one workspace can both hold a row for one repo. The
    delivery is for whoever switched it on; picking the disabled sibling would
    refuse a review its owner asked for."""
    store.upsert(cfg(user_id="alice@example.com", repo_slug="s-a", enabled=False))
    store.upsert(cfg(user_id="bob@example.com", repo_slug="s-b", enabled=True))

    found = store.config_for_repo("github", "acme/payments")

    assert found is not None
    assert found.enabled is True
    assert found.user_id == "bob@example.com"


def test_the_disabled_state_is_visible_to_the_caller(store):
    """The dispatcher refuses on this field, so it has to survive the round
    trip rather than defaulting back to True."""
    store.upsert(cfg(enabled=False))

    found = store.config_for_repo("github", "acme/payments")

    assert found is not None
    assert found.enabled is False


def test_the_workspace_answer_is_unchanged(store):
    """`workspace_for_repo` has other callers and its behaviour must not move
    just because a richer lookup was added beside it."""
    store.upsert(cfg())

    assert store.workspace_for_repo("github", "acme/payments") == "ws-1"
    assert store.workspace_for_repo("github", "acme/nothing-here") is None


# ─── the dispatcher itself ───────────────────────────────────────────


@pytest.fixture()
def dispatch(monkeypatch, store):
    """`_dispatch_review` with the store swapped and the queue captured."""
    import src.api.auto_review as ar
    import src.sync.queue as q

    monkeypatch.setattr(ar, "get_auto_review_store", lambda: store)
    queued: list[dict] = []
    monkeypatch.setattr(
        q, "enqueue",
        lambda **kw: queued.append(kw) or "job-1",
    )
    from src.review.webhook import _dispatch_review
    return _dispatch_review, queued


@pytest.mark.asyncio
async def test_the_queued_job_names_the_owner(dispatch, store):
    _dispatch_review, queued = dispatch
    store.upsert(cfg(user_id="alice@example.com"))

    await _dispatch_review("github", "acme/payments", 7, expected_workspace_id="ws-1")

    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert payload["user_id"] == "alice@example.com"
    assert payload["user_id"] != "default"
    assert payload["workspace_id"] == "ws-1"
    assert payload["pr_number"] == 7


@pytest.mark.asyncio
async def test_auto_review_switched_off_queues_nothing(dispatch, store):
    """The binding row outlives the toggle, so "the repo is known" was never
    the same question as "the owner wants this"."""
    _dispatch_review, queued = dispatch
    store.upsert(cfg(enabled=False))

    await _dispatch_review("github", "acme/payments", 7, expected_workspace_id="ws-1")

    assert queued == []


@pytest.mark.asyncio
async def test_an_unbound_repo_queues_nothing(dispatch):
    _dispatch_review, queued = dispatch

    await _dispatch_review("github", "acme/stranger", 7)

    assert queued == []


@pytest.mark.asyncio
async def test_a_repo_in_two_workspaces_queues_nothing(dispatch, store):
    _dispatch_review, queued = dispatch
    store.upsert(cfg(user_id="alice@example.com", workspace_id="ws-1"))
    store.upsert(cfg(user_id="bob@example.com", workspace_id="ws-2"))

    await _dispatch_review("github", "acme/payments", 7)

    assert queued == []


@pytest.mark.asyncio
async def test_a_delivery_signed_for_another_workspace_queues_nothing(dispatch, store):
    """Unchanged behaviour, pinned here because the lookup around it moved."""
    _dispatch_review, queued = dispatch
    store.upsert(cfg(workspace_id="ws-1"))

    await _dispatch_review("github", "acme/payments", 7, expected_workspace_id="ws-2")

    assert queued == []
