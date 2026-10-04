"""The pull request hears a review begin, and hears how it ended.

Until this change a review posted nothing at all until it was finished —
minutes of silence after a push — and the early skips (draft, too large, no
reviewable hunks) and crashes posted nothing ever. Now:

  * "🔄 Celmis is reviewing this PR…" goes up as soon as the skip gates are
    passed, in the persistent marked comment;
  * the final summary is written INTO that comment — same id, never a second
    one — in a Kodus-style shape: summary prose, a per-file walkthrough,
    findings by severity and by source, scope folded away;
  * a skip rewrites an existing comment as "⏭️ Skipped: …", a crash or a
    refused post rewrites this run's placeholder as "❌ Review failed: …"
    with no provider text in it;
  * the summary prose comes from ONE cheap LLM call, booked as
    `review_summary`, which may fail without failing anything.

The fake git server is the stateful one from the re-run tests, so "one
comment, same id" is a fact about what is left on the pull request.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    HunkSide,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.orchestrator import ReviewOrchestrator
from src.review.pr_summary import (
    SUMMARY_OPERATION,
    SUMMARY_TIMEOUT_SECONDS,
    build_diff_digest,
    generate_pr_summary,
    parse_reply,
)
from src.review.providers.base import (
    STATUS_IN_PROGRESS_MARK,
    WALKTHROUGH_MAX_FILES,
    _format_started_comment,
    _format_summary,
)
from src.review.providers.github import GitHubPRProvider
from tests.review.test_a_rerun_does_not_double_the_comments import (
    MARKER,
    _FakeGitHub,
    _install_settings,
    _patch_client,
)

# ─── fixtures ───────────────────────────────────────────────────────


def _hunk(path: str, added: int = 2, body: str = "+x = 1\n") -> Hunk:
    return Hunk(
        file_path=path, old_file_path=path, old_start=1, old_count=1,
        new_start=1, new_count=1 + added,
        content=f"@@ -1,1 +1,{1 + added} @@\n line\n" + body * added,
    )


def _pr(*, files: int = 2, draft: bool = False, url: str = "https://github.com/o/r/pull/1",
        hunks: list[Hunk] | None = None) -> PullRequest:
    hs = hunks if hunks is not None else [_hunk(f"src/mod{i}.py") for i in range(files)]
    return PullRequest(
        provider="github", repo="o/r", number=1, title="Add caching",
        description="Adds a cache.", author="alice",
        base_ref="main", base_sha="base1", head_ref="feat",
        head_sha="abcdef1234567890", state="open", is_draft=draft, url=url,
        hunks=hs, raw_diff="diff --git a/x b/x\n" if hs else "",
    )


def _finding(path: str = "src/mod0.py", line: int = 2,
             severity: FindingSeverity = FindingSeverity.ERROR,
             agent: str = "defect", title: str = "Cache never expires") -> Finding:
    return Finding(file_path=path, line=line, severity=severity, title=title,
                   body="because", agent=agent, rule_id=f"{agent}.rule",
                   reasoning="seen in the diff")


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.input_tokens = 120
        self.output_tokens = 40
        self.cost_usd = 0.0004


class _Client:
    """An LLMClient double: records every call, answers from a script."""

    def __init__(self, reply: str | None = None, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.reply = reply
        self.raises = raises

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return _Reply(self.reply or "")


GOOD_REPLY = json.dumps({
    "overview": "Adds a read-through cache in front of the module loaders. "
                "Repeated imports now hit memory instead of disk.",
    "files": [
        {"path": "src/mod0.py", "summary": "Adds the cache | wrapper"},
        {"path": "src/mod1.py", "summary": "Uses the cache in load()"},
        {"path": "src/invented.py", "summary": "not part of the PR"},
    ],
})


class _Agent(ReviewAgent):
    """Returns canned findings and records what the PR looked like meanwhile."""

    def __init__(self, name: str = "defect", findings=None, probe=None) -> None:
        self.name = name
        self._findings = list(findings or [])
        self._probe = probe

    def review(self, context: AgentContext) -> AgentRunResult:
        if self._probe is not None:
            self._probe()
        return AgentRunResult(agent=self.name, findings=list(self._findings))


class _PassThroughVerifier:
    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider(GitHubPRProvider):
    """The real GitHub provider over the fake server, with a canned PR."""

    def __init__(self, fake: _FakeGitHub, pr: PullRequest) -> None:
        super().__init__(token="fake")
        _patch_client(self, httpx.MockTransport(fake))
        self._pr = pr

    def fetch_pull_request(self, repo, pr_number):
        return self._pr


@pytest.fixture
def env(monkeypatch):
    """No database, no graph, no notifications, no workspace config."""
    import src.api.routers.llm as llm_router
    import src.notifications as notifications
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    import src.review.reviewer_assignment as ra

    _install_settings(monkeypatch)
    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})
    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))
    monkeypatch.setattr(ra, "assign_reviewers_by_ownership", lambda **kw: None)
    monkeypatch.setattr(notifications, "notify", lambda **kw: None)


def _orch(monkeypatch, *, agents, client=None, policy=None) -> ReviewOrchestrator:
    orch = ReviewOrchestrator(agents=agents, verifier=_PassThroughVerifier())
    monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
    monkeypatch.setattr(
        orch, "_build_context",
        lambda pr, **kw: AgentContext(pull_request=pr, llm_client=client),
    )
    return orch


def _run(orch, provider, **kw):
    return orch.review("github", "o/r", 1, provider=provider, **kw)


# ─── end to end through the orchestrator ────────────────────────────


def test_the_placeholder_is_up_while_the_agents_run_and_becomes_the_summary(
    env, monkeypatch,
):
    fake = _FakeGitHub()
    seen_during_review: list[str] = []
    agent = _Agent(findings=[_finding()],
                   probe=lambda: seen_during_review.extend(fake.bodies(fake.issue)))
    client = _Client(GOOD_REPLY)
    orch = _orch(monkeypatch, agents=[agent], client=client)

    result = _run(orch, _Provider(fake, _pr()))

    [placeholder] = seen_during_review
    assert MARKER in placeholder and STATUS_IN_PROGRESS_MARK in placeholder
    assert "🔄 Celmis is reviewing this PR…" in placeholder
    assert "`abcdef1`" in placeholder, "the head sha (short) is missing"
    assert "`defect`" in placeholder
    assert "Files to review: **2**" in placeholder
    assert "will replace this comment" in placeholder

    assert len(fake.issue) == 1, "the summary did not replace the placeholder"
    summary_id = fake.issue[0]["id"]
    assert result.provider_response["summary_comment_id"] == summary_id
    body = fake.issue[0]["body"]
    assert STATUS_IN_PROGRESS_MARK not in body
    assert "### Summary" in body and "read-through cache" in body
    assert "### Changes walkthrough" in body
    assert "| `src/mod0.py` | +2 / -0 | Adds the cache \\| wrapper |" in body
    assert "invented.py" not in body
    assert "**By severity:**" in body and "**By category:** Bug: **1**" in body
    assert ("[`src/mod0.py:2`](https://github.com/o/r/blob/abcdef1234567890/"
            "src/mod0.py#L2)") in body
    assert "<details>" in body

    # One summary call, booked under its own operation, short and unretried.
    [call] = client.calls
    assert call["operation"] == SUMMARY_OPERATION == "review_summary"
    assert call["num_retries"] == 0
    assert call["timeout"] == SUMMARY_TIMEOUT_SECONDS <= 60
    assert "src/mod0.py: +2 / -0" in call["code_context"]
    assert "English" in call["prompt"]
    # Its tokens are the review's tokens.
    assert result.batch.tokens_in >= 120


def test_a_failed_summary_call_degrades_to_a_summary_without_prose(env, monkeypatch):
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=[_Agent(findings=[_finding()])],
                 client=_Client(raises=TimeoutError("slow")))

    result = _run(orch, _Provider(fake, _pr()))

    assert result.posted is True
    [comment] = fake.issue
    assert "### Summary" not in comment["body"]
    assert "### Changes walkthrough" not in comment["body"]
    assert "### Findings" in comment["body"]
    assert "Cache never expires" in comment["body"]


def test_an_unreadable_summary_reply_degrades_the_same_way(env, monkeypatch):
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=[_Agent()], client=_Client("I cannot do JSON"))
    _run(orch, _Provider(fake, _pr()))
    [comment] = fake.issue
    assert "### Summary" not in comment["body"]
    assert "_No issues detected._" in comment["body"]


@pytest.mark.parametrize("kw", [
    pytest.param({"dry_run": True}, id="dry-run"),
    pytest.param({"post_comments": False}, id="post-comments-off"),
])
def test_a_run_that_does_not_post_posts_nothing_and_calls_no_model(env, monkeypatch, kw):
    fake = _FakeGitHub()
    client = _Client(GOOD_REPLY)
    orch = _orch(monkeypatch, agents=[_Agent(findings=[_finding()])], client=client)

    _run(orch, _Provider(fake, _pr()), **kw)

    assert fake.issue == [] and fake.inline == [] and fake.reviews == []
    assert fake.list_requests == 0
    assert client.calls == [], "the summary was paid for with nobody to read it"


def test_summary_disabled_keeps_the_compact_comment(env, monkeypatch):
    fake = _FakeGitHub()
    client = _Client(GOOD_REPLY)
    orch = _orch(monkeypatch, agents=[_Agent(findings=[_finding()])], client=client,
                 policy={"enabled": True, "target_branches": [],
                         "summary_enabled": False})

    _run(orch, _Provider(fake, _pr()))

    [comment] = fake.issue
    assert "### Scope" in comment["body"]
    assert "<details>" not in comment["body"]
    assert "### Summary" not in comment["body"]
    assert client.calls == []


def test_the_started_comment_can_be_switched_off(env, monkeypatch):
    fake = _FakeGitHub()
    seen: list[int] = []
    agent = _Agent(probe=lambda: seen.append(len(fake.issue)))
    orch = _orch(monkeypatch, agents=[agent], client=_Client(GOOD_REPLY),
                 policy={"enabled": True, "target_branches": [],
                         "started_comment_enabled": False})

    _run(orch, _Provider(fake, _pr()))

    assert seen == [0], "a placeholder went up although the policy said no"
    assert len(fake.issue) == 1, "the final summary must still be posted"


def test_language_and_instructions_reach_the_summary_prompt(env, monkeypatch):
    fake = _FakeGitHub()
    client = _Client(GOOD_REPLY)
    orch = _orch(monkeypatch, agents=[_Agent()], client=client,
                 policy={"enabled": True, "target_branches": [],
                         "review_language": "uk",
                         "summary_instructions": "Mention migrations first."})
    _run(orch, _Provider(fake, _pr()))
    [call] = client.calls
    assert "Ukrainian" in call["prompt"]
    assert "Mention migrations first." in call["prompt"]


def test_a_rerun_replaces_the_previous_summary_with_the_placeholder_then_the_summary(
    env, monkeypatch,
):
    fake = _FakeGitHub()
    previous = fake.add_issue(f"{MARKER}\n## old summary")
    seen: list[str] = []
    agent = _Agent(probe=lambda: seen.extend(fake.bodies(fake.issue)))
    orch = _orch(monkeypatch, agents=[agent], client=_Client(GOOD_REPLY))

    _run(orch, _Provider(fake, _pr()))

    assert len(seen) == 1 and STATUS_IN_PROGRESS_MARK in seen[0]
    assert [c["id"] for c in fake.issue] == [previous]
    assert STATUS_IN_PROGRESS_MARK not in fake.issue[0]["body"]


# ─── skips and failures finalize the comment ────────────────────────


def test_a_skip_finalizes_a_stale_placeholder(env, monkeypatch):
    """A killed run left "reviewing…" behind; the next (skipped) run ends it."""
    fake = _FakeGitHub()
    previous = fake.add_issue(f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n## 🔄 reviewing…")
    orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY))

    result = _run(orch, _Provider(fake, _pr(draft=True)))

    assert result.batch.verdict == ReviewVerdict.SKIPPED
    [comment] = fake.issue
    assert comment["id"] == previous
    assert "⏭️ Skipped: the pull request is a draft" in comment["body"]
    assert STATUS_IN_PROGRESS_MARK not in comment["body"]
    assert MARKER in comment["body"]


@pytest.mark.parametrize("pr_kw", [
    pytest.param({"draft": True}, id="draft"),
    pytest.param({"hunks": []}, id="no-hunks"),
])
def test_a_skip_leaves_a_finished_summary_alone(env, monkeypatch, pr_kw):
    """A PR reviewed yesterday and turned into a draft today keeps yesterday's
    review: a skip says nothing about the code, so it must not erase what a
    finished review said about it."""
    fake = _FakeGitHub()
    finished = f"{MARKER}\n## 🤖 Code Review for PR #1\n\n✅ **APPROVED**"
    previous = fake.add_issue(finished)
    # `status_feedback` off: the skip is silent, as it was before 2.3.0. With
    # it on the finished summary is still left alone and a separate note is
    # added — see test_the_pr_says_what_it_does.py.
    orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY),
                 policy={"enabled": True, "target_branches": [],
                         "status_feedback": False})

    result = _run(orch, _Provider(fake, _pr(**pr_kw)))

    assert result.batch.verdict == ReviewVerdict.SKIPPED
    assert [c["id"] for c in fake.issue] == [previous]
    assert fake.issue[0]["body"] == finished


def test_a_lost_github_summary_makes_the_run_partial(env, monkeypatch):
    """Parity with Bitbucket: the review posted but the summary comment did
    not, and the run record says so (post_error, PARTIAL)."""
    from src.api.review_runs import completion_status, post_failure

    fake = _FakeGitHub()

    def _refuse_issue_comments(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/issues/" in request.url.path:
            return httpx.Response(502, json={"message": "bad gateway"})
        return fake(request)

    provider = _Provider(fake, _pr())
    _patch_client(provider, httpx.MockTransport(_refuse_issue_comments))
    orch = _orch(monkeypatch, agents=[_Agent(findings=[_finding()])],
                 client=_Client(GOOD_REPLY))

    result = _run(orch, provider)

    assert len(fake.reviews) == 1, "the review itself still went up"
    assert fake.issue == []
    assert "502" in result.provider_response["error"]
    assert post_failure(result) == result.provider_response["error"]
    assert completion_status(result.batch, post_failure(result)) == "partial"


def test_a_skip_on_a_quiet_pr_starts_no_thread(env, monkeypatch):
    """With `status_feedback` off. On (the 2.3.0 default) a quiet PR gets one
    brief note — test_the_pr_says_what_it_does.py."""
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=[_Agent()],
                 policy={"enabled": True, "target_branches": [],
                         "status_feedback": False})
    _run(orch, _Provider(fake, _pr(hunks=[])))
    assert fake.issue == []


def test_a_crash_after_the_placeholder_says_failed_without_the_message(env, monkeypatch):
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY))

    def _boom(*a, **kw):
        raise RuntimeError("token=ghp_supersecret leaked in a message")

    monkeypatch.setattr(orch, "_run_agents_parallel", _boom)
    with pytest.raises(RuntimeError):
        _run(orch, _Provider(fake, _pr()))

    [comment] = fake.issue
    assert "❌ Review failed:" in comment["body"]
    assert "RuntimeError" in comment["body"]
    assert "supersecret" not in comment["body"]
    assert STATUS_IN_PROGRESS_MARK not in comment["body"]


def test_a_refused_post_finalizes_the_placeholder(env, monkeypatch):
    fake = _FakeGitHub()

    def _refuse_review(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/reviews"):
            return httpx.Response(500, json={"message": "secret body"})
        return fake(request)

    provider = _Provider(fake, _pr())
    _patch_client(provider, httpx.MockTransport(_refuse_review))
    orch = _orch(monkeypatch, agents=[_Agent(findings=[_finding()])],
                 client=_Client(GOOD_REPLY))

    result = _run(orch, provider)

    assert "error" in result.provider_response
    [comment] = fake.issue
    assert "❌ Review failed: the review could not be posted" in comment["body"]
    assert "secret body" not in comment["body"]


def test_a_crash_before_any_placeholder_touches_nothing(env, monkeypatch):
    fake = _FakeGitHub()
    previous = fake.add_issue(f"{MARKER}\n## a good summary from yesterday")
    orch = _orch(monkeypatch, agents=[_Agent()])

    def _boom(*a, **kw):
        raise RuntimeError("graph down")

    monkeypatch.setattr(orch, "_build_context", _boom)
    with pytest.raises(RuntimeError):
        _run(orch, _Provider(fake, _pr()))
    assert [c["id"] for c in fake.issue] == [previous]
    assert "good summary from yesterday" in fake.issue[0]["body"]


def test_a_provider_that_cannot_post_status_comments_still_reviews(env, monkeypatch):
    """A test double or a provider without the method keeps working."""
    pr = _pr()
    posted: list[ReviewBatch] = []

    class _Bare:
        def fetch_pull_request(self, repo, number):
            return pr

        def post_review(self, batch, dry_run=False):
            posted.append(batch)
            return {}

    orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY))
    result = orch.review("github", "o/r", 1, provider=_Bare())
    assert result.posted is True and len(posted) == 1
    assert posted[0].pr_overview.startswith("Adds a read-through cache")


# ─── rendering ──────────────────────────────────────────────────────


def _batch(**kw) -> ReviewBatch:
    batch = ReviewBatch(pull_request=_pr(), verdict=ReviewVerdict.COMMENT,
                        agents_run=["defect"], **kw)
    batch.elapsed_seconds = 12.5
    return batch


def test_the_compact_form_is_the_default_for_a_hand_built_batch():
    body = _format_summary(_batch(findings=[_finding()]), MARKER)
    assert "### Scope" in body and "### Findings" in body
    assert "<details>" not in body and "**By category:**" not in body


def test_the_rich_form_without_prose_omits_both_sections():
    body = _format_summary(_batch(findings=[_finding()], rich_summary=True), MARKER)
    assert "### Summary" not in body
    assert "### Changes walkthrough" not in body
    assert "### Findings" in body and "**Top findings:**" in body
    assert "<summary>Scope &amp; performance</summary>" in body
    assert "Analysis time: **12.5s**" in body


def test_the_walkthrough_caps_at_thirty_files():
    pr = _pr(files=WALKTHROUGH_MAX_FILES + 5)
    batch = ReviewBatch(pull_request=pr, rich_summary=True, agents_run=["defect"],
                        walkthrough={p: f"changes {p}" for p in pr.changed_files})
    body = _format_summary(batch, MARKER)
    rows = [line for line in body.splitlines() if line.startswith("| `src/mod")]
    assert len(rows) == WALKTHROUGH_MAX_FILES
    assert "| _+5 more files_ | | |" in body


def test_findings_by_severity_and_source_with_the_threshold_note():
    findings = [
        _finding(severity=FindingSeverity.CRITICAL, agent="security", title="SQLi"),
        _finding(severity=FindingSeverity.WARNING, agent="defect", title="Off by one"),
        _finding(severity=FindingSeverity.INFO, agent="defect", title="Naming"),
    ]
    batch = _batch(findings=findings, rich_summary=True,
                   comment_min_severity="warning")
    body = _format_summary(batch, MARKER)
    assert "🔴 Critical: **1**" in body and "💡 Info: **1**" in body
    assert "**By category:** Bug: **2** · Security: **1**" in body
    assert "1. 🔴 **SQLi**" in body
    assert "Naming" not in body.split("**Top findings:**")[1].split("_")[0]
    assert "1 below the comment threshold" in body


def test_a_deleted_line_gets_no_link_into_the_head_commit():
    f = _finding(line=5)
    f.side = HunkSide.LEFT
    body = _format_summary(_batch(findings=[f], rich_summary=True), MARKER)
    assert "`src/mod0.py:5`" in body
    assert "blob/" not in body


def test_a_failed_run_says_so_first():
    batch = ReviewBatch(pull_request=_pr(), rich_summary=True,
                        agents_failed=["defect", "security"],
                        agent_errors={"defect": "the provider is unavailable",
                                      "security": "the provider is unavailable"})
    batch.verdict = batch.compute_verdict()
    body = _format_summary(batch, MARKER)
    assert "### ❌ Review failed: the provider is unavailable" in body
    assert "_No issues detected._" not in body


def test_the_started_comment_names_what_will_run():
    body = _format_started_comment(_pr(files=3), agents=["defect", "security"],
                                   started_at="2026-10-04 12:03 UTC")
    assert "`abcdef1`" in body
    assert "`defect`, `security`" in body
    assert "Files to review: **3**" in body
    assert "Started: 2026-10-04 12:03 UTC" in body


# ─── the summary call itself ────────────────────────────────────────


def test_the_reply_is_sanitised_before_it_reaches_the_comment():
    reply = "```json\n" + json.dumps({
        "overview": f"Pings @octocat {MARKER} <img src=x> ![t](http://e/x.png) "
                    "[click](http://evil)",
        "files": [{"path": "a.py", "summary": "line\nbreak"}, {"path": "b.py"}],
    }) + "\n```"
    overview, walkthrough = parse_reply(reply, ["a.py", "b.py"])
    assert "@octocat" not in overview and "@​octocat" in overview
    assert "<!--" not in overview and "<img" not in overview
    assert "http://" not in overview
    assert walkthrough == {"a.py": "line break"}


def test_the_digest_keeps_every_file_name_within_its_budget():
    pr = _pr(hunks=[_hunk(f"f{i}.py", added=50, body="+" + "y" * 200 + "\n")
                    for i in range(40)])
    digest = build_diff_digest(pr, budget_chars=5_000)
    for i in range(40):
        assert f"- f{i}.py: +50 / -0" in digest
    assert "were left out to stay within budget" in digest
    assert len(digest) < 5_000 + 4_000  # the list itself plus one capped file


def test_no_client_and_a_raising_client_both_come_back_empty():
    pr = _pr()
    assert generate_pr_summary(pr, llm_client=None).error
    res = generate_pr_summary(pr, llm_client=_Client(raises=ValueError("key=sk-123")))
    assert res.overview == "" and res.walkthrough == {}
    assert res.error == "ValueError"
