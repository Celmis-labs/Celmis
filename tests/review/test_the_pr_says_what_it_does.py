"""What a review does to a pull request beyond commenting (2.3.0, Kodus parity).

Six repository settings, each off or conservative by default, each honoured
the same way by GitHub, GitLab and Bitbucket:

  * approve_when_clean — a COMPLETE review with nothing to post approves, and
    a later run that is not clean takes the approval back;
  * request_changes_on_critical — a critical finding blocks (GitHub event,
    Bitbucket request-changes; GitLab has none, so it withdraws its approval
    and says so), and the block is lifted when the next run has none;
  * committable_suggestions — an exact replacement becomes a one-click block
    (GitHub ```suggestion, GitLab ```suggestion:-0+N), never on Bitbucket;
  * summary_target="description" — the overview and walkthrough go into the
    PR description between markers, under the existing-description and
    new-commit modes, and the comment stops repeating them;
  * status_feedback — a skip with no placeholder leaves one brief note, and a
    re-run rewrites it instead of adding another;
  * message_started / message_finished_header — templates that cannot raise.

The behaviour change underneath all of it: GitHub's review event is COMMENT
unless one of the first two settings asks for more. Until 2.3.0 it followed
the verdict, so every clean review approved on its own.

The fakes extend the stateful servers of the re-run tests, so "one note",
"approval withdrawn" and "description rewritten" are facts about what is left
on the pull request, not counts of requests.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest

from src.review.markers import reveal
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    HunkSide,
    PRActions,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
    parse_suggested_code,
    parse_suggested_end_line,
)
from src.review.orchestrator import _pr_actions
from src.review.pr_actions import (
    APPROVE,
    REQUEST_CHANGES,
    SUMMARY_END,
    SUMMARY_START,
    compose_description,
    render_template,
    review_decision,
    split_description,
)
from src.review.providers.base import (
    STATUS_FEEDBACK_MARK,
    STATUS_IN_PROGRESS_MARK,
    _format_finding_body,
    _format_started_comment,
    _format_summary,
)
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.test_a_rerun_does_not_double_the_comments import (
    BOT,
    HUMAN,
    MARKER,
    _FakeBitbucket,
    _FakeGitHub,
    _FakeGitLab,
    _install_settings,
    _patch_client,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    GOOD_REPLY,
    _Agent,
    _Client,
    _orch,
    _Reply,
)


@pytest.fixture
def settings(monkeypatch):
    return _install_settings(monkeypatch)


@pytest.fixture
def env(monkeypatch):
    """No database, no graph, no notifications, no workspace config — the
    same isolation as the lifecycle tests' fixture of this name."""
    import src.api.routers.llm as llm_router
    import src.notifications as notifications
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    import src.review.reviewer_assignment as ra
    from src.review.agents.base import AgentRunResult

    _install_settings(monkeypatch)
    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})
    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))
    monkeypatch.setattr(ra, "assign_reviewers_by_ownership", lambda **kw: None)
    monkeypatch.setattr(notifications, "notify", lambda **kw: None)


# ─── fixtures ───────────────────────────────────────────────────────

#: new-file lines 10..14 of src/a.py: a, B, C, c, d (B and C added).
HUNK = Hunk(
    file_path="src/a.py", old_file_path="src/a.py",
    old_start=10, old_count=4, new_start=10, new_count=5,
    content="@@ -10,4 +10,5 @@\n a\n-b\n+B\n+C\n c\n d\n",
)


def _pr(provider: str, repo: str, number: int, *, hunks=None, description: str = "d",
        url: str = "") -> PullRequest:
    return PullRequest(
        provider=provider, repo=repo, number=number, title="Add caching",
        description=description, author="alice", base_ref="main", base_sha="base1",
        head_ref="feat", head_sha="abcdef1234567890", state="open", url=url,
        hunks=list(hunks if hunks is not None else [HUNK]),
    )


def _finding(severity=FindingSeverity.WARNING, *, line: int = 11, **kw) -> Finding:
    base = dict(file_path="src/a.py", line=line, severity=severity, title="t",
                body="because", agent="defect", rule_id="defect.r",
                reasoning="seen")
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


def _batch(pr: PullRequest, findings=(), *, actions: PRActions | None = None,
           agents_run=("defect",), agents_failed=()) -> ReviewBatch:
    batch = ReviewBatch(pull_request=pr, findings=list(findings),
                        agents_run=list(agents_run), agents_failed=list(agents_failed))
    batch.verdict = batch.compute_verdict()
    batch.mark_complete()
    if actions is not None:
        batch.pr_actions = actions
    return batch


# ─── the decision itself ────────────────────────────────────────────


class TestTheDecision:
    def test_nothing_is_decided_by_default(self) -> None:
        batch = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.CRITICAL)])
        assert review_decision(batch) is None
        assert review_decision(_batch(_pr("github", "o/r", 1))) is None

    def test_a_clean_complete_review_approves(self) -> None:
        batch = _batch(_pr("github", "o/r", 1),
                       actions=PRActions(approve_when_clean=True))
        assert review_decision(batch) == APPROVE

    def test_a_postable_finding_is_not_clean(self) -> None:
        batch = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.INFO)],
                       actions=PRActions(approve_when_clean=True))
        assert review_decision(batch) is None

    def test_a_finding_below_the_threshold_still_approves(self) -> None:
        batch = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.INFO)],
                       actions=PRActions(approve_when_clean=True))
        batch.comment_min_severity = "error"
        assert review_decision(batch) == APPROVE

    @pytest.mark.parametrize("failed,ran", [
        pytest.param(("security",), ("defect",), id="partial"),
        pytest.param(("defect",), (), id="failed"),
        pytest.param((), (), id="skipped"),
    ])
    def test_only_a_complete_review_approves(self, failed, ran) -> None:
        batch = _batch(_pr("github", "o/r", 1), agents_run=ran, agents_failed=failed,
                       actions=PRActions(approve_when_clean=True))
        assert review_decision(batch) is None

    def test_a_critical_requests_changes_and_never_approves_too(self) -> None:
        both = PRActions(approve_when_clean=True, request_changes_on_critical=True)
        batch = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.CRITICAL)],
                       actions=both)
        batch.comment_min_severity = "critical"
        assert review_decision(batch) == REQUEST_CHANGES

    def test_a_partial_review_still_requests_changes(self) -> None:
        batch = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.CRITICAL)],
                       agents_failed=("contract",),
                       actions=PRActions(request_changes_on_critical=True))
        assert review_decision(batch) == REQUEST_CHANGES

    def test_policy_words_outside_the_vocabulary_fall_back(self) -> None:
        actions = _pr_actions({"summary_target": "wiki", "summary_on_new_commits": None,
                               "summary_existing_description": "COMPLEMENT",
                               "approve_when_clean": 1, "message_started": 7})
        assert actions.summary_target == "comment"
        assert actions.summary_on_new_commits == "replace"
        assert actions.summary_existing_description == "complement"
        assert actions.approve_when_clean is True
        assert actions.message_started is None
        # Nothing configured: the built-ins. The closing comment is the one
        # setting whose built-in differs from the dataclass default (a
        # hand-built batch stays classic); the commands guide is on by default
        # now that the commands it lists exist.
        assert _pr_actions(None) == PRActions(
            completed_comment="completed", commands_guide_enabled=True)
        assert _pr_actions({"completed_comment": "fancy"}).completed_comment == "completed"

    def test_the_guide_leaves_out_the_commands_of_a_feature_switched_off(self) -> None:
        off = _pr_actions({"chat_enabled": False, "memories_enabled": False,
                           "task_context_enabled": False}).guide_commands_off
        assert set(off) == {"chat", "remember", "business-logic"}
        assert _pr_actions({"chat_enabled": True}).guide_commands_off == ()


# ─── GitHub ─────────────────────────────────────────────────────────

_STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
          "COMMENT": "COMMENTED"}


class _GitHub(_FakeGitHub):
    """+ review objects with a state, dismissals, and the PR body."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.review_objects: list[dict] = []
        self.dismissed: list[int] = []
        self.pr_body = "Author's words."
        self.pr_patches: list[str] = []

    def add_review(self, state: str, *, author: str | None = None) -> int:
        rid = next(self._ids)
        self.review_objects.append({
            "id": rid, "node_id": f"PRR_{rid}", "state": state,
            "body": f"{MARKER}\nverdict", "user": {"login": author or self.viewer},
        })
        return rid

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if re.fullmatch(r"/repos/o/r/pulls/1/reviews", path):
            if method == "GET":
                return httpx.Response(200, json=self.review_objects)
            payload = json.loads(request.content)
            for c in payload.get("comments") or []:
                self.add_inline(c["body"])
            self.reviews.append(payload)
            rid = self.add_review(_STATE[payload["event"]])
            self.review_objects[-1]["body"] = payload["body"]
            return httpx.Response(200, json={"id": rid})
        dismiss = re.fullmatch(r"/repos/o/r/pulls/1/reviews/(\d+)/dismissals", path)
        if dismiss and method == "PUT":
            assert json.loads(request.content)["event"] == "DISMISS"
            rid = int(dismiss.group(1))
            self.dismissed.append(rid)
            for r in self.review_objects:
                if r["id"] == rid:
                    r["state"] = "DISMISSED"
            return httpx.Response(200, json={"id": rid})
        if path == "/repos/o/r/pulls/1" and method == "GET":
            return httpx.Response(200, json={"body": self.pr_body, "number": 1})
        if path == "/repos/o/r/pulls/1" and method == "PATCH":
            self.pr_body = json.loads(request.content)["body"]
            self.pr_patches.append(self.pr_body)
            return httpx.Response(200, json={"body": self.pr_body})
        return super().__call__(request)


def _github(fake) -> GitHubPRProvider:
    provider = GitHubPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestGitHub:
    def test_the_event_is_comment_unless_asked_behaviour_change(self, settings) -> None:
        """2.2.x approved every clean review; the verdict no longer decides."""
        fake = _GitHub()
        batch = _batch(_pr("github", "o/r", 1))
        assert batch.verdict == ReviewVerdict.APPROVE
        _github(fake).post_review(batch)
        assert fake.reviews[-1]["event"] == "COMMENT"

        blocking = _batch(_pr("github", "o/r", 1), [_finding(FindingSeverity.CRITICAL)])
        assert blocking.verdict == ReviewVerdict.REQUEST_CHANGES
        _github(fake).post_review(blocking)
        assert fake.reviews[-1]["event"] == "COMMENT"
        assert fake.dismissed == []

    def test_approve_when_clean(self, settings) -> None:
        fake = _GitHub()
        _github(fake).post_review(
            _batch(_pr("github", "o/r", 1), actions=PRActions(approve_when_clean=True)))
        assert fake.reviews[-1]["event"] == "APPROVE"

    def test_request_changes_on_critical(self, settings) -> None:
        fake = _GitHub()
        _github(fake).post_review(_batch(
            _pr("github", "o/r", 1), [_finding(FindingSeverity.CRITICAL)],
            actions=PRActions(approve_when_clean=True, request_changes_on_critical=True)))
        assert fake.reviews[-1]["event"] == "REQUEST_CHANGES"

    def test_a_later_run_with_issues_dismisses_our_approval_only(self, settings) -> None:
        fake = _GitHub()
        ours = fake.add_review("APPROVED")
        theirs = fake.add_review("APPROVED", author=HUMAN)
        blocked = fake.add_review("CHANGES_REQUESTED")
        result = _github(fake).post_review(_batch(
            _pr("github", "o/r", 1), [_finding()],
            actions=PRActions(approve_when_clean=True,
                              request_changes_on_critical=True)))
        assert fake.reviews[-1]["event"] == "COMMENT"
        assert sorted(fake.dismissed) == sorted([ours, blocked])
        assert theirs not in fake.dismissed
        assert result["review_state"]["dismissed"] == 2

    def test_an_approving_rerun_dismisses_nothing(self, settings) -> None:
        """An APPROVE supersedes our earlier verdict by itself; idempotent."""
        fake = _GitHub()
        fake.add_review("APPROVED")
        actions = PRActions(approve_when_clean=True)
        for _ in range(2):
            _github(fake).post_review(_batch(_pr("github", "o/r", 1), actions=actions))
        assert [r["event"] for r in fake.reviews] == ["APPROVE", "APPROVE"]
        assert fake.dismissed == []

    def test_a_refused_dismissal_does_not_fail_the_review(self, settings) -> None:
        fake = _GitHub()
        fake.add_review("APPROVED")

        def refuse(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/dismissals"):
                return httpx.Response(403, json={"message": "protected branch"})
            return fake(request)

        provider = GitHubPRProvider(token="fake")
        _patch_client(provider, httpx.MockTransport(refuse))
        result = provider.post_review(_batch(
            _pr("github", "o/r", 1), [_finding()],
            actions=PRActions(approve_when_clean=True)))
        assert result["review_state"] == {
            "event": "COMMENT", "dismissed": 0, "dismiss_failed": 1}
        assert result["summary_comment_id"]


# ─── committable suggestions ────────────────────────────────────────


class TestSuggestions:
    def _posted(self, settings, finding, *, committable=True) -> dict:
        fake = _GitHub()
        _github(fake).post_review(_batch(
            _pr("github", "o/r", 1), [finding],
            actions=PRActions(committable_suggestions=committable)))
        [comment] = fake.reviews[-1]["comments"]
        return comment

    def test_github_single_line(self, settings) -> None:
        c = self._posted(settings, _finding(suggested_code="X = 1"))
        assert "```suggestion\nX = 1\n```" in c["body"]
        assert c["line"] == 11 and "start_line" not in c

    def test_github_multi_line_spans_the_range(self, settings) -> None:
        c = self._posted(settings, _finding(suggested_code="B2\nC2",
                                            suggested_end_line=12))
        assert "```suggestion\nB2\nC2\n```" in c["body"]
        assert (c["start_line"], c["line"], c["start_side"]) == (11, 12, "RIGHT")

    def test_off_means_a_readable_diff_not_a_button(self, settings) -> None:
        c = self._posted(settings, _finding(suggested_code="B2\nC2",
                                            suggested_end_line=12), committable=False)
        assert "```suggestion" not in c["body"]
        assert "```diff\n-B\n-C\n+B2\n+C2\n```" in c["body"]
        assert "start_line" not in c

    def test_a_snapped_anchor_is_never_committable(self, settings) -> None:
        """Line 40 is outside the hunk; the comment moves to 14 and a
        suggestion there would replace the wrong line."""
        c = self._posted(settings, _finding(line=40, suggested_code="x"))
        assert c["line"] == 14
        assert "```suggestion" not in c["body"]

    def test_a_range_leaving_the_hunk_is_not_committable(self, settings) -> None:
        c = self._posted(settings, _finding(suggested_code="a\nb\nc\nd\ne\nf",
                                            suggested_end_line=16))
        assert "```suggestion" not in c["body"]

    def test_a_hint_is_never_committable(self, settings) -> None:
        c = self._posted(settings, _finding(suggestion="=== / !=="))
        assert "```suggestion" not in c["body"]
        assert "**Suggestion:**" in c["body"]

    def test_gitlab_offsets(self) -> None:
        f = _finding(suggested_code="B2\nC2\nc2", suggested_end_line=13)
        body = _format_finding_body(f, MARKER, committable="gitlab")
        assert "```suggestion:-0+2\nB2\nC2\nc2\n```" in body
        one = _format_finding_body(_finding(suggested_code="B2"), committable="gitlab")
        assert "```suggestion:-0+0\nB2\n```" in one

    def test_a_backtick_in_the_code_lengthens_the_fence(self) -> None:
        body = _format_finding_body(_finding(suggested_code="s = '```'"),
                                    committable="github")
        assert "````suggestion\ns = '```'\n````" in body

    def test_the_agents_output_is_parsed_defensively(self) -> None:
        assert parse_suggested_code({"suggested_code": "x = 1\n"}) == "x = 1"
        assert parse_suggested_code({"suggested_code": 3}) is None
        assert parse_suggested_code({"suggested_code": "\n".join(["x"] * 41)}) is None
        assert parse_suggested_end_line({"suggested_code": "a\nb",
                                         "suggested_end_line": 12}, 11) == 12
        assert parse_suggested_end_line({"suggested_code": "a",
                                         "suggested_end_line": 9}, 11) is None
        assert parse_suggested_end_line({"suggested_end_line": 12}, 11) is None


# ─── GitLab ─────────────────────────────────────────────────────────


class _GitLab(_FakeGitLab):
    """+ approvals and the MR description."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.approved_by: list[str] = []
        self.approval_calls: list[tuple[str, dict]] = []
        self.description = "Author's words."
        self.discussion_bodies: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        method = request.method
        if url.endswith("/merge_requests/5/approvals") and method == "GET":
            return httpx.Response(200, json={"approved_by": [
                {"user": {"username": u}} for u in self.approved_by]})
        if url.endswith("/merge_requests/5/approve") and method == "POST":
            self.approval_calls.append(("approve", self._form(request)))
            if self.viewer in self.approved_by:
                return httpx.Response(401, json={"message": "already approved"})
            self.approved_by.append(self.viewer)
            return httpx.Response(201, json={})
        if url.endswith("/merge_requests/5/unapprove") and method == "POST":
            self.approval_calls.append(("unapprove", {}))
            if self.viewer not in self.approved_by:
                return httpx.Response(404, json={"message": "not found"})
            self.approved_by.remove(self.viewer)
            return httpx.Response(201, json={})
        if url.endswith("/merge_requests/5") and method == "GET":
            return httpx.Response(200, json={"description": self.description})
        if url.endswith("/merge_requests/5") and method == "PUT":
            self.description = self._form(request)["description"]
            return httpx.Response(200, json={"description": self.description})
        if url.endswith("/discussions") and method == "POST":
            self.discussion_bodies.append(self._form(request)["body"])
        return super().__call__(request)


def _gitlab(fake) -> GitLabPRProvider:
    provider = GitLabPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestGitLab:
    def test_approve_once_and_pin_the_commit(self, settings) -> None:
        fake = _GitLab()
        actions = PRActions(approve_when_clean=True)
        for _ in range(2):
            _gitlab(fake).post_review(
                _batch(_pr("gitlab", "group/proj", 5), actions=actions))
        assert fake.approved_by == [BOT]
        assert fake.approval_calls == [("approve", {"sha": "abcdef1234567890"})]

    def test_issues_on_a_later_run_unapprove(self, settings) -> None:
        fake = _GitLab()
        fake.approved_by = [BOT, HUMAN]
        result = _gitlab(fake).post_review(_batch(
            _pr("gitlab", "group/proj", 5), [_finding()],
            actions=PRActions(approve_when_clean=True)))
        assert fake.approved_by == [HUMAN]
        assert result["review_state"] == {"approval": "unapproved"}

    def test_nothing_to_withdraw_sends_nothing(self, settings) -> None:
        fake = _GitLab()
        _gitlab(fake).post_review(_batch(
            _pr("gitlab", "group/proj", 5), [_finding()],
            actions=PRActions(approve_when_clean=True)))
        assert fake.approval_calls == []

    def test_request_changes_falls_back_to_withdrawing_and_saying_so(self, settings) -> None:
        fake = _GitLab()
        fake.approved_by = [BOT]
        _gitlab(fake).post_review(_batch(
            _pr("gitlab", "group/proj", 5), [_finding(FindingSeverity.CRITICAL)],
            actions=PRActions(approve_when_clean=True,
                              request_changes_on_critical=True)))
        assert fake.approved_by == []
        summary = [b for b in fake.bodies() if "Code Review for PR" in b]
        assert len(summary) == 1 and "Changes requested" in summary[0]

    def test_off_means_no_approval_traffic(self, settings) -> None:
        fake = _GitLab()
        fake.approved_by = [BOT]
        _gitlab(fake).post_review(_batch(_pr("gitlab", "group/proj", 5)))
        assert fake.approval_calls == [] and fake.approved_by == [BOT]

    def test_the_suggestion_is_rendered_with_its_offset(self, settings) -> None:
        fake = _GitLab()
        _gitlab(fake).post_review(_batch(
            _pr("gitlab", "group/proj", 5),
            [_finding(suggested_code="B2\nC2", suggested_end_line=12)],
            actions=PRActions(committable_suggestions=True)))
        [body] = fake.discussion_bodies
        assert "```suggestion:-0+1\nB2\nC2\n```" in body


# ─── Bitbucket ──────────────────────────────────────────────────────


class _Bitbucket(_FakeBitbucket):
    """+ participants (approve / request changes) and the PR description."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.state: str | None = None
        self.calls: list[str] = []
        self.description = "Author's words."
        self.puts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        base = "/2.0/repositories/ws/r/pullrequests/3"
        if path == base and method == "GET":
            participants = [{"user": _FakeBitbucket._user_for(HUMAN),
                             "state": "approved", "approved": True}]
            if self.state is not None:
                participants.append({"user": dict(self.viewer_user),
                                     "state": self.state,
                                     "approved": self.state == "approved"})
            return httpx.Response(200, json={
                "title": "Add caching", "description": self.description,
                "participants": participants,
                "reviewers": [{"uuid": "{uuid-dana}", "nickname": HUMAN}],
            })
        if path == base and method == "PUT":
            payload = json.loads(request.content)
            self.puts.append(payload)
            self.description = payload["description"]
            return httpx.Response(200, json=payload)
        for name, state in (("approve", "approved"), ("request-changes", "changes_requested")):
            if path == f"{base}/{name}":
                self.calls.append(f"{method} {name}")
                if method == "POST":
                    self.state = state
                    return httpx.Response(200, json={"state": state})
                if self.state != state:
                    return httpx.Response(404, json={"error": {"message": "nope"}})
                self.state = None
                return httpx.Response(204)
        return super().__call__(request)


def _bitbucket(fake) -> BitbucketPRProvider:
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestBitbucket:
    def test_approve_idempotently(self, settings) -> None:
        fake = _Bitbucket()
        actions = PRActions(approve_when_clean=True)
        for _ in range(2):
            _bitbucket(fake).post_review(_batch(_pr("bitbucket", "ws/r", 3), actions=actions))
        assert fake.state == "approved"
        assert fake.calls == ["POST approve"]

    def test_request_changes_withdraws_the_approval_first(self, settings) -> None:
        fake = _Bitbucket()
        fake.state = "approved"
        _bitbucket(fake).post_review(_batch(
            _pr("bitbucket", "ws/r", 3), [_finding(FindingSeverity.CRITICAL)],
            actions=PRActions(approve_when_clean=True, request_changes_on_critical=True)))
        assert fake.calls == ["DELETE approve", "POST request-changes"]
        assert fake.state == "changes_requested"

    def test_the_block_is_lifted_when_the_next_run_has_no_critical(self, settings) -> None:
        fake = _Bitbucket()
        fake.state = "changes_requested"
        result = _bitbucket(fake).post_review(_batch(
            _pr("bitbucket", "ws/r", 3), [_finding()],
            actions=PRActions(request_changes_on_critical=True)))
        assert fake.calls == ["DELETE request-changes"]
        assert fake.state is None
        assert result["review_state"]["done"] == ["withdrew_request-changes"]

    def test_off_sends_nothing(self, settings) -> None:
        fake = _Bitbucket()
        fake.state = "approved"
        _bitbucket(fake).post_review(_batch(_pr("bitbucket", "ws/r", 3), [_finding()]))
        assert fake.calls == []

    def test_a_suggestion_is_a_diff_block_never_a_button(self, settings) -> None:
        fake = _Bitbucket()
        _bitbucket(fake).post_review(_batch(
            _pr("bitbucket", "ws/r", 3), [_finding(suggested_code="B2")],
            actions=PRActions(committable_suggestions=True)))
        [inline] = [b for b in fake.bodies() if "because" in b]
        assert "```suggestion" not in inline
        assert "```diff\n-B\n+B2\n```" in inline

    def test_the_description_put_keeps_title_and_reviewers(self, settings) -> None:
        fake = _Bitbucket()
        out = _bitbucket(fake).update_description(
            _pr("bitbucket", "ws/r", 3), lambda cur: cur + "\n\nmore")
        assert out["written"] is True
        assert fake.puts == [{"description": "Author's words.\n\nmore",
                              "title": "Add caching",
                              "reviewers": [{"uuid": "{uuid-dana}"}]}]


# ─── the summary in the description: modes ──────────────────────────

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)


def _compose(current, *, existing="append", new="replace", commit="c1" * 4,
             complement=None, now=NOW, insights="OURS"):
    return compose_description(current, insights=insights, commit=commit,
                               existing_mode=existing, new_commits_mode=new,
                               complement=complement, now=now)


class TestDescriptionModes:
    def test_first_review_append_keeps_the_author_first(self) -> None:
        out = _compose("Author text.")
        assert out.startswith("Author text.\n\n" + SUMMARY_START)
        assert out.rstrip().endswith(SUMMARY_END)
        assert "OURS" in split_description(out)[1]

    def test_first_review_replace_is_only_our_block(self) -> None:
        out = _compose("Author text.", existing="replace")
        assert out.startswith(SUMMARY_START) and "Author text." not in out

    def test_first_review_complement_merges_and_keeps_the_original(self) -> None:
        seen = {}

        def merge(author, ours):
            seen.update(author=author, ours=ours)
            return "Merged: author intent + OURS"

        out = _compose("Author text.", existing="complement", complement=merge)
        assert seen == {"author": "Author text.", "ours": "OURS"}
        before, inner, after = split_description(out)
        assert before == "" and after == ""
        assert inner.strip().startswith("Merged: author intent + OURS")
        assert "Original description" in inner and "Author text." in inner

    @pytest.mark.parametrize("failure", [
        pytest.param(lambda a, o: None, id="no-answer"),
        pytest.param(lambda a, o: 1 / 0, id="raises"),
        pytest.param(None, id="no-client"),
    ])
    def test_a_failed_complement_falls_back_to_append(self, failure) -> None:
        out = _compose("Author text.", existing="complement", complement=failure)
        assert out == _compose("Author text.", existing="append")

    def test_nothing_on_new_commits_writes_only_once(self) -> None:
        first = _compose("Author text.", new="nothing")
        assert _compose(first, new="nothing", commit="d2" * 4, insights="NEW") is None

    def test_replace_on_new_commits_rewrites_only_our_block(self) -> None:
        first = _compose("Author text.")
        edited = first + "\n\nAuthor added this later."
        out = _compose(edited, commit="d2" * 4, insights="NEW")
        assert out.startswith("Author text.\n\n")
        assert out.endswith("Author added this later.")
        assert "NEW" in out and "OURS" not in out
        assert out.count(SUMMARY_START) == 1

    def test_append_on_new_commits_adds_a_dated_section_once_per_commit(self) -> None:
        first = _compose("Author text.", new="append")
        second = _compose(first, new="append", commit="d2" * 4, insights="NEW",
                          now=LATER)
        inner = split_description(second)[1]
        assert "OURS" in inner and "NEW" in inner
        assert "### Update — 2026-10-05 (commit `d2d2d2d`)" in inner
        # A re-run of the same commit rewrites its own section, never a copy.
        again = _compose(second, new="append", commit="d2" * 4, insights="NEWER",
                         now=LATER)
        inner = split_description(again)[1]
        assert inner.count("### Update") == 1
        assert "NEWER" in inner and "NEW\n" not in inner and "OURS" in inner

    def test_replace_on_new_commits_recomplements_from_the_original(self) -> None:
        merges = []

        def merge(author, ours):
            merges.append(author)
            return f"merged[{ours}]"

        first = _compose("Author text.", existing="complement", complement=merge)
        second = _compose(first, existing="complement", complement=merge,
                          commit="d2" * 4, insights="NEW")
        assert merges == ["Author text.", "Author text."]
        assert "merged[NEW]" in second and "merged[OURS]" not in second

    def test_no_insights_writes_nothing(self) -> None:
        assert _compose("Author text.", insights="  ") is None


# ─── end to end: the orchestrator writes the description ────────────


class _Provider(GitHubPRProvider):
    def __init__(self, fake, pr) -> None:
        super().__init__(token="fake")
        _patch_client(self, httpx.MockTransport(fake))
        self._pr = pr

    def fetch_pull_request(self, repo, pr_number):
        return self._pr


def _e2e_pr(**kw) -> PullRequest:
    from tests.review.test_the_pr_hears_the_review_begin_and_end import _pr as pr
    return pr(**kw)


class _ComplementFails(_Client):
    def generate(self, **kwargs):
        if "Author's description" in kwargs.get("prompt", ""):
            self.calls.append(kwargs)
            raise TimeoutError("slow")
        return super().generate(**kwargs)


class TestDescriptionEndToEnd:
    POLICY = {"enabled": True, "target_branches": [], "summary_target": "description"}

    def test_the_walkthrough_moves_to_the_description(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY),
                     policy=self.POLICY)
        result = orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))

        assert result.provider_response["description"]["written"] is True
        assert fake.pr_body.startswith("Author's words.\n\n" + SUMMARY_START)
        assert "Adds a read-through cache" in fake.pr_body
        assert "| `src/mod0.py` |" in fake.pr_body
        [summary] = fake.issue
        assert "Changes walkthrough" not in summary["body"]
        assert "Adds a read-through cache" not in summary["body"]
        assert "summary is in the pull request description" in summary["body"]

    def test_a_failed_description_keeps_the_walkthrough_in_the_comment(
        self, env, monkeypatch,
    ) -> None:
        fake = _GitHub()

        def refuse(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH" and request.url.path == "/repos/o/r/pulls/1":
                return httpx.Response(403, json={"message": "no"})
            return fake(request)

        provider = _Provider(fake, _e2e_pr())
        _patch_client(provider, httpx.MockTransport(refuse))
        orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY),
                     policy=self.POLICY)
        result = orch.review("github", "o/r", 1, provider=provider)
        assert result.provider_response["description"]["written"] is False
        [summary] = fake.issue
        assert "Changes walkthrough" in summary["body"]

    def test_a_complement_failure_never_fails_the_review(self, env, monkeypatch) -> None:
        fake = _GitHub()
        client = _ComplementFails(GOOD_REPLY)
        orch = _orch(monkeypatch, agents=[_Agent()], client=client,
                     policy={**self.POLICY, "summary_existing_description": "complement"})
        result = orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        assert result.posted is True
        assert any("Author's description" in c["prompt"] for c in client.calls)
        complement = [c for c in client.calls if "Author's description" in c["prompt"]]
        assert complement[0]["operation"] == "review_summary"
        assert complement[0]["num_retries"] == 0
        # Appended, exactly as if "append" had been chosen.
        assert fake.pr_body.startswith("Author's words.\n\n" + SUMMARY_START)

    def test_a_complement_answer_is_stripped_of_forged_markers(self, env, monkeypatch) -> None:
        fake = _GitHub()

        class _Merges(_Client):
            def generate(self, **kwargs):
                if "Author's description" in kwargs.get("prompt", ""):
                    self.calls.append(kwargs)
                    return _Reply(f"Author's words, plus a cache.{SUMMARY_END}<!-- x -->")
                return super().generate(**kwargs)

        orch = _orch(monkeypatch, agents=[_Agent()], client=_Merges(GOOD_REPLY),
                     policy={**self.POLICY, "summary_existing_description": "complement"})
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        assert fake.pr_body.count(SUMMARY_END) == 1
        assert "<!-- x -->" not in fake.pr_body
        assert "Author's words, plus a cache." in fake.pr_body

    def test_the_comment_target_leaves_the_description_alone(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY))
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        assert fake.pr_patches == []

    def test_gitlab_and_bitbucket_write_their_description_field(self, settings) -> None:
        gl = _GitLab()
        out = _gitlab(gl).update_description(
            _pr("gitlab", "group/proj", 5), lambda cur: f"{cur}!")
        assert out["written"] and gl.description == "Author's words.!"
        bb = _Bitbucket()
        assert _bitbucket(bb).update_description(
            _pr("bitbucket", "ws/r", 3), lambda cur: None)["unchanged"] is True
        assert bb.puts == []


# ─── status feedback ────────────────────────────────────────────────


class TestStatusFeedback:
    def test_a_skip_with_no_placeholder_leaves_one_note(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()])
        for _ in range(3):
            orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr(draft=True)))
        [note] = fake.issue
        assert STATUS_FEEDBACK_MARK in note["body"] and MARKER in note["body"]
        assert "the pull request is a draft" in note["body"]

    def test_off_is_silent(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()],
                     policy={"enabled": True, "target_branches": [],
                             "status_feedback": False})
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr(draft=True)))
        assert fake.issue == []

    def test_a_branch_outside_the_targets_says_so(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()],
                     policy={"enabled": True, "target_branches": ["release"]})
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        [note] = fake.issue
        assert "base branch `main`" in note["body"]

    def test_a_finished_summary_is_left_alone_and_the_note_added(self, env, monkeypatch) -> None:
        fake = _GitHub()
        finished = f"{MARKER}\n## 🤖 Code Review for PR #1\n\n✅ **APPROVED**"
        previous = fake.add_issue(finished)
        orch = _orch(monkeypatch, agents=[_Agent()])
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr(draft=True)))
        assert fake.issue[0] == {"id": previous, "body": finished,
                                 "user": {"login": BOT}}
        assert len(fake.issue) == 2 and STATUS_FEEDBACK_MARK in fake.issue[1]["body"]

    def test_a_placeholder_is_finalized_instead_of_noted(self, env, monkeypatch) -> None:
        fake = _GitHub()
        fake.add_issue(f"{MARKER}\n{STATUS_IN_PROGRESS_MARK}\n## 🔄 reviewing…")
        orch = _orch(monkeypatch, agents=[_Agent()])
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr(draft=True)))
        [comment] = fake.issue
        assert "⏭️ Skipped" in comment["body"]
        assert STATUS_FEEDBACK_MARK not in comment["body"]

    def test_the_next_real_review_takes_the_note_over(self, env, monkeypatch) -> None:
        fake = _GitHub()
        orch = _orch(monkeypatch, agents=[_Agent()], client=_Client(GOOD_REPLY))
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr(draft=True)))
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        [summary] = fake.issue
        assert STATUS_FEEDBACK_MARK not in summary["body"]
        assert "Code Review for PR #1" in summary["body"]

    @pytest.mark.parametrize("which", ["gitlab", "bitbucket"])
    def test_gitlab_and_bitbucket_upsert_the_same_note(self, settings, which) -> None:
        if which == "gitlab":
            fake, make, pr = _GitLab(), _gitlab, _pr("gitlab", "group/proj", 5)
            fake.add_note(f"{MARKER}\nfinished")
        else:
            fake, make, pr = _Bitbucket(), _bitbucket, _pr("bitbucket", "ws/r", 3)
            fake.add_comment(f"{MARKER}\nfinished")
        for reason in ("first reason", "second reason"):
            assert make(fake).upsert_feedback_comment(pr, reason) is not None
        # Bitbucket stores the marker hidden; read back, it is the HTML form.
        notes = [b for b in map(reveal, fake.bodies()) if STATUS_FEEDBACK_MARK in b]
        assert len(notes) == 1 and "second reason" in notes[0]
        assert f"{MARKER}\nfinished" in fake.bodies()


# ─── message templates ──────────────────────────────────────────────


class TestTemplates:
    VALUES = {"commit": "abc1234", "agents": "defect, security", "files": 3,
              "pr_number": 7}

    def test_known_placeholders_are_filled_and_unknown_left_literal(self) -> None:
        out = render_template(
            "PR #{pr_number} @ {commit}: {agents} on {files} files {unknown} {0} {"
            " {commit.__class__} {{commit}}", self.VALUES, limit=500)
        assert out == ("PR #7 @ abc1234: defect, security on 3 files {unknown} {0} { "
                       "{commit.__class__} {abc1234}")

    def test_nothing_or_junk_renders_nothing(self) -> None:
        for template in (None, "", "   ", 42, ["x"]):
            assert render_template(template, self.VALUES, limit=50) == ""

    def test_the_length_is_capped(self) -> None:
        out = render_template("{agents}" * 100, self.VALUES, limit=40)
        assert len(out) == 40 and out.endswith("…")

    def test_the_started_message_keeps_the_in_progress_mark(self) -> None:
        pr = _pr("github", "o/r", 7)
        body = _format_started_comment(
            pr, agents=["defect"], started_at="now", marker=MARKER,
            template="Looking at {commit} with {agents} ({files} file) {nope}")
        assert body.splitlines() == [
            MARKER, STATUS_IN_PROGRESS_MARK,
            "Looking at abcdef1 with defect (1 file) {nope}"]

    def test_the_finished_header_replaces_the_heading(self) -> None:
        batch = _batch(_pr("github", "o/r", 7),
                       actions=PRActions(message_finished_header="### Done: #{pr_number}"))
        batch.rich_summary = True
        for text in (_format_summary(batch, MARKER),
                     _format_summary(_rich_off(batch), MARKER)):
            assert "### Done: #7" in text
            assert "Code Review for PR" not in text

    def test_the_started_template_reaches_the_pull_request(self, env, monkeypatch) -> None:
        fake = _GitHub()
        seen: list[str] = []

        def probe():
            seen.extend(c["body"] for c in fake.issue)

        orch = _orch(monkeypatch, agents=[_Agent(probe=probe)],
                     policy={"enabled": True, "target_branches": [],
                             "message_started": "Celmis on `{commit}` — {agents}"})
        orch.review("github", "o/r", 1, provider=_Provider(fake, _e2e_pr()))
        [placeholder] = seen
        assert STATUS_IN_PROGRESS_MARK in placeholder
        assert re.search(r"^Celmis on `abcdef1` — defect", placeholder, re.MULTILINE)
        assert "Celmis is reviewing this PR" not in placeholder


def _rich_off(batch: ReviewBatch) -> ReviewBatch:
    import dataclasses
    return dataclasses.replace(batch, rich_summary=False)


def test_a_left_side_finding_keeps_its_hint_readable() -> None:
    f = _finding(side=HunkSide.LEFT, suggested_code="x", suggestion="use x")
    body = _format_finding_body(f, committable=None)
    assert "**Suggested change:**" in body and "**Suggestion:**" in body
