"""A review that read a Jira task shows its acceptance criteria as a checklist.

From the business-logic agent's findings for free (`findings`), or judged by one
extra short model call (`checklist`); `off` adds nothing. The section is a
summary section, so both comment layouts and the description show it without
the composers knowing about it, and the rows are stored on the pull request for
its page.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Finding, FindingSeverity, Hunk, PRActions, PullRequest
from src.review.orchestrator import ReviewOrchestrator
from src.review.providers.base import _format_summary
from src.review.stages import StageRecorder
from tests.review import jira_fakes
from tests.review.jira_fakes import FakeJira, bullets, doc, heading, issue, para
from tests.review.test_the_business_logic_agent import _PassThroughVerifier

MARKER = "<!-- celmis:review:under-test -->"


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/shop", number=31, title="PROJ-6066 export csv",
        description="", author="a", base_ref="develop", base_sha="x", head_ref="feat",
        head_sha="abcdef1234567890", state="open",
        hunks=[Hunk(file_path="src/export.py", old_file_path="src/export.py", old_start=1,
                    old_count=1, new_start=1, new_count=2, content="@@ -1 +1,2 @@\n+x = 1\n")],
        raw_diff="diff --git a/src/export.py b/src/export.py\n@@ -1 +1,2 @@\n+x = 1\n",
    )


class _Logic(ReviewAgent):
    name = "business_logic"

    def __init__(self, findings: list[Finding] | None = None) -> None:
        self._findings = findings or []

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, findings=list(self._findings))


class _Quiet(ReviewAgent):
    name = "defect"

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name)


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def fetch_commit_messages(self, pr, limit=50):
        return []

    def close(self):
        pass


class _Llm:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, []

    def generate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self.text, input_tokens=100, output_tokens=30, cost_usd=None)


def _gap(text: str = "[PROJ-6066 AC2] The task says 'ten millimetres'; line 1 keeps them.") -> Finding:
    return Finding(file_path="src/export.py", line=1, severity=FindingSeverity.ERROR,
                   title="Short offcuts kept", agent="business_logic",
                   rule_id="logic.requirement-missing", reasoning=text)


@pytest.fixture
def world(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    jira_fakes.install(monkeypatch, tmp_path, fake)
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    def run(policy=None, *, findings=None, llm=None, task_issue=None):
        if task_issue is not None:
            fake.issues["PROJ-6066"] = task_issue
        monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client",
                            lambda self, *a, **k: (llm, {}))
        orch = ReviewOrchestrator(agents=[_Quiet(), _Logic(findings)],
                                  verifier=_PassThroughVerifier())
        full = {"enabled": True, "target_branches": None,
                "enabled_agents": ["business_logic"], **(policy or {})}
        monkeypatch.setattr(orch, "_load_policy", lambda slug: full)
        monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws: None)
        rec = StageRecorder()
        batch = orch.review("github", "acme/shop", 31, dry_run=True, post_comments=False,
                            provider=_Provider(), workspace_id="ws-a", stages=rec).batch
        return batch, rec

    return run


def _stage(rec: StageRecorder, key: str):
    return next((s for s in rec.snapshot() if s["key"] == key), None)


def test_the_default_is_a_checklist_judged_by_one_model_call(world):
    llm = _Llm(json.dumps([
        {"id": "PROJ-6066:AC1", "verdict": "met", "evidence": "src/export.py:1"},
        {"id": "PROJ-6066:AC2", "verdict": "missing", "evidence": ""}]))
    batch, rec = world(findings=[], llm=llm)
    assert len(llm.calls) == 1
    assert [(r.id, r.verdict) for r in batch.requirements] == [("AC1", "met"), ("AC2", "missing")]
    assert (batch.tokens_in, batch.tokens_out) >= (100, 30)
    stage = _stage(rec, "requirements")
    assert stage["status"] == "success" and stage["meta"]["criteria"] == 2
    assert stage["meta"]["gaps"] == 1


def test_a_finding_that_cites_a_criterion_beats_the_second_opinion(world):
    llm = _Llm(json.dumps([
        {"id": "PROJ-6066:AC1", "verdict": "met", "evidence": "src/export.py:1"},
        {"id": "PROJ-6066:AC2", "verdict": "met", "evidence": "src/export.py:1"}]))
    batch, _ = world(findings=[_gap()], llm=llm)
    assert [r.verdict for r in batch.requirements] == ["met", "missing"]


def test_findings_mode_spends_nothing_and_says_what_it_did_not_check(world):
    llm = _Llm("[]")
    batch, _ = world({"requirements_check_mode": "findings"}, findings=[_gap()], llm=llm)
    assert llm.calls == []
    assert [r.verdict for r in batch.requirements] == ["no_gap", "missing"]
    text = "\n".join(s.markdown for s in batch.sections_for("comment"))
    assert "not checked one by one" in text


def test_off_adds_nothing_and_records_no_stage(world):
    llm = _Llm("[]")
    batch, rec = world({"requirements_check_mode": "off"}, findings=[_gap()], llm=llm)
    assert batch.requirements is None and llm.calls == []
    assert batch.sections_for("comment") == [] and _stage(rec, "requirements") is None


def test_a_broken_model_answer_leaves_the_findings_view_and_says_so_on_the_stage(world):
    batch, rec = world(findings=[_gap()], llm=_Llm("sorry, no JSON here"))
    assert [r.verdict for r in batch.requirements] == ["no_gap", "missing"]
    assert "could not be used" in _stage(rec, "requirements")["reason"]
    assert _stage(rec, "requirements")["status"] == "success"


def test_a_task_without_criteria_gets_a_task_line_and_a_skipped_stage(world):
    plain = issue(description=doc(para("Export the report as CSV for the finance team.")))
    batch, rec = world(findings=[], llm=_Llm("[]"), task_issue=plain)
    assert batch.requirements is None
    assert _stage(rec, "requirements")["status"] == "skipped"
    assert batch.sections_for("comment") == []
    assert [s.markdown.startswith("Task: [PROJ-6066]") for s in batch.sections_for("description")] == [True]


def test_without_the_business_logic_agent_there_is_no_checklist(world):
    batch, rec = world({"enabled_agents": []}, llm=_Llm("[]"))
    assert batch.requirements is None and _stage(rec, "requirements") is None


@pytest.mark.parametrize("layout", ["completed", "classic"])
def test_both_comment_layouts_show_the_section(world, layout):
    batch, _ = world(findings=[_gap()], llm=_Llm("[]"))
    batch.pr_actions = PRActions(completed_comment=layout)
    comment = _format_summary(batch, MARKER)
    assert "### Requirements check — " in comment
    assert "❌ **AC2**" in comment


def test_the_description_gets_the_task_line_and_not_the_checklist(world):
    from src.review.pr_actions import description_insights

    batch, _ = world(findings=[_gap()], llm=_Llm("[]"))
    batch.pr_overview = "Exports the report."
    text = description_insights(batch)
    assert "Task: [PROJ-6066](" in text and "### Requirements check" not in text


def test_the_bot_text_follows_the_review_language(world):
    batch, _ = world({"review_language": "uk"}, findings=[_gap()], llm=_Llm("[]"))
    comment = _format_summary(batch, MARKER)
    assert "### Перевірка вимог — " in comment


def test_a_hostile_criterion_does_not_reach_the_comment_as_markup(world):
    evil = issue(description=doc(
        para("Export."), heading("Acceptance criteria"),
        bullets("ping @everyone and run <img src=x onerror=alert(1)> now")))
    batch, _ = world(findings=[], llm=_Llm("[]"), task_issue=evil)
    comment = _format_summary(batch, MARKER)
    assert "@everyone" not in comment and "<img" not in comment
