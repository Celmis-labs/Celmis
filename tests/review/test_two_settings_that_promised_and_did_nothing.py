"""Two `ReviewSettings` fields named a behaviour the product did not have.

    review_timeout_seconds: int = 300  # 5 min total budget per PR review
    max_diff_size_bytes:    int = 500_000  # 500 KB — skip review if larger

Neither was read by any code path outside the settings module. Each promised a
bound in its own comment and applied none, which is the shape this codebase
keeps killing elsewhere: a field that always means the same thing is worse
than no field, because it looks like an answer.

The timeout carried a second defect on top. `env_prefix = "REVIEW_"` plus a
field called `review_timeout_seconds` means pydantic reads
REVIEW_REVIEW_TIMEOUT_SECONDS — while REVIEW_TIMEOUT_SECONDS, the spelling
anybody would actually write, was read by nothing and ignored in silence. It
was the only field in the class carrying the class's own prefix.

AND THE OLD DEFAULT WAS WRONG FOR ITS OWN PURPOSE. Measured across 175 real
reviews from seven benchmark runs: median 85s, p90 341s, p99 1438s. Turning
the 300-second budget on as written would have cut 14.3% of real reviews. 900
cuts 2.3% — the tail that genuinely hangs. Enforcing a dead setting at its
dead default would have been worse than leaving it dead.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewVerdict,
)
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings, get_review_settings

# ─── the name ────────────────────────────────────────────────────────


def test_no_field_repeats_the_class_prefix():
    """The general form of the bug, not the one instance. Any field starting
    with `review_` under `env_prefix="REVIEW_"` reads a doubled variable that
    nobody will guess."""
    doubled = [f for f in ReviewSettings.model_fields if f.startswith("review_")]
    assert not doubled, (
        f"{doubled} would be read as REVIEW_REVIEW_… — the spelling an "
        f"operator writes is silently ignored"
    )


def test_the_env_prefix_is_still_what_the_names_assume():
    assert ReviewSettings.model_config["env_prefix"] == "REVIEW_"


def test_the_intuitive_variable_is_the_one_that_works(monkeypatch):
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "777")
    monkeypatch.delenv("REVIEW_REVIEW_TIMEOUT_SECONDS", raising=False)
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().timeout_seconds == 777
    finally:
        get_review_settings.cache_clear()


def test_the_doubled_spelling_still_works_for_whoever_found_it(monkeypatch):
    monkeypatch.setenv("REVIEW_REVIEW_TIMEOUT_SECONDS", "555")
    monkeypatch.delenv("REVIEW_TIMEOUT_SECONDS", raising=False)
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().timeout_seconds == 555
    finally:
        get_review_settings.cache_clear()


def test_the_intuitive_variable_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "111")
    monkeypatch.setenv("REVIEW_REVIEW_TIMEOUT_SECONDS", "999")
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().timeout_seconds == 111
    finally:
        get_review_settings.cache_clear()


def test_a_junk_doubled_value_does_not_take_the_review_down(monkeypatch):
    monkeypatch.setenv("REVIEW_REVIEW_TIMEOUT_SECONDS", "five minutes")
    monkeypatch.delenv("REVIEW_TIMEOUT_SECONDS", raising=False)
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().timeout_seconds > 0
    finally:
        get_review_settings.cache_clear()


# ─── the default, from the measurement ───────────────────────────────


def test_the_default_budget_reflects_measured_reviews(monkeypatch):
    """Not a round number somebody liked. 175 reviews: p90 341s, p99 1438s."""
    monkeypatch.delenv("REVIEW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("REVIEW_REVIEW_TIMEOUT_SECONDS", raising=False)
    assert ReviewSettings().timeout_seconds >= 600, (
        "a budget at or below the measured p90 (341s) cuts real reviews; the "
        "old 300 would have truncated 14.3% of 175 measured runs"
    )


# ─── and now they do something ───────────────────────────────────────
#
# Driven end-to-end through the real orchestrator, not grepped out of its
# source. A test that greps for a string literal is keyed on a surface
# feature — it passes for a `max_diff_size_bytes` mentioned in a comment and
# fails for a working one somebody reworded.


#: The half of the skip message that only the size gate can produce. Both
#: SKIPPED paths say "has NOT been reviewed"; only this one names the knob.
_CAP_KNOB = "REVIEW_MAX_DIFF_SIZE_BYTES"


class _PassThroughVerifier:
    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider:
    def __init__(self, pr):
        self._pr = pr
        self.posted = 0

    def fetch_pull_request(self, repo, number):
        return self._pr

    def post_review(self, batch, dry_run=False):
        self.posted += 1
        return {}

    def close(self):
        pass


class _Clock:
    """`time` as the orchestrator imports it. The review starts at 0; every
    later reading is `elapsed`, which is what the stage gate subtracts."""

    def __init__(self, elapsed: float) -> None:
        self._readings = [0.0, float(elapsed)]

    def time(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


class _Finding(ReviewAgent):
    """One agent, one finding — so a cut-short review has something to lose."""

    name = "defect"

    def review(self, context):
        return AgentRunResult(agent="defect", findings=[Finding(
            file_path="src/foo.py", line=2, severity=FindingSeverity.WARNING,
            title="something real", body="found before the budget ran out",
            agent="defect",
        )])


def _pr(raw_diff: str = "@@ -1 +1,2 @@\n line\n+added\n") -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open", raw_diff=raw_diff,
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


@pytest.fixture
def run(monkeypatch):
    """A real ReviewOrchestrator with everything outside the two gates stubbed,
    and the two tail stages instrumented so a skip is observable rather than
    inferred."""
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    import src.review.orchestrator as orchestrator_mod

    ran: list[str] = []
    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: (ran.append("breaking_change"),
                                     AgentRunResult(agent="breaking_change"))[1])
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: (ran.append("compliance"),
                                     AgentRunResult(agent="compliance"))[1])

    def _run(pr=None, *, elapsed=0.0, agents=None, **setting_overrides):
        pr = pr if pr is not None else _pr()
        settings = ReviewSettings(**setting_overrides)
        if elapsed:
            monkeypatch.setattr(orchestrator_mod, "time", _Clock(elapsed))
        orch = ReviewOrchestrator(
            settings,
            # A roster with something in it. An EMPTY one is its own
            # SKIPPED verdict — "nothing dispatched" — which would mask
            # both verdicts under test behind a third one.
            agents=[_Finding()] if agents is None else agents,
            verifier=_PassThroughVerifier(),
        )
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        provider = _Provider(pr)
        result = orch.review("github", "acme/api", 7, dry_run=True,
                             post_comments=False, provider=provider)
        return result.batch, ran

    return _run


# ─── the diff cap ────────────────────────────────────────────────────


def test_an_oversized_diff_is_refused_not_reviewed(run):
    """It used to reach the agents, where `_format_diff_for_prompt` cut it to
    50k characters and said so nowhere the reader could see: a review of the
    first fifth of a change, presented as a review of the change."""
    batch, ran = run(_pr("x" * 5_000), max_diff_size_bytes=1_000)

    assert batch.verdict is ReviewVerdict.SKIPPED
    assert not ran, "no stage may run on a diff we refused to read"
    assert not batch.findings, "nor may a finding survive from one"
    assert _CAP_KNOB in batch.summary


def test_the_refusal_names_the_size_the_limit_and_the_knob(run):
    """A skip the reader cannot act on is a skip they will read as a clean
    repository."""
    batch, _ = run(_pr("x" * 5_000), max_diff_size_bytes=1_000)

    assert "5,000" in batch.summary and "1,000" in batch.summary
    assert "REVIEW_MAX_DIFF_SIZE_BYTES" in batch.summary


def test_a_diff_under_the_cap_is_reviewed(run):
    batch, ran = run(_pr("x" * 500), max_diff_size_bytes=1_000)

    assert _CAP_KNOB not in batch.summary
    assert ran == ["breaking_change", "compliance"]


def test_the_cap_counts_bytes_because_it_says_bytes(run):
    """600 Cyrillic characters are 1200 bytes. `len()` on a str counts code
    points, so a diff of non-ASCII source — which is most of this product's
    own repositories — measured at half its real size and slipped a cap set
    for the transport that actually carries it."""
    batch, _ = run(_pr("д" * 600), max_diff_size_bytes=1_000)

    assert batch.verdict is ReviewVerdict.SKIPPED
    assert "1,200 bytes" in batch.summary


def test_a_zero_cap_switches_the_gate_off(run):
    """The escape hatch for an install that would rather review everything."""
    batch, ran = run(_pr("x" * 50_000), max_diff_size_bytes=0)

    assert _CAP_KNOB not in batch.summary
    assert ran == ["breaking_change", "compliance"]


# ─── the wall-clock budget ───────────────────────────────────────────


def test_an_over_budget_review_stands_the_tail_stages_down(run):
    batch, ran = run(elapsed=5_000, timeout_seconds=900)

    assert "breaking_change" not in ran
    assert "compliance" not in ran


def test_a_review_inside_its_budget_runs_them(run):
    batch, ran = run(elapsed=10, timeout_seconds=900)

    assert ran == ["breaking_change", "compliance"]


def test_a_stage_the_orchestrator_declined_is_skipped_not_failed(run):
    """The distinction `agents_skipped` was added for. Both stages below sit
    in `try:` blocks whose broad handler records a FAILURE, so a gate raising
    anything they recognise would report an operator's own setting as an
    outage — and `compliance` has such a handler, so this is the one that
    could really have gone wrong."""
    batch, _ = run(elapsed=5_000, timeout_seconds=900)

    assert set(batch.agents_skipped) >= {"breaking_change", "compliance"}
    assert "breaking_change" not in batch.agents_failed
    assert "compliance" not in batch.agents_failed


def test_the_cut_review_says_so_and_names_the_knob(run):
    """Fewer findings with no explanation is indistinguishable from a clean
    change — the false negative this whole wave of work is about."""
    batch, _ = run(elapsed=5_000, timeout_seconds=900)

    assert "REVIEW CUT SHORT" in batch.summary
    assert "900s" in batch.summary
    assert "REVIEW_TIMEOUT_SECONDS" in batch.summary, "and how to raise it"


def test_the_findings_already_in_hand_survive_the_cut(run):
    """The budget stands down what has not run. It does not throw away what
    the agents already paid for and produced."""
    batch, _ = run(elapsed=5_000, timeout_seconds=900)

    assert [f.title for f in batch.findings] == ["something real"]
    assert "defect" in batch.agents_run


def test_a_zero_budget_switches_the_gate_off(run):
    """Same escape hatch as the cap, and the reason the gate reads `bool(budget)`
    rather than trusting the default: an install that has decided a review may
    take as long as it takes."""
    batch, ran = run(elapsed=5_000, timeout_seconds=0)

    assert ran == ["breaking_change", "compliance"]
    assert "REVIEW CUT SHORT" not in batch.summary
