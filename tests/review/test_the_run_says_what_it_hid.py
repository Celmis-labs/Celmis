"""What a run hid before posting reaches the run record, by cause.

A filter that can only say "dropped 7" is the shape that let the LLM veto
delete true positives for five benchmark runs while reading as a success.
The deny-list count per rule existed on the batch and went nowhere: not the
row, not the API, not the page — "what was hidden and why" lived in two log
lines. The parser's evidence gate counted its refusals on each agent's result
and nothing summed them.

Pinned here, from the agents to the wire:

  * the aggregation loop sums every agent's evidence refusals onto the batch,
    a FAILED agent's included — it refused them on its way to failing;
  * the prefilter's counts and the veto's count land on the batch, and a veto
    the policy switched off leaves its count at zero rather than unknown;
  * `hidden_payload` is None for a batch that never carried the counts (an
    engine or a double from before they existed) and a full dict of zeros for
    one that carried them and hid nothing — the two are different answers;
  * the column arrives by the same additive migration the rosters did, NULL
    stays "not recorded", and the API serves the dict on the run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.review_runs import (
    ReviewRun,
    ReviewRunStore,
    hidden_payload,
    record_completed_review,
)
from src.api.routers.reviews import _run_to_out
from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest, ReviewBatch
from src.review.orchestrator import ReviewOrchestrator


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


def _finding(agent: str, line: int = 1) -> Finding:
    return Finding(
        agent=agent, file_path="src/foo.py", line=line, severity=FindingSeverity.WARNING,
        title="t", body="b", confidence=0.9, reasoning="because",
    )


class _Canned(ReviewAgent):
    def __init__(self, name: str, **result) -> None:
        self.name = name
        self._result = result

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, **self._result)


class _CountingVerifier:
    """Both halves with their counts filled in, so the batch under test is
    shaped by what the stage reports and nothing else."""

    def __init__(self, *, by_rule=None, dedup=0, near=0, low=0, veto=0) -> None:
        self.by_rule = dict(by_rule or {})
        self.dedup, self.near, self.low, self.veto = dedup, near, low, veto
        self.llm_calls = 0

    def prefilter(self, findings, **_):
        return PrefilterResult(
            kept=list(findings), dropped_by_rule=dict(self.by_rule),
            dropped_dedup=self.dedup, dropped_near_duplicate=self.near,
            dropped_low_confidence=self.low,
        )

    def llm_pass(self, findings, context):
        self.llm_calls += 1
        return VerifierResult(kept=list(findings), dropped_llm_filter=self.veto)


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

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    def _run(*agents, verifier=None, policy=None) -> ReviewBatch:
        orch = ReviewOrchestrator(agents=list(agents), verifier=verifier or _CountingVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        return orch.review(
            "github", "acme/api", 7, dry_run=True, post_comments=False,
            provider=_Provider(),
        ).batch

    return _run


@pytest.fixture
def store(tmp_path: Path) -> ReviewRunStore:
    return ReviewRunStore(tmp_path / "review_runs.db")


@dataclass
class _Result:
    batch: ReviewBatch
    posted: bool = True


# ─── the loop sums what the agents refused ───────────────────────────


def test_evidence_refusals_are_summed_onto_the_batch(run):
    batch = run(
        _Canned("architect", findings=[_finding("architect")], dropped_no_evidence=2),
        _Canned("quality", findings=[_finding("quality", 2)], dropped_no_evidence=1),
        _Canned("tests", findings=[]),
    )
    assert batch.dropped_no_evidence == 3


def test_a_failed_agents_refusals_still_count(run):
    """It refused two claims, then died on the corrective retry. The two were
    refused all the same, and a record that forgets them because the agent
    later failed is a record that says the agent found nothing to refuse."""
    batch = run(
        _Canned("security", error="boom", dropped_no_evidence=2),
        _Canned("quality", findings=[_finding("quality")], dropped_no_evidence=1),
    )
    assert "security" in batch.agents_failed
    assert batch.dropped_no_evidence == 3


# ─── the prefilter's and the veto's counts land on the batch ─────────


def test_the_prefilters_counts_reach_the_batch(run):
    verifier = _CountingVerifier(
        by_rule={"quality.todo": 2, "tests.no-coverage": 1}, dedup=3, near=1, low=2, veto=4,
    )
    # The veto is off unless a repository asks — `ReviewSettings.
    # verifier_enabled`. This test is about the counts a veto that RAN
    # produces, so the repository has to ask.
    batch = run(
        _Canned("quality", findings=[_finding("quality")]), verifier=verifier,
        policy={"enabled": True, "target_branches": [], "verifier_enabled": True},
    )
    assert batch.dropped_by_rule == {"quality.todo": 2, "tests.no-coverage": 1}
    assert batch.dropped_duplicates == 3
    assert batch.dropped_near_duplicates == 1
    assert batch.dropped_low_confidence == 2
    assert batch.dropped_by_veto == 4
    assert verifier.llm_calls == 1


def test_a_veto_switched_off_hid_nothing_by_veto(run):
    verifier = _CountingVerifier(by_rule={"quality.todo": 2}, dedup=1, veto=4)
    batch = run(
        _Canned("quality", findings=[_finding("quality")]), verifier=verifier,
        policy={"enabled": True, "target_branches": [], "disabled_agents": ["verifier"]},
    )
    assert verifier.llm_calls == 0
    assert batch.dropped_by_veto == 0, "a pass that never ran dropped nothing"
    assert batch.dropped_duplicates == 1, "the prefilter still ran"
    assert batch.dropped_by_rule == {"quality.todo": 2}


# ─── the payload: None is "not recorded", zeros are "nothing hidden" ──


def test_a_batch_without_the_counts_is_not_recorded():
    assert hidden_payload(SimpleNamespace(findings=[])) is None


def test_a_batch_that_hid_nothing_says_so_in_zeros():
    payload = hidden_payload(ReviewBatch(pull_request=_pr()))
    assert payload == {
        "by_rule": {}, "duplicates": 0, "near_duplicates": 0,
        "low_confidence": 0, "no_evidence": 0, "coverage_claim": 0, "veto": 0,
    }


def test_the_payload_carries_every_cause():
    batch = ReviewBatch(
        pull_request=_pr(), dropped_by_rule={"quality.todo": 2},
        dropped_duplicates=1, dropped_near_duplicates=2, dropped_low_confidence=3,
        dropped_no_evidence=4, dropped_by_veto=5,
    )
    assert hidden_payload(batch) == {
        "by_rule": {"quality.todo": 2}, "duplicates": 1, "near_duplicates": 2,
        "low_confidence": 3, "no_evidence": 4, "coverage_claim": 0, "veto": 5,
    }


# ─── the row ─────────────────────────────────────────────────────────


def test_the_column_arrives_by_migration_and_a_row_before_it_reads_null(tmp_path: Path):
    """The same additive ALTER as the rosters: an installation whose
    review_runs.db predates the column opens, and its rows say 'not recorded'
    rather than 'nothing hidden'."""
    db = tmp_path / "review_runs.db"
    store = ReviewRunStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE review_runs DROP COLUMN hidden_json")
        conn.commit()
    store.insert(ReviewRun(id="old", user_id="u", pr_ref="github:acme/api#7"))
    reopened = ReviewRunStore(db)  # re-applies the ALTER
    assert reopened.get("old").hidden is None
    reopened.update("old", hidden={"by_rule": {"quality.todo": 1}, "veto": 0})
    assert reopened.get("old").hidden == {"by_rule": {"quality.todo": 1}, "veto": 0}


def test_zeros_written_are_zeros_read_not_null(store: ReviewRunStore):
    store.insert(ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7"))
    zeros = hidden_payload(ReviewBatch(pull_request=_pr()))
    store.update("r", hidden=zeros)
    assert store.get("r").hidden == zeros


def test_update_without_the_field_leaves_the_column_alone(store: ReviewRunStore):
    store.insert(ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7"))
    store.update("r", hidden={"by_rule": {"a": 1}})
    store.update("r", summary="later")
    assert store.get("r").hidden == {"by_rule": {"a": 1}}


def test_record_completed_review_writes_what_was_hidden(store: ReviewRunStore):
    store.insert(ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7"))
    batch = ReviewBatch(
        pull_request=_pr(), dropped_by_rule={"quality.todo": 2}, dropped_no_evidence=1,
    )
    batch.verdict = batch.compute_verdict()
    batch.mark_complete()
    record_completed_review(_Result(batch=batch), run_id="r", store=store)
    assert store.get("r").hidden == {
        "by_rule": {"quality.todo": 2}, "duplicates": 0, "near_duplicates": 0,
        "low_confidence": 0, "no_evidence": 1, "coverage_claim": 0, "veto": 0,
    }


def test_an_unreadable_column_does_not_fail_the_history(store: ReviewRunStore):
    store.insert(ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7"))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_runs SET hidden_json = ? WHERE id = ?", ("{not json", "r"))
        conn.commit()
    assert store.get("r").hidden is None


# ─── the wire ────────────────────────────────────────────────────────


def test_the_api_serves_the_dict_on_the_run():
    run = ReviewRun(
        id="r", user_id="u", pr_ref="github:acme/api#7", status="complete",
        hidden={"by_rule": {"quality.todo": 2}, "no_evidence": 1, "veto": 0},
    )
    out = _run_to_out(run)
    assert out.hidden is not None
    assert out.hidden.by_rule == {"quality.todo": 2}
    assert out.hidden.no_evidence == 1
    assert out.hidden.duplicates == 0, "a key another version never wrote defaults"


def test_the_api_serves_null_for_a_run_recorded_before_the_column():
    run = ReviewRun(id="r", user_id="u", pr_ref="github:acme/api#7", status="complete")
    assert _run_to_out(run).hidden is None


def test_a_key_another_version_grew_does_not_fail_the_request():
    run = ReviewRun(
        id="r", user_id="u", pr_ref="github:acme/api#7", status="complete",
        hidden={"by_rule": {}, "future_cause": 3},
    )
    assert _run_to_out(run).hidden.by_rule == {}
