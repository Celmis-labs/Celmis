"""The three-finder roster, and the seams a stored configuration crosses.

The restructure (five LLM finders → three) was measured into existence — see
agents/__init__.py for the numbers — and this file pins the CONTRACT it made
with everything that outlives a deploy: stored policies, stored env vars,
stored prompt overrides, the benchmark harness that parses comment footers,
and the run rows that carry agents_run.
"""

from __future__ import annotations

from src.review.settings import (
    LEGACY_AGENT_NAMES,
    REVIEW_AGENTS,
    ReviewSettings,
    resolve_agent_llm,
)

# ─── the roster ──────────────────────────────────────────────────────


def test_the_roster_is_the_measured_one():
    assert REVIEW_AGENTS == ("defect", "contract", "security", "verifier", "compliance")


def test_the_old_names_are_gone_from_the_roster():
    for name in ("architect", "quality", "tests"):
        assert name not in REVIEW_AGENTS


def test_the_orchestrator_dispatches_exactly_the_three_finders():
    from src.review.orchestrator import ReviewOrchestrator

    llm = [a.name for a in ReviewOrchestrator._default_agents()
           if not getattr(a, "deterministic", False)]
    # The deterministic agents carry no flag — identify the LLM ones by class.
    from src.review.agents.base import LLMReviewAgent

    llm = [a.name for a in ReviewOrchestrator._default_agents()
           if isinstance(a, LLMReviewAgent)]
    assert sorted(llm) == ["contract", "defect", "security"], (
        f"LLM finder roster is {llm} — the bench's cost and dedup arithmetic "
        "assume exactly three model calls per review"
    )


# ─── the boundary is disjoint by construction ────────────────────────


def test_the_two_remits_do_not_share_their_defining_instruction():
    """defect must forbid what contract requires. One prompt cannot hold two
    standards of evidence, and that is the entire reason there are two."""
    from src.review.agents.contract import ContractAgent
    from src.review.agents.defect import DefectAgent

    defect = " ".join(DefectAgent.system_prompt.split())
    contract = " ".join(ContractAgent.system_prompt.split())

    # defect: single-file only, cross-file explicitly handed away
    assert "it belongs to the contract reviewer" in defect
    # contract: cross-file only, single-file explicitly handed away
    assert "is the defect reviewer's finding, not yours" in contract
    # and the evidence standards are different in the direction measured
    assert "quote both sides" in contract.lower()
    assert "quote both sides" not in defect.lower()


def test_security_was_not_touched():
    """50% precision as measured. The restructure's promise to the bench was
    that this agent changes by nothing — not its prompt, not its model field.

    "Not its model field" means NOT SINGLED OUT, which is what this asserts.
    It used to name the model literally, and so it failed the day the install
    default moved for a measured reason that had nothing to do with the
    security agent: the shipped roster makes three concurrent calls and
    gemini-3.1-pro-preview refused at four. A test keyed on the value rather
    than on the relationship fails when the code improves — and the
    relationship is the promise. If a future change moves security ALONE, this
    still catches it."""
    from src.review.agents.security import SecurityAgent

    assert SecurityAgent.name == "security"
    assert "authorised code review" in SecurityAgent.system_prompt

    s = ReviewSettings()
    assert s.security_model == s.defect_model == s.contract_model, (
        "security carries a model of its own — the restructure promised it "
        "would move with the roster or not at all"
    )


# ─── stored configuration crosses the rename ─────────────────────────


def test_a_stored_policy_column_still_pins_its_successor():
    """The DB kept architect_model/quality_model — no migration — and the
    resolver maps each column to the agent that inherited the remit."""
    assert resolve_agent_llm(
        "contract", policy={"architect_model": "gpt-4o"},
    ).model == "gpt-4o"
    assert resolve_agent_llm(
        "defect", policy={"quality_model": "gpt-4o"},
    ).model == "gpt-4o"


def test_a_new_name_column_would_outrank_the_legacy_one():
    assert resolve_agent_llm(
        "contract",
        policy={"contract_model": "new-wins", "architect_model": "old-loses"},
    ).model == "new-wins"


def test_the_legacy_env_vars_still_pin(monkeypatch):
    """REVIEW_ARCHITECT_MODEL stopped being a field name; pydantic reads only
    declared fields, so without the bridge an install that pinned it would
    silently lose the pin on upgrade.

    `get_review_settings` is lru_cached for the process — correct in
    production, where the environment is fixed at start — so the test clears
    it around the calls rather than asserting through a stale cache.
    """
    from src.review.settings import get_review_settings

    monkeypatch.setenv("REVIEW_ARCHITECT_MODEL", "pinned-by-legacy-env")
    monkeypatch.delenv("REVIEW_CONTRACT_MODEL", raising=False)
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().contract_model == "pinned-by-legacy-env"
    finally:
        get_review_settings.cache_clear()


def test_the_new_env_var_beats_the_legacy_one(monkeypatch):
    from src.review.settings import get_review_settings

    monkeypatch.setenv("REVIEW_ARCHITECT_MODEL", "old")
    monkeypatch.setenv("REVIEW_CONTRACT_MODEL", "new")
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().contract_model == "new"
    finally:
        get_review_settings.cache_clear()


def test_the_alias_map_and_the_roster_agree():
    """Every alias target must be a current agent, or the mapping sends a
    stored pin to nobody."""
    for target in LEGACY_AGENT_NAMES.values():
        assert target in REVIEW_AGENTS, target


# ─── what the benchmark harness parses ───────────────────────────────


def test_the_comment_footer_format_is_unchanged():
    """The bench attributes findings by parsing `agent: \\`X\\`` from the
    posted comment's footer. The restructure changes the VALUES, never the
    format — this is the promise the night harness was built on."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "src/review/providers/base.py").read_text(encoding="utf-8")
    assert 'meta.append(f"agent: `{finding.agent}`")' in src


def test_rule_id_prefixes_follow_the_agents():
    from src.review.agents.contract import ContractAgent
    from src.review.agents.defect import DefectAgent

    assert "`defect.<rule>`" in DefectAgent.system_prompt
    assert "`contract.<rule>`" in ContractAgent.system_prompt
