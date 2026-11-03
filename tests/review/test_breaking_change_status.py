"""The `celmis/breaking-change` commit status, per provider, with mocked HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from src.review.models import Finding, FindingSeverity, HunkSide, PullRequest, ReviewBatch
from src.review.orchestrator import ReviewOrchestrator
from src.review.providers.base import BREAKING_CHANGE_STATUS
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider

from .test_providers import _patch_client

LINK = "https://celmis.example.com/reviews"


def _pr(provider: str, repo: str) -> PullRequest:
    return PullRequest(provider=provider, repo=repo, number=3, title="t", description="",
                       author="u", base_ref="main", base_sha="b" * 40, head_ref="f",
                       head_sha="abc123", state="open", url="https://git.example.com/pr/3")


def _recorder(status: int = 201):  # noqa: ANN202
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json={})

    return seen, httpx.MockTransport(handler)


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content) if req.content else {}


def test_github_posts_to_the_statuses_endpoint() -> None:
    p = GitHubPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    res = p.post_commit_status(_pr("github", "acme/svc"), state="failure",
                               description="2 breaking changes", target_url=LINK)
    (req,) = seen
    assert res.posted and res.error == ""
    assert req.method == "POST" and req.url.path == "/repos/acme/svc/statuses/abc123"
    assert _body(req) == {"state": "failure", "context": BREAKING_CHANGE_STATUS,
                          "description": "2 breaking changes", "target_url": LINK}


def test_gitlab_maps_failure_to_failed_and_names_the_check() -> None:
    p = GitLabPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    assert p.post_commit_status(_pr("gitlab", "acme/svc"), state="failure",
                                description="x", target_url=LINK).posted
    (req,) = seen
    assert req.method == "POST" and req.url.path.endswith("/statuses/abc123")
    q = dict(req.url.params)
    assert q["state"] == "failed" and q["name"] == BREAKING_CHANGE_STATUS
    assert q["target_url"] == LINK


def test_gitlab_success_state() -> None:
    p = GitLabPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    p.post_commit_status(_pr("gitlab", "acme/svc"), state="success", description="ok")
    assert dict(seen[0].url.params)["state"] == "success"
    assert "target_url" not in dict(seen[0].url.params)


def test_bitbucket_posts_a_build_status() -> None:
    p = BitbucketPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    assert p.post_commit_status(_pr("bitbucket", "acme/svc"), state="success",
                                description="ok", target_url=LINK).posted
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path.endswith("/repositories/acme/svc/commit/abc123/statuses/build")
    body = _body(req)
    assert body["state"] == "SUCCESSFUL" and body["name"] == BREAKING_CHANGE_STATUS
    assert body["key"] == BREAKING_CHANGE_STATUS and body["url"] == LINK


def test_bitbucket_failure_state() -> None:
    p = BitbucketPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    p.post_commit_status(_pr("bitbucket", "acme/svc"), state="failure", description="x")
    assert _body(seen[0])["state"] == "FAILED"


@pytest.mark.parametrize("cls,provider,hint", [
    (GitHubPRProvider, "github", "repo:status"),
    (GitLabPRProvider, "gitlab", "api"),
    (BitbucketPRProvider, "bitbucket", "Repositories"),
])
@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_missing_permission_is_a_warning_not_an_exception(cls, provider, hint, code, caplog):
    p = cls(token="fake")
    _, transport = _recorder(code)
    _patch_client(p, transport)
    with caplog.at_level("WARNING"):
        res = p.post_commit_status(_pr(provider, "acme/svc"), state="success", description="ok")
    assert not res.posted and res.permission_denied
    assert hint in res.error and str(code) in res.error
    assert any(r.levelname == "WARNING" and "commit_status_permission" in r.getMessage()
               for r in caplog.records)
    assert "fake" not in caplog.text and "fake" not in res.error


def test_a_server_error_or_a_network_failure_never_raises() -> None:
    p = GitHubPRProvider(token="fake")
    _, transport = _recorder(500)
    _patch_client(p, transport)
    res = p.post_commit_status(_pr("github", "a/b"), state="success", description="ok")
    assert not res.posted and not res.permission_denied and "500" in res.error

    def boom(req):  # noqa: ANN001, ANN202
        raise httpx.ConnectError("no route")

    _patch_client(p, httpx.MockTransport(boom))
    res = p.post_commit_status(_pr("github", "a/b"), state="success", description="ok")
    assert not res.posted and "ConnectError" in res.error


def test_a_pull_request_without_a_head_commit_posts_nothing() -> None:
    p = GitHubPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    pr = _pr("github", "a/b")
    pr.head_sha = ""
    assert not p.post_commit_status(pr, state="success", description="ok").posted
    assert seen == []


def test_the_description_is_cut_to_what_providers_accept() -> None:
    p = GitHubPRProvider(token="fake")
    seen, transport = _recorder()
    _patch_client(p, transport)
    p.post_commit_status(_pr("github", "a/b"), state="success", description="x" * 500)
    assert len(_body(seen[0])["description"]) <= 140


def test_an_unknown_state_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        GitHubPRProvider(token="fake").post_commit_status(
            _pr("github", "a/b"), state="pending", description="x")


# ─── the orchestrator side ───────────────────────────────────────


class _Stages:
    def __init__(self) -> None:
        self.ended: dict[str, tuple[str, str]] = {}

    def begin(self, key, name, reason="", **kw):  # noqa: ANN001, ANN003, ANN201
        pass

    def end(self, key, status, reason="", **kw):  # noqa: ANN001, ANN003, ANN201
        self.ended[key] = (status, reason)


def _batch(*rule_ids: str) -> ReviewBatch:
    findings = [Finding(file_path="a.py", line=1, side=HunkSide.RIGHT,
                        severity=FindingSeverity.WARNING, title="t", body="b",
                        agent="breaking_change", rule_id=r) for r in rule_ids]
    return ReviewBatch(pull_request=_pr("github", "acme/svc"), findings=findings, summary="S")


class _Poster:
    def __init__(self, result) -> None:  # noqa: ANN001
        self.result, self.calls = result, []

    def post_commit_status(self, pr, **kw):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(kw)
        return self.result


def _ok():  # noqa: ANN202
    from src.review.providers.base import StatusResult

    return StatusResult(True, "success")


def test_strong_consumers_set_failure() -> None:
    poster, stages = _Poster(_ok()), _Stages()
    ReviewOrchestrator._post_breaking_change_status(
        poster, _batch("bc.strong.param_renamed.f"), checked=True, stages=stages)
    (call,) = poster.calls
    assert call["state"] == "failure" and call["context"] == BREAKING_CHANGE_STATUS
    assert stages.ended["breaking_change_status"][0] == "success"


def test_weak_or_no_findings_set_success() -> None:
    for rules in ((), ("bc.weak.route_changed.x",)):
        poster = _Poster(_ok())
        ReviewOrchestrator._post_breaking_change_status(
            poster, _batch(*rules), checked=True, stages=_Stages())
        assert poster.calls[0]["state"] == "success"


def test_nothing_is_posted_when_the_check_did_not_finish() -> None:
    poster, stages = _Poster(_ok()), _Stages()
    ReviewOrchestrator._post_breaking_change_status(poster, _batch(), checked=False, stages=stages)
    assert poster.calls == [] and stages.ended["breaking_change_status"][0] == "skipped"


def test_a_missing_permission_is_reported_in_the_review_and_does_not_fail_it() -> None:
    from src.review.providers.base import StatusResult

    denied = StatusResult(False, "failure", "the token cannot write commit statuses (HTTP 403); "
                          "it needs `repo:status`", permission_denied=True)
    batch, stages = _batch("bc.strong.symbol_removed.f"), _Stages()
    ReviewOrchestrator._post_breaking_change_status(
        _Poster(denied), batch, checked=True, stages=stages)
    assert "could not be set" in batch.summary and "repo:status" in batch.summary
    assert batch.summary.startswith("S")
    assert stages.ended["breaking_change_status"][0] == "failed"


def test_a_provider_that_raises_is_contained() -> None:
    class Boom:
        def post_commit_status(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("x")

    stages = _Stages()
    ReviewOrchestrator._post_breaking_change_status(Boom(), _batch(), checked=True, stages=stages)
    assert stages.ended["breaking_change_status"][0] == "failed"


def test_a_provider_without_status_support_is_skipped() -> None:
    stages = _Stages()
    ReviewOrchestrator._post_breaking_change_status(object(), _batch(), checked=True, stages=stages)
    assert stages.ended["breaking_change_status"][0] == "skipped"

