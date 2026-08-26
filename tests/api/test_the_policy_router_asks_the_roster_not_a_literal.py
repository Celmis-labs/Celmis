"""Three literals in one router went stale on the same rename.

The Phase-18 restructure renamed the review agents. `review_policies.py` held
the roster in three separate hand-written places, and all three broke:

  * the prompt-preview query pattern, `^(architect|security|quality|tests)$` —
    so `defect` and `contract` were refused with 422;
  * its registry, `{"architect": ArchitectAgent(), …}` — so the names it DID
    accept crashed on classes that no longer exist. Measured on production:
    422 for the live agents, 500 for the dead ones. Dead either way, and no
    test caught it because the import is lazy and nothing exercised the route.
  * the prompt-override whitelist, `{"architect", …, "verifier"}` — so a
    per-repo prompt override for `defect` or `contract`, the two boxes the
    policy page now renders, was dropped IN SILENCE on save. The worst of the
    three: a 422 argues with you, a 500 stops you, a silent drop lets you
    believe the prompt is in force.

All three now derive from `ReviewOrchestrator._default_agents()`. The tests
below assert the derivation, not the current answer — a hand-written
`{"defect","contract","security"}` here would pass today and go stale in
exactly the same way next time.
"""

from __future__ import annotations


def _roster() -> set[str]:
    from src.review.agents.base import LLMReviewAgent
    from src.review.orchestrator import ReviewOrchestrator

    return {a.name for a in ReviewOrchestrator._default_agents()
            if isinstance(a, LLMReviewAgent)}


def test_the_roster_is_not_empty():
    """Guards the guards: an empty roster would make every assertion below
    vacuously true."""
    assert _roster()


def test_the_preview_pattern_admits_exactly_the_llm_agents():
    import re

    from src.api.routers.review_policies import _PREVIEWABLE_PATTERN

    rx = re.compile(_PREVIEWABLE_PATTERN)
    for name in _roster():
        assert rx.fullmatch(name), f"{name} cannot be previewed"
    for retired in ("architect", "quality", "tests"):
        assert not rx.fullmatch(retired), f"{retired} still accepted"


def test_the_preview_default_is_an_agent_that_exists():
    """The default was `architect`, so the endpoint 500'd on a bare call."""
    import inspect

    from src.api.routers import review_policies as mod

    sig = inspect.signature(mod.prompt_preview)
    default = sig.parameters["agent"].default
    assert getattr(default, "default", None) in _roster()


def test_every_llm_agent_can_carry_a_prompt_override():
    from src.api.routers.review_policies import _OVERRIDABLE_AGENTS

    missing = _roster() - set(_OVERRIDABLE_AGENTS)
    assert not missing, (
        f"{sorted(missing)} would have its per-repo prompt override dropped "
        f"silently on save"
    )


def test_the_verifier_keeps_its_override_slot():
    """It takes a system prompt like the finders, even though it finds
    nothing itself."""
    from src.api.routers.review_policies import _OVERRIDABLE_AGENTS

    assert "verifier" in _OVERRIDABLE_AGENTS


def test_the_retired_names_cannot_carry_one():
    """Not tolerance for its own sake: accepting `architect` here would store
    an override no agent will ever read, and the page would badge the repo as
    having a custom prompt that does nothing."""
    from src.api.routers.review_policies import _OVERRIDABLE_AGENTS

    for retired in ("architect", "quality", "tests"):
        assert retired not in _OVERRIDABLE_AGENTS


def test_the_toggle_list_covers_the_llm_agents_too():
    """`disabled_agents` names what an operator may switch off. An LLM agent
    absent from it cannot be switched off at all — and one that used to be
    there and is now missing reads as "this toggle no longer works"."""
    from src.api.routers.review_policies import TOGGLEABLE_AGENTS

    missing = _roster() - set(TOGGLEABLE_AGENTS)
    assert not missing, sorted(missing)


def test_the_registry_is_built_not_listed():
    """The mechanical guard against the whole class. `prompt_preview` must not
    name an agent class; it must ask the orchestrator."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "src/api/routers/review_policies.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "prompt_preview")
    named = [
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id.endswith("Agent")
    ]
    assert not named, (
        f"prompt_preview instantiates {named} by name — the roster has to come "
        f"from the orchestrator, or the next rename breaks this route again"
    )
