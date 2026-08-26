"""The verifier's LLM veto switches off by policy; its prefilter never does.

The veto is a stage after the agents, not an agent, so the parallel runner's
filter cannot see it and the switch has to live in the orchestrator. Measured
on the Martian bench (14 PRs, one judge): with the veto ON, TP 10 / FP 6 /
FN 43, F1 29.0; with it OFF, TP 24 / FP 31 / FN 29, F1 44.4. An operator who
can see that needs the switch, and a review without the veto must not read as
one that was vetted and found everything clean.

But "verifier" in `disabled_agents` used to switch off the whole of
`verify()` — the exact dedup, the confidence floor and the severity sort with
it — and `batch.findings = list(all_findings)` then handed the providers an
unsorted, un-deduped list for their findings[:max_inline_comments] cap to
truncate. The deterministic prefilter now runs on every review; the policy
switch reaches the LLM pass and nothing else.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.api.routers.review_policies import TOGGLEABLE_AGENTS
from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import VerifierAgent
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
)
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings


def test_the_verifier_is_toggleable_from_the_api():
    assert "verifier" in TOGGLEABLE_AGENTS


def test_a_batch_can_record_a_skipped_stage_apart_from_failures():
    """A skipped stage is not a failure. The field is separate so the verdict
    cannot confuse the two."""
    b = ReviewBatch.__dataclass_fields__
    assert "agents_skipped" in b and "agents_failed" in b


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


def _f(path, line, rule, *, severity=FindingSeverity.WARNING, confidence=0.9,
       agent="architect", title="", body="", evidence_kind="inferred") -> Finding:
    return Finding(
        file_path=path, line=line, rule_id=rule, severity=severity,
        confidence=confidence, agent=agent, title=title or rule, body=body,
        evidence_kind=evidence_kind,
    )


@pytest.fixture
def run(monkeypatch):
    """The real orchestrator loop and the real VerifierAgent, no network.

    The LLM is a MagicMock on the context, so the test can both script the
    veto's verdict and prove whether the veto was consulted at all.
    """
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(
        bc_mod, "run_breaking_change", lambda ctx: AgentRunResult(agent="breaking_change"),
    )
    monkeypatch.setattr(
        comp_mod, "run_compliance", lambda ctx: AgentRunResult(agent="compliance"),
    )

    def _run(agents, *, policy, verdict='{"keep": [], "reasons": {}}'):
        llm = MagicMock()
        reply = MagicMock()
        reply.text, reply.input_tokens, reply.output_tokens = verdict, 10, 5
        llm.generate.return_value = reply

        orch = ReviewOrchestrator(
            settings=ReviewSettings(), agents=list(agents), verifier=VerifierAgent(),
        )
        monkeypatch.setattr(orch, "_load_policy", lambda slug: policy)
        monkeypatch.setattr(
            orch, "_build_context",
            lambda pr, **kw: AgentContext(pull_request=pr, llm_client=llm),
        )
        batch = orch.review(
            "github", "acme/api", 7,
            dry_run=True, post_comments=False, provider=_Provider(),
        ).batch
        return batch, llm

    return _run


_VERIFIER_OFF = {"enabled": True, "target_branches": [], "disabled_agents": ["verifier"]}

#: The veto is OFF unless a repository asks for it — see
#: `ReviewSettings.verifier_enabled`. These tests are about what the veto does
#: WHEN ON, so they now have to say so; `None` used to mean "on" only because
#: the deny-list was the sole way to express "off".
_VERIFIER_ON = {"enabled": True, "target_branches": [], "verifier_enabled": True}


def test_switching_the_verifier_off_switches_off_the_llm_pass_and_says_so(run):
    batch, llm = run(
        [_Canned("architect", [_f("a.py", i, f"r.{i}") for i in range(4)])],
        policy=_VERIFIER_OFF,
    )
    llm.generate.assert_not_called()
    assert batch.agents_skipped == ["verifier"]
    assert len(batch.findings) == 4, "four distinct findings, none vetoed"


def test_the_prefilter_still_runs_when_the_verifier_is_off(run):
    """The whole of the deterministic pass, observed through the batch:
    the same (file, line, rule) from two agents is one finding carrying both
    names and the worse severity; a finding under the floor is gone; and the
    list the providers will cap is in severity order."""
    batch, llm = run(
        [
            _Canned("architect", [
                _f("a.py", 10, "r.dup", agent="architect", title="Null deref in parse"),
                _f("z.py", 1, "r.info", agent="architect", severity=FindingSeverity.INFO),
                _f("c.py", 1, "r.low", agent="architect", confidence=0.2),
            ]),
            _Canned("security", [
                _f("a.py", 10, "r.dup", agent="security", title="Null deref in parse",
                   severity=FindingSeverity.ERROR),
                _f("b.py", 5, "r.crit", agent="security", severity=FindingSeverity.CRITICAL),
            ]),
        ],
        policy=_VERIFIER_OFF,
    )
    llm.generate.assert_not_called()

    at_a10 = [f for f in batch.findings if (f.file_path, f.line) == ("a.py", 10)]
    assert len(at_a10) == 1, (
        "two agents on one line posted twice — the dedup went off with the veto"
    )
    assert at_a10[0].severity is FindingSeverity.ERROR
    assert {"architect", "security"} <= set(at_a10[0].agent.split(","))

    assert all(f.confidence >= 0.5 for f in batch.findings), (
        "the confidence floor went off with the veto"
    )
    assert [f.severity for f in batch.findings] == [
        FindingSeverity.CRITICAL, FindingSeverity.ERROR, FindingSeverity.INFO,
    ], "the providers cap on position, and the order is thread order again"


def test_the_llm_pass_still_runs_when_the_verifier_is_on(run):
    batch, llm = run(
        [_Canned("security", [_f("a.py", i, f"sec.{i}", agent="security") for i in range(3)])],
        policy=_VERIFIER_ON,
    )
    llm.generate.assert_called_once()
    assert batch.findings == [], "the veto said keep nothing, and nothing was kept"
    assert batch.agents_skipped == []


def test_the_llm_pass_judges_the_prefiltered_list_not_the_raw_one(run):
    """Four agents flagging one line is one candidate in the prompt, not
    four — the prefilter precedes the pass, it does not replace it."""
    same_line = [
        _f("a.py", 1, "r.same", agent=a, title="Unchecked index")
        for a in ("architect", "security", "quality", "tests")
    ]
    others = [_f("a.py", 2, "r.two"), _f("a.py", 3, "r.three")]
    batch, llm = run(
        [_Canned("architect", same_line + others)],
        policy=_VERIFIER_ON, verdict='{"keep": [0, 1, 2], "reasons": {}}',
    )
    prompt = llm.generate.call_args.kwargs["prompt"]
    assert "Finding #2" in prompt and "Finding #3" not in prompt
    assert len(batch.findings) == 3


def test_the_llm_pass_never_sees_a_proven_finding_and_cannot_veto_it(run):
    """The existing guarantee, now at the orchestrator: a lookup is not a
    judgement. The veto rejects everything it is shown; the CVE it was not
    shown is still posted."""
    cve = _f("package-lock.json", 4102, "sec.cve-GHSA-jf85-cpcp-j695", agent="cve",
             title="lodash 4.17.15 — prototype pollution", confidence=1.0,
             severity=FindingSeverity.CRITICAL, evidence_kind="proven")
    guesses = [_f("a.py", i, f"sec.cwe-{i}", agent="security") for i in range(3)]
    batch, llm = run([_Canned("security", guesses), _Canned("cve", [cve])], policy=_VERIFIER_ON)

    llm.generate.assert_called_once()
    assert "lodash" not in llm.generate.call_args.kwargs["prompt"]
    assert [f.rule_id for f in batch.findings] == ["sec.cve-GHSA-jf85-cpcp-j695"]
