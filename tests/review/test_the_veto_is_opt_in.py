"""The model's veto is off unless a repository asks, and that is a decision.

It shipped ON, and not by anyone's choice: the only way to express "off" was
to name "verifier" in a policy's `disabled_agents`, and an unconfigured
repository names nothing. A default that exists only because its opposite was
unsayable is not a default — it is the shape of a deny-list leaking into the
product.

Squeezing it into that list was the root of it. The list holds AGENTS; the
veto is a stage that runs after them over their combined output. The parallel
dispatcher never sees it, the bypass has to live in the orchestrator, and the
orchestrator says so in three places. A stage forced into an agent deny-list
can only ever be switched off, never on.

WHAT IS OFF, PRECISELY. The model's veto, and nothing else. The deterministic
prefilter — exact dedup, near-duplicate clustering, the rule deny-list, the
confidence floor, the severity sort — runs on every review either way. It used
to be switched off along with the veto, which was its own defect: a critical
finding at position 21 fell off the providers' inline-comment cap while four
copies of one warning posted.

THREE ANSWERS, MOST SPECIFIC FIRST, and the reason travels with each so the
log says whose decision it was rather than reporting three situations with one
word:

  * "verifier" in `disabled_agents` — the old spelling of off. It still wins,
    because whoever wrote it meant off, and a rename must not turn their
    answer around. Seven repositories on this installation hold it.
  * `policy.verifier_enabled` — this repository decided. True and false are
    both decisions; NULL is not one.
  * `REVIEW_VERIFIER_ENABLED` — the install default, itself False.
"""

from __future__ import annotations

import pytest

from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings


@pytest.fixture
def resolve():
    def _resolve(policy=None, disabled=(), **settings_kw):
        orch = ReviewOrchestrator(ReviewSettings(**settings_kw), agents=[])
        return orch._verifier_enabled(policy, set(disabled))

    return _resolve


# ─── the default ─────────────────────────────────────────────────────


def test_the_install_default_is_off():
    assert ReviewSettings().verifier_enabled is False


def test_a_repository_that_never_said_anything_does_not_run_it(resolve):
    """The case that was impossible to reach before: no policy row at all."""
    on, reason = resolve(policy=None)
    assert on is False
    assert reason == "off_by_default"


def test_a_policy_that_says_nothing_about_it_inherits(resolve):
    on, _ = resolve(policy={"enabled": True, "disabled_agents": []})
    assert on is False


def test_an_install_can_turn_it_on_for_everything(resolve):
    on, reason = resolve(policy=None, verifier_enabled=True)
    assert on is True
    assert reason == "enabled_by_install"


def test_the_env_var_reaches_it(monkeypatch):
    from src.review.settings import get_review_settings

    monkeypatch.setenv("REVIEW_VERIFIER_ENABLED", "true")
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().verifier_enabled is True
    finally:
        get_review_settings.cache_clear()


# ─── a repository deciding ───────────────────────────────────────────


def test_a_repository_can_ask_for_it(resolve):
    on, reason = resolve(policy={"verifier_enabled": True})
    assert on is True
    assert reason == "enabled_by_policy"


def test_a_repository_can_refuse_it_against_an_install_that_wants_it(resolve):
    """False is a decision, not an absence — which is the whole reason the
    column is nullable."""
    on, reason = resolve(policy={"verifier_enabled": False}, verifier_enabled=True)
    assert on is False
    assert reason == "disabled_by_policy"


def test_null_is_not_false(resolve):
    """A row written before the column existed must inherit, not refuse. Had
    the column been NOT NULL DEFAULT FALSE, an operator raising the install
    default would watch it apply to no repository at all."""
    on, _ = resolve(policy={"verifier_enabled": None}, verifier_enabled=True)
    assert on is True


# ─── the old spelling still means what it meant ──────────────────────


def test_the_deny_list_still_switches_it_off(resolve):
    on, reason = resolve(policy={"verifier_enabled": True}, disabled=["verifier"])
    assert on is False, (
        "seven repositories on this installation hold this entry meaning off; "
        "a rename must not turn their answer around"
    )
    assert reason == "disabled_by_policy"


def test_the_deny_list_beats_an_install_that_wants_it(resolve):
    on, _ = resolve(policy=None, disabled=["verifier"], verifier_enabled=True)
    assert on is False


def test_another_agent_in_the_deny_list_is_not_about_the_veto(resolve):
    on, _ = resolve(policy={"verifier_enabled": True}, disabled=["security", "cve"])
    assert on is True


# ─── what stays on ───────────────────────────────────────────────────


def test_the_deterministic_prefilter_is_not_what_was_switched_off():
    """The veto is the model call. Dedup, clustering, the rule deny-list, the
    confidence floor and the severity sort are not, and run on every review —
    a distinction that was collapsed once and cost a critical finding its
    place in the posted comments."""
    import ast
    import inspect

    src = inspect.getsource(ReviewOrchestrator._review_impl)
    tree = ast.parse(src.lstrip())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    prefilter = [c for c in calls if getattr(c.func, "attr", None) == "prefilter"]
    assert prefilter, "the prefilter is no longer called unconditionally"

    # It must not sit inside the branch that decides about the veto.
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            inner = [c for c in ast.walk(node)
                     if isinstance(c, ast.Call)
                     and getattr(c.func, "attr", None) == "prefilter"]
            test_src = ast.dump(node.test)
            if inner and "veto_on" in test_src:
                pytest.fail("the prefilter was moved inside the veto's branch")


def test_a_review_without_the_veto_still_names_it_as_skipped():
    """The row records what did not run. It is the PR comment that must not
    carry an unconditional line about a stage nobody enabled."""
    from src.review.models import ReviewBatch

    assert "agents_skipped" in ReviewBatch.__dataclass_fields__
