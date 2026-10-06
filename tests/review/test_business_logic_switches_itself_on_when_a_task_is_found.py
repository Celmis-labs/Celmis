"""`business_logic_auto` lets a found Jira task turn the agent on, and nothing else does.

The business-logic category is off until someone opts in, because without a
statement it has nothing to say. With `when_task_found` a pull request that
names a task Jira could show gets the check without anyone switching it on, and
a pull request that names none costs nothing: Jira is not even asked when the
agent could not run anyway.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Hunk, PullRequest
from src.review.orchestrator import ReviewOrchestrator
from src.review.task_context import service
from tests.review import jira_fakes
from tests.review.jira_fakes import FakeJira, issue
from tests.review.test_the_business_logic_agent import _PassThroughVerifier


def _pr(title: str) -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=31, title=title, description="",
        author="a", base_ref="main", base_sha="x", head_ref="feat", head_sha="y",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1, old_count=1,
                    new_start=1, new_count=2, content="@@ -1 +1,2 @@\n+x = 1\n")],
    )


class _Agent(ReviewAgent):
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.saw_task = None

    def review(self, context: AgentContext) -> AgentRunResult:
        self.calls += 1
        self.saw_task = context.task_context
        return AgentRunResult(agent=self.name)


class _Provider:
    def __init__(self, title: str, commits: list[str] | None = None) -> None:
        self._title = title
        self.commit_calls = 0
        self._commits = commits or []

    def fetch_pull_request(self, repo, number):
        return _pr(self._title)

    def fetch_commit_messages(self, pr, limit=50):
        self.commit_calls += 1
        return self._commits

    def close(self):
        pass


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    jira_fakes.install(monkeypatch, tmp_path, fake)
    return fake


@pytest.fixture
def world(monkeypatch, site):
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client",
                        lambda self, *a, **k: (None, {}))

    def run(policy=None, *, title="PROJ-6066 cut profiles", connected=True, commits=None):
        if not connected:
            monkeypatch.setattr(service, "load_connection", lambda w, store=None: None)
        logic = _Agent("business_logic")
        provider = _Provider(title, commits)
        orch = ReviewOrchestrator(agents=[_Agent("defect"), logic],
                                  verifier=_PassThroughVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
        monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws: None)
        batch = orch.review("github", "acme/api", 31, dry_run=True, post_comments=False,
                            provider=provider, workspace_id="ws-a").batch
        return batch, logic, provider

    return run


def _policy(**fields) -> dict:
    return {"enabled": True, "target_branches": None, **fields}


def test_by_default_a_task_does_not_switch_the_agent_on(world, site):
    _, logic, _ = world(_policy())
    assert logic.calls == 0
    assert site.calls == [], "Jira was asked although the agent could not run"


def test_when_a_task_is_found_the_agent_runs_with_the_task(world):
    batch, logic, _ = world(_policy(business_logic_auto="when_task_found"))
    assert logic.calls == 1
    assert [t.key for t in logic.saw_task.tasks] == ["PROJ-6066"]
    assert batch.task_context is not None and batch.task_context.ok
    assert "business_logic" in batch.agents_run


def test_a_pull_request_that_names_no_task_leaves_the_agent_dormant(world):
    batch, logic, _ = world(_policy(business_logic_auto="when_task_found"), title="tidy up")
    assert logic.calls == 0
    assert batch.task_context.status == "no_key"


def test_a_task_that_cannot_be_read_does_not_switch_the_agent_on(world, site):
    site.status_for["PROJ-6066"] = 403
    batch, logic, _ = world(_policy(business_logic_auto="when_task_found"))
    assert logic.calls == 0
    assert batch.task_context.status == "forbidden"


def test_naming_the_agent_in_disabled_agents_beats_the_auto_setting(world, site):
    _, logic, _ = world(_policy(
        business_logic_auto="when_task_found", disabled_agents=["business_logic"]))
    assert logic.calls == 0
    assert site.calls == [], "Jira was asked although the agent is switched off"


def test_opting_in_without_the_auto_setting_still_hands_the_agent_its_task(world):
    batch, logic, _ = world(_policy(enabled_agents=["business_logic"]))
    assert logic.calls == 1 and logic.saw_task.ok


def test_without_a_jira_connection_the_agent_stays_dormant(world):
    batch, logic, _ = world(_policy(business_logic_auto="when_task_found"), connected=False)
    assert logic.calls == 0
    assert batch.task_context.status == "no_connection"


def test_switching_the_task_read_off_leaves_the_auto_setting_inert(world, site):
    _, logic, _ = world(_policy(
        business_logic_auto="when_task_found", task_context_enabled=False))
    assert logic.calls == 0 and site.calls == []


def test_commit_messages_are_fetched_only_when_the_pull_request_itself_names_nothing(world):
    _, _, provider = world(_policy(business_logic_auto="when_task_found"))
    assert provider.commit_calls == 0
    _, logic, provider = world(_policy(business_logic_auto="when_task_found"),
                               title="tidy", commits=["PROJ-6066 wip"])
    assert provider.commit_calls == 1 and logic.calls == 1


def _stored_row(*tasks):
    """The row's task_refs after one review run per entry of `tasks`."""
    from types import SimpleNamespace

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from src.db.models import ReviewIssue, ReviewPullRequest
    from src.review.issues import record_review_run
    from src.review.models import ReviewBatch, ReviewVerdict

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(engine)
    ReviewPullRequest.__table__.create(engine)
    for n, task in enumerate(tasks):
        batch = ReviewBatch(pull_request=_pr("PROJ-6066 cut"), verdict=ReviewVerdict.COMMENT)
        batch.task_context = task
        record_review_run(SimpleNamespace(batch=batch, posted=True, provider_response={}),
                          run_id=f"r{n}", workspace_id="ws", status="complete", engine=engine)
    with Session(engine) as s:
        return s.execute(select(ReviewPullRequest)).scalar_one().task_refs


def test_the_tasks_a_review_read_are_stored_on_the_pull_request_row():
    from src.review.task_context.models import TaskContext, TaskIssue

    ctx = TaskContext(status="ok", tasks=[TaskIssue(
        key="PROJ-1", url="https://acme.atlassian.net/browse/PROJ-1", summary="s",
        status="Open", issue_type="Story")])
    assert _stored_row(ctx) == [{
        "key": "PROJ-1", "url": "https://acme.atlassian.net/browse/PROJ-1",
        "summary": "s", "status": "Open", "issue_type": "Story"}]


def test_a_review_that_looked_and_found_no_task_stores_none():
    from src.review.task_context.models import TaskContext

    assert _stored_row(TaskContext(status="no_key")) is None


def test_a_review_that_did_not_look_leaves_the_stored_tasks_alone():
    assert _stored_row(None) is None


@pytest.mark.parametrize("status", ["error", "not_found", "forbidden"])
def test_a_jira_failure_keeps_the_tasks_an_earlier_review_stored(status):
    from src.review.task_context.models import TaskContext, TaskIssue

    read = TaskContext(status="ok", tasks=[TaskIssue(
        key="PROJ-1", url="https://acme.atlassian.net/browse/PROJ-1", summary="s",
        status="Open", issue_type="Story")])
    stored = _stored_row(read, TaskContext(status=status, note="Jira was down"))
    assert stored and stored[0]["key"] == "PROJ-1"


def test_a_review_that_finds_no_key_after_one_that_did_clears_the_stored_tasks():
    from src.review.task_context.models import TaskContext, TaskIssue

    read = TaskContext(status="ok", tasks=[TaskIssue(
        key="PROJ-1", url="https://acme.atlassian.net/browse/PROJ-1", summary="s",
        status="Open", issue_type="Story")])
    assert _stored_row(read, TaskContext(status="no_key")) is None
