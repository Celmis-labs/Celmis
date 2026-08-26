"""Three behaviours this wave shipped and left with no test on them.

Found by mutation: each mutation below was applied to the shipped bytes, the
whole of tests/review (and tests/api for the third) stayed green, and the
production code was restored byte-identical afterwards.

  * `_anchorable_ranges` registers a LEFT span under BOTH names a rename
    gives a file. Deleting `hunk.old_file_path` from that set left the suite
    green: the existing LEFT test builds its hunk with `old_file_path` equal
    to `file_path`, so the rename half of the decision was never exercised.
    It matters because a finding on a renamed file names whichever path the
    agent was shown, and an anchor GitHub refuses costs the whole review —
    one refused batch already cost four findings on this bench.
  * the architect's second reasoning form carries one operative sentence —
    do not bend such a finding into form (a), and do not DISCARD it. Every
    other line of the block was pinned; that one was not, and it is the line
    the two recovered goldens depend on. Replacing it with "Such a finding is
    admissible." left 47 tests green.
  * `record_completed_review` writes `cost_source or None`, so an empty
    string cannot read back as "recorded, total unknowable". Dropping the
    `or None` left 17 tests green: the existing cases pass a real source or
    None, never "".
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.api.review_runs import ReviewRun, ReviewRunStore, record_completed_review
from src.review.agents.defect import DefectAgent
from src.review.models import (
    Finding,
    Hunk,
    HunkSide,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.providers import github as github_module
from src.review.providers.github import GitHubPRProvider
from src.review.settings import AgentLLMSettings, ReviewSettings

MARKER = "<!-- unpinned-under-test -->"


# ─── 1. a renamed file's LEFT anchor ─────────────────────────────────


def _renamed_pr() -> PullRequest:
    """One hunk on a file this PR renames, with a real old-side span."""
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="alice", base_ref="main", base_sha="a", head_ref="feat",
        head_sha="h", state="open",
        hunks=[Hunk(
            file_path="src/new_name.py", old_file_path="src/old_name.py",
            old_start=200, old_count=10, new_start=1, new_count=0,
            content="@@ -200,10 +1,0 @@\n", is_renamed=True,
        )],
    )


def test_a_renamed_files_left_anchor_snaps_under_the_name_the_agent_saw(monkeypatch):
    """The agent was shown the old path; the anchor must still be placed.

    GitHub validates the review as one object, so an anchor it will not take
    refuses every other finding in the batch with it. Registering the old
    side's span only under the NEW path leaves a finding that names the old
    one with no span to snap to — it goes out at its original line, the POST
    422s, and the whole review falls back to unanchored text.
    """
    anchored: list[dict] = []
    refusals: list[str] = []
    covered = {("src/old_name.py", "LEFT", n) for n in range(200, 210)}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/user":
            return httpx.Response(200, json={"login": "bot"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/reviews"):
            payload = json.loads(request.content)
            for comment in payload.get("comments", []):
                if (comment["path"], comment["side"], comment["line"]) not in covered:
                    refusals.append(f'{comment["path"]}:{comment["line"]}')
                    return httpx.Response(
                        422, json={"message": "line must be part of the diff"},
                    )
            anchored.extend(payload.get("comments", []))
            return httpx.Response(200, json={"id": 7, "html_url": "https://gh/pr/1"})
        if request.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404, json={"message": "unrouted"})

    monkeypatch.setattr(
        github_module, "get_review_settings",
        lambda: ReviewSettings(comment_marker=MARKER, replace_on_synchronize=False),
    )
    pr = _renamed_pr()
    batch = ReviewBatch(
        pull_request=pr, verdict=ReviewVerdict.COMMENT,
        findings=[Finding(
            file_path="src/old_name.py", line=400, title="gone", body="b",
            side=HunkSide.LEFT,
        )],
    )
    provider = GitHubPRProvider(token="fake")
    provider._http.close()
    provider._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    provider.post_review(batch)
    provider.close()

    assert refusals == [], f"the anchor was refused: {refusals}"
    assert [(c["path"], c["side"], c["line"]) for c in anchored] == [
        ("src/old_name.py", "LEFT", 209),
    ]


# ─── 2. the sentence the second reasoning form turns on ──────────────


class _Probe:
    """An LLMClient double. Named `probe`, never after a real agent — the
    base class resolves a canonical prompt by agent name."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        from src.llm.client import LLMResult

        self.calls.append(kwargs)
        return LLMResult(
            text="[]", input_tokens=1, output_tokens=1, model="double",
            finish_reason="stop", cost_usd=0.0, cost_source="litellm_estimate",
            provider="gemini",
        )


@pytest.fixture
def architect_prompt(monkeypatch) -> str:
    """The system instruction the model is actually sent, default install."""
    import src.api.routers.agents as agents_router
    import src.api.routers.llm as llm_router
    from src.review.agents.base import AgentContext

    monkeypatch.setattr(
        agents_router, "get_effective_system_prompt", lambda *a, **k: None,
    )
    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda *a, **k: {})

    client = _Probe()
    agent = DefectAgent()
    context = AgentContext(
        pull_request=PullRequest(
            provider="github", repo="o/r", number=1, title="t", description="d",
            author="alice", base_ref="main", base_sha="a", head_ref="f",
            head_sha="h", state="open",
            hunks=[Hunk(
                file_path="src/foo.py", old_file_path="src/foo.py",
                old_start=1, old_count=1, new_start=1, new_count=2,
                content="@@ -1 +1,2 @@\n-a\n+b\n+c\n",
            )],
        ),
        llm_client=client,
        agent_llm={agent.name: AgentLLMSettings(model="double", max_output_tokens=64)},
    )
    result = agent.review(context)
    assert result.error is None, result.error
    return " ".join(client.calls[0]["system_instruction"].split())


def test_the_second_form_says_not_to_discard_the_finding_that_does_not_fit(
    architect_prompt: str,
) -> None:
    """The block's operative sentence, and the only one nothing pinned.

    Naming a second admissible shape is not the same instruction as "do not
    throw away a defect because the first shape will not hold it". Both of
    the goldens this change exists for — a `postMessage` handed a full
    referer where the contract takes an origin, and an `Authenticate` that
    now returns ErrDeviceLimitReached — were written by the architect in the
    run before the reasoning field existed and by nobody in the run after it.
    A prompt that offers two forms and never says "do not discard" leaves the
    behaviour that lost them intact.
    """
    assert "do not discard it for failing to fit" in architect_prompt
    assert "Do not bend such a finding into form (a)" in architect_prompt


# ─── 3. an empty cost_source is not a recorded cost ──────────────────


def _batch_with(cost_source: str | None) -> ReviewBatch:
    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="t", description="d",
        author="alice", base_ref="main", base_sha="a", head_ref="f",
        head_sha="h", state="open",
    )
    batch = ReviewBatch(pull_request=pr)
    batch.cost_usd = None
    batch.cost_source = cost_source
    batch.tokens_in = 0
    batch.tokens_out = 0
    batch.verdict = batch.compute_verdict()
    batch.mark_complete()
    return batch


@pytest.mark.parametrize("empty", ["", None])
def test_a_cost_source_that_says_nothing_is_not_read_back_as_recorded(
    tmp_path: Path, empty,
) -> None:
    """`cost_source` is the ONLY thing separating "recorded, but the total is
    unknowable" from "nobody wrote a cost down": both leave `cost_usd` NULL.
    An engine that leaves the field empty rather than unset must land on the
    second reading, or a run that recorded nothing reads as a priced one.
    """
    store = ReviewRunStore(tmp_path / "review_runs.db")
    store.insert(ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7"))
    record_completed_review(
        SimpleNamespace(batch=_batch_with(empty), provider_response={}),
        run_id="r", store=store,
    )
    row = store.get("r")
    assert row.cost_usd is None
    assert row.cost_source is None, (
        "an empty cost_source read back as a recorded cost with an unknown total"
    )
