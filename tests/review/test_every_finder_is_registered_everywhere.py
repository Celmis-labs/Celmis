"""Every LLM finder is known to every list that decides what it may do.

2.3 adds two finders along Kodus's categories — `performance` and
`business_logic`. The roster has been renamed once before and three literals
in one router went stale on the same day (see
tests/api/test_the_policy_router_asks_the_roster_not_a_literal.py). An agent
missing from one of these lists does not fail loudly: its prompt override is
dropped on save, its model setting is refused, its failure reads as "found
nothing", or its findings are filed under "Other".

So the expected membership is DERIVED from the orchestrator's roster here,
never restated, and each list below is checked against it. The web files are
read with their comments stripped: a name surviving in prose must not count as
a name surviving in code.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    POLICY,
    WEB,
    _strip_comments,
)

API_TS = WEB / "lib" / "api.ts"
CATEGORIES_TS = WEB / "lib" / "review-categories.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _finders() -> set[str]:
    from src.review.agents.base import LLMReviewAgent
    from src.review.orchestrator import ReviewOrchestrator

    return {a.name for a in ReviewOrchestrator._default_agents()
            if isinstance(a, LLMReviewAgent)}


def _ts_string_list(path, const: str) -> list[str]:
    code = _strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(rf"export const {const} = \[([^\]]*)\]", code)
    assert m, f"{const} is gone from {path.name}"
    return re.findall(r'"(\w+)"', m.group(1))


def _ts_record(path, const: str) -> dict[str, str]:
    code = _strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(rf"export const {const}: Record<string, string> = \{{([^}}]*)\}}", code)
    assert m, f"{const} is gone from {path.name}"
    return dict(re.findall(r'(\w+):\s*"([^"]*)"', m.group(1)))


# ─── the roster itself ───────────────────────────────────────────────


def test_the_category_finders_are_in_the_roster():
    """Guards the guards: without them every check below is vacuous for the
    two agents this file exists for."""
    assert {"performance", "business_logic"} <= _finders()


def test_the_default_roster_runs_performance_and_not_business_logic():
    from src.review.orchestrator import ReviewOrchestrator

    assert frozenset({"business_logic"}) == ReviewOrchestrator.OFF_BY_DEFAULT
    assert _finders() >= ReviewOrchestrator.OFF_BY_DEFAULT
    assert ReviewOrchestrator._dormant_agents(None) == {"business_logic"}


# ─── the server's lists ──────────────────────────────────────────────


def test_every_finder_is_a_configurable_agent():
    """REVIEW_AGENTS feeds the per-agent model/limits resolver, the workspace
    `agents` blob validator and the repo policy's `agent_llm_overrides`."""
    from src.review.settings import REVIEW_AGENTS

    assert _finders() <= set(REVIEW_AGENTS)


def test_every_finder_has_an_install_model():
    """`resolve_agent_llm`'s floor is `<agent>_model` on ReviewSettings."""
    from src.review.settings import ReviewSettings, default_agent_model

    s = ReviewSettings()
    for name in _finders():
        assert getattr(s, f"{name}_model", None), name
        assert default_agent_model(name, s) == getattr(s, f"{name}_model")


def test_every_finder_gets_a_resolved_llm_entry():
    from src.review.settings import resolve_agent_llm

    for name in _finders():
        resolved = resolve_agent_llm(name, policy=None, workspace_cfg={
            "agents": {name: {"max_output_tokens": 777}}})
        assert resolved.max_output_tokens == 777, name


def test_every_finder_takes_a_per_repo_prompt_and_a_preview_and_a_rule():
    from src.api.routers.review_policies import (
        _OVERRIDABLE_AGENTS,
        _PREVIEWABLE_PATTERN,
        _rule_target_agents,
    )

    rx = re.compile(_PREVIEWABLE_PATTERN)
    for name in _finders():
        assert name in _OVERRIDABLE_AGENTS, name
        assert rx.fullmatch(name), name
        assert name in _rule_target_agents(), name


def test_every_finder_can_be_switched_off():
    from src.api.routers.review_policies import TOGGLEABLE_AGENTS

    assert _finders() <= set(TOGGLEABLE_AGENTS)


def test_every_finder_is_on_the_admin_agents_page_with_its_own_prompt():
    """/admin/agents lists `_AGENTS`; a workspace prompt override is refused
    404 for a name not in it, and the built-in prompt shown is the module's
    `_SYSTEM`."""
    from src.api.routers import agents as agents_router

    for name in _finders():
        assert name in agents_router._AGENTS, name
        assert len(agents_router._default_system_prompt(name)) > 200, name
        assert agents_router._default_user_template(name), name


def test_every_finder_is_critical():
    from src.review.models import ReviewBatch

    assert _finders() <= set(ReviewBatch._CRITICAL_AGENTS)


def test_every_finder_has_a_category():
    from src.review.categories import AGENT_CATEGORY

    assert _finders() <= set(AGENT_CATEGORY)


@pytest.mark.parametrize(("agent", "label"), [
    ("defect", "Bug"), ("contract", "Contract"), ("security", "Security"),
    ("performance", "Performance"), ("business_logic", "Business logic"),
    ("compliance", "Compliance"),
])
def test_the_kodus_categories_map_as_named(agent, label):
    from src.review.categories import category_of

    assert category_of(agent) == label


def test_a_merged_finding_is_filed_under_its_first_agent():
    from src.review.categories import category_of

    assert category_of("performance,security") == "Performance"
    assert category_of("", "perf.n-plus-one") == "Performance"
    assert category_of(None, "nonsense") == "Other"


def test_the_issue_tracker_files_the_new_agents():
    from src.review.issues import categorize

    assert categorize("performance", "perf.x", "Loop does work") == "performance"
    assert categorize("business_logic", "logic.contradiction", "Limit differs") == "bug"


# ─── the web's lists ─────────────────────────────────────────────────


def test_the_web_configurable_agents_are_the_servers():
    from src.review.settings import REVIEW_AGENTS

    assert _ts_string_list(API_TS, "REVIEW_AGENTS") == list(REVIEW_AGENTS)


def test_the_web_off_by_default_list_is_the_servers():
    from src.review.orchestrator import ReviewOrchestrator

    assert set(_ts_string_list(API_TS, "OFF_BY_DEFAULT_AGENTS")) == set(
        ReviewOrchestrator.OFF_BY_DEFAULT)


def test_the_web_category_map_is_the_servers():
    from src.review.categories import AGENT_CATEGORY

    assert _ts_record(CATEGORIES_TS, "AGENT_CATEGORY") == AGENT_CATEGORY


def test_every_finder_has_a_web_label():
    labels = _ts_record(CATEGORIES_TS, "AGENT_LABEL")
    for name in _finders() | {"verifier", "compliance"}:
        assert name in labels, name


def test_the_settings_switches_cover_every_finder():
    """The Review categories section draws one switch per agent of the
    server's participation map (`agent_participation_defaults`), not a list
    of its own; an opt-in agent (built-in off) is switched through
    `enabled_agents`, the others through `disabled_agents`."""
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.review_defaults import AGENT_PARTICIPATION_DEFAULTS

    section = WEB / "components" / "review-settings" / "section-categories.tsx"
    code = _strip_comments(section.read_text(encoding="utf-8"))
    assert "meta.participationDefaults" in code
    assert "defaults[agent] === false" in code, "opt-in agents lost their badge"
    m = re.search(r"const FINDER_ORDER = \[([^\]]*)\]", code)
    assert m
    order = set(re.findall(r'"(\w+)"', m.group(1)))
    assert order <= set(AGENT_PARTICIPATION_DEFAULTS), "the order names an unknown agent"
    # Every finder the orchestrator runs can be switched, and the opt-in
    # ones are exactly the ones whose built-in is off.
    assert _finders() <= set(AGENT_PARTICIPATION_DEFAULTS)
    off = {a for a, on in AGENT_PARTICIPATION_DEFAULTS.items() if not on}
    assert off & _finders() == ReviewOrchestrator.OFF_BY_DEFAULT & _finders()
    model = _strip_comments(POLICY.read_text(encoding="utf-8"))
    switch = model[model.index("export function switchAgent("):]
    assert "defaults[agent] === false" in switch and "enabled_agents" in switch


@pytest.mark.parametrize("path", sorted(MESSAGES.glob("*.json")), ids=lambda p: p.stem)
def test_every_finder_has_a_role_line_in_every_locale(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for name in _finders():
        assert f"admin.reviewPolicies.agentRole.{name}" in data, (path.name, name)


def test_the_role_lines_are_written_in_english_and_ukrainian():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for name in ("performance", "business_logic"):
        key = f"admin.reviewPolicies.agentRole.{name}"
        assert en[key] != uk[key], name
        assert re.search(r"[а-яіїєґ]", uk[key]), name


# ─── what the assistant knows ────────────────────────────────────────


def test_the_knowledge_names_every_finder_and_the_base_instruction():
    from src.automation.knowledge import BY_ID

    how = BY_ID["review-how"].body
    prompts = BY_ID["agent-prompts"].body
    for name in _finders():
        assert name in how, name
        assert name in prompts, name
    assert "base instruction" in prompts.lower()
    assert "2,000" in prompts
