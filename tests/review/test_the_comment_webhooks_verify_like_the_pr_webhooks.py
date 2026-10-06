"""A comment delivery is authenticated exactly like a pull-request one, and
bound to ONE workspace before anything is recorded or answered.

Signature or token first (fail closed without a secret), dedup by delivery id,
then the repo-to-workspace binding the review dispatcher uses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.review.settings import ReviewSettings
from src.review.webhook import _dispatch_command, build_webhook_app
from tests.review.comment_support import command, event
from tests.review.test_the_comment_payload_fixtures_parse import _load


@pytest.fixture
def client():
    settings = ReviewSettings(webhook_secret="github-secret", gitlab_token="gitlab-token",
                              bitbucket_secret="bb-secret")
    with patch("src.review.webhook._dispatch_command", new_callable=AsyncMock) as dispatch, \
            patch("src.review.webhook._dispatch_review", new_callable=AsyncMock) as review:
        yield TestClient(build_webhook_app(settings)), dispatch, review


def _gh(payload, *, secret=b"github-secret", delivery="d-1", event_name="issue_comment"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig, "X-GitHub-Delivery": delivery,
                  "X-GitHub-Event": event_name}


def _bb(payload, *, secret=b"bb-secret", uuid="u-1", key="pullrequest:comment_created"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature": sig, "X-Request-UUID": uuid, "X-Event-Key": key}


def test_a_github_comment_with_a_bad_signature_is_rejected(client):
    c, dispatch, _ = client
    body, headers = _gh(_load("github_issue_comment"), secret=b"wrong")
    assert c.post("/webhook/github", content=body, headers=headers).status_code == 401
    dispatch.assert_not_called()


def test_a_bitbucket_comment_with_a_bad_signature_is_rejected(client):
    c, dispatch, _ = client
    body, headers = _bb(_load("bitbucket_comment_created_top_level"), secret=b"wrong")
    assert c.post("/webhook/bitbucket", content=body, headers=headers).status_code == 401
    dispatch.assert_not_called()


def test_a_gitlab_note_with_a_bad_token_is_rejected(client):
    c, dispatch, _ = client
    resp = c.post("/webhook/gitlab", content=json.dumps(_load("gitlab_note_diff")).encode(),
                  headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Note Hook"})
    assert resp.status_code == 401
    dispatch.assert_not_called()


def test_without_a_configured_secret_a_comment_is_refused_not_trusted(monkeypatch):
    # A developer's own .env or environment must not supply the secret.
    for name in ("REVIEW_WEBHOOK_SECRET", "REVIEW_REVIEW_WEBHOOK_SECRET", "WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    app = build_webhook_app(ReviewSettings(_env_file=None, webhook_secret=None))
    body, headers = _gh(_load("github_issue_comment"))
    resp = TestClient(app).post("/webhook/github", content=body, headers=headers)
    assert resp.status_code == 500


def test_a_signed_github_comment_naming_the_bot_is_accepted(client):
    c, dispatch, review = client
    body, headers = _gh(_load("github_issue_comment"))
    resp = c.post("/webhook/github", content=body, headers=headers)
    assert resp.status_code == 202 and resp.json()["command"] == "start-review"
    review.assert_not_called()  # a comment is never a review trigger by itself


def test_a_signed_bitbucket_comment_naming_the_bot_is_accepted(client):
    c, dispatch, _ = client
    body, headers = _bb(_load("bitbucket_comment_created_top_level"))
    assert c.post("/webhook/bitbucket", content=body, headers=headers).status_code == 202


def test_a_bitbucket_edit_is_accepted_too(client):
    c, _, _ = client
    body, headers = _bb(_load("bitbucket_comment_updated"), key="pullrequest:comment_updated")
    assert c.post("/webhook/bitbucket", content=body, headers=headers).status_code == 202


def test_a_gitlab_note_naming_the_bot_is_accepted(client):
    c, _, _ = client
    payload = _load("gitlab_note_diff")
    payload["project"]["web_url"] = "https://gitlab.com/acme/shop"
    with patch("src.review.webhook._gitlab_instance_matches", return_value=True):
        resp = c.post("/webhook/gitlab", content=json.dumps(payload).encode(),
                      headers={"X-Gitlab-Token": "gitlab-token",
                               "X-Gitlab-Event": "Note Hook"})
    assert resp.status_code == 202


def test_a_gitlab_note_from_another_instance_is_refused(client):
    c, dispatch, _ = client
    with patch("src.review.webhook._gitlab_instance_matches", return_value=False):
        resp = c.post("/webhook/gitlab",
                      content=json.dumps(_load("gitlab_note_diff")).encode(),
                      headers={"X-Gitlab-Token": "gitlab-token",
                               "X-Gitlab-Event": "Note Hook"})
    assert resp.status_code == 403
    dispatch.assert_not_called()


def test_a_redelivered_comment_is_answered_as_a_duplicate(client):
    c, _, _ = client
    body, headers = _gh(_load("github_issue_comment"))
    assert c.post("/webhook/github", content=body, headers=headers).status_code == 202
    again = c.post("/webhook/github", content=body, headers=headers)
    assert again.json()["status"] == "duplicate"


def test_a_comment_that_does_not_name_the_bot_is_ignored_before_any_work(client):
    c, dispatch, _ = client
    payload = _load("github_issue_comment")
    payload["comment"]["body"] = "looks good to me"
    body, headers = _gh(payload, delivery="d-2")
    resp = c.post("/webhook/github", content=body, headers=headers)
    assert resp.json()["status"] == "ignored"
    dispatch.assert_not_called()


def test_a_bot_authored_comment_is_ignored(client):
    c, dispatch, _ = client
    payload = _load("github_issue_comment")
    payload["comment"]["user"] = {"login": "ci[bot]", "id": 5, "type": "Bot"}
    body, headers = _gh(payload, delivery="d-3")
    assert c.post("/webhook/github", content=body, headers=headers).json()["status"] == "ignored"


def test_the_receiver_counts_the_commands_it_accepted(client):
    c, _, _ = client
    body, headers = _gh(_load("github_issue_comment"), delivery="d-4")
    c.post("/webhook/github", content=body, headers=headers)
    assert c.get("/webhook/stats").json()["commands"] == 1


# ─── the tenant binding ──────────────────────────────────────────


class _Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def config_for_repo(self, provider, repo):
        return self._cfg


def _cfg(workspace="ws-1", enabled=True):
    return SimpleNamespace(workspace_id=workspace, user_id="owner", enabled=enabled)


@pytest.fixture
def seams(monkeypatch):
    """The ledger claim and the queue, replaced by recorders."""
    seen = SimpleNamespace(accepted=[], queued=[])

    def accept(ev, cmd, *, workspace_id, user_id):
        seen.accepted.append((workspace_id, user_id))
        return {"ledger_id": "row-1"}

    monkeypatch.setattr("src.review.commands.handlers.accept", accept)
    monkeypatch.setattr("src.sync.queue.enqueue", lambda **kw: seen.queued.append(kw) or "job-1")

    def bind(cfg):
        monkeypatch.setattr("src.api.auto_review.get_auto_review_store",
                            lambda: _Store(cfg))

    seen.bind = bind
    return seen


@pytest.mark.asyncio
async def test_a_command_runs_even_when_auto_review_is_off(seams):
    seams.bind(_cfg(enabled=False))
    await _dispatch_command(event(), command(), expected_workspace_id="ws-1")
    assert seams.accepted == [("ws-1", "owner")]
    [job] = seams.queued
    assert job["kind"] == "pr_command"
    assert job["dedup_key"] == "cmd:github:acme/shop#7:c1"


@pytest.mark.asyncio
async def test_a_repo_bound_to_no_workspace_is_not_commanded(seams):
    seams.bind(None)
    await _dispatch_command(event(), command(), expected_workspace_id="ws-1")
    assert seams.accepted == [] and seams.queued == []


@pytest.mark.asyncio
async def test_a_delivery_signed_for_another_workspace_is_dropped(seams):
    seams.bind(_cfg(workspace="ws-victim"))
    await _dispatch_command(event(), command(), expected_workspace_id="ws-attacker")
    assert seams.accepted == [] and seams.queued == []


@pytest.mark.asyncio
async def test_the_legacy_route_trusts_the_binding_alone(seams):
    seams.bind(_cfg(workspace="ws-1"))
    await _dispatch_command(event(), command(), expected_workspace_id=None)
    assert seams.accepted == [("ws-1", "owner")]


@pytest.mark.asyncio
async def test_a_duplicate_claim_queues_nothing(seams, monkeypatch):
    seams.bind(_cfg())
    monkeypatch.setattr("src.review.commands.handlers.accept",
                        lambda *a, **k: None)
    await _dispatch_command(event(), command(), expected_workspace_id="ws-1")
    assert seams.queued == []


@pytest.mark.asyncio
async def test_a_broken_queue_falls_back_to_running_it_here(seams, monkeypatch):
    seams.bind(_cfg())
    ran = []

    def broken(**kw):
        raise RuntimeError("queue down")

    monkeypatch.setattr("src.sync.queue.enqueue", broken)
    monkeypatch.setattr("src.review.commands.handlers.execute", ran.append)
    await _dispatch_command(event(), command(), expected_workspace_id="ws-1")
    assert ran == [{"ledger_id": "row-1"}]


@pytest.mark.asyncio
async def test_a_failing_dispatch_never_raises_into_the_receiver(seams, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", boom)
    await _dispatch_command(event(), command(), expected_workspace_id="ws-1")
