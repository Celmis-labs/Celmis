"""The performance agent: Kodus's Performance category, held to the finders' contract.

What it must share with the defect agent it is modelled on, each pinned
through the real `LLMReviewAgent.review` path with a fake model client:

  * the output contract — the same `reasoning`-first JSON array, parsed by
    the same parser, so an unbacked claim or a file the PR does not touch is
    refused exactly as it is for every finder;
  * its own model, ceiling and reasoning level, resolved per agent;
  * findings the verifier's prefilter treats like any other finder's.

And what is its own: a remit stated in terms of what GROWS, and `perf.` rule
ids that the summary files under "Performance".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.review.agents.base import AgentContext
from src.review.agents.performance import PerformanceAgent
from src.review.models import FindingSeverity, Hunk, PullRequest
from src.review.settings import AgentLLMSettings

_DIFF = (
    "@@ -10,3 +10,6 @@ def totals(orders):\n"
    "     out = []\n"
    "+    for order in orders:\n"
    "+        user = db.query(User).get(order.user_id)\n"
    "+        out.append((user.name, order.total))\n"
    "     return out\n"
)


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/shop", number=12,
        title="Show buyer names on the totals report",
        description="Adds the buyer name to every row.", author="a",
        base_ref="main", base_sha="x", head_ref="feat", head_sha="y",
        state="open",
        hunks=[Hunk(file_path="app/report.py", old_file_path="app/report.py",
                    old_start=10, old_count=3, new_start=10, new_count=6,
                    content=_DIFF)],
    )


def _reply(items: list[dict]) -> MagicMock:
    r = MagicMock()
    r.text = json.dumps(items)
    r.input_tokens = 900
    r.output_tokens = 120
    r.cost_usd = 0.002
    r.cost_source = "litellm_estimate"
    r.model = "perf-model"
    return r


_N_PLUS_ONE = {
    "reasoning": "Line 12 runs a User query once per order inside the loop on "
                 "line 11; a report over 5,000 orders makes 5,000 round-trips.",
    "file": "app/report.py",
    "line": 12,
    "severity": "error",
    "title": "N+1 query: one User lookup per order",
    "body": "Load the users with one `IN` query before the loop.",
    "rule_id": "perf.n-plus-one",
    "confidence": 0.85,
}


def _run(items: list[dict], **ctx):
    client = MagicMock()
    client.generate.return_value = _reply(items)
    context = AgentContext(
        pull_request=_pr(), llm_client=client,
        graph_brief="totals (app/report.py) is called from 14 request handlers",
        agent_llm={"performance": AgentLLMSettings(
            model="perf-model", max_output_tokens=4321, reasoning="low")},
        **ctx,
    )
    return PerformanceAgent().review(context), client


def test_a_reply_round_trips_into_a_performance_finding():
    result, client = _run([_N_PLUS_ONE])

    assert result.error is None
    [f] = result.findings
    assert f.agent == "performance"
    assert f.rule_id == "perf.n-plus-one"
    assert f.severity is FindingSeverity.ERROR
    assert (f.file_path, f.line) == ("app/report.py", 12)
    assert f.reasoning.startswith("Line 12 runs a User query")
    assert result.tokens_in == 900 and result.cost_usd == 0.002
    assert client.generate.call_count == 1


def test_the_call_carries_its_own_model_limits_and_operation():
    _, client = _run([])
    kw = client.generate.call_args.kwargs
    assert kw["model"] == "perf-model"
    assert kw["max_output_tokens"] == 4321
    assert kw["reasoning"] == "low"
    assert kw["agent"] == "performance"
    assert kw["operation"] == "review_performance"


def test_the_prompt_states_the_remit_and_the_evidence_standard():
    _, client = _run([])
    kw = client.generate.call_args.kwargs
    system = " ".join(kw["system_instruction"].split())
    for shape in ("N+1", "blocking I/O", "DOM thrash", "memory", "quadratic"):
        assert shape.lower() in system.lower(), shape
    assert "the quantity that grows" in system.lower()
    assert '"reasoning"' in system, "the shared output contract is missing"
    assert "perf.<rule>" in system
    # Correctness and security are handed away, as the defect agent hands
    # cross-file claims away — one standard of evidence per prompt.
    assert "the defect reviewer writes those" in system

    prompt = kw["prompt"]
    assert "app/report.py" in prompt and "db.query(User)" in prompt
    assert "called from 14 request handlers" in prompt, "the hot-path hint is missing"


def test_a_claim_without_reasoning_or_outside_the_pr_is_refused():
    unbacked = {**_N_PLUS_ONE, "reasoning": ""}
    elsewhere = {**_N_PLUS_ONE, "file": "app/other.py"}
    result, _ = _run([unbacked, elsewhere, _N_PLUS_ONE])
    assert len(result.findings) == 1
    assert result.dropped_no_evidence == 2


def test_the_verifier_prefilter_takes_its_findings_like_any_finders():
    from src.review.agents.verifier import VerifierAgent

    result, _ = _run([_N_PLUS_ONE])
    pre = VerifierAgent().prefilter(result.findings, suppressed_rules=frozenset())
    assert [f.rule_id for f in pre.kept] == ["perf.n-plus-one"]


def test_its_findings_are_counted_under_performance_in_the_summary():
    from src.review.models import ReviewBatch
    from src.review.providers.base import _rich_findings_lines

    result, _ = _run([_N_PLUS_ONE])
    batch = ReviewBatch(pull_request=_pr(), findings=result.findings)
    batch.agents_run = ["performance"]
    text = "\n".join(_rich_findings_lines(batch))
    assert "**By category:** Performance: **1**" in text
