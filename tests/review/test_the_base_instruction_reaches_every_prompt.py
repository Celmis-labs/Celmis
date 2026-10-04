"""The base instruction — how every suggestion is written — reaches every prompt.

Kodus calls it the "Base instruction": one text, at most 2000 characters,
that governs how every suggestion in a review is worded. In Celmis it is a
review-policy setting (`base_instruction`, read through `_policy_value` like
every other policy key) and it must reach:

  * EVERY LLM finder's system prompt — after the agent's own prompt, before
    any custom rule — whichever layer that prompt came from;
  * the verifier's prompt, with the rider that wording is never a reason to
    drop a finding;
  * nothing past 2000 characters.

The finders are taken from the orchestrator's roster, not listed, so a finder
added later is covered the day it is added.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.review.agents.base import (
    BASE_INSTRUCTION_MAX_CHARS,
    AgentContext,
    LLMReviewAgent,
    clamp_base_instruction,
)
from src.review.models import Hunk, PullRequest

MARK = "BASE-INSTRUCTION-MARK: write every suggestion as one imperative sentence."
RULE = "RULE-MARK: never call os.system."


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=5, title="Cap uploads at 10 MB",
        description=("Uploads larger than ten megabytes are rejected with a clear "
                     "error message before anything is written to storage."),
        author="a", base_ref="main", base_sha="x", head_ref="f", head_sha="y",
        state="open",
        hunks=[Hunk(file_path="app/upload.py", old_file_path="app/upload.py",
                    old_start=1, old_count=1, new_start=1, new_count=2,
                    content="@@ -1 +1,2 @@\n x\n+LIMIT = 10\n")],
    )


@pytest.fixture(autouse=True)
def _no_workspace_layers(monkeypatch):
    """No workspace prompt override and no workspace extras: the built-in
    prompt is the base, so its position in the composed text is knowable."""
    from src.api.routers import agents as agents_router
    from src.api.routers import llm as llm_router

    monkeypatch.setattr(agents_router, "_load_override", lambda *a, **k: None)
    monkeypatch.setattr(llm_router, "_load_workspace_config",
                        lambda workspace_id="default": {})


def _finders() -> list[LLMReviewAgent]:
    from src.review.orchestrator import ReviewOrchestrator

    return [a for a in ReviewOrchestrator._default_agents()
            if isinstance(a, LLMReviewAgent)]


def _client() -> MagicMock:
    r = MagicMock()
    r.text, r.input_tokens, r.output_tokens = "[]", 10, 2
    r.cost_usd, r.cost_source, r.model = 0.0, "litellm_estimate", "m"
    client = MagicMock()
    client.generate.return_value = r
    return client


def _ctx(**kw) -> AgentContext:
    return AgentContext(pull_request=_pr(), base_instruction=MARK,
                        custom_rules=RULE, **kw)


def test_the_roster_has_finders():
    assert len(_finders()) >= 5


@pytest.mark.parametrize("agent", _finders(), ids=lambda a: a.name)
def test_every_finder_sends_it_between_its_prompt_and_the_rules(agent):
    client = _client()
    agent.review(_ctx(llm_client=client))

    assert client.generate.call_count >= 1, f"{agent.name} made no call"
    for call in client.generate.call_args_list:
        system = call.kwargs["system_instruction"]
        assert MARK in system, agent.name
        own = system.index(agent.system_prompt.strip()[:60])
        assert own < system.index(MARK) < system.index(RULE), agent.name


@pytest.mark.parametrize("agent", _finders(), ids=lambda a: a.name)
def test_a_repo_prompt_override_does_not_drop_it(agent):
    client = _client()
    agent.review(_ctx(llm_client=client, repo_agent_prompts={
        agent.name: 'Custom prompt for this repo. Answer with "reasoning" first.'}))
    system = client.generate.call_args.kwargs["system_instruction"]
    assert system.startswith("Custom prompt for this repo.")
    assert MARK in system


def test_without_one_the_prompt_is_unchanged():
    from src.review.agents.base import _compose_effective_system_prompt
    from src.review.agents.defect import DefectAgent

    plain = AgentContext(pull_request=_pr(), custom_rules=RULE)
    composed = _compose_effective_system_prompt(
        agent_name="defect", default_system=DefectAgent.system_prompt, context=plain)
    assert "Base instruction" not in composed


def test_the_verifier_hears_it_and_is_told_not_to_veto_on_wording():
    from src.review.agents.verifier import _VERIFIER_SYSTEM, verifier_system_prompt

    prompt = verifier_system_prompt(_ctx())
    assert prompt.startswith(_VERIFIER_SYSTEM.strip()[:40])
    assert MARK in prompt
    assert "never drop a finding for its wording" in prompt

    plain = verifier_system_prompt(AgentContext(pull_request=_pr()))
    assert MARK not in plain and "Base instruction" not in plain


def test_it_is_capped_at_two_thousand_characters():
    assert BASE_INSTRUCTION_MAX_CHARS == 2000
    long_text = "x" * 1990 + "TAIL-BEYOND-THE-CAP" + "y" * 3000
    clamped = clamp_base_instruction(long_text)
    assert len(clamped) == 2000
    assert "TAIL-BEYOND-THE-CAP" not in clamped
    assert clamp_base_instruction(None) == ""
    assert clamp_base_instruction("  ") == ""


def test_the_orchestrator_reads_it_from_the_policy_and_clamps_it(monkeypatch):
    from types import SimpleNamespace

    import src.review.orchestrator as orch_mod

    graph = SimpleNamespace(summary="", brief="", note=None, cross_repo_callers_count=0)
    orch = orch_mod.ReviewOrchestrator(agents=[])
    monkeypatch.setattr(orch_mod, "build_graph_context",
                        lambda pr, workspace_id="default": graph)
    monkeypatch.setattr(orch, "_build_cross_repo_drift", lambda pr, **kw: "")
    monkeypatch.setattr(orch, "_build_mcp_evidence", lambda pr, **kw: "")
    monkeypatch.setattr(orch, "_build_llm_client", lambda *a: (None, {}))
    monkeypatch.setattr(orch, "_load_repo_overview", lambda pr: "")
    monkeypatch.setattr(orch, "_load_style_guide", lambda pr: "")

    ctx = orch._build_context(_pr(), policy={"base_instruction": "  " + "z" * 2500})
    assert ctx.base_instruction == "z" * 2000

    assert orch._build_context(_pr(), policy=None).base_instruction == ""
    assert orch._build_context(
        _pr(), policy={"base_instruction": None}).base_instruction == ""


def test_the_compliance_auditor_hears_it_and_keeps_its_json():
    """Compliance is an LLM call too; its reply is a strict-JSON verdict
    under a 256-token floor, so the instruction is scoped to `reason`."""
    from src.review.compliance import _SYSTEM, compliance_system_prompt

    prompt = compliance_system_prompt(_ctx())
    assert prompt.startswith(_SYSTEM.rstrip())
    assert MARK in prompt
    assert "still exactly the JSON object" in prompt
    assert compliance_system_prompt(AgentContext(pull_request=_pr())) == _SYSTEM
