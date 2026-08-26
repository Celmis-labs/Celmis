"""Six rule ids are hidden by a gate in code, counted, and overridable per repo.

Measured on the Martian bench with the LLM veto OFF (14 PRs, one judge):
tests.no-coverage, quality.todo, quality.typing, quality.duplication,
quality.maintainability and quality.magic_numbers produced 6-7 false positives
between them and not one true positive. Augment's remedy is a line in the
system prompt — "specify which comment categories to avoid" — which a model
follows most of the time. A deny-list in the prefilter is followed every time,
and what it hides is counted by rule on the batch so a run can say what it hid.

The list is `ReviewSettings.suppressed_rules`: the env overrides it like every
other field there, and a repo policy's `suppressed_rules` REPLACES it — a list
narrows or widens, `[]` hides nothing, and NULL (every row written before the
column existed) inherits the code default.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import VerifierAgent, prefilter
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings

#: Six categories, each in two spellings: the old-prefix id that historical
#: rows and stored policies carry, and the defect.* id the merged agent would
#: reach for today. One category un-banned in one spelling is the same
#: measured-zero-TP comment back on PRs, so the default carries both.
MEASURED_ZERO_TP = frozenset({
    "tests.no-coverage",
    "quality.todo",
    "quality.typing",
    "quality.duplication",
    "quality.maintainability",
    "quality.magic_numbers",
    "defect.no-coverage",
    "defect.todo",
    "defect.typing",
    "defect.duplication",
    "defect.maintainability",
    "defect.magic_numbers",
})


def _f(rule, line=1, *, agent="quality", evidence_kind="inferred", confidence=0.9,
       title=None) -> Finding:
    return Finding(
        file_path="src/app.py", line=line, rule_id=rule, title=title or rule,
        agent=agent, confidence=confidence, evidence_kind=evidence_kind,
    )


# ─── the default, and the env ────────────────────────────────────────


def test_the_default_is_exactly_the_measured_rules_in_both_spellings():
    assert ReviewSettings().suppressed_rules == MEASURED_ZERO_TP


def test_the_env_replaces_the_default_like_every_other_setting(monkeypatch):
    monkeypatch.setenv("REVIEW_SUPPRESSED_RULES", '["quality.todo", "arch.layering"]')
    assert ReviewSettings().suppressed_rules == frozenset({"quality.todo", "arch.layering"})


def test_the_env_can_empty_it(monkeypatch):
    monkeypatch.setenv("REVIEW_SUPPRESSED_RULES", "[]")
    assert ReviewSettings().suppressed_rules == frozenset()


# ─── the gate ────────────────────────────────────────────────────────


def test_the_prefilter_drops_exactly_the_listed_rules_and_counts_each():
    neighbours = ["quality.complexity", "tests.weak-assert", "sec.cwe-862"]
    findings = (
        [_f(r, line=i) for i, r in enumerate(sorted(MEASURED_ZERO_TP), start=1)]
        + [_f("quality.todo", line=40), _f("quality.todo", line=41)]
        + [_f(r, line=100 + i) for i, r in enumerate(neighbours)]
    )
    result = prefilter(findings, suppressed_rules=MEASURED_ZERO_TP)

    assert sorted(f.rule_id for f in result.kept) == sorted(neighbours), (
        "a rule outside the list was dropped, or one inside it survived"
    )
    assert result.dropped_by_rule == {
        "tests.no-coverage": 1, "quality.typing": 1, "quality.duplication": 1,
        "quality.maintainability": 1, "quality.magic_numbers": 1,
        "defect.no-coverage": 1, "defect.todo": 1, "defect.typing": 1,
        "defect.duplication": 1, "defect.maintainability": 1,
        "defect.magic_numbers": 1,
        "quality.todo": 3,
    }


def test_counting_is_per_finding_not_per_surviving_group():
    """Three agents on one line under a hidden rule is three hidden findings:
    the count is what the agents said, not what the dedup would have left."""
    result = prefilter(
        [_f("quality.todo", agent=a) for a in ("quality", "architect", "tests")],
        suppressed_rules={"quality.todo"},
    )
    assert result.dropped_by_rule == {"quality.todo": 3}
    assert result.dropped_dedup == 0


def test_the_match_is_exact_not_by_prefix():
    result = prefilter(
        [_f("quality.todo"), _f("quality.todo.stale", line=2), _f("quality", line=3)],
        suppressed_rules={"quality.todo"},
    )
    assert sorted(f.rule_id for f in result.kept) == ["quality", "quality.todo.stale"]


def test_a_suppressed_rule_hides_a_proven_finding_too():
    """Pinned as a decision, not an accident: the list is the operator's
    explicit word, and a policy that names `sec.cve-…` means it. The veto is
    kept away from proven findings because a MODEL must not overrule a
    database; an operator may."""
    result = prefilter(
        [_f("sec.cve-GHSA-1", agent="cve", evidence_kind="proven")],
        suppressed_rules={"sec.cve-GHSA-1"},
    )
    assert result.kept == []
    assert result.dropped_by_rule == {"sec.cve-GHSA-1": 1}


def test_the_agent_reads_the_code_default_when_handed_nothing(monkeypatch):
    import src.review.agents.verifier as verifier_mod

    monkeypatch.setattr(
        verifier_mod, "get_review_settings",
        lambda: ReviewSettings(suppressed_rules=frozenset({"only.this"})),
    )
    agent = VerifierAgent(confidence_threshold=0.0)
    result = agent.prefilter([_f("only.this"), _f("quality.todo", line=2)])
    assert [f.rule_id for f in result.kept] == ["quality.todo"]

    # And an explicit empty list is "hide nothing", not "use the default".
    assert len(agent.prefilter([_f("only.this")], suppressed_rules=[]).kept) == 1


# ─── the repo policy, through the orchestrator ───────────────────────


def test_none_inherits_and_a_list_replaces():
    s = ReviewSettings()
    assert ReviewOrchestrator._suppressed_rules(None, s) == MEASURED_ZERO_TP
    assert ReviewOrchestrator._suppressed_rules({"suppressed_rules": None}, s) == MEASURED_ZERO_TP
    assert ReviewOrchestrator._suppressed_rules({"suppressed_rules": []}, s) == frozenset()
    assert ReviewOrchestrator._suppressed_rules(
        {"suppressed_rules": ["sec.cwe-862", " quality.todo ", ""]}, s,
    ) == frozenset({"sec.cwe-862", "quality.todo"})


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/app.py", old_file_path="src/app.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


class _Canned(ReviewAgent):
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, findings=list(self._findings))


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):  # pragma: no cover - not posted
        return {}

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    """The real loop, the real VerifierAgent, the veto switched off by the
    policy so the deny-list is the only thing deciding what posts."""
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(
        bc_mod, "run_breaking_change", lambda ctx: AgentRunResult(agent="breaking_change"),
    )
    monkeypatch.setattr(
        comp_mod, "run_compliance", lambda ctx: AgentRunResult(agent="compliance"),
    )

    def _run(findings: list[Finding], *, policy_rules):
        policy = {"enabled": True, "target_branches": [], "disabled_agents": ["verifier"]}
        if policy_rules != "absent":
            policy["suppressed_rules"] = policy_rules
        orch = ReviewOrchestrator(
            settings=ReviewSettings(),
            agents=[_Canned("quality", findings)], verifier=VerifierAgent(),
        )
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
        monkeypatch.setattr(
            orch, "_build_context",
            lambda pr, **kw: AgentContext(pull_request=pr, llm_client=MagicMock()),
        )
        return orch.review(
            "github", "acme/api", 7,
            dry_run=True, post_comments=False, provider=_Provider(),
        ).batch

    return _run


# Two different TODO findings — distinct titles, so the near-duplicate merge
# leaves them as two and the counts below are about the gate alone.
_SAMPLE = [
    _f("quality.todo", line=1, title="TODO left in the request handler"),
    _f("quality.todo", line=2, title="Unfinished stub returns None"),
    _f("sec.cwe-862", line=3, agent="security"),
    _f("arch.layering", line=4, agent="architect"),
]


def test_a_policy_that_says_nothing_gets_the_default_and_the_count(run):
    batch = run(_SAMPLE, policy_rules="absent")
    assert sorted(f.rule_id for f in batch.findings) == ["arch.layering", "sec.cwe-862"]
    assert batch.dropped_by_rule == {"quality.todo": 2}, (
        "the batch has to say what it hid, or the filter is invisible again"
    )


def test_a_policy_can_narrow_the_default_to_nothing(run):
    batch = run(_SAMPLE, policy_rules=[])
    assert len(batch.findings) == 4
    assert batch.dropped_by_rule == {}


def test_a_policy_list_replaces_the_default_in_both_directions(run):
    """Widened to `sec.cwe-862`, narrowed off `quality.todo` — in one list."""
    batch = run(_SAMPLE, policy_rules=["sec.cwe-862"])
    assert sorted(f.rule_id for f in batch.findings) == [
        "arch.layering", "quality.todo", "quality.todo",
    ]
    assert batch.dropped_by_rule == {"sec.cwe-862": 1}


def test_the_count_survives_the_veto_being_on(run, monkeypatch):
    """Hidden findings are hidden by the prefilter, which runs whether or not
    the LLM pass does — so the count is there either way."""
    # One judgeable finding left after the gate: below the pass's threshold,
    # so no verdict is needed and nothing here depends on a mocked reply.
    batch = run(_SAMPLE[:3], policy_rules="absent")
    assert batch.dropped_by_rule == {"quality.todo": 2}
    assert [f.rule_id for f in batch.findings] == ["sec.cwe-862"]
    assert FindingSeverity.WARNING is batch.findings[0].severity
