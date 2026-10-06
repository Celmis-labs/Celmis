"""A Bitbucket push moves the index the way a GitHub push does.

The GitHub handler already treats `push` as an INDEX trigger. Bitbucket
delivered `repo:push` too, but the hook was never subscribed to it and the
handler answered "ignored": a repository on Bitbucket was re-indexed only by
the daily sweep, so the code a developer's assistant read could be a day old.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.review.settings import ReviewSettings
from src.review.webhook import _extract_bitbucket_push, build_webhook_app
from src.review.webhook_install import EVENTS


def _push(change_new=None, repo="acme/widgets", changes=None):
    if changes is None:
        changes = [{"new": change_new if change_new is not None else {
            "type": "branch", "name": "develop", "target": {"hash": "b" * 40}}}]
    return {"repository": {"full_name": repo} if repo else {},
            "push": {"changes": changes}}


def test_a_branch_update_is_ours():
    assert _extract_bitbucket_push(_push()) == {
        "repo": "acme/widgets", "ref": "refs/heads/develop", "after": "b" * 40}


def test_a_tag_push_is_not():
    tag = {"type": "tag", "name": "v1", "target": {"hash": "c" * 40}}
    assert _extract_bitbucket_push(_push(tag)) is None


def test_a_branch_deletion_is_not():
    assert _extract_bitbucket_push(_push(changes=[{"new": None, "old": {"type": "branch"}}])) is None


def test_a_payload_without_a_repository_is_not():
    assert _extract_bitbucket_push(_push(repo=None)) is None


def test_the_first_branch_change_wins_when_a_tag_comes_first():
    tag = {"new": {"type": "tag", "name": "v1", "target": {"hash": "c" * 40}}}
    branch = {"new": {"type": "branch", "name": "main", "target": {"hash": "d" * 40}}}
    got = _extract_bitbucket_push(_push(changes=[tag, branch]))
    assert got and got["ref"] == "refs/heads/main"


def test_a_change_without_a_commit_hash_is_not_ours():
    assert _extract_bitbucket_push(_push({"type": "branch", "name": "x", "target": {}})) is None


def test_a_hook_installed_for_bitbucket_subscribes_to_pushes_as_well_as_pull_requests():
    assert "repo:push" in EVENTS["bitbucket"]
    assert any(e.startswith("pullrequest:") for e in EVENTS["bitbucket"])


# ─── the route ───────────────────────────────────────────────────────

SECRET = "bb-secret"


def _post(client, payload: dict, event: str):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/bitbucket", content=body,
        headers={"X-Hub-Signature": sig, "X-Event-Key": event,
                 "Content-Type": "application/json"})


@pytest.fixture
def client():
    settings = ReviewSettings(webhook_secret="g", gitlab_token="t", bitbucket_secret=SECRET)
    with patch("src.review.webhook._dispatch_review", new_callable=AsyncMock) as review, \
            patch("src.review.webhook._dispatch_refresh", new_callable=AsyncMock) as refresh:
        yield TestClient(build_webhook_app(settings)), review, refresh


def test_a_push_to_a_branch_reaches_the_refresh_path_and_never_the_review_one(client):
    c, review, refresh = client
    resp = _post(c, _push(), "repo:push")
    assert resp.status_code == 202 and resp.json()["ref"] == "refs/heads/develop"
    refresh.assert_awaited_once()
    assert refresh.await_args.args[:2] == ("bitbucket", "acme/widgets")
    review.assert_not_awaited()


def test_a_tag_push_is_answered_ignored_and_starts_nothing(client):
    c, review, refresh = client
    tag = {"type": "tag", "name": "v1", "target": {"hash": "c" * 40}}
    resp = _post(c, _push(tag), "repo:push")
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    refresh.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_repository_with_auto_review_switched_off_is_still_refreshed():
    """Review being off says nothing about whether the code should be current."""
    from src.review import webhook

    class _Cfg:
        full_name = "acme/widgets"
        provider = "bitbucket"
        repo_slug = "bitbucket_acme-widgets"
        user_id = "u"
        enabled = False

    class _Store:
        def workspace_for_repo(self, p, n):
            return "ws"

        def list_for_workspace(self, ws):
            return [_Cfg()]

    calls: list[str] = []

    def fake_check(slug, **kw):
        calls.append(slug)
        return type("R", (), {"state": "behind", "reindex_job_id": "j1"})()

    import src.api.auto_review as ar
    import src.repos.freshness as fr

    with patch.object(ar, "get_auto_review_store", lambda: _Store()), \
            patch.object(fr, "check_repo", fake_check):
        await webhook._dispatch_refresh("bitbucket", "acme/widgets", expected_workspace_id="ws")
    assert calls == ["bitbucket_acme-widgets"]
