"""Workspace review defaults reach every repository that does not override them.

    repo policy (non-null) > workspace default (non-null) > install > built-in

Asserted twice: on the pure resolver the API reports from, and on the real
orchestrator loop — which agents run, whether the veto runs, which branches
are reviewed and what the batch carries for the providers — so the page and
the review cannot disagree about what "inherit" means.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import VerifierAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest, ReviewVerdict
from src.review.orchestrator import ReviewOrchestrator
from src.review.review_defaults import (
    INHERITABLE_FIELDS,
    install_defaults,
    merge_policy,
    resolve,
)
from src.review.settings import ReviewSettings

# ─── the resolver ────────────────────────────────────────────────────

INSTALL = install_defaults(ReviewSettings())


def test_with_nothing_set_everything_is_the_install_default():
    values, sources = resolve(None, None, INSTALL)
    assert set(sources.values()) == {"install"}
    assert set(values) == set(INHERITABLE_FIELDS)
    assert values["disabled_agents"] == []
    assert values["summary_enabled"] is True
    assert values["max_inline_comments"] == ReviewSettings().max_inline_comments


def test_the_workspace_beats_the_install_and_the_repo_beats_both():
    ws = {"disabled_agents": ["security"], "max_inline_comments": 5,
          "summary_enabled": False, "comment_min_severity": "error"}
    repo = SimpleNamespace(disabled_agents=[], max_inline_comments=None,
                           summary_enabled=None, comment_min_severity="critical")
    values, sources = resolve(repo, ws, INSTALL)
    assert (values["disabled_agents"], sources["disabled_agents"]) == ([], "repo")
    assert (values["max_inline_comments"], sources["max_inline_comments"]) == (5, "workspace")
    assert (values["summary_enabled"], sources["summary_enabled"]) == (False, "workspace")
    assert (values["comment_min_severity"], sources["comment_min_severity"]) == (
        "critical", "repo")
    assert sources["ignore_globs"] == "install"


def test_an_empty_list_on_the_repo_is_an_answer_not_an_inherit():
    """[] is "every agent runs" / "every branch" — this repository's own
    decision over a workspace default, never a silent inherit."""
    values, sources = resolve({"target_branches": []}, {"target_branches": ["main"]}, INSTALL)
    assert values["target_branches"] == [] and sources["target_branches"] == "repo"


def test_the_old_spelling_of_verifier_off_still_wins():
    values, sources = resolve({"disabled_agents": ["verifier"]},
                              {"verifier_enabled": True}, INSTALL)
    assert values["verifier_enabled"] is False and sources["verifier_enabled"] == "repo"


def test_no_workspace_row_leaves_the_policy_untouched():
    policy = {"enabled": True, "disabled_agents": None}
    assert merge_policy(policy, None) is policy
    assert merge_policy(None, None) is None
    assert merge_policy(None, {n: None for n in INHERITABLE_FIELDS}) is None


# ─── the orchestrator, driven for real ───────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


class _Canned(ReviewAgent):
    def __init__(self, name: str) -> None:
        self.name = name

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, findings=[Finding(
            file_path="src/foo.py", line=2, rule_id=f"{self.name}.r",
            severity=FindingSeverity.WARNING, confidence=0.9, agent=self.name,
            title=f"{self.name} finding", body="b", evidence_kind="inferred",
        )])


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):  # pragma: no cover - not posted
        return {}

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(
        bc_mod, "run_breaking_change", lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(
        comp_mod, "run_compliance", lambda ctx: AgentRunResult(agent="compliance"))

    def _run(*, policy, workspace, asked: list | None = None):
        llm = MagicMock()
        reply = MagicMock()
        reply.text, reply.input_tokens, reply.output_tokens = (
            '{"keep": [0, 1], "reasons": {}}', 10, 5)
        llm.generate.return_value = reply
        orch = ReviewOrchestrator(
            settings=ReviewSettings(),
            agents=[_Canned("defect"), _Canned("security")],
            verifier=VerifierAgent(),
        )
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)

        def _defaults(ws_id):
            if asked is not None:
                asked.append(ws_id)
            return workspace

        monkeypatch.setattr(orch, "_load_workspace_defaults", _defaults)
        monkeypatch.setattr(
            orch, "_build_context",
            lambda pr, **kw: AgentContext(pull_request=pr, llm_client=llm),
        )
        batch = orch.review(
            "github", "acme/api", 7, dry_run=True, post_comments=False,
            provider=_Provider(), workspace_id="ws-1",
        ).batch
        return batch, llm

    return _run


def _agents_that_spoke(batch) -> set[str]:
    return {a for f in batch.findings for a in str(f.agent).split(",")}


def _repo(**fields) -> dict:
    base = {"enabled": True, "target_branches": None, "disabled_agents": None,
            "workspace_id": "ws-owner"}
    return {**base, **fields}


def test_a_repo_without_a_policy_runs_the_workspace_roster(run):
    asked: list = []
    batch, _ = run(policy=None, workspace={"disabled_agents": ["security"]}, asked=asked)
    assert _agents_that_spoke(batch) == {"defect"}
    assert asked == ["ws-1"], "no policy row: the review's own workspace answers"


def test_a_repo_that_inherits_runs_the_workspace_roster(run):
    asked: list = []
    batch, _ = run(policy=_repo(), workspace={"disabled_agents": ["security"]},
                   asked=asked)
    assert _agents_that_spoke(batch) == {"defect"}
    assert asked == ["ws-owner"], "the workspace that owns the policy row answers"


def test_a_repo_that_decided_keeps_its_own_roster(run):
    batch, _ = run(policy=_repo(disabled_agents=[]),
                   workspace={"disabled_agents": ["security"]})
    assert _agents_that_spoke(batch) == {"defect", "security"}


@pytest.mark.parametrize("repo, workspace, expected", [
    (None, {"verifier_enabled": True}, (True, "enabled_by_policy")),
    ({"verifier_enabled": None}, {"verifier_enabled": True}, (True, "enabled_by_policy")),
    ({"verifier_enabled": False}, {"verifier_enabled": True}, (False, "disabled_by_policy")),
    ({"verifier_enabled": None}, None, (False, "off_by_default")),
])
def test_the_workspace_can_switch_the_veto_on_and_a_repo_back_off(
    monkeypatch, repo, workspace, expected,
):
    orch = ReviewOrchestrator(settings=ReviewSettings(), agents=[], verifier=VerifierAgent())
    monkeypatch.setattr(orch, "_load_policy",
                        lambda slug: None if repo is None else _repo(**repo))
    monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws: workspace)
    policy = orch._resolved_policy("acme/api", "ws-1")
    assert orch._verifier_enabled(policy, set()) == expected


def test_workspace_target_branches_skip_an_untargeted_pr(run):
    batch, _ = run(policy=None, workspace={"target_branches": ["release"]})
    assert batch.verdict == ReviewVerdict.SKIPPED
    batch, _ = run(policy=_repo(target_branches=[]),
                   workspace={"target_branches": ["release"]})
    assert batch.verdict != ReviewVerdict.SKIPPED


def test_comment_settings_reach_the_batch(run):
    batch, _ = run(policy=_repo(max_inline_comments=None, comment_min_severity=None),
                   workspace={"max_inline_comments": 3, "comment_min_severity": "error",
                              "summary_enabled": False})
    assert batch.max_inline_comments == 3
    assert batch.comment_min_severity == "error"
    assert batch.rich_summary is False
    batch, _ = run(policy=_repo(max_inline_comments=9),
                   workspace={"max_inline_comments": 3})
    assert batch.max_inline_comments == 9
