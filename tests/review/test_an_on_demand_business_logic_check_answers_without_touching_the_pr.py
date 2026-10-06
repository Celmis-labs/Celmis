"""`@celmis -v business-logic <task>`: one forced-on agent, one answer.

The task is named by a key, a link to the connected site, or (when the
repository allows it) a Confluence page of that site; with nothing named it is
the one the pull request names. The agent runs even where the policy leaves it
off, the project allowlist does not filter a task somebody named, and the
answer is markdown for a reply: nothing is posted to the code and nothing is
recorded on the pull request. Every failure is a sentence in the answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest
from src.review.orchestrator import ReviewOrchestrator
from src.review.task_context import on_demand, service
from src.review.task_context.on_demand import TaskSpec, parse_task_spec
from src.sync.jira_instance import JiraInstance
from tests.review import jira_fakes
from tests.review.jira_fakes import SITE, FakeJira, issue, page

INSTANCE = JiraInstance(SITE)


def _pr(title: str = "tidy the export", description: str = "") -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/shop", number=31, title=title, description=description,
        author="a", base_ref="develop", base_sha="x", head_ref="feat",
        head_sha="abcdef1234567890", state="open",
        hunks=[Hunk(file_path="src/export.py", old_file_path="src/export.py", old_start=1,
                    old_count=1, new_start=1, new_count=2, content="@@ -1 +1,2 @@\n+x = 1\n")],
        raw_diff="diff --git a/src/export.py b/src/export.py\n@@ -1 +1,2 @@\n+x = 1\n",
    )


class _Logic(ReviewAgent):
    name = "business_logic"

    def __init__(self, findings=None, *, error=None, skip=None) -> None:
        self.findings, self.error, self.skip = findings or [], error, skip
        self.contexts: list[AgentContext] = []

    def review(self, context: AgentContext) -> AgentRunResult:
        self.contexts.append(context)
        return AgentRunResult(agent=self.name, findings=list(self.findings), error=self.error,
                              error_code="timeout" if self.error else None,
                              skip_reason=self.skip)


class _Other(ReviewAgent):
    name = "defect"

    def __init__(self) -> None:
        self.calls = 0

    def review(self, context):
        self.calls += 1
        return AgentRunResult(agent=self.name)


class _Llm:
    def __init__(self, text: str = "[]") -> None:
        self.text, self.calls = text, 0

    def generate(self, **kw):
        self.calls += 1
        return SimpleNamespace(text=self.text, input_tokens=1, output_tokens=1, cost_usd=None)


def _gap() -> Finding:
    return Finding(file_path="src/export.py", line=1, severity=FindingSeverity.ERROR,
                   title="Short offcuts kept", agent="business_logic",
                   rule_id="logic.requirement-missing",
                   reasoning="[PROJ-6066 AC2] The task says 'ten millimetres'; line 1 keeps them.")


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    fake.issues["PROJ-7"] = issue("PROJ-7", summary="Other project")
    fake.pages["4242"] = page("4242")
    jira_fakes.install(monkeypatch, tmp_path, fake)
    return fake


@pytest.fixture
def check(monkeypatch, site):
    def run(spec, policy=None, *, logic=None, llm=None, pr=None):
        logic = logic or _Logic()
        other = _Other()
        monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client",
                            lambda self, *a, **k: (llm or _Llm(), {}))
        orch = ReviewOrchestrator(agents=[other, logic])
        out = on_demand.run_business_logic_check(
            pr or _pr(), spec, workspace_id="ws-a", policy=policy or {"enabled": True},
            orchestrator=orch, language="en")
        return out, logic, other

    return run


# ─── what a person may name ──────────────────────────────────────────


@pytest.mark.parametrize("spec,expected", [
    ("PROJ-123", TaskSpec("issue", "PROJ-123")),
    ("proj-123", TaskSpec("issue", "PROJ-123")),
    (f"{SITE}/browse/PROJ-123", TaskSpec("issue", "PROJ-123")),
    (f"{SITE}/jira/software/projects/PROJ/boards/1?selectedIssue=PROJ-9", TaskSpec("issue", "PROJ-9")),
    (f"{SITE}/wiki/spaces/ENG/pages/4242/Cutting+spec", TaskSpec("page", "4242")),
    (f"{SITE}/wiki/pages/4242", TaskSpec("page", "4242")),
])
def test_a_key_or_a_link_to_the_connected_site_names_a_task(spec, expected):
    assert parse_task_spec(spec, INSTANCE) == expected


@pytest.mark.parametrize("spec", [
    "", "   ", "no task here", "https://evil.example.com/browse/PROJ-123",
    "https://docs.google.com/document/d/abc/edit", "https://acme.atlassian.net.evil.example/wiki/pages/1",
    f"{SITE.replace('https', 'http')}/browse/PROJ-123",
])
def test_anything_else_names_no_task(spec):
    assert parse_task_spec(spec, INSTANCE) is None


# ─── what it does ────────────────────────────────────────────────────


def test_the_agent_runs_forced_on_where_the_policy_leaves_it_off(check):
    out, logic, other = check("PROJ-6066", {"enabled": True, "disabled_agents": ["business_logic"],
                                            "enabled_agents": [], "business_logic_auto": "off"})
    assert len(logic.contexts) == 1 and other.calls == 0
    assert out.error == "" and out.markdown.startswith(
        "## Business-logic check — [PROJ-6066](https://acme.atlassian.net/browse/PROJ-6066)")
    assert "No gap found between this change and the task." in out.markdown


def test_the_agent_sees_the_task_that_was_named_not_the_one_the_pr_names(check):
    pr = _pr(title="PROJ-7 something else")
    _, logic, _ = check("PROJ-6066", pr=pr)
    task = logic.contexts[0].task_context
    assert [t.key for t in task.tasks] == ["PROJ-6066"] and task.explicit


def test_a_named_task_of_an_allowed_project_is_read(check):
    out, logic, _ = check("PROJ-6066", {"enabled": True, "task_project_keys": ["AIR", "proj"]})
    assert out.error == "" and len(logic.contexts) == 1


def test_a_named_task_outside_the_project_allowlist_is_refused_and_not_fetched(check, site):
    out, logic, _ = check("PROJ-6066", {"enabled": True, "task_project_keys": ["AIR"]})
    assert logic.contexts == [] and site.calls == []
    assert "task_project_keys" in out.markdown
    assert "Gap" not in out.markdown and "Cutting" not in out.markdown


def test_an_empty_allowlist_limits_nothing(check):
    out, logic, _ = check("PROJ-7", {"enabled": True, "task_project_keys": []})
    assert out.error == "" and len(logic.contexts) == 1


def test_a_page_is_refused_while_the_repository_limits_tasks_to_projects(check, site):
    link = f"{SITE}/wiki/spaces/ENG/pages/4242/Cutting+spec"
    out, logic, _ = check(link, {"enabled": True, "task_urls_enabled": True,
                                 "task_project_keys": ["PROJ"]})
    assert logic.contexts == [] and "task_project_keys" in out.markdown
    assert site.count("/wiki/pages/") == 0


def test_the_reason_a_check_did_not_run_is_in_the_review_language(check, site):
    out = on_demand.run_business_logic_check(
        _pr(), "the thing we discussed", workspace_id="ws-a", policy={"enabled": True},
        orchestrator=ReviewOrchestrator(agents=[_Logic()]), language="uk")
    assert "Перевірка не запускалася: з наданого тексту" in out.markdown


def test_a_link_without_a_jira_connection_says_there_is_no_connection(monkeypatch, check):
    monkeypatch.setattr(service, "load_connection", lambda w, store=None: None)
    out, logic, _ = check(f"{SITE}/browse/PROJ-6066")
    assert logic.contexts == [] and "no Jira connection" in out.markdown


def test_without_a_spec_the_task_is_the_one_the_pr_names(check):
    out, logic, _ = check("", pr=_pr(title="PROJ-6066 cut profiles"))
    assert [t.key for t in logic.contexts[0].task_context.tasks] == ["PROJ-6066"]
    assert out.error == ""


def test_without_a_spec_and_without_a_key_the_answer_says_so(check):
    out, logic, _ = check("", pr=_pr(title="tidy up"))
    assert logic.contexts == [] and out.error.startswith("no Jira key")
    assert out.markdown.startswith("## Business-logic check\n\nThe check did not run: no Jira key")


def test_findings_and_the_checklist_are_in_the_answer(check):
    llm = _Llm(json.dumps([
        {"id": "PROJ-6066:AC1", "verdict": "met", "evidence": "src/export.py:1"},
        {"id": "PROJ-6066:AC2", "verdict": "met", "evidence": "src/export.py:1"}]))
    out, _, _ = check("PROJ-6066", logic=_Logic([_gap()]), llm=llm)
    assert "### Gaps found" in out.markdown
    assert "🟠 `src/export.py:1` **Short offcuts kept**" in out.markdown
    assert "### Requirements check — " in out.markdown and "❌ **AC2**" in out.markdown
    assert [r.verdict for r in out.requirements] == ["met", "missing"]
    assert out.findings == [_gap()] or len(out.findings) == 1
    assert llm.calls == 1


def test_the_findings_mode_asks_the_model_nothing_more(check):
    llm = _Llm()
    out, _, _ = check("PROJ-6066", {"enabled": True, "requirements_check_mode": "findings"},
                      logic=_Logic([_gap()]), llm=llm)
    assert llm.calls == 0 and "not checked one by one" in out.markdown


def test_the_requirements_off_leaves_only_the_findings(check):
    out, _, _ = check("PROJ-6066", {"enabled": True, "requirements_check_mode": "off"},
                      logic=_Logic([_gap()]))
    assert "### Requirements check" not in out.markdown and out.requirements == []


def test_the_answer_says_it_changed_nothing(check):
    out, _, _ = check("PROJ-6066")
    assert out.markdown.endswith(
        "_One-off check of commit `abcdef123456`. Nothing was added to the code and the "
        "review summary is unchanged._")


def test_the_answer_speaks_the_repository_language(monkeypatch, site):
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client", lambda self, *a, **k: (None, {}))
    out = on_demand.run_business_logic_check(
        _pr(), "PROJ-6066", workspace_id="ws-a", policy={"enabled": True},
        orchestrator=ReviewOrchestrator(agents=[_Logic()]), language="uk")
    assert out.markdown.startswith("## Перевірка бізнес-логіки — ")


# ─── what goes wrong ─────────────────────────────────────────────────


def test_a_task_jira_cannot_show_is_a_sentence_and_the_agent_never_runs(check, site):
    site.status_for["PROJ-6066"] = 403
    out, logic, _ = check("PROJ-6066")
    assert logic.contexts == []
    assert "The check did not run: PROJ-6066 could not be read: Jira returned 403" in out.markdown


def test_a_missing_connection_is_a_sentence(monkeypatch, check):
    monkeypatch.setattr(service, "load_connection", lambda w, store=None: None)
    out, logic, _ = check("PROJ-6066")
    assert logic.contexts == [] and "no Jira connection" in out.markdown


def test_switching_the_task_reading_off_is_respected_and_said(check):
    out, logic, _ = check("PROJ-6066", {"enabled": True, "task_context_enabled": False})
    assert logic.contexts == [] and "switched off for this repository" in out.markdown


def test_words_that_name_no_task_are_a_sentence(check):
    out, logic, _ = check("the thing we discussed")
    assert logic.contexts == [] and "no Jira key or link" in out.markdown


def test_a_skip_reason_from_the_agent_is_passed_on(check):
    out, _, _ = check("PROJ-6066", logic=_Logic(skip="nothing is stated"))
    assert "The check did not run: nothing is stated" in out.markdown


def test_a_failed_agent_is_a_curated_sentence_never_the_providers_words(check):
    out, _, _ = check("PROJ-6066", logic=_Logic(error="litellm 401 sk-secret-abc leaked"))
    assert "The check could not be finished: " in out.markdown
    assert "sk-secret-abc" not in out.markdown and out.error


def test_an_exception_while_building_the_context_is_a_sentence(monkeypatch, site):
    def boom(self, *a, **k):
        raise RuntimeError("graph exploded with password=hunter2")

    monkeypatch.setattr(ReviewOrchestrator, "_build_context", boom)
    out = on_demand.run_business_logic_check(
        _pr(), "PROJ-6066", workspace_id="ws-a", policy={"enabled": True},
        orchestrator=ReviewOrchestrator(agents=[_Logic()]), language="en")
    assert "hunter2" not in out.markdown and out.error.startswith("internal error")


# ─── a Confluence page of the same site ──────────────────────────────


def test_a_page_link_is_refused_until_the_repository_allows_it(check, site):
    link = f"{SITE}/wiki/spaces/ENG/pages/4242/Cutting+spec"
    out, logic, _ = check(link)
    assert logic.contexts == [] and "task_urls_enabled" in out.markdown
    assert site.count("/wiki/pages/") == 0, "the page was fetched although it is not allowed"


def test_an_allowed_page_is_read_with_the_connections_own_credentials(check, site):
    link = f"{SITE}/wiki/spaces/ENG/pages/4242/Cutting+spec"
    out, logic, _ = check(link, {"enabled": True, "task_urls_enabled": True})
    task = logic.contexts[0].task_context
    assert [t.key for t in task.tasks] == ["PAGE-4242"]
    assert task.tasks[0].summary == "Cutting spec"
    assert [c.id for c in task.tasks[0].criteria] == ["AC1"]
    assert site.count("/wiki/pages/4242") == 1
    assert out.markdown.startswith("## Business-logic check — [PAGE-4242](")


def test_a_page_the_token_cannot_see_is_a_sentence(check, site):
    link = f"{SITE}/wiki/spaces/ENG/pages/9999/Gone"
    out, logic, _ = check(link, {"enabled": True, "task_urls_enabled": True})
    assert logic.contexts == [] and "the page is missing" in out.markdown


def test_a_link_to_another_site_is_never_fetched(check, site):
    out, logic, _ = check("https://evil.example.com/wiki/spaces/X/pages/4242/A",
                          {"enabled": True, "task_urls_enabled": True})
    assert logic.contexts == [] and site.calls == []
    assert "no Jira key or link" in out.markdown


# ─── the single-agent run itself ─────────────────────────────────────


def test_a_single_agent_run_posts_nothing_and_records_nothing(monkeypatch, site):
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client", lambda self, *a, **k: (None, {}))
    recorded: list = []
    monkeypatch.setattr("src.review.issues.record_review_run",
                        lambda *a, **k: recorded.append(1) or True)
    provider = SimpleNamespace(post_review=lambda *a, **k: recorded.append("post"),
                               upsert_status_comment=lambda *a, **k: recorded.append("status"))
    logic, other = _Logic([_gap()]), _Other()
    orch = ReviewOrchestrator(agents=[other, logic])
    run = orch.run_single_agent(_pr(), "business_logic", policy={"enabled": True},
                                workspace_id="ws-a", provider=provider)
    assert recorded == [] and other.calls == 0
    assert [f.title for f in run.result.findings] == ["Short offcuts kept"]


def test_a_finding_in_a_file_the_pr_did_not_change_is_dropped(monkeypatch, site):
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client", lambda self, *a, **k: (None, {}))
    stray = Finding(file_path="src/elsewhere.py", line=3, severity=FindingSeverity.ERROR,
                    title="stray", agent="business_logic")
    orch = ReviewOrchestrator(agents=[_Logic([_gap(), stray])])
    run = orch.run_single_agent(_pr(), "business_logic", policy={"enabled": True},
                                workspace_id="ws-a")
    assert [f.file_path for f in run.result.findings] == ["src/export.py"]


def test_an_unknown_agent_name_is_an_error_not_a_crash(monkeypatch, site):
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client", lambda self, *a, **k: (None, {}))
    run = ReviewOrchestrator(agents=[_Other()]).run_single_agent(
        _pr(), "business_logic", policy={"enabled": True}, workspace_id="ws-a")
    assert run.result.error and run.result.findings == []
