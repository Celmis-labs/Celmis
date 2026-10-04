"""The webhook's early draft skip obeys `run_on_drafts` too.

GitHub "opened" for a draft and every GitLab draft MR event were dropped at
the door, before a review — and so before the orchestrator's gate — could
ask the policy. They now ask the same rows (`run_on_drafts_for_repo`): off,
or unknown, is the old skip; on, the delivery is dispatched.

The resolver is exercised for real against SQLite and a real AutoReview
store; the HTTP half patches only the dispatcher.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from src.review.settings import ReviewSettings
from src.review.webhook import build_webhook_app


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


# ─── the resolver ────────────────────────────────────────────────────


@pytest.fixture
def bound(tmp_path, monkeypatch):
    """acme/api bound to ws-1, an empty policy table and defaults table."""
    import src.api.auto_review as ar_mod
    from src.api.auto_review import RepoConfig
    from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

    store = ar_mod.AutoReviewStore(tmp_path / "ar.db")
    monkeypatch.setattr(ar_mod, "_default_store", store)
    store.upsert(RepoConfig(user_id="u1", repo_slug="github_acme-api", provider="github",
                            full_name="acme/api", url="https://github.com/acme/api",
                            workspace_id="ws-1", enabled=True))
    url = f"sqlite:///{tmp_path}/celmis.db"
    engine = sa.create_engine(url)
    RepoReviewPolicy.__table__.create(engine)
    WorkspaceReviewDefaults.__table__.create(engine)
    monkeypatch.setenv("DATABASE_URL", url)

    def _set(*, repo=None, workspace=None):
        with engine.begin() as conn:
            conn.execute(RepoReviewPolicy.__table__.delete())
            conn.execute(WorkspaceReviewDefaults.__table__.delete())
            if repo is not None:
                conn.execute(RepoReviewPolicy.__table__.insert().values(
                    repo_slug="github_acme-api", workspace_id="ws-1", enabled=True,
                    prompt_template="", folder_rules=[], agent_prompt_overrides={},
                    mcp_sources=[], run_on_drafts=repo))
            if workspace is not None:
                conn.execute(WorkspaceReviewDefaults.__table__.insert().values(
                    workspace_id="ws-1", run_on_drafts=workspace))

    yield _set
    engine.dispose()


@pytest.mark.parametrize("repo, workspace, expected", [
    (None, None, False),
    (None, True, True),
    (False, True, False),
    (True, None, True),
    (True, False, True),
])
def test_the_resolver_reads_repo_then_workspace_then_builtin(bound, repo, workspace,
                                                             expected):
    from src.review.review_defaults import run_on_drafts_for_repo

    bound(repo=repo, workspace=workspace)
    assert run_on_drafts_for_repo("github", "acme/api") is expected


def test_an_unbound_repo_or_an_unreadable_database_is_the_old_skip(bound, monkeypatch):
    from src.review.review_defaults import run_on_drafts_for_repo

    bound(workspace=True)
    assert run_on_drafts_for_repo("github", "someone/else") is False
    monkeypatch.setenv("DATABASE_URL", "sqlite:////nonexistent/dir/x.db")
    assert run_on_drafts_for_repo("github", "acme/api") is False


# ─── the receivers ───────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    with patch("src.review.webhook._dispatch_review", new_callable=AsyncMock) as dispatch:
        app = build_webhook_app(ReviewSettings(
            webhook_secret="github-secret", gitlab_token="gitlab-token"))
        yield TestClient(app), dispatch


def _github(client, delivery: str):
    body = json.dumps({
        "action": "opened",
        "pull_request": {"number": 3, "draft": True, "head": {"sha": "abc"}},
        "repository": {"full_name": "acme/api"},
    }).encode()
    sig = "sha256=" + hmac.new(b"github-secret", body, hashlib.sha256).hexdigest()
    return client.post("/webhook/github", content=body, headers={
        "X-Hub-Signature-256": sig, "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "pull_request"})


def _gitlab(client, sha: str):
    body = json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 9, "draft": True,
                              "last_commit": {"id": sha}},
        "project": {"path_with_namespace": "acme/api"},
    }).encode()
    return client.post("/webhook/gitlab", content=body, headers={
        "X-Gitlab-Token": "gitlab-token", "X-Gitlab-Event": "Merge Request Hook"})


@pytest.mark.parametrize("on", [False, True])
def test_a_github_draft_is_dispatched_only_when_drafts_are_reviewed(client, monkeypatch,
                                                                   on):
    import src.review.review_defaults as rd

    asked: list = []
    monkeypatch.setattr(rd, "run_on_drafts_for_repo",
                        lambda provider, repo: asked.append((provider, repo)) or on)
    http, dispatch = client
    r = _github(http, f"draft-{on}")
    assert asked == [("github", "acme/api")]
    if on:
        assert r.status_code == 202 and r.json()["status"] == "accepted"
        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs.get("skip_reason") is None
    else:
        assert r.json() == {"status": "skipped", "reason": "draft PR"}
        _only_the_skip_is_recorded(dispatch)


def _only_the_skip_is_recorded(dispatch) -> None:
    """A skipped draft still reaches the dispatcher — only to be RECORDED as
    a skipped run (src/review/dispatch.py:record_gate_skip), never reviewed."""
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs["skip_reason"] == "draft"


@pytest.mark.parametrize("on", [False, True])
def test_a_gitlab_draft_is_dispatched_only_when_drafts_are_reviewed(client, monkeypatch,
                                                                   on):
    import src.review.review_defaults as rd

    monkeypatch.setattr(rd, "run_on_drafts_for_repo", lambda provider, repo: on)
    http, dispatch = client
    r = _gitlab(http, f"sha-{on}")
    if on:
        assert r.status_code == 202, r.text
        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs.get("skip_reason") is None
    else:
        assert r.json() == {"status": "skipped", "reason": "draft MR"}
        _only_the_skip_is_recorded(dispatch)
