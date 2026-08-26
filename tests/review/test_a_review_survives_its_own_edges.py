"""Four ways a review lost work it had already done.

Each was reproduced by running the pipeline, and each has the same shape as the
generation bugs found alongside them: something went wrong, nothing raised, and
what came out looked like a normal result.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


# ─── the verifier's two different "no" answers ───────────────────────


def test_an_empty_verdict_drops_everything():
    """`{"keep": []}` is the verifier saying none of these findings are real —
    a valid and useful answer. It was treated as a parse failure, so every
    finding it had just rejected was kept and posted: the agent whose whole job
    is removing false positives could not remove all of them."""
    from src.review.agents.verifier import VerifierAgent

    assert VerifierAgent._parse_keep_indices('{"keep": []}', total=3) == []


def test_an_unreadable_reply_keeps_everything():
    """The other half of the distinction. An unreadable verifier must not
    silently delete a real finding, so this one fails OPEN — and it has to be
    tellable from the verdict above, which is why None is not []."""
    from src.review.agents.verifier import VerifierAgent

    for junk in ("the model rambled", '{"keep": [0,', "", "{}"):
        assert VerifierAgent._parse_keep_indices(junk, total=3) is None


def test_a_partial_verdict_survives():
    from src.review.agents.verifier import VerifierAgent

    assert VerifierAgent._parse_keep_indices('{"keep": [1]}', total=3) == [1]


def test_the_caller_distinguishes_them():
    source = (SRC / "review" / "agents" / "verifier.py").read_text(encoding="utf-8")
    assert "if keep_indices is None:" in source, (
        "an empty verdict and an unreadable reply are the same branch again"
    )


# ─── one bad anchor used to cost every finding ───────────────────────


def test_a_rejected_anchor_costs_the_anchor_not_the_findings(monkeypatch):
    """GitHub validates a review as one object: a single comment on a line
    outside the diff returns 422 and takes every other finding with it. That is
    not an edge case — an agent saying "this function should also do X" points
    at the function, not at the changed line.

    Driven against a fake transport rather than grepped out of the source: the
    assertion that matters is that the review still lands and the findings are
    still readable, which no substring of github.py can show.
    """
    import json

    import httpx

    from src.review.models import (
        Finding,
        PullRequest,
        ReviewBatch,
        ReviewVerdict,
    )
    from src.review.providers import github as github_module
    from src.review.providers.github import GitHubPRProvider
    from src.review.settings import ReviewSettings

    posted: list[dict] = []
    issue_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/user":
            return httpx.Response(200, json={"login": "bot"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/reviews"):
            payload = json.loads(request.content)
            if payload.get("comments"):
                return httpx.Response(422, json={"message": "line must be part of the diff"})
            posted.append(payload)
            return httpx.Response(200, json={"id": 7, "html_url": "https://gh/pr/1"})
        if request.method == "POST" and path.endswith("/comments"):
            issue_bodies.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404, json={"message": "unrouted"})

    cfg = ReviewSettings(comment_marker="<!-- edges-under-test -->")
    monkeypatch.setattr(github_module, "get_review_settings", lambda: cfg)

    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="u", base_ref="main", base_sha="b", head_ref="f", head_sha="h",
        state="open",
    )
    batch = ReviewBatch(
        pull_request=pr,
        verdict=ReviewVerdict.COMMENT,
        findings=[
            Finding(file_path="src/a.py", line=9, title="first", body="because"),
            Finding(file_path="src/b.py", line=4, title="second", body="because"),
        ],
    )
    provider = GitHubPRProvider(token="fake")
    provider._http.close()
    provider._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    provider.post_review(batch)
    provider.close()

    assert len(posted) == 1, "the review was lost with its anchors"
    assert posted[0]["comments"] == []
    summary = "\n".join(issue_bodies)
    for finding in ("src/a.py", "first", "src/b.py", "second"):
        assert finding in summary, f"{finding} was dropped, not moved"


def test_the_fallback_keeps_the_position_as_text():
    """GitHub will not render it as a marker; a reader can still navigate to
    it. The position is information either way."""
    from src.review.providers.github import _findings_as_body

    body = _findings_as_body([
        {"path": "src/auth.py", "line": 42, "body": "Check the tenant."},
    ])
    assert "src/auth.py" in body and "42" in body and "Check the tenant." in body


# ─── a queued review left no trace ───────────────────────────────────


def test_a_queued_review_is_recorded():
    """Two paths into a review, and only the one from the UI wrote a row. A
    webhook or poller review posted its comments to the pull request and left
    nothing behind — which is why /api/reviews/history showed zero runs on an
    installation where auto review had been configured."""
    handlers = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert "record_completed_review" in handlers
    assert "store.insert" in handlers


def test_a_failed_queued_review_is_not_left_running():
    """A row stuck at "running" is indistinguishable from a worker that died."""
    handlers = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert 'status="error"' in handlers


def test_the_row_shape_is_written_once():
    """The two call sites had drifted: the UI copy grew evidence_kind and
    cross_repo_callers while the other had nothing at all. Sharing it is the
    fix for the drift, not just for the absence."""
    from src.api.review_runs import record_completed_review

    assert callable(record_completed_review)
    runs = (SRC / "api" / "review_runs.py").read_text(encoding="utf-8")
    assert "evidence_kind" in runs and "cross_repo_callers" in runs


# ─── the agent engine no longer re-runs itself ───────────────────────


def test_a_failed_agent_session_is_not_retried_wholesale():
    """`except RuntimeError` was there to detect "already inside an event
    loop". It also caught every failure the session raises — and once empty
    documents started raising RuntimeError, a failed run re-ran the entire
    agent to fail the same way twice, at twice the cost."""
    source = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")
    assert "asyncio.get_running_loop()" in source, (
        "the loop is detected by catching a failure again"
    )
    body = source[source.index("def generate("):source.index("async def _run(")]
    assert "except RuntimeError:" in body
    # …but only around the loop probe, not around the session.
    probe = body[body.index("asyncio.get_running_loop()") - 60:]
    assert "except RuntimeError:" in probe[:200]


def test_generation_settings_are_not_dropped():
    """None meant "the provider's default", not the installation's: batch
    documentation ran at whatever temperature the vendor picked that month, and
    with no output ceiling, so a truncated document arrived with no signal."""
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert "gemini_temperature_generation" in engines
    assert "gemini_max_output_tokens" in engines
