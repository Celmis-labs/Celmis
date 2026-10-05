"""A merge request on a self-hosted GitLab is found, and found on the right one.

Two halves:

  * a reference typed by hand — the shorthand `gitlab:group/proj#7` or the
    merge-request URL copied from the browser, sub-path included — resolves
    against the workspace's configured instance;
  * a webhook delivery is accepted only from that instance: the token proves
    the sender knows the workspace secret, `project.web_url` proves which
    GitLab the event is about.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.cli import _parse_pr_ref
from src.sync.gitlab_instance import METADATA_KEY

BASE = "https://example.com/gitlab"


# ─── references ──────────────────────────────────────────────────────


@pytest.mark.parametrize("ref, expected", [
    (f"{BASE}/group/proj/-/merge_requests/7", ("gitlab", "group/proj", 7)),
    (f"{BASE}/group/sub/proj/-/merge_requests/12/diffs", ("gitlab", "group/sub/proj", 12)),
    (f"{BASE}/group/proj/-/merge_requests/3#note_9", ("gitlab", "group/proj", 3)),
    ("gitlab:group/sub/proj#4", ("gitlab", "group/sub/proj", 4)),
    ("https://gitlab.com/group/proj/-/merge_requests/5", ("gitlab", "group/proj", 5)),
])
def test_a_reference_resolves_against_the_instance(ref, expected):
    assert _parse_pr_ref(ref, gitlab_base_url=BASE) == expected


def test_the_sub_path_is_never_taken_for_a_group():
    provider, repo, _ = _parse_pr_ref(f"{BASE}/team/app/-/merge_requests/1",
                                      gitlab_base_url=BASE)
    assert repo == "team/app"


@pytest.mark.parametrize("ref", [
    "https://evil.example.com/group/proj/-/merge_requests/7",
    "https://example.com/group/proj/-/merge_requests/7",      # outside /gitlab
])
def test_a_url_on_another_host_is_not_this_instance(ref):
    with pytest.raises(ValueError):
        _parse_pr_ref(ref, gitlab_base_url=BASE)


def test_without_an_instance_a_self_hosted_url_does_not_parse():
    with pytest.raises(ValueError):
        _parse_pr_ref(f"{BASE}/group/proj/-/merge_requests/7")


def test_the_review_trigger_slug_skips_the_sub_path(monkeypatch):
    from src.api.routers import reviews

    monkeypatch.setattr(reviews, "_gitlab_base", lambda ws: BASE)
    assert reviews._parse_ref(_parse_pr_ref, f"{BASE}/g/p/-/merge_requests/2", "ws-a") \
        == ("gitlab", "g/p", 2)


# ─── the webhook receiver ────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    from src.review.settings import ReviewSettings
    from src.review.webhook import build_webhook_app

    monkeypatch.setattr(
        "src.credentials.resolve_git_credential",
        lambda provider, **kw: SimpleNamespace(
            secret="glpat-x", metadata={METADATA_KEY: BASE}),
    )
    settings = ReviewSettings(gitlab_token="gitlab-token")
    with patch("src.review.webhook._dispatch_review", new_callable=AsyncMock) as dispatch:
        yield TestClient(build_webhook_app(settings)), dispatch


def _deliver(client, web_url: str | None):
    project = {"path_with_namespace": "group/proj"}
    if web_url is not None:
        project["web_url"] = web_url
    body = json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 7, "last_commit": {"id": "abc"}},
        "project": project,
    }).encode()
    return client.post("/webhook/gitlab", content=body, headers={
        "X-Gitlab-Token": "gitlab-token", "X-Gitlab-Event": "Merge Request Hook",
        "Content-Type": "application/json"})


def test_an_event_from_the_configured_instance_is_accepted(client):
    c, dispatch = client
    resp = _deliver(c, f"{BASE}/group/proj")
    assert resp.status_code == 202, resp.text
    assert dispatch.await_count == 1


@pytest.mark.parametrize("web_url", [
    "https://gitlab.com/group/proj",                 # same path, other GitLab
    "https://evil.example.com/gitlab/group/proj",
    "https://example.com/group/proj",                # same host, outside the sub-path
])
def test_an_event_from_another_instance_is_refused(client, web_url):
    c, dispatch = client
    resp = _deliver(c, web_url)
    assert resp.status_code == 403
    assert dispatch.await_count == 0


def test_the_token_check_still_comes_first(client):
    c, dispatch = client
    resp = c.post("/webhook/gitlab", content=b"{}", headers={
        "X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook"})
    assert resp.status_code == 401
    assert dispatch.await_count == 0


def test_a_payload_without_web_url_is_not_refused(client):
    c, _ = client
    assert _deliver(c, None).status_code == 202


def test_a_workspace_without_a_gitlab_connection_is_not_blocked(monkeypatch):
    from src.review.webhook import _gitlab_instance_matches

    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **k: None)
    assert _gitlab_instance_matches("ws-a", "https://gitlab.com/g/p")
