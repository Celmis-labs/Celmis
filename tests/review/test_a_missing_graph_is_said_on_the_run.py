"""A review without a code graph says so — on the batch, the banner, the row.

On the benchmark this product was measured against, every one of 161 runs had
no graph: the forks were never indexed, the orchestrator's own lookup returned
"(repo not indexed …)" as the summary text and 0 as the caller count, and
nothing — not the run row, not the PR comment, not the API — distinguished
"no blast radius" from "an empty blast radius". The comparison with Kodus and
cubic was run with the product's main differentiator switched off, and nobody
could tell from the record.

Pinned here, from the lookup to the banner:

  * `_build_context` hands the agents what `build_graph_context` found — the
    full summary to the agents that reason about impact, the brief to the rest,
    the caller count to the batch — and the orchestrator owns no second lookup;
  * the brief reaches the quality and tests prompts under its own heading, the
    summary still reaches architect and security, and an empty one prints the
    template's own "(no graph context)" rather than a blank section;
  * a context whose graph could not answer carries a note, and the orchestrator
    appends it to the same list a dropped temperature rides, so the banner
    prints it, `adjustments_payload` persists it, and a context with a graph
    adds nothing.
"""

from __future__ import annotations

import pytest

import src.review.orchestrator as orchestrator_mod
from src.api.review_runs import adjustments_payload
from src.llm.capabilities import ParameterAdjustment
from src.review.agents import ContractAgent, DefectAgent
from src.review.agents.base import AgentContext, AgentRunResult
from src.review.agents.security import SecurityAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.graph_context import (
    ADJUST_PARTIAL,
    ADJUST_UNAVAILABLE,
    GRAPH_REQUESTED,
    PARAM_GRAPH_CONTEXT,
    STATUS_NOT_INDEXED,
    STATUS_OK,
    GraphContext,
)
from src.review.models import Hunk, PullRequest, ReviewBatch
from src.review.orchestrator import ReviewOrchestrator

NOT_INDEXED = ParameterAdjustment(
    agent=None, parameter=PARAM_GRAPH_CONTEXT, requested=GRAPH_REQUESTED,
    sent=None, action=ADJUST_UNAVAILABLE,
    reason="repository not indexed; run `analyzer generate` or index it from "
           "the Repositories page (POST /api/repos/index-all)",
)
PARTIAL = ParameterAdjustment(
    agent=None, parameter=PARAM_GRAPH_CONTEXT, requested=GRAPH_REQUESTED,
    sent="3 of 5 changed files", action=ADJUST_PARTIAL,
    # Shaped like what `_missing_reason` now emits: one clause per cause, and
    # the re-index named only for the half it can reach. The sentence this
    # replaced ("the index predates them — re-run `analyzer generate`") was
    # attached to both halves and was false for the second.
    reason="2 of 5 changed files have no symbols in the index; 1 of them is "
           "still in the checkout the index was built from (src/other.py) — the "
           "index is stale there; 1 of them is not in that checkout at all "
           "(src/gone.py) — this PR's base is older than the indexed revision, "
           "so no re-index can bring it back",
)


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


class _PassThroughVerifier:
    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):  # pragma: no cover - not posted
        return {}

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    """The real orchestrator with the context handed in — the loop under test
    is the one that reads the context, not the one that builds it."""
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    def _run(context_of) -> ReviewBatch:
        orch = ReviewOrchestrator(agents=[], verifier=_PassThroughVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(orch, "_build_context", lambda pr, **kw: context_of(pr))
        return orch.review(
            "github", "acme/api", 7, dry_run=True, post_comments=False,
            provider=_Provider(),
        ).batch

    return _run


# ─── the note reaches the batch, the banner and the row ──────────────


def test_a_review_without_a_graph_carries_the_note(run):
    batch = run(lambda pr, **_kw: AgentContext(pull_request=pr, graph_note=NOT_INDEXED))
    assert NOT_INDEXED in batch.parameter_adjustments
    notice = batch.adjustments_notice
    assert "graph context unavailable" in notice
    assert "not indexed" in notice
    assert "analyzer generate" in notice, "the remedy travels with the note"
    # The banner the PR comment prints is built from the same list.
    assert "graph context unavailable" in batch.summary


def test_the_note_is_persisted_as_data(run):
    batch = run(lambda pr, **_kw: AgentContext(pull_request=pr, graph_note=NOT_INDEXED))
    rows = adjustments_payload(batch)
    graph_rows = [r for r in rows if r["parameter"] == PARAM_GRAPH_CONTEXT]
    assert len(graph_rows) == 1
    assert graph_rows[0]["action"] == ADJUST_UNAVAILABLE
    assert graph_rows[0]["agent"] is None, "the graph is a stage, not an agent"


def test_a_partial_graph_says_how_partial(run):
    batch = run(lambda pr, **_kw: AgentContext(pull_request=pr, graph_note=PARTIAL))
    assert "graph context partial (3 of 5 changed files)" in batch.adjustments_notice
    assert "no re-index can bring it back" in batch.adjustments_notice


def test_a_review_with_a_graph_adds_nothing(run):
    batch = run(lambda pr, **_kw: AgentContext(
        pull_request=pr, graph_summary="Symbols in changed files (3 found)",
        cross_repo_callers_count=2,
    ))
    assert not [a for a in batch.parameter_adjustments
                if getattr(a, "parameter", None) == PARAM_GRAPH_CONTEXT]
    assert "graph context" not in batch.adjustments_notice
    assert batch.cross_repo_callers == 2


# ─── the lookup is the module's, and every rendering reaches the context ──


def _quiet(orch, monkeypatch):
    """Everything `_build_context` reaches for besides the graph, stubbed."""
    monkeypatch.setattr(orch, "_build_cross_repo_drift", lambda pr, **_kw: "")
    monkeypatch.setattr(orch, "_build_custom_rules", lambda pr, policy: "")
    monkeypatch.setattr(orch, "_build_mcp_evidence", lambda pr, user_id: "")
    monkeypatch.setattr(orch, "_build_llm_client", lambda u, w, p: (None, {}))
    monkeypatch.setattr(orch, "_load_repo_overview", lambda pr, **_kw: "")
    monkeypatch.setattr(orch, "_load_style_guide", lambda pr, **_kw: "")


def test_build_context_hands_the_agents_what_the_graph_found(monkeypatch):
    graph = GraphContext(
        status=STATUS_OK, summary="FULL SUMMARY", brief="THREE LINES",
        cross_repo_callers_count=4,
    )
    seen: list[PullRequest] = []

    def _fake(pr, **kw):
        seen.append(pr)
        return graph

    monkeypatch.setattr(orchestrator_mod, "build_graph_context", _fake)
    orch = ReviewOrchestrator(agents=[], verifier=_PassThroughVerifier())
    _quiet(orch, monkeypatch)
    ctx = orch._build_context(_pr(), policy=None)
    assert seen and seen[0].number == 7
    assert ctx.graph_summary == "FULL SUMMARY"
    assert ctx.graph_brief == "THREE LINES"
    assert ctx.graph_note is None
    assert ctx.cross_repo_callers_count == 4


def test_build_context_hands_on_the_note_when_there_is_no_graph(monkeypatch):
    graph = GraphContext(status=STATUS_NOT_INDEXED, note=NOT_INDEXED)
    monkeypatch.setattr(orchestrator_mod, "build_graph_context", lambda pr, **kw: graph)
    orch = ReviewOrchestrator(agents=[], verifier=_PassThroughVerifier())
    _quiet(orch, monkeypatch)
    ctx = orch._build_context(_pr(), policy=None)
    assert ctx.graph_note is NOT_INDEXED
    assert ctx.graph_summary == "" and ctx.graph_brief == ""
    assert ctx.cross_repo_callers_count == 0


def test_the_orchestrator_owns_no_second_lookup():
    """Two lookups drift — the one in the orchestrator capped at five files
    and LIMIT 50 for as long as it existed. There is one now."""
    assert not hasattr(ReviewOrchestrator, "_build_graph_context")
    assert not hasattr(ReviewOrchestrator, "_query_cross_repo_callers")


# ─── every agent reads the graph in its own size ─────────────────────


def test_the_defect_agent_gets_no_graph_at_all():
    """It used to get the BRIEF, on the theory that a little structural
    context helps where the full summary would drown.

    Reading the brief killed that theory: every line of it is about OTHER
    files — caller counts, cross-repo references, most-depended-on symbols —
    and this agent's boundary rule forbids acting on any of it. A claim only
    true because of what another file contains belongs to the contract agent.
    So the brief was cross-file evidence handed to the one agent told not to
    use it, and the 50-PR bench measured reviews with a COMPLETE graph at
    40.2% precision against 51.1% with a partial one.
    """
    prompt = DefectAgent(passes=1)._build_prompt(AgentContext(
        pull_request=_pr(),
        graph_brief="Most depended-on: `parse` (12 callers)",
        graph_summary="THE FULL SUMMARY",
    ))
    assert "Blast radius" not in prompt
    assert "Most depended-on" not in prompt
    assert "THE FULL SUMMARY" not in prompt


def test_the_diff_and_the_style_guide_still_reach_it():
    """The removal is of cross-file context, not of context."""
    prompt = DefectAgent(passes=1)._build_prompt(AgentContext(
        pull_request=_pr(), style_guide="TABS NOT SPACES",
    ))
    assert "TABS NOT SPACES" in prompt
    assert "## Diff" in prompt


@pytest.mark.parametrize("agent_cls", [ContractAgent, SecurityAgent])
def test_the_summary_still_reaches_the_agents_that_reason_about_impact(agent_cls):
    ctx = AgentContext(
        pull_request=_pr(), graph_summary="Symbols in changed files (3 found)",
        graph_brief="THE BRIEF",
    )
    prompt = agent_cls()._build_prompt(ctx)
    assert "Symbols in changed files (3 found)" in prompt


def test_the_contract_agent_still_says_when_the_graph_is_missing():
    """The placeholder still has a reader — the agent whose claims are ABOUT
    the graph has to be able to tell "no callers" from "nobody looked"."""
    prompt = ContractAgent()._build_prompt(AgentContext(pull_request=_pr()))
    assert "(no graph context)" in prompt
