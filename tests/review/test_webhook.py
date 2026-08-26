"""Tests для webhook receiver — HMAC verification + dedup + dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.review.settings import ReviewSettings
from src.review.webhook import (
    InMemoryDedup,
    _verify_bitbucket_signature,
    _verify_github_signature,
    _verify_gitlab_token,
    build_webhook_app,
)

# ─── HMAC verification ──────────────────────────────────────────


class TestGitHubSignature:
    def test_valid_signature(self) -> None:
        secret = "my-secret"
        body = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        assert _verify_github_signature(body, sig, secret) is True

    def test_invalid_signature(self) -> None:
        assert _verify_github_signature(b"data", "sha256=wrong", "secret") is False

    def test_missing_signature(self) -> None:
        assert _verify_github_signature(b"data", None, "secret") is False

    def test_wrong_format(self) -> None:
        assert _verify_github_signature(b"data", "md5=foo", "secret") is False


class TestGitLabToken:
    def test_valid_token(self) -> None:
        assert _verify_gitlab_token("my-token", "my-token") is True

    def test_invalid_token(self) -> None:
        assert _verify_gitlab_token("wrong", "expected") is False

    def test_missing_token(self) -> None:
        assert _verify_gitlab_token(None, "expected") is False


class TestBitbucketSignature:
    def test_valid_signature(self) -> None:
        secret = "bb-secret"
        body = b'{"event": "push"}'
        sig = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        assert _verify_bitbucket_signature(body, sig, secret) is True


# ─── Dedup ──────────────────────────────────────────────────────


class TestInMemoryDedup:
    def test_first_request_not_duplicate(self) -> None:
        dedup = InMemoryDedup()
        assert dedup.is_duplicate("delivery-1") is False

    def test_repeat_request_is_duplicate(self) -> None:
        dedup = InMemoryDedup()
        assert dedup.is_duplicate("delivery-1") is False
        assert dedup.is_duplicate("delivery-1") is True

    def test_different_ids_not_duplicates(self) -> None:
        dedup = InMemoryDedup()
        assert dedup.is_duplicate("a") is False
        assert dedup.is_duplicate("b") is False

    def test_capacity_eviction(self) -> None:
        dedup = InMemoryDedup(maxsize=3, ttl_seconds=3600)
        for i in range(5):
            dedup.is_duplicate(f"d-{i}")
        # First 2 evicted (capacity=3)
        assert dedup.is_duplicate("d-0") is False  # re-registered
        assert dedup.is_duplicate("d-4") is True   # still у cache

    def test_ttl_expiration(self) -> None:
        dedup = InMemoryDedup(maxsize=10, ttl_seconds=0)  # immediate expire
        dedup.is_duplicate("d-1")
        time.sleep(0.01)
        # Past TTL — re-registered, not duplicate
        assert dedup.is_duplicate("d-1") is False


# ─── Webhook receiver — GitHub ─────────────────────────────────


@pytest.fixture
def settings_with_secrets() -> ReviewSettings:
    return ReviewSettings(
        webhook_secret="github-secret",
        gitlab_token="gitlab-token",
        bitbucket_secret="bb-secret",
    )


@pytest.fixture
def app_with_dispatch_mock(settings_with_secrets):
    """App де _dispatch_review patched — щоб не trigger real review."""
    with patch(
        "src.review.webhook._dispatch_review",
        new_callable=AsyncMock,
    ) as dispatch_mock:
        app = build_webhook_app(settings_with_secrets)
        client = TestClient(app)
        yield client, dispatch_mock


class TestGitHubWebhook:
    def test_healthz(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_invalid_signature_rejected(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        resp = client.post(
            "/webhook/github",
            content=b'{"action": "opened"}',
            headers={
                "X-Hub-Signature-256": "sha256=bad",
                "X-GitHub-Delivery": "abc123",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert resp.status_code == 401

    def test_pr_opened_dispatches(self, app_with_dispatch_mock) -> None:
        client, dispatch = app_with_dispatch_mock
        body = json.dumps({
            "action": "opened",
            "pull_request": {
                "number": 42,
                "draft": False,
                "head": {"sha": "abc123"},
            },
            "repository": {"full_name": "octo/repo"},
        }).encode()
        sig = "sha256=" + hmac.new(
            b"github-secret", body, hashlib.sha256,
        ).hexdigest()

        resp = client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["repo"] == "octo/repo"
        assert data["number"] == 42

    def test_duplicate_delivery_returns_200(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = json.dumps({
            "action": "opened",
            "pull_request": {"number": 1, "draft": False, "head": {"sha": "x"}},
            "repository": {"full_name": "o/r"},
        }).encode()
        sig = "sha256=" + hmac.new(
            b"github-secret", body, hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "dup-1",
            "X-GitHub-Event": "pull_request",
        }
        # First call
        r1 = client.post("/webhook/github", content=body, headers=headers)
        assert r1.status_code == 202

        # Second call — same delivery — duplicate
        r2 = client.post("/webhook/github", content=body, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"

    def test_draft_pr_skipped(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = json.dumps({
            "action": "opened",
            "pull_request": {"number": 1, "draft": True, "head": {"sha": "x"}},
            "repository": {"full_name": "o/r"},
        }).encode()
        sig = "sha256=" + hmac.new(
            b"github-secret", body, hashlib.sha256,
        ).hexdigest()
        resp = client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": "draft-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "draft PR"

    def test_non_pr_event_ignored(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = b'{}'
        sig = "sha256=" + hmac.new(
            b"github-secret", body, hashlib.sha256,
        ).hexdigest()
        resp = client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "push-1",
            },
        )
        assert resp.json()["status"] == "ignored"


# ─── Webhook receiver — GitLab ─────────────────────────────────


class TestGitLabWebhook:
    def test_invalid_token_rejected(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        resp = client.post(
            "/webhook/gitlab",
            content=b"{}",
            headers={
                "X-Gitlab-Token": "wrong",
                "X-Gitlab-Event": "Merge Request Hook",
            },
        )
        assert resp.status_code == 401

    def test_mr_open_dispatches(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = json.dumps({
            "object_kind": "merge_request",
            "object_attributes": {
                "action": "open",
                "iid": 7,
                "last_commit": {"id": "abc"},
                "draft": False,
            },
            "project": {"path_with_namespace": "group/proj"},
        }).encode()

        resp = client.post(
            "/webhook/gitlab",
            content=body,
            headers={
                "X-Gitlab-Token": "gitlab-token",
                "X-Gitlab-Event": "Merge Request Hook",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["repo"] == "group/proj"


# ─── Webhook receiver — Bitbucket ──────────────────────────────


class TestBitbucketWebhook:
    def test_pr_created_dispatches(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = json.dumps({
            "pullrequest": {
                "id": 5,
                "source": {"commit": {"hash": "head-sha"}},
            },
            "repository": {"full_name": "ws/repo"},
        }).encode()
        sig = "sha256=" + hmac.new(
            b"bb-secret", body, hashlib.sha256,
        ).hexdigest()

        resp = client.post(
            "/webhook/bitbucket",
            content=body,
            headers={
                "X-Hub-Signature": sig,
                "X-Event-Key": "pullrequest:created",
                "X-Request-UUID": "uuid-1",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["repo"] == "ws/repo"
        assert resp.json()["number"] == 5

    def test_invalid_signature_rejected(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        resp = client.post(
            "/webhook/bitbucket",
            content=b"{}",
            headers={
                "X-Hub-Signature": "sha256=bad",
                "X-Event-Key": "pullrequest:created",
            },
        )
        assert resp.status_code == 401


# ─── Stats endpoint ────────────────────────────────────────────


class TestStatsEndpoint:
    def test_stats_initially_zero(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        resp = client.get("/webhook/stats")
        data = resp.json()
        assert data["received"] == 0

    def test_stats_increment_on_call(self, app_with_dispatch_mock) -> None:
        client, _ = app_with_dispatch_mock
        body = json.dumps({
            "action": "opened",
            "pull_request": {"number": 1, "draft": False, "head": {"sha": "x"}},
            "repository": {"full_name": "o/r"},
        }).encode()
        sig = "sha256=" + hmac.new(
            b"github-secret", body, hashlib.sha256,
        ).hexdigest()
        client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": "stat-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        resp = client.get("/webhook/stats")
        data = resp.json()
        assert data["received"] >= 1
        assert data["dispatched"] >= 1
