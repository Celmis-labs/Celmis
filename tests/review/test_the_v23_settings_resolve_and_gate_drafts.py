"""The 2.3.0 review settings resolve like every other one, and drafts obey theirs.

    repo policy (non-null) > workspace default (non-null) > built-in

Asserted for EVERY new key on the pure resolver the API reports from, on the
policy dict the orchestrator reads (`_resolved_policy` + `_policy_setting`),
and — for the one behaviour this change owns — on the real review loop and
the webhook: a draft is skipped unless `run_on_drafts` says otherwise.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import VerifierAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest, ReviewVerdict
from src.review.orchestrator import ReviewOrchestrator, _policy_setting
from src.review.review_defaults import (
    AGENT_PARTICIPATION_DEFAULTS,
    BUILTIN_DEFAULTS,
    INHERITABLE_FIELDS,
    V23_FIELDS,
    agent_participation,
    install_defaults,
    merge_policy,
    message_template_error,
    render_message_template,
    resolve,
)
from src.review.settings import ReviewSettings

INSTALL = install_defaults(ReviewSettings())

#: The contract with the agents implementing the behaviours: these names,
#: these built-ins. A rename here is a rename they must hear about.
EXPECTED_BUILTINS = {
    "enabled_agents": [],
    "run_on_drafts": False,
    "approve_when_clean": False,
    "request_changes_on_critical": False,
    "status_feedback": True,
    "committable_suggestions": False,
    "apply_filters_to_rules": True,
    "summary_target": "comment",
    "summary_on_new_commits": "replace",
    "summary_existing_description": "append",
    "base_instruction": None,
    "message_started": None,
    "message_finished_header": None,
}

#: Per key: (workspace value, repo value) — each different from the built-in
#: and from each other, so a test can tell the three layers apart.
LAYER_VALUES = {
    "enabled_agents": (["business_logic"], []),
    "run_on_drafts": (True, False),
    "approve_when_clean": (True, False),
    "request_changes_on_critical": (True, False),
    "status_feedback": (False, True),
    "committable_suggestions": (True, False),
    "apply_filters_to_rules": (False, True),
    "summary_target": ("description", "comment"),
    "summary_on_new_commits": ("append", "nothing"),
    "summary_existing_description": ("complement", "replace"),
    "base_instruction": ("Be terse.", "Explain the why."),
    "message_started": ("Reviewing {commit}", "On it: {files} files"),
    "message_finished_header": ("Done #{pr_number}", "Review of {commit}"),
}


def test_the_new_keys_and_their_builtins_are_the_promised_ones():
    assert tuple(EXPECTED_BUILTINS) == V23_FIELDS
    assert set(V23_FIELDS) <= set(INHERITABLE_FIELDS)
    for name, value in EXPECTED_BUILTINS.items():
        assert BUILTIN_DEFAULTS[name] == value, name
        assert INSTALL[name] == value, name


@pytest.mark.parametrize("name", sorted(LAYER_VALUES))
def test_each_key_resolves_repo_over_workspace_over_builtin(name):
    ws_value, repo_value = LAYER_VALUES[name]

    values, sources = resolve(None, None, INSTALL)
    assert (values[name], sources[name]) == (EXPECTED_BUILTINS[name], "install")

    values, sources = resolve(None, {name: ws_value}, INSTALL)
    assert (values[name], sources[name]) == (ws_value, "workspace")

    # A repository that says nothing (None) inherits the workspace's…
    values, sources = resolve(SimpleNamespace(**{name: None}), {name: ws_value}, INSTALL)
    assert (values[name], sources[name]) == (ws_value, "workspace")

    # …and one that answers keeps its answer, False / [] included.
    values, sources = resolve({name: repo_value}, {name: ws_value}, INSTALL)
    assert (values[name], sources[name]) == (repo_value, "repo")


@pytest.mark.parametrize("name", ["base_instruction", "message_started",
                                  "message_finished_header"])
def test_a_blank_text_inherits_rather_than_silencing(name):
    ws_value, _ = LAYER_VALUES[name]
    values, sources = resolve({name: "   "}, {name: ws_value}, INSTALL)
    assert (values[name], sources[name]) == (ws_value, "workspace")
    # The same on the policy dict a review reads.
    merged = merge_policy({"enabled": True, name: ""}, {name: ws_value})
    assert merged[name] == ws_value


@pytest.mark.parametrize("name", sorted(LAYER_VALUES))
def test_the_review_reads_every_key_through_the_resolved_policy(monkeypatch, name):
    """`_resolved_policy` carries every key, and `_policy_setting` answers the
    built-in when nothing does — the call the behaviour agents will make."""
    ws_value, repo_value = LAYER_VALUES[name]
    orch = ReviewOrchestrator(settings=ReviewSettings(), agents=[], verifier=VerifierAgent())

    def resolved(repo, ws):
        monkeypatch.setattr(orch, "_load_policy", lambda slug: repo)
        monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws_id: ws)
        return orch._resolved_policy("acme/api", "ws-1")

    assert _policy_setting(resolved(None, None), name) == EXPECTED_BUILTINS[name]
    policy = resolved(None, {name: ws_value})
    assert name in policy and _policy_setting(policy, name) == ws_value
    repo = {"enabled": True, "workspace_id": "ws-1", name: None}
    assert _policy_setting(resolved(repo, {name: ws_value}), name) == ws_value
    repo[name] = repo_value
    assert _policy_setting(resolved(repo, {name: ws_value}), name) == repo_value


def test_load_policy_carries_every_new_key(monkeypatch, tmp_path):
    """Through the real row → dict loader, on SQLite."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _json(type_, compiler, **kw):  # pragma: no cover
        return "JSON"

    from src.db.models import RepoReviewPolicy

    url = f"sqlite:///{tmp_path}/p.db"
    engine = sa.create_engine(url)
    RepoReviewPolicy.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(RepoReviewPolicy.__table__.insert().values(
            repo_slug="github_acme-api", workspace_id="ws-1", enabled=True,
            prompt_template="", folder_rules=[], agent_prompt_overrides={},
            mcp_sources=[], run_on_drafts=True, summary_target="description",
            base_instruction="  ", enabled_agents=["business_logic"],
            performance_model="gpt-x"))
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    policy = ReviewOrchestrator(
        settings=ReviewSettings(), agents=[], verifier=VerifierAgent(),
    )._load_policy("github_acme-api")
    assert set(V23_FIELDS) <= set(policy)
    assert policy["run_on_drafts"] is True
    assert policy["summary_target"] == "description"
    assert policy["base_instruction"] is None          # blank = inherit
    assert policy["approve_when_clean"] is None        # unanswered = inherit
    assert policy["enabled_agents"] == ["business_logic"]
    assert policy["performance_model"] == "gpt-x"


# ─── agent participation ─────────────────────────────────────────────


def test_the_new_agents_have_explicit_defaults():
    assert AGENT_PARTICIPATION_DEFAULTS["performance"] is True
    assert AGENT_PARTICIPATION_DEFAULTS["business_logic"] is False
    on = agent_participation([], [])
    assert on["performance"] is True and on["business_logic"] is False
    assert on["defect"] is True


def test_an_opt_in_agent_runs_only_when_enabled_and_never_when_disabled():
    assert agent_participation(None, ["business_logic"])["business_logic"] is True
    assert agent_participation(["business_logic"], ["business_logic"])[
        "business_logic"] is False
    assert agent_participation(["performance"], None)["performance"] is False


def test_a_list_written_before_the_new_agents_keeps_its_meaning():
    """["cve"] stored in 2.2 meant "everything but cve" — and business_logic
    did not exist. It must not be switched on by that old list."""
    on = agent_participation(["cve"], None)
    assert on["cve"] is False and on["business_logic"] is False
    assert on["performance"] is True


# ─── message templates ───────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Reviewing {commit} with {agents} over {files} files (#{pr_number})",
    "No placeholders at all", "Literal {{braces}} are fine",
])
def test_a_valid_template_passes_and_renders(text):
    assert message_template_error(text) is None
    out = render_message_template(text, {"commit": "abc1234", "agents": "defect",
                                         "files": 3, "pr_number": 7})
    assert "{commit}" not in out


@pytest.mark.parametrize("text", ["Hi {author}", "Bad { brace", "{commit!r}",
                                  "{files:>4}", "{0}"])
def test_an_unknown_or_malformed_placeholder_is_refused(text):
    assert message_template_error(text)


def test_rendering_never_raises_in_a_review():
    assert render_message_template("Hi {author}", {}) == "Hi {author}"
    assert render_message_template("{commit}", {}) == ""


# ─── the drafts gate, on the real review loop ────────────────────────


def _pr(draft: bool) -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open", is_draft=draft,
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
    def __init__(self, draft: bool) -> None:
        self.draft = draft

    def fetch_pull_request(self, repo, number):
        return _pr(self.draft)

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

    def _run(*, draft, policy, workspace, agents=("defect",)):
        llm = MagicMock()
        orch = ReviewOrchestrator(
            settings=ReviewSettings(), agents=[_Canned(a) for a in agents],
            verifier=VerifierAgent(),
        )
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
        monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws_id: workspace)
        monkeypatch.setattr(
            orch, "_build_context",
            lambda pr, **kw: AgentContext(pull_request=pr, llm_client=llm),
        )
        return orch.review(
            "github", "acme/api", 7, dry_run=True, post_comments=False,
            provider=_Provider(draft), workspace_id="ws-1",
        ).batch

    return _run


def _repo(**fields) -> dict:
    return {"enabled": True, "target_branches": None, "disabled_agents": None,
            "workspace_id": "ws-1", **fields}


def test_a_draft_is_skipped_by_default(run):
    batch = run(draft=True, policy=None, workspace=None)
    assert batch.verdict == ReviewVerdict.SKIPPED
    assert "draft" in batch.summary.lower()


@pytest.mark.parametrize("policy, workspace", [
    (None, {"run_on_drafts": True}),
    (_repo(run_on_drafts=None), {"run_on_drafts": True}),
    (_repo(run_on_drafts=True), None),
    (_repo(run_on_drafts=True), {"run_on_drafts": False}),
])
def test_a_draft_is_reviewed_when_run_on_drafts_says_so(run, policy, workspace):
    batch = run(draft=True, policy=policy, workspace=workspace)
    assert batch.verdict != ReviewVerdict.SKIPPED
    assert {f.agent for f in batch.findings} == {"defect"}


def test_a_repo_can_keep_skipping_drafts_over_the_workspace(run):
    batch = run(draft=True, policy=_repo(run_on_drafts=False),
                workspace={"run_on_drafts": True})
    assert batch.verdict == ReviewVerdict.SKIPPED


def test_a_ready_pr_is_reviewed_whatever_the_setting(run):
    batch = run(draft=False, policy=_repo(run_on_drafts=False), workspace=None)
    assert batch.verdict != ReviewVerdict.SKIPPED


def test_an_opt_in_agent_is_skipped_until_enabled(run):
    """business_logic is dispatched by the roster but off by default; the
    workspace's enabled_agents switches it on, a repo's disabled_agents off."""
    agents = ("defect", "business_logic")
    batch = run(draft=False, policy=None, workspace=None, agents=agents)
    assert {f.agent for f in batch.findings} == {"defect"}
    batch = run(draft=False, policy=None,
                workspace={"enabled_agents": ["business_logic"]}, agents=agents)
    assert {a for f in batch.findings for a in f.agent.split(",")} == set(agents)
    batch = run(draft=False, policy=_repo(disabled_agents=["business_logic"]),
                workspace={"enabled_agents": ["business_logic"]}, agents=agents)
    assert {f.agent for f in batch.findings} == {"defect"}
