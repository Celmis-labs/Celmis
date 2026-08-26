"""The agent pool is bounded now, and only LLM agents count against the bound.

`_run_agents_parallel` used to size its executor `len(active)`: six agents,
six simultaneous provider connections per PR. Benchmarked against a real fork
set, that was the source of ConnectError on a weak uplink — 5 of the 9 agent
failures in one run — and of 503s from Gemini. `ReviewSettings.
agent_concurrency` (default 3, env REVIEW_AGENT_CONCURRENCY) is the ceiling.

The deterministic agents are deliberately OUTSIDE the bounded pool. The bound
exists for provider connections; ast-grep and osv-scanner open none, and a
slow osv scan seated in the pool would silently shrink the LLM bound below
the configured number.

Asserted at the executor seam — which pool was built with which bound, and
which agents were submitted into it — because that is deterministic: a
timing-based "at most 3 ran at once" either sleeps for real (forbidden here)
or passes by luck on the broken code it exists to catch.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import src.review.orchestrator as orch_mod
from src.review.agents.base import AgentRunResult, LLMReviewAgent, ReviewAgent
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings


class _LlmAgent(LLMReviewAgent):
    """An LLM-backed agent that answers instantly — the class is the point:
    LLMReviewAgent IS the "calls a provider" contract the bound keys on."""

    def __init__(self, name: str) -> None:
        self.name = name

    def review(self, context) -> AgentRunResult:
        return AgentRunResult(agent=self.name)


class _LocalAgent(ReviewAgent):
    """Deterministic, no provider connection — the structural/CVE shape."""

    def __init__(self, name: str) -> None:
        self.name = name

    def review(self, context) -> AgentRunResult:
        return AgentRunResult(agent=self.name)


def _recording_executor(created: list):
    class _Pool(ThreadPoolExecutor):
        def __init__(self, max_workers=None, **kwargs):
            super().__init__(max_workers=max_workers, **kwargs)
            self.bound = max_workers
            self.agents_in = []
            created.append(self)

        def submit(self, fn, *args, **kwargs):
            self.agents_in.append(getattr(fn, "__self__", None))
            return super().submit(fn, *args, **kwargs)

    return _Pool


def _run(monkeypatch, settings: ReviewSettings, agents: list) -> tuple[list, list]:
    created: list = []
    monkeypatch.setattr(orch_mod, "ThreadPoolExecutor", _recording_executor(created))
    orchestrator = ReviewOrchestrator(
        settings=settings, agents=agents, verifier=MagicMock(),
    )
    results = orchestrator._run_agents_parallel(MagicMock())
    return created, results


def _pool_holding(created: list, agent) -> object:
    return next(p for p in created if agent in p.agents_in)


def test_four_llm_agents_share_three_slots_and_the_scanners_wait_for_none(monkeypatch):
    llm = [_LlmAgent(n) for n in ("architect", "security", "quality", "tests")]
    local = [_LocalAgent("structural"), _LocalAgent("cve")]
    created, results = _run(monkeypatch, ReviewSettings(), llm + local)

    llm_pool = _pool_holding(created, llm[0])
    assert llm_pool.bound == 3, "default is half the six-agent roster"
    assert set(llm_pool.agents_in) == set(llm), (
        "every LLM agent must sit under the bound — one outside it is one "
        "uncounted provider connection"
    )
    local_pool = _pool_holding(created, local[0])
    assert local_pool is not llm_pool, (
        "a scanner seated in the LLM pool silently shrinks the LLM bound"
    )
    assert set(local_pool.agents_in) == set(local)
    assert {r.agent for r in results} == {a.name for a in llm + local}, (
        "the split must not lose anybody's result"
    )


def test_the_bound_is_the_configured_setting(monkeypatch):
    llm = [_LlmAgent(n) for n in ("architect", "security", "quality", "tests")]
    created, _ = _run(monkeypatch, ReviewSettings(agent_concurrency=2), llm)

    assert _pool_holding(created, llm[0]).bound == 2


def test_a_nonsense_bound_wedges_at_serial_not_at_zero(monkeypatch):
    """Fail closed: a zero or negative setting must serialise the agents,
    never wedge the review — ThreadPoolExecutor(0) raises."""
    llm = [_LlmAgent("architect")]
    created, results = _run(monkeypatch, ReviewSettings(agent_concurrency=0), llm)

    assert _pool_holding(created, llm[0]).bound == 1
    assert len(results) == 1 and results[0].error is None
