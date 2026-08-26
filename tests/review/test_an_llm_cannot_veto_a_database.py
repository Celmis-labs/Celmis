"""The verifier's LLM pass judges judgements, never lookups.

The FP filter exists to catch model hallucinations. A proven finding — a
purl+version that matched an OSV advisory, a structural rule that fired —
is not a judgement, and the pass's own prompt orders it dropped: it says to
discard findings whose line is not in the diff, and a CVE finding anchored
in a lockfile that review filtering kept OUT of the hunks fails that test
while being exactly right. Found by the adversarial verify of the CVE
agent: three or more findings trigger the LLM pass, and the pass could veto
the one finding in the batch that was checkable at a URL in seconds.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="src/app.py", old_file_path="src/app.py",
                    old_start=1, old_count=1, new_start=1, new_count=2,
                    content="@@ -1 +1,2 @@\n line\n+added\n")],
    )


def _proven_cve() -> Finding:
    return Finding(
        file_path="package-lock.json", line=4102,
        severity=FindingSeverity.CRITICAL,
        title="lodash 4.17.15 — prototype pollution",
        body="GHSA-jf85-cpcp-j695 / CVE-2019-10744; fixed in 4.17.19",
        agent="cve", rule_id="sec.cve-GHSA-jf85-cpcp-j695",
        confidence=1.0, evidence_kind="proven",
    )


def _inferred(i: int) -> Finding:
    return Finding(
        file_path="src/app.py", line=2, severity=FindingSeverity.WARNING,
        title=f"model guess {i}", body="…", agent="security",
        rule_id=f"sec.cwe-{i}", confidence=0.8,
    )


def test_a_reject_everything_verifier_cannot_take_the_cve_with_it():
    """The strongest verdict the LLM can give must not reach the lookup."""
    findings = [_proven_cve(), _inferred(1), _inferred(2), _inferred(3)]
    agent = VerifierAgent()
    reply = MagicMock()
    reply.text = '{"keep": [], "reasons": {"0": "no", "1": "no", "2": "no"}}'
    reply.input_tokens = 10
    reply.output_tokens = 5

    ctx = AgentContext(pull_request=_pr(), llm_client=MagicMock())
    ctx.llm_client.generate.return_value = reply
    result = agent.verify(findings, ctx)

    kept_rules = {f.rule_id for f in result.kept}
    assert "sec.cve-GHSA-jf85-cpcp-j695" in kept_rules, (
        "the LLM rejected everything and the proven CVE went with it — "
        "an LLM must not be able to veto a database"
    )
    assert not any(f.agent == "security" for f in result.kept), (
        "the judgeable findings were rejected and must stay rejected"
    )


def test_the_llm_never_even_sees_the_proven_finding():
    """Cheaper and safer than trusting the model to keep it: the lockfile
    anchor is 'not in the diff' by the prompt's own rules, so showing it at
    all invites the drop."""
    findings = [_proven_cve(), _inferred(1), _inferred(2), _inferred(3)]
    agent = VerifierAgent()
    reply = MagicMock()
    reply.text = '{"keep": [0, 1, 2], "reasons": {}}'
    reply.input_tokens = 10
    reply.output_tokens = 5

    ctx = AgentContext(pull_request=_pr(), llm_client=MagicMock())
    ctx.llm_client.generate.return_value = reply
    agent.verify(findings, ctx)

    prompt = ctx.llm_client.generate.call_args.kwargs.get("prompt") or \
        (ctx.llm_client.generate.call_args.args[0]
         if ctx.llm_client.generate.call_args.args else "")
    assert "sec.cve-GHSA-jf85-cpcp-j695" not in str(prompt)
    assert "lodash" not in str(prompt)


def test_proven_findings_alone_never_trigger_the_llm_pass():
    """Three proven findings are three facts, not three hallucination risks —
    no tokens are spent second-guessing them."""
    findings = [_proven_cve(), _proven_cve(), _proven_cve(), _proven_cve()]
    agent = VerifierAgent()
    ctx = AgentContext(pull_request=_pr(), llm_client=MagicMock())
    result = agent.verify(findings, ctx)

    ctx.llm_client.generate.assert_not_called()
    assert len(result.kept) == 1, "identical findings still dedup"


def test_mixed_batch_below_threshold_keeps_everything_unjudged():
    findings = [_proven_cve(), _inferred(1)]
    agent = VerifierAgent()
    ctx = AgentContext(pull_request=_pr(), llm_client=MagicMock())
    result = agent.verify(findings, ctx)
    ctx.llm_client.generate.assert_not_called()
    assert len(result.kept) == 2
