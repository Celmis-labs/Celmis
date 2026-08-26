"""One output contract, four agents, and the parser enforces it.

The architect carried its own reasoning-first schema (e01531d); security
paraphrased it; quality and tests said "like the others" and showed no shape
at all. Three wordings of one contract — and since `_dict_to_finding` now
drops a finding with no `reasoning`, a prompt that forgets to ask for one
loses every finding it produces. So the shape is one constant, spliced into
all four, in the order the benchmark rewards: reasoning → finding →
confidence. The avoid-list is the same kind of constant for the same reason:
the downstream deny-list keys on its names, and two copies drift.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    AVOIDED_CATEGORIES,
    FINDING_OUTPUT_FORMAT,
    AgentContext,
    _compose_effective_system_prompt,
)
from src.review.agents.contract import ContractAgent
from src.review.agents.defect import DefectAgent
from src.review.agents.security import SecurityAgent
from src.review.models import PullRequest

AGENTS = [DefectAgent, ContractAgent, SecurityAgent]


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.name)
def test_every_agent_asks_in_the_shared_shape_exactly_once(agent):
    assert agent.system_prompt.count(FINDING_OUTPUT_FORMAT) == 1


def test_the_shape_is_reasoning_then_finding_then_confidence():
    fmt = FINDING_OUTPUT_FORMAT
    assert fmt.index('"reasoning"') < fmt.index('"file"') < fmt.index('"confidence"')
    assert fmt.rindex('"confidence"') > fmt.rindex('"suggestion"'), "confidence is last"


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.name)
def test_every_agent_is_told_why_the_order_matters(agent):
    prompt = _flat(agent.system_prompt)
    assert "written FIRST" in prompt
    assert "the reasoning sentence you cannot finish is the finding you do not have" in prompt
    assert "written LAST" in prompt


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.name)
def test_every_agent_carries_the_one_avoid_list(agent):
    assert agent.system_prompt.count(AVOID_LIST_PROMPT) == 1
    for name, cat in AVOIDED_CATEGORIES.items():
        assert f"- {name} (" in agent.system_prompt
        for rule_id in cat.rule_ids:
            assert rule_id in agent.system_prompt


def test_the_avoid_list_names_the_measured_categories():
    """The four the instruction named, each with the rule id it would have
    carried — the name the prompt uses and the name the deny-list uses."""
    assert {"style", "tests-unnamed", "todo", "typing"} <= set(AVOIDED_CATEGORIES)
    for name, cat in AVOIDED_CATEGORIES.items():
        assert cat.what.strip(), name
        assert cat.rule_ids, f"{name} names no rule id to cross-reference"


def test_everything_the_deny_list_hides_the_prompts_already_forbid():
    """The drift guard. `ReviewSettings.suppressed_rules` is the prefilter's
    rule deny-list; a rule id in it that no avoided category names is a
    comment the agents are still asked to write and the filter then hides —
    tokens spent, nothing posted, and nobody reading either file can tell."""
    from src.review.settings import get_review_settings

    suppressed = getattr(get_review_settings(), "suppressed_rules", None)
    if suppressed is None:
        pytest.skip("ReviewSettings.suppressed_rules is not in this tree")
    named = {rid for cat in AVOIDED_CATEGORIES.values() for rid in cat.rule_ids}
    assert set(suppressed) <= named, sorted(set(suppressed) - named)


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.name)
def test_no_prompt_offers_a_rule_id_the_deny_list_hides(agent):
    """The rule_id examples each prompt gives are the ids the model reaches
    for. An example that the prefilter suppresses — `tests.no-coverage` was
    one — steers every legitimate finding of that kind into the bin."""
    forbidden = {rid for cat in AVOIDED_CATEGORIES.values() for rid in cat.rule_ids}
    head, _, _ = agent.system_prompt.partition(AVOID_LIST_PROMPT)
    _, _, tail = agent.system_prompt.rpartition(AVOID_LIST_PROMPT)
    for rid in forbidden:
        assert f"`{rid}`" not in head + tail, f"{agent.name} offers {rid} as an example"


def test_confidence_is_self_reported_and_not_a_threshold():
    assert "your own estimate" in _flat(FINDING_OUTPUT_FORMAT)


# ─── an override that forgot to ask ──────────────────────────────────


def _ctx(override: str | None) -> AgentContext:
    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
    )
    return AgentContext(
        pull_request=pr,
        repo_agent_prompts={"defect": override} if override else {},
    )


def test_an_override_that_does_not_ask_for_reasoning_gets_the_shape_appended():
    """Without this, a repo whose admin replaced the quality prompt last
    spring produces findings with no `reasoning`, every one of which the
    parser now drops — a review lost over a filter."""
    effective = _compose_effective_system_prompt(
        agent_name="defect",
        default_system=DefectAgent.system_prompt,
        context=_ctx("Find bugs. Reply with a JSON array of {file, line, title, body}."),
    )
    assert effective.startswith("Find bugs.")
    assert effective.count(FINDING_OUTPUT_FORMAT.strip()) == 1


def test_an_override_that_asks_in_its_own_words_is_left_alone():
    own = 'Reply as JSON: [{"reasoning": "...", "file": "...", "line": 1, "title": "..."}]'
    effective = _compose_effective_system_prompt(
        agent_name="defect", default_system=DefectAgent.system_prompt, context=_ctx(own),
    )
    assert FINDING_OUTPUT_FORMAT.strip() not in effective


def test_the_default_prompt_is_not_doubled():
    effective = _compose_effective_system_prompt(
        agent_name="defect",
        default_system=DefectAgent.system_prompt,
        context=_ctx(DefectAgent.system_prompt),
    )
    assert effective.count(FINDING_OUTPUT_FORMAT.strip()) == 1
