"""The one transient failure a plain resend cannot fix, resent unchanged.

Every other transient class is answered by asking again on a fresh connection:
a dropped socket, a 5xx, a proxy restart. The deadline had nothing to do with
any of them. A TIMEOUT is the one where the deadline IS the failure — the model
was thinking, we stopped listening — and the ladder resent the same question to
the same model on the same clock. A second guaranteed failure, at full price,
and then the agent gave up.

Measured on the benchmark install before the deadline was made settable: 16
agent failures in 8 hours, every classified one an APITimeoutError. Each cost
two full deadlines, and sometimes a fallback call after them.

Doubled once, for the second and last attempt. A call that merely needed longer
gets it; a genuinely stuck one still ends. Nothing else widens: a rejected key
does not get more time to be rejected in.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import _llm_timeout
from src.review.settings import ReviewSettings


class _Reply:
    text = "[]"
    input_tokens = output_tokens = 0
    model = "m"
    cost_usd = 0.0
    cost_source = "estimate"
    parameter_adjustments = ()
    max_output_tokens_clamped_to = None


def _ladder(outcomes):
    """Drive `_generate_and_parse` with a scripted `generate`, recording the
    deadline each attempt was given."""
    from src.review.agents.base import AgentContext, LLMReviewAgent
    from src.review.models import Hunk, PullRequest

    seen: list[float | None] = []
    seq = list(outcomes)

    def _gen(system, budget, model_override=None, timeout=None):
        seen.append(timeout)
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    class _A(LLMReviewAgent):
        name = "defect"
        _SYSTEM = "s"

        def _build_prompt(self, context):  # pragma: no cover - unused here
            return "p"

    pr = PullRequest(
        provider="github", repo="a/b", number=1, title="t", description="",
        author="a", base_ref="main", base_sha="x", head_ref="f", head_sha="y",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@\n+x\n")],
    )
    agent = _A()
    result = agent._generate_and_parse(
        _gen, "s", AgentContext(pull_request=pr), t0=0.0,
        max_output_tokens=1024,
    )
    return seen, result


def _timeout_exc():
    litellm = pytest.importorskip("litellm")
    return litellm.Timeout("timed out", model="m", llm_provider="gemini")


def _connection_exc():
    litellm = pytest.importorskip("litellm")
    return litellm.APIConnectionError(message="reset", model="m", llm_provider="g")


# ─── the widening ────────────────────────────────────────────────────


def test_the_first_attempt_uses_the_configured_deadline():
    seen, _ = _ladder([_Reply()])
    assert seen == [None], "None lets the closure read the setting itself"


def test_a_timeout_buys_the_second_attempt_more_time(monkeypatch):
    seen, _ = _ladder([_timeout_exc(), _Reply()])

    assert len(seen) == 2
    assert seen[1] == pytest.approx(_llm_timeout() * 2), (
        "resending into the same wall is a second guaranteed failure"
    )


def test_a_dropped_connection_does_not_buy_extra_time():
    """The deadline had nothing to do with it, and widening for every
    transient class would let a stuck call hold a worker thread for three
    deadlines whatever killed it."""
    seen, _ = _ladder([_connection_exc(), _Reply()])

    assert seen == [None, None]


def test_the_widening_is_bounded_by_the_review_budget():
    """Worst case is the configured deadline plus twice it. `timeout_seconds`
    has to stay above that or the stage gate becomes unreachable."""
    s = ReviewSettings()
    assert s.llm_timeout_seconds * 3 <= s.timeout_seconds


def test_it_widens_once_not_every_time():
    """Two attempts is a retry; a ladder that keeps doubling is a way to turn
    somebody else's slow day into an hour of held worker threads."""
    import ast
    import inspect

    import src.review.agents.base as base

    src = inspect.getsource(base.LLMReviewAgent._generate_and_parse)
    tree = ast.parse(src.lstrip())
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
    attempts = [n for n in loops
                if isinstance(n.iter, ast.Tuple) and len(n.iter.elts) == 2]
    assert attempts, "the attempt budget is no longer two"


# ─── the deadline is written down where the failure is ───────────────


def test_the_failure_log_names_the_deadline():
    """A timeout whose bound is not recorded reports a symptom with the cause
    left out — a reader cannot tell 120 seconds of patience from 900 without
    going to look."""
    import inspect

    import src.review.agents.base as base

    src = inspect.getsource(base)
    failures = [ln for ln in src.splitlines() if "agent_llm_failed agent=" in ln]
    assert failures, "no failure log line left"
    # Each log call's format spans two lines; check the deadline is in the call.
    assert src.count("deadline=%.0fs") >= 3, (
        "a failure log without the bound it hit"
    )
