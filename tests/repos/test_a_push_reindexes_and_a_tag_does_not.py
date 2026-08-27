"""A merge changes the code every later answer comes from. Nothing noticed.

The GitHub webhook read one event: `pull_request`. Everything else — a merge
to the tracked branch, a direct push, a force-push — was answered
`{"status": "ignored"}` and the index went on holding whatever it held.

`push` is not a review trigger. It is an INDEX trigger, and giving it one
means the graph is current within seconds of a merge rather than within a day.

WHAT IS DELIBERATELY NOT HERE. The handler does not decide which branch
matters. It passes any branch push to `check_repo`, which resolves the
repository's tracked ref and compares. Two routes each deciding "the branch we
index" is how they come to disagree, and the disagreement would be invisible:
one of them would simply never fire.
"""
from __future__ import annotations

import pytest

from src.review.webhook import _extract_github_push


def _push(ref="refs/heads/main", after="a" * 40, repo="acme/widgets"):
    return {"ref": ref, "after": after,
            "repository": {"full_name": repo} if repo else {}}


def test_a_branch_push_is_ours():
    got = _extract_github_push(_push())
    assert got == {"repo": "acme/widgets", "ref": "refs/heads/main", "after": "a" * 40}


def test_a_tag_push_is_not():
    """A tag does not move the code the index was built from."""
    assert _extract_github_push(_push(ref="refs/tags/v1.2.3")) is None


def test_a_branch_deletion_is_not():
    """`after` is forty zeroes and there is nothing to index."""
    assert _extract_github_push(_push(after="0" * 40)) is None


def test_a_payload_without_a_repository_is_not():
    assert _extract_github_push(_push(repo=None)) is None


def test_an_empty_after_is_not():
    assert _extract_github_push(_push(after="")) is None


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/release/2026",
                                 "refs/heads/feature/a-b_c"])
def test_branch_names_with_slashes_and_dashes_survive(ref):
    got = _extract_github_push(_push(ref=ref))
    assert got and got["ref"] == ref


# ─── the route ───────────────────────────────────────────────────────

def test_the_handler_sends_a_push_to_the_refresh_path_not_the_review_one():
    """A merge must not book an LLM review of a pull request that closed.

    Keyed on the dispatch, not on wording: the push branch has to reach
    `_dispatch_refresh`, and the review dispatcher must not be what handles
    it.
    """
    import inspect

    from src.review import webhook

    src = inspect.getsource(webhook.build_webhook_app)
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    push_branch = body.split('x_github_event == "push"', 1)
    assert len(push_branch) == 2, "the GitHub handler does not look at push events"
    arm = push_branch[1].split('if x_github_event != "pull_request"', 1)[0]
    assert "_dispatch_refresh" in arm
    assert "_dispatch_review" not in arm, (
        "a push would book a review — an LLM call per merge, on no pull request"
    )


def test_the_refresh_dispatcher_goes_through_the_same_check():
    """One definition of "current", shared by the webhook, the sweep and the button."""
    import inspect

    from src.review.webhook import _dispatch_refresh

    src = inspect.getsource(_dispatch_refresh)
    assert "check_repo" in src, (
        "the push route decides for itself what to index; two routes deciding "
        "that separately is how they come to disagree"
    )


def test_the_refresh_dispatcher_swallows_its_own_failures():
    """It runs in a fire-and-forget task, where an exception reaches nobody."""
    import inspect

    from src.review.webhook import _dispatch_refresh

    src = inspect.getsource(_dispatch_refresh)
    assert "except Exception" in src


# ─── a burst of pushes is not a burst of git processes ───────────────

@pytest.mark.asyncio
async def test_a_second_push_for_the_same_repo_does_not_start_a_second_check():
    """A force-push arrives as several deliveries seconds apart.

    The check already running will read the branch as it is now, including
    the newest push, so a second one asks the same question of the same
    provider for the same answer.
    """
    import asyncio

    from src.review import webhook

    started, release = [], asyncio.Event()

    async def _slow(*a, **kw):
        started.append(1)
        await release.wait()
        return type("R", (), {"state": "up_to_date", "reindex_job_id": None})()

    class _Cfg:
        full_name = "acme/widgets"
        provider = "github"
        repo_slug = "github_acme-widgets"
        user_id = "u"

    class _Store:
        def workspace_for_repo(self, p, n): return "ws"
        def list_for_workspace(self, ws): return [_Cfg()]

    import src.api.auto_review as ar
    orig_store, orig_thread = ar.get_auto_review_store, asyncio.to_thread
    ar.get_auto_review_store = lambda: _Store()
    asyncio.to_thread = _slow  # type: ignore[assignment]
    try:
        first = asyncio.create_task(
            webhook._dispatch_refresh("github", "acme/widgets", expected_workspace_id="ws"))
        await asyncio.sleep(0.05)
        await webhook._dispatch_refresh("github", "acme/widgets", expected_workspace_id="ws")
        assert len(started) == 1, "the second delivery started a second check"
        release.set()
        await first
        # ...and once it is done the gate is open again.
        assert "github:acme/widgets" not in webhook._REFRESH_INFLIGHT
    finally:
        ar.get_auto_review_store = orig_store
        asyncio.to_thread = orig_thread  # type: ignore[assignment]


def test_concurrent_checks_are_bounded():
    """Fifty repositories pushed at once must not be fifty git processes."""
    from src.review import webhook

    assert webhook._REFRESH_GATE._value <= 8, (
        "the semaphore is wide enough to be no bound at all"
    )
