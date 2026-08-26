"""The reply budget is per agent now, and it has to arrive per agent.

One global `agent_max_output_tokens` was the fix for the 43% architect failure
rate — 4096 was sized for a model that spends its output budget on the answer,
and Gemini 3.x spends most of it thinking first. A single global is the wrong
shape for the fix: the architect wants room for a long findings array, the
verifier replies with a list of integers, and compliance replies with one
sentence. Raising one number for all of them buys the architect its room by
paying for three agents that never needed it.

So the number resolves per agent, through one chain:

    repo policy → workspace `agents` entry → workspace-wide → ReviewSettings

with every field optional at every layer. The tests below assert each layer
beats the one under it, and — the part that matters — that the resolved number
is the one `litellm.completion` is actually handed. A resolver nobody's request
reaches is the class of bug this project has shipped twice.

The verifier's 1024 and compliance's 256 were literals inside those two files.
They stay as the FLOOR of the same chain, because they are deliberate for a
short structured reply — and because as literals they could be neither seen
nor raised when a reasoning model started eating them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.review.settings import (
    ReviewSettings,
    default_agent_max_output_tokens,
    resolve_agent_llm,
)

# ─── the chain, layer by layer ───────────────────────────────────────


def test_the_floor_is_the_review_setting():
    settings = ReviewSettings(agent_max_output_tokens=12345)

    resolved = resolve_agent_llm("defect", settings=settings)

    assert resolved.max_output_tokens == 12345
    assert resolved.reasoning is None, "nothing configured means nothing sent"


def test_the_workspace_model_beats_the_review_setting():
    settings = ReviewSettings(defect_model="env-model")

    resolved = resolve_agent_llm(
        "defect",
        workspace_cfg={"model": "gemini/gemini-3-flash-preview"},
        settings=settings,
    )

    assert resolved.model == "gemini/gemini-3-flash-preview"


def test_the_per_agent_entry_beats_the_workspace_model():
    settings = ReviewSettings(agent_max_output_tokens=12345)
    workspace_cfg = {
        "model": "gemini/gemini-3-flash-preview",
        "agents": {"defect": {"max_output_tokens": 32000, "reasoning": "high"}},
    }

    defect = resolve_agent_llm(
        "defect", workspace_cfg=workspace_cfg, settings=settings,
    )
    security = resolve_agent_llm(
        "security", workspace_cfg=workspace_cfg, settings=settings,
    )

    assert (defect.max_output_tokens, defect.reasoning) == (32000, "high")
    assert (security.max_output_tokens, security.reasoning) == (12345, None), (
        "an entry for one agent leaked into another — the whole point is that "
        "they differ"
    )
    assert security.model == "gemini/gemini-3-flash-preview", (
        "the workspace-wide MODEL still applies to every agent without an entry"
    )


def test_the_legacy_workspace_ceiling_does_not_cap_the_agents():
    """The landmine this resolver had to step around.

    PUT /api/llm/config has written a top-level `max_output_tokens` into the
    workspace blob since long before per-agent settings existed — on every
    save, with a default of 4096 — and no completion has ever read it. Wiring
    it into this chain would have made it live retroactively, and 4096 is
    exactly the ceiling that failed the architect agent in 43% of runs. Every
    workspace that had once opened the settings page would have been returned
    to it, silently, having changed nothing.
    """
    settings = ReviewSettings(agent_max_output_tokens=16384)

    resolved = resolve_agent_llm(
        "defect",
        workspace_cfg={"max_output_tokens": 4096, "temperature": 0.1},
        settings=settings,
    )

    assert resolved.max_output_tokens == 16384, (
        "the legacy workspace-wide 4096 became the agents' ceiling again — "
        "that number is the bug this whole change was written after"
    )


def test_the_repo_policy_beats_the_workspace():
    settings = ReviewSettings(agent_max_output_tokens=12345)

    resolved = resolve_agent_llm(
        "defect",
        policy={
            # The LEGACY column: repo policies store quality_model from
            # before the restructure, and the resolver maps it to the agent
            # that inherited the remit (defect). A new-name key would be
            # asserting a column the table does not have.
            "quality_model": "anthropic/claude-sonnet-4-5",
            "agents": {"defect": {"max_output_tokens": 40000, "reasoning": "low"}},
        },
        workspace_cfg={
            "model": "gemini/gemini-3-flash-preview",
            "agents": {"defect": {"max_output_tokens": 32000, "reasoning": "high"}},
        },
        settings=settings,
    )

    assert resolved.model == "anthropic/claude-sonnet-4-5"
    assert resolved.max_output_tokens == 40000
    assert resolved.reasoning == "low"


def test_an_absent_field_inherits_rather_than_blanking():
    """Every field is optional at every layer. A per-agent entry that names
    only the reasoning must not reset the ceiling to the floor."""
    settings = ReviewSettings(agent_max_output_tokens=12345)

    resolved = resolve_agent_llm(
        "defect",
        workspace_cfg={"agents": {"defect": {"reasoning": "high"}}},
        settings=settings,
    )

    assert resolved.max_output_tokens == 12345
    assert resolved.reasoning == "high"


def test_a_malformed_layer_costs_that_layer_and_nothing_more():
    """The blob is edited through a UI. One bad corner must not fail a review."""
    settings = ReviewSettings(agent_max_output_tokens=12345)

    resolved = resolve_agent_llm(
        "defect",
        workspace_cfg={"agents": {"defect": {"max_output_tokens": "lots"}}},
        settings=settings,
    )

    assert resolved.max_output_tokens == 12345


def test_the_two_short_reply_agents_keep_their_own_floors():
    """Their own numbers, not the shared one — for different reasons now.

    Compliance still answers {"passes": bool, "reason": "one sentence"} and
    256 has never been measured against a truncation. The verifier's 1024 was
    measured: 128 of 144 calls in a 50-PR benchmark run hit it, so the agent
    that decides WHICH findings survive was cut off mid-answer nine times out
    of ten. It now shares the general ceiling. What this test pins is that the
    two remain SEPARATE settings, so either can move without the other.
    """
    settings = ReviewSettings(agent_max_output_tokens=16384)

    assert default_agent_max_output_tokens("compliance", settings) == 256
    assert default_agent_max_output_tokens("defect", settings) == 16384
    assert default_agent_max_output_tokens("verifier", settings) == (
        settings.verifier_max_output_tokens
    )
    assert settings.verifier_max_output_tokens >= 8192, (
        f"verifier ceiling {settings.verifier_max_output_tokens} is back in the "
        f"range where 89% of calls were truncated"
    )


def test_a_per_agent_setting_can_raise_a_short_reply_floor():
    """Which is the reason they became settings — a reasoning model spends
    that budget thinking before it writes the first character."""
    settings = ReviewSettings()

    resolved = resolve_agent_llm(
        "compliance",
        workspace_cfg={"agents": {"compliance": {"max_output_tokens": 4000}}},
        settings=settings,
    )

    assert resolved.max_output_tokens == 4000


# ─── the orchestrator wires the chain to the agents ──────────────────


def test_the_orchestrator_resolves_every_configurable_agent(monkeypatch):
    from src.api.routers import llm as llm_router
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.settings import REVIEW_AGENTS

    monkeypatch.setattr(
        llm_router, "_load_workspace_config",
        lambda workspace_id="default": {
            "model": "gemini/gemini-3-flash-preview",
            "agents": {"defect": {"max_output_tokens": 40000, "reasoning": "high"}},
        },
    )

    _client, by_agent = ReviewOrchestrator()._build_llm_client(
        "u", "ws-test", policy=None,
    )

    assert set(by_agent) == set(REVIEW_AGENTS), (
        f"agents missing from the resolved map: {set(REVIEW_AGENTS) - set(by_agent)} "
        "— an agent absent here falls through to a default nobody chose"
    )
    assert by_agent["defect"].max_output_tokens == 40000
    assert by_agent["defect"].reasoning == "high"
    # The PROPERTY is "security inherits the floor", not "the floor is 16384":
    # REVIEW_AGENT_MAX_OUTPUT_TOKENS in a developer's .env changes the number
    # and must not fail the test — a test that fails on a dev machine and
    # passes in CI teaches people to ignore it.
    from src.review.settings import get_review_settings
    assert by_agent["security"].max_output_tokens == (
        get_review_settings().agent_max_output_tokens
    )
    assert by_agent["compliance"].model == "gemini/gemini-3-flash-preview", (
        "compliance was the one agent missing from the map, so it silently "
        "ignored the model the workspace had chosen"
    )


# ─── and the number reaches litellm ──────────────────────────────────


VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 100
    completion_tokens = 40
    prompt_tokens_details = None
    total_cost = 0.01


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeLiteLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        return _Completion(self.reply)

    @property
    def budgets(self) -> list[int | None]:
        return [kw.get("max_tokens") for kw in self.kwargs_seen]


@pytest.fixture(autouse=True)
def _fresh_capability_state():
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


@pytest.fixture
def wire(monkeypatch):
    """Install a fake `litellm.completion` and a workspace config to resolve
    from — the two ends of the chain this module is about."""
    import litellm

    from src.api.routers import llm as llm_router

    def _install(reply: str, workspace_cfg: dict) -> _FakeLiteLLM:
        fake = _FakeLiteLLM(reply)
        monkeypatch.setattr(litellm, "completion", fake)
        monkeypatch.setattr(
            llm_router, "_load_workspace_config",
            lambda workspace_id="default": dict(workspace_cfg),
        )
        return fake

    return _install


def _client(tmp_path: Path, model: str = "openai/gpt-4"):
    from src.llm.client import LLMClient
    from src.security.audit import AuditLogger

    return LLMClient(
        resolve_key=lambda provider: "sk-test",
        resolve_model=lambda agent: model,
        surface="review",
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        workspace_id="ws-test",
    )


def _pr():
    from src.review.models import Hunk, PullRequest

    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )


def _findings():
    from src.review.models import Finding, FindingSeverity

    return [
        Finding(file_path="a.py", line=i + 1, severity=FindingSeverity.WARNING,
                title=f"t{i}", body=f"b{i}", agent="security",
                rule_id=f"rule.{i}", confidence=0.9)
        for i in range(3)
    ]


def _agent():
    from src.review.agents.base import LLMReviewAgent

    class _Architect(LLMReviewAgent):
        name = "defect"
        system_prompt = "find problems"

        def _build_prompt(self, context):
            return "p"

    return _Architect()


def test_the_agents_configured_ceiling_is_what_the_provider_is_asked_for(
    tmp_path, wire,
):
    from src.review.agents.base import AgentContext
    from src.review.settings import resolve_agent_llm

    workspace_cfg = {"agents": {"defect": {"max_output_tokens": 3000}}}
    fake = wire(VALID, workspace_cfg)
    ctx = AgentContext(
        pull_request=_pr(),
        llm_client=_client(tmp_path),
        agent_llm={
            "defect": resolve_agent_llm("defect", workspace_cfg=workspace_cfg),
        },
    )

    _agent().review(ctx)

    assert fake.budgets == [3000], (
        f"the provider was asked for {fake.budgets}; the resolver said 3000 and "
        "the request never heard about it"
    )


def test_the_verifier_no_longer_asks_for_a_budget_it_exhausts(tmp_path, wire):
    from src.review.agents.base import AgentContext
    from src.review.agents.verifier import VerifierAgent

    fake = wire('{"keep": [0, 1, 2], "reasons": {}}', {})
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    result = VerifierAgent().verify(_findings(), ctx)

    # The number itself depends on what the resolved model admits, so what is
    # pinned here is the property the measurement was about: the verifier no
    # longer asks for a budget it was shown to exhaust. 128 of 144 calls in a
    # 50-PR run stopped at 1024, and the agent deciding WHICH findings survive
    # was cut off mid-answer nine times in ten.
    assert fake.budgets and all(b > 1024 for b in fake.budgets), (
        f"the verifier asked for {fake.budgets} — back at or below the 1024 "
        f"that truncated 89% of its replies"
    )
    assert len(result.kept) == 3


def test_a_workspace_can_raise_the_verifiers_ceiling(tmp_path, wire):
    from src.review.agents.base import AgentContext
    from src.review.agents.verifier import VerifierAgent

    fake = wire(
        '{"keep": [0, 1, 2], "reasons": {}}',
        {"agents": {"verifier": {"max_output_tokens": 4000}}},
    )
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    VerifierAgent().verify(_findings(), ctx)

    assert fake.budgets == [4000], (
        "1024 stayed a literal — a per-agent setting could not lift it"
    )


def test_compliance_asks_for_its_short_default(tmp_path, wire, monkeypatch):
    from src.review import compliance
    from src.review.agents.base import AgentContext
    from src.review.compliance import ComplianceCheckSpec, run_compliance

    fake = wire('{"passes": true, "reason": "ok"}', {})
    monkeypatch.setattr(
        compliance, "load_active_checks",
        lambda repo: [ComplianceCheckSpec(
            id="c1", name="n", scope="workspace", glob_pattern="**",
            rule="r", severity="error", blocking=False,
        )],
    )
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    run_compliance(ctx)

    assert fake.budgets == [256], (
        f"compliance asked for {fake.budgets} — one sentence of JSON has a "
        "deliberate 256-token default"
    )


def test_a_workspace_can_raise_the_compliance_ceiling(tmp_path, wire, monkeypatch):
    from src.review import compliance
    from src.review.agents.base import AgentContext
    from src.review.compliance import ComplianceCheckSpec, run_compliance

    fake = wire(
        '{"passes": true, "reason": "ok"}',
        {"agents": {"compliance": {"max_output_tokens": 2000}}},
    )
    monkeypatch.setattr(
        compliance, "load_active_checks",
        lambda repo: [ComplianceCheckSpec(
            id="c1", name="n", scope="workspace", glob_pattern="**",
            rule="r", severity="error", blocking=False,
        )],
    )
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    run_compliance(ctx)

    assert fake.budgets == [2000], (
        "256 stayed a literal — a per-agent setting could not lift it"
    )


def test_the_reasoning_level_configured_for_one_agent_reaches_its_call(
    tmp_path, wire,
):
    """End to end: workspace blob → resolver → AgentContext → litellm kwargs.

    The whole chain in one assertion, because every previous version of this
    setting was correct at one end and absent at the other.
    """
    from src.review.agents.base import AgentContext
    from src.review.settings import resolve_agent_llm

    workspace_cfg = {
        "model": "gemini/gemini-3-flash-preview",
        "agents": {"defect": {"reasoning": "low"}},
    }
    fake = wire(VALID, workspace_cfg)
    ctx = AgentContext(
        pull_request=_pr(),
        llm_client=_client(tmp_path, "gemini/gemini-3-flash-preview"),
        agent_llm={
            "defect": resolve_agent_llm("defect", workspace_cfg=workspace_cfg),
        },
    )

    _agent().review(ctx)

    assert fake.kwargs_seen[0].get("reasoning_effort") == "low"


# ─── the fallback model rides the same resolver — workspace level only ──


def test_the_fallback_model_comes_from_the_workspace_card():
    resolved = resolve_agent_llm(
        "defect",
        workspace_cfg={"review_fallback_model": "gemini/gemini-3-flash-preview"},
        settings=ReviewSettings(),
    )

    assert resolved.fallback_model == "gemini/gemini-3-flash-preview"


def test_nothing_configured_means_no_fallback_at_all():
    resolved = resolve_agent_llm("defect", settings=ReviewSettings())

    assert resolved.fallback_model is None, (
        "a fallback nobody named must not be invented — the failure has to "
        "stay the failure"
    )


def test_the_policy_layer_cannot_smuggle_a_fallback_in():
    """Workspace level ONLY, for now — one layer means one place a surprising
    fallback call can have come from. The policy can inherit a slot in the
    chain the day a repo needs its own; today a policy key is a no-op."""
    resolved = resolve_agent_llm(
        "defect",
        policy={
            "review_fallback_model": "policy-model",
            "agents": {"defect": {"fallback_model": "entry-model"}},
        },
        workspace_cfg={"review_fallback_model": "workspace-model"},
        settings=ReviewSettings(),
    )

    assert resolved.fallback_model == "workspace-model"
