"""Webhooks: a merge or decline queues one refresh of that PR; everything else only marks it stale."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.productivity import sync as ps
from src.review import webhook
from src.review.settings import ReviewSettings
from src.review.webhook import build_webhook_app
from tests.productivity.support import (
    NOW,
    PROVIDER,
    REPO,
    WS,
    FakeProvider,
    at,
    enable,
    make_engine,
    pr,
    prs_in,
    sync,
)


def _event(engine, event, number=7, **kw):
    queued = []
    result = ps.on_lifecycle_event(
        WS, PROVIDER, REPO, number, event, engine=engine,
        enqueue=lambda **q: queued.append(q) or "job", **kw)
    return result, queued


def test_a_merge_queues_one_refresh_of_that_pr_with_a_dedup_key() -> None:
    engine = make_engine()
    enable(engine)
    result, queued = _event(engine, "merged", user_id="u1")
    assert result == "queued"
    (job,) = queued
    assert job["dedup_key"] == f"prodpr:{WS}:{REPO}:7"
    assert job["payload"]["number"] == 7 and job["payload"]["user_id"] == "u1"
    assert job["workspace_id"] == WS and job["delay_seconds"] > 0


@pytest.mark.parametrize("event", ["merged", "closed", "fulfilled", "rejected"])
def test_every_ending_of_a_pr_refreshes_it(event: str) -> None:
    engine = make_engine()
    enable(engine)
    assert _event(engine, event)[0] == "queued"


@pytest.mark.parametrize("event", ["created", "updated", "approved", "unapproved", "reopened"])
def test_anything_else_marks_a_known_pr_stale_and_calls_no_provider(event: str) -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(7, merged=at(-3))]))
    assert prs_in(engine)[7].detail_state == "full"
    result, queued = _event(engine, event)
    assert result == "stale" and queued == []
    assert prs_in(engine)[7].detail_state == "partial"


def test_a_stale_pr_is_detailed_again_by_the_next_run() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(7, merged=at(-3))]))
    _event(engine, "approved")
    next_run = FakeProvider([pr(7, merged=at(-3))])
    sync(engine, next_run, now=at(0, hours=1))
    assert next_run.detail_calls == [7]


def test_an_unknown_pr_is_left_for_the_next_list_to_find() -> None:
    engine = make_engine()
    enable(engine)
    assert _event(engine, "updated", number=99)[0] == "unknown"


def test_a_repository_that_did_not_opt_in_does_nothing() -> None:
    engine = make_engine()
    result, queued = _event(engine, "merged")
    assert result == "disabled" and queued == []


def test_a_broken_queue_never_breaks_the_webhook() -> None:
    engine = make_engine()
    enable(engine)

    def boom(**kw):
        raise RuntimeError("queue down")

    assert ps.on_lifecycle_event(WS, PROVIDER, REPO, 7, "merged", engine=engine, enqueue=boom) == "unknown"


def test_the_refresh_job_reads_the_pr_again_and_rederives_deployments() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"])
    provider = FakeProvider([pr(7, target="master", merged=at(-1), merge_sha="m7")])
    result = ps.refresh_pull_request(WS, PROVIDER, REPO, 7, provider_factory=lambda cfg: provider,
                                     engine=engine, now=NOW)
    assert result.status == "ok" and result.deployments == 1
    assert prs_in(engine)[7].deploy_link == "direct"
    assert provider.detail_calls == [7]


# ── the receivers ─────────────────────────────────────────────────────

@pytest.fixture
def client():
    settings = ReviewSettings(github_webhook_secret="gh", gitlab_webhook_token="gl", bitbucket_secret="bb-secret")
    with patch("src.review.webhook._dispatch_review", new_callable=AsyncMock) as review, \
            patch("src.review.webhook._dispatch_pr_touch", new_callable=AsyncMock) as touch, \
            patch("src.review.webhook._dispatch_pr_state", new_callable=AsyncMock) as state:
        yield TestClient(build_webhook_app(settings)), review, touch, state


def _post(client, event, uuid, **pull):
    body = json.dumps({"pullrequest": {"id": 5, "source": {"commit": {"hash": "h"}}, **pull},
                       "repository": {"full_name": "ws/repo"}}).encode()
    sig = "sha256=" + hmac.new(b"bb-secret", body, hashlib.sha256).hexdigest()
    return client.post("/webhook/bitbucket", content=body, headers={
        "X-Hub-Signature": sig, "X-Event-Key": event, "X-Request-UUID": uuid,
        "Content-Type": "application/json"})


def test_an_approval_is_recorded_for_productivity_and_starts_no_review(client) -> None:
    http, review, touch, _ = client
    resp = _post(http, "pullrequest:approved", "u-approved")
    assert resp.status_code == 200 and resp.json()["status"] == "recorded"
    touch.assert_awaited_once()
    assert touch.await_args.args[0] == {"provider": "bitbucket", "repo": "ws/repo", "number": 5, "event": "approved"}
    review.assert_not_awaited()


def test_an_unapproval_is_recorded_too(client) -> None:
    http, review, touch, _ = client
    assert _post(http, "pullrequest:unapproved", "u-un").json()["status"] == "recorded"
    touch.assert_awaited_once()


def test_an_update_is_both_a_stale_mark_and_a_review_trigger(client) -> None:
    http, review, touch, _ = client
    resp = _post(http, "pullrequest:updated", "u-upd")
    assert resp.status_code == 202
    touch.assert_awaited_once()
    review.assert_awaited_once()


def test_a_merge_goes_through_the_state_path_not_the_touch_path(client) -> None:
    http, review, touch, state = client
    resp = _post(http, "pullrequest:fulfilled", "u-ful")
    assert resp.json()["status"] == "recorded" and resp.json()["state"] == "merged"
    state.assert_awaited_once()
    touch.assert_not_awaited()


def test_an_unsigned_approval_is_refused_before_anything_is_read(client) -> None:
    http, _, touch, _ = client
    resp = http.post("/webhook/bitbucket", content=b"{}", headers={
        "X-Hub-Signature": "sha256=bad", "X-Event-Key": "pullrequest:approved"})
    assert resp.status_code == 401
    touch.assert_not_awaited()


def test_a_merge_webhook_reaches_the_productivity_hook_under_the_repos_own_workspace(monkeypatch) -> None:
    import asyncio

    cfg = SimpleNamespace(workspace_id=WS, full_name=REPO, user_id="u1", repo_slug="acme-app")
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store",
                        lambda: SimpleNamespace(config_for_repo=lambda p, r: cfg))
    monkeypatch.setattr("src.review.issues.record_pr_state", lambda **kw: True)
    seen = []
    monkeypatch.setattr("src.productivity.sync.on_lifecycle_event", lambda *a, **kw: seen.append((a, kw)) or "queued")
    info = {"provider": "bitbucket", "repo": REPO, "number": 7, "state": "merged", "title": "t"}
    asyncio.run(webhook._dispatch_pr_state(info, expected_workspace_id=WS))
    assert seen == [((WS, "bitbucket", REPO, 7, "merged"), {"user_id": "u1", "repo_slug": "acme-app"})]


def test_a_delivery_signed_for_another_workspace_reaches_nothing(monkeypatch) -> None:
    import asyncio

    cfg = SimpleNamespace(workspace_id="ws-other", full_name=REPO, user_id="u1", repo_slug="x")
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store",
                        lambda: SimpleNamespace(config_for_repo=lambda p, r: cfg))
    seen = []
    monkeypatch.setattr("src.productivity.sync.on_lifecycle_event", lambda *a, **kw: seen.append(a))
    info = {"provider": "bitbucket", "repo": REPO, "number": 7, "event": "approved"}
    asyncio.run(webhook._dispatch_pr_touch(info, expected_workspace_id=WS))
    assert seen == []


def test_bitbucket_is_asked_for_approvals_when_the_hook_is_installed() -> None:
    from src.review.webhook_install import EVENTS

    assert {"pullrequest:approved", "pullrequest:unapproved"} <= set(EVENTS["bitbucket"])
