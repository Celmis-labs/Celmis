"""A 120-second deadline nobody chose, reported as somebody else's outage.

`LLMClient.generate` took `timeout: float = 120` and no review agent ever
passed one, so every call to every model had a two-minute deadline that no
setting named and no operator could change. Reasoning models routinely think
for longer than that on a large diff.

The benchmark install shows what it cost: sixteen `agent_llm_failed` entries in
eight hours, every classified one an APITimeoutError. Because `classify` put
`litellm.Timeout` in the same bucket as ServiceUnavailable and InternalServer,
each of them reached the operator as "the provider is unavailable" — and the
harness watching the runs read the streak as provider quota and stopped three
benchmark runs to protect a dataset that was never in danger. The product's own
output offered nothing that could have told them apart.

Two claims are wrong there and only one of them is about the number:

  * a timeout is OUR deadline elapsing. The only thing observed is that no
    answer had arrived yet; the provider may be working on it perfectly well,
    merely slower than the number we chose. "The provider is unavailable"
    sends the reader to a status page for a problem that lives in this
    repository's settings.
  * the number itself was unreachable. The fix is not 300 instead of 120 —
    no single number is right for every model — it is that the number now has
    a name, `REVIEW_LLM_TIMEOUT_SECONDS`, and a row an operator can read.

300 is argued from the only measurement there was: over 517 real reviews the
WHOLE review has median 74s and p90 328s, and a review runs its finders
concurrently, so one call outliving the p90 of an entire review is anomalous
rather than slow. That the argument had to be made from review totals is its
own finding — `AgentRunResult.elapsed_seconds` was computed by every agent and
read by nothing, so per-agent timing did not exist. It is logged now.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.llm.errors import TRANSIENT, classify
from src.review.settings import ReviewSettings, get_review_settings

# ─── the deadline has a name ─────────────────────────────────────────


def test_the_deadline_is_a_setting(monkeypatch):
    monkeypatch.setenv("REVIEW_LLM_TIMEOUT_SECONDS", "45")
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().llm_timeout_seconds == 45
    finally:
        get_review_settings.cache_clear()


def test_the_default_is_longer_than_the_one_that_was_cutting_calls():
    assert ReviewSettings().llm_timeout_seconds > 120, (
        "120 is the value that produced 16 agent failures in 8 hours"
    )


def test_one_call_cannot_outlive_the_whole_review_budget():
    """A per-call deadline at or above `timeout_seconds` would make the review
    budget unreachable — and the transient retry means a stuck call costs
    twice this number before the agent gives up."""
    s = ReviewSettings()
    assert s.llm_timeout_seconds * 2 <= s.timeout_seconds


def test_both_agent_branches_pass_it():
    """`_generate_and_parse` is fed by two different closures — the injected
    client and the one this module builds. A deadline on one of them is the
    shape `FINDING_OUTPUT_FORMAT` was in before it was shared: live for some
    calls, absent for others, and nothing says which."""
    import src.review.agents.base as base

    whole = inspect.getsource(base)
    closures = [n for n in ast.walk(ast.parse(whole))
                if isinstance(n, ast.FunctionDef) and n.name == "_gen"]
    assert len(closures) == 2, f"expected two _gen closures, found {len(closures)}"
    for c in closures:
        kwargs = {k.arg for n in ast.walk(c) if isinstance(n, ast.Call)
                  for k in n.keywords if k.arg}
        assert "timeout" in kwargs, "a _gen closure with no deadline"


def test_the_deadline_is_read_per_call_not_captured_at_import():
    """A value copied once stops tracking the setting it came from — an
    operator raising it would see no effect until a restart."""
    import src.review.agents.base as base

    tree = ast.parse(inspect.getsource(base._llm_timeout))
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "get_review_settings"
               for n in ast.walk(tree))


# ─── and it stops blaming the provider ───────────────────────────────


def _litellm():
    litellm = pytest.importorskip("litellm")
    return litellm


def test_a_timeout_is_not_an_outage():
    le = _litellm()
    failure = classify(le.Timeout("timed out", model="m", llm_provider="gemini"))

    assert failure.code == "local_timeout"
    assert failure.code != "provider_unavailable", (
        "this is the sentence that sent a reader to a status page for a "
        "problem in their own settings"
    )


def test_the_sentence_names_the_knob():
    """These travel into a review summary and a run record. "The provider is
    unavailable" is not something the reader can act on; the deadline is."""
    le = _litellm()
    reason = classify(le.Timeout("timed out", model="m", llm_provider="gemini")).reason

    assert "REVIEW_LLM_TIMEOUT_SECONDS" in reason
    assert "provider is unavailable" not in reason


def test_a_real_outage_still_reads_as_one():
    """The distinction has to cut both ways, or it is just a rename."""
    le = _litellm()
    for exc in (le.ServiceUnavailableError("503", model="m", llm_provider="g"),
                le.APIConnectionError(message="reset", model="m", llm_provider="g")):
        assert classify(exc).code == "provider_unavailable", exc


def test_a_timeout_is_still_worth_one_more_try():
    """A slow call is often a call that would have answered. What it must not
    do is blame the provider on the way past."""
    le = _litellm()
    assert classify(le.Timeout("t", model="m", llm_provider="g")).disposition \
        is TRANSIENT


def test_the_new_code_has_a_row_in_both_tables():
    """The module's own rule: a code without a disposition row falls through
    to UNRECOGNISED, and one without a reason row gets the generic sentence
    that calls everything a provider."""
    from src.llm.errors import _DISPOSITION, _REASON

    assert "local_timeout" in _DISPOSITION
    assert "local_timeout" in _REASON


# ─── the timing that was computed and thrown away ────────────────────


def test_the_orchestrator_says_how_long_each_agent_took():
    """`AgentRunResult.elapsed_seconds` was filled in by every agent and read
    by nothing, so the only timing this product recorded was the whole
    review's — which is why the replacement deadline had to be argued from
    review totals rather than measured."""
    import src.review.orchestrator as orch

    tree = ast.parse(inspect.getsource(orch))
    fmts = [n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) in {"info", "warning"}
            and n.args and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)]
    per_agent = [f for f in fmts if "agent=" in f and "elapsed=" in f]
    assert per_agent, "no log line carries one agent's elapsed time"
