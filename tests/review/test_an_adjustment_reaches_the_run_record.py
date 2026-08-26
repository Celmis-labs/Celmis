"""What the run changed behind the operator's back reaches the run record.

Four self-heals, four hiding places: the ceiling clamp and the dropped
reasoning level on `LLMResult`, the dropped temperature in the audit log only,
the fallback model on `AgentRunResult.fallback_used`. The orchestrator's
aggregation loop read none of them, the run row had no column for them, the PR
comment's banner named only the agents that failed. A review whose architect
quietly ran without the reasoning level somebody configured looked exactly
like one that ran with it — and a review quietly getting worse with nobody
knowing which knob to turn is the whole reason this list exists.

Pinned here, from the agent to the row:

  * an agent gathers every call's adjustments — the corrective retry's too —
    and a FAILED agent keeps the ones it made on its way to failing, because
    an agent that was clamped and then died still made the adjustment;
  * the swap to the fallback model is an adjustment, written down whether or
    not the fallback answered, naming both models and the primary's ending;
  * the orchestrator merges the lists onto the batch, failed agents included;
  * both completion writers persist the same dicts, NULL stays "not recorded"
    and '[]' means "nothing was adjusted", and the column arrives by the same
    additive migration the rosters did — applies, reopens, survives going away;
  * the banner says it in one line per kind, derived from the SAME list the
    row persists, so the PR comment cannot name an adjustment the API says
    nothing about.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from litellm import exceptions as le

import src.review.agents.base as base_mod
from src.api.review_runs import ReviewRun, ReviewRunStore, record_completed_review
from src.llm.capabilities import ParameterAdjustment
from src.llm.client import LLMResult
from src.review.agents.base import (
    AgentContext,
    AgentRunResult,
    LLMReviewAgent,
    ReviewAgent,
    _agent_error_text,
)
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import Hunk, PullRequest, ReviewBatch, ReviewRunStatus
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import AgentLLMSettings

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "src/foo.py", "line": 1, "severity": "warning", "title": "t", "body": "b"}]'
GARBAGE = "no json here"

CLAMP = ParameterAdjustment(
    agent="architect", parameter="max_output_tokens", requested=100_000,
    sent=65_535, action="clamped", reason="model ceiling is 65535",
    model="gemini/gemini-3.7-flash",
)
REASONING = ParameterAdjustment(
    agent="security", parameter="reasoning", requested="minimal", sent=None,
    action="dropped", model="gemini/gemini-3.7-flash",
    reason="Thinking level MINIMAL is not supported for this model. "
           "Please retry with other thinking level.",
)
TEMPERATURE = ParameterAdjustment(
    agent="architect", parameter="temperature", requested=0.1, sent=None,
    action="dropped", reason="temperature: only temperature=1 is supported for this model.",
    model="anthropic/claude-sonnet-5",
)
SWAP = ParameterAdjustment(
    agent="quality", parameter="model", requested="gemini/gemini-3.7-flash",
    sent="gemini/gemini-3.6-flash", action="swapped",
    reason="provider quota exhausted", model="gemini/gemini-3.7-flash",
)


# ─── doubles ─────────────────────────────────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _reply(text: str, *adjustments: ParameterAdjustment, model: str = "primary") -> LLMResult:
    """What `LLMClient.generate` hands back, adjustments included."""
    return LLMResult(
        text=text, input_tokens=100, output_tokens=40, model=model,
        finish_reason="stop", cost_usd=0.01, cost_source="litellm_estimate",
        provider="gemini", parameter_adjustments=list(adjustments),
    )


class _Client:
    """An LLMClient double: one outcome per call — an LLMResult or an exception."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Architect(LLMReviewAgent):
    name = "architect"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return "p"


def _context(client, *, fallback_model: str | None = None) -> AgentContext:
    return AgentContext(
        pull_request=_pr(), llm_client=client,
        agent_llm={"architect": AgentLLMSettings(
            model="primary", max_output_tokens=1000, fallback_model=fallback_model,
        )},
    )


def _throttled() -> le.RateLimitError:
    return le.RateLimitError(message="Too many requests", llm_provider="gemini", model="primary")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The transport retry and the fallback pause before they fire — never on
    the test clock."""
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: None)


class _Canned(ReviewAgent):
    """An agent that returns a canned AgentRunResult — what the loop reads."""

    def __init__(self, name: str, **result) -> None:
        self.name = name
        self._result = result

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, **self._result)


class _PassThroughVerifier:
    """Both halves of the stage the orchestrator calls — the deterministic
    prefilter it always runs and the LLM pass it runs by policy — as
    identity, so the loop under test is the only thing shaping the batch."""

    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):  # pragma: no cover - not posted
        return {}

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    """Drive the real aggregation loop with no network and no database —
    the same harness the cost-accounting tests use, for the same reason: a
    copy of the loop would keep passing after the original changed."""
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    def _run(*agents) -> ReviewBatch:
        orch = ReviewOrchestrator(agents=list(agents), verifier=_PassThroughVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        return orch.review(
            "github", "acme/api", 7, dry_run=True, post_comments=False,
            provider=_Provider(),
        ).batch

    return _run


@pytest.fixture
def store(tmp_path: Path) -> ReviewRunStore:
    return ReviewRunStore(tmp_path / "review_runs.db")


def _batch(**kw) -> ReviewBatch:
    """A finished batch, the way the orchestrator finishes one."""
    b = ReviewBatch(pull_request=_pr(), **kw)
    b.verdict = b.compute_verdict()
    b.apply_partial_banner()
    b.mark_complete()
    return b


@dataclass
class _Result:
    batch: ReviewBatch
    posted: bool = True


def _dicts(adjustments) -> list[dict]:
    return [a.as_dict() for a in adjustments]


# ─── the agent gathers what its calls reported ───────────────────────


def test_an_agent_gathers_every_calls_adjustments():
    """Both calls' — the corrective retry doubled the budget and was clamped
    again, and a doubled number cut to the ceiling is a fact about THAT call."""
    second_clamp = ParameterAdjustment(
        agent="architect", parameter="max_output_tokens", requested=2000,
        sent=1500, action="clamped", reason="model ceiling is 1500", model="primary",
    )
    client = _Client([_reply(GARBAGE, CLAMP), _reply(VALID, second_clamp)])

    result = _Architect().review(_context(client))

    assert result.error is None and result.findings
    assert result.parameter_adjustments == [CLAMP, second_clamp]


def test_an_agent_that_died_after_being_clamped_still_reports_the_clamp():
    """An unreadable reply that was clamped, then a rejected key: the agent
    fails, and the clamp it made on the way must not die with it."""
    client = _Client([
        _reply(GARBAGE, CLAMP),
        le.AuthenticationError(message="bad key", llm_provider="gemini", model="primary"),
    ])

    result = _Architect().review(_context(client))

    assert result.error, "the agent was supposed to fail"
    assert result.parameter_adjustments == [CLAMP]


def test_an_agent_with_nothing_adjusted_reports_an_empty_list():
    result = _Architect().review(_context(_Client([_reply(VALID)])))
    assert result.parameter_adjustments == []


# ─── the swap to the fallback model is an adjustment ─────────────────


def test_the_swap_to_the_fallback_names_both_models_and_the_primarys_ending():
    from src.llm.errors import classify

    exc = _throttled()
    client = _Client([exc, _reply(VALID, model="backup")])

    result = _Architect().review(_context(client, fallback_model="backup"))

    assert result.fallback_used and result.error is None
    assert client.calls[-1]["model"] == "backup"
    assert _dicts(result.parameter_adjustments) == [{
        "agent": "architect", "parameter": "model",
        "requested": "primary", "sent": "backup", "action": "swapped",
        "reason": _agent_error_text(exc, classify(exc)), "model": "primary",
    }]


def test_a_fallback_that_also_failed_still_records_the_swap():
    """`fallback_used` has always been True on every ending — the swap was
    made whether or not the second model answered, and the record keeps the
    same rule."""
    client = _Client([_throttled(), _throttled()])

    result = _Architect().review(_context(client, fallback_model="backup"))

    assert result.error and result.fallback_used
    assert [(a.parameter, a.action, a.requested, a.sent)
            for a in result.parameter_adjustments] == [("model", "swapped", "primary", "backup")]


def test_the_fallbacks_own_adjustments_ride_along_with_the_swap():
    """The fallback call is re-fitted to ITS model inside the client, so a
    clamp it reports belongs to the same agent and lands on the same list."""
    fallback_clamp = ParameterAdjustment(
        agent="architect", parameter="max_output_tokens", requested=1000,
        sent=512, action="clamped", reason="model ceiling is 512", model="backup",
    )
    client = _Client([_throttled(), _reply(VALID, fallback_clamp, model="backup")])

    result = _Architect().review(_context(client, fallback_model="backup"))

    assert [a.parameter for a in result.parameter_adjustments] == ["model", "max_output_tokens"]
    assert result.parameter_adjustments[1] == fallback_clamp


# ─── the orchestrator merges, failed agents included ──────────────────


def test_the_orchestrator_merges_every_agents_adjustments_onto_the_batch(run):
    batch = run(
        _Canned("architect", parameter_adjustments=[CLAMP, TEMPERATURE]),
        _Canned("security", parameter_adjustments=[REASONING]),
        _Canned("quality", parameter_adjustments=[SWAP]),
        _Canned("tests"),
    )

    # Agents finish in whatever order the pool hands them back; what is pinned
    # is that nothing is lost and nothing is doubled, not the order. Within
    # one agent the order is kept — the architect's two arrive as a pair.
    assert sorted(map(repr, batch.parameter_adjustments)) == sorted(
        map(repr, [CLAMP, TEMPERATURE, REASONING, SWAP]))
    architects = [a for a in batch.parameter_adjustments if a.agent == "architect"]
    assert architects == [CLAMP, TEMPERATURE]


def test_a_failed_agents_adjustments_are_not_lost(run):
    """Before the `continue`, like the ledger: the agent that was clamped and
    then died still made the adjustment."""
    batch = run(
        _Canned("architect", error="provider quota exhausted",
                parameter_adjustments=[CLAMP]),
        _Canned("security", parameter_adjustments=[REASONING]),
    )

    assert "architect" in batch.agents_failed
    assert CLAMP in batch.parameter_adjustments
    assert sorted(map(repr, batch.parameter_adjustments)) == sorted(map(repr, [CLAMP, REASONING]))


def test_a_clean_run_carries_an_empty_list(run):
    batch = run(_Canned("architect"), _Canned("security"))
    assert batch.parameter_adjustments == []
    assert batch.run_status == ReviewRunStatus.COMPLETE


# ─── both completion writers persist the same dicts ──────────────────


def test_every_kind_round_trips_through_the_run_store(store: ReviewRunStore):
    batch = _batch(agents_run=["architect", "security", "quality"],
                   parameter_adjustments=[CLAMP, REASONING, TEMPERATURE, SWAP])
    store.insert(ReviewRun(id="r1", user_id="u1", pr_ref="github:acme/api#7"))

    record_completed_review(_Result(batch=batch), run_id="r1", store=store)

    row = store.get("r1")
    assert row.parameter_adjustments == _dicts([CLAMP, REASONING, TEMPERATURE, SWAP])
    assert row.adjustments_count == 4
    # Typed values survive JSON: the number stays a number, the float a float.
    by_param = {a["parameter"]: a for a in row.parameter_adjustments}
    assert by_param["max_output_tokens"]["requested"] == 100_000
    assert by_param["temperature"]["requested"] == 0.1
    assert by_param["reasoning"]["sent"] is None


def test_the_ui_path_writes_the_same_list(monkeypatch, store: ReviewRunStore):
    """The background task behind /api/reviews/trigger is the other writer.
    They had drifted before, so both are asserted."""
    import src.api.routers.reviews as reviews_mod
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    batch = _batch(agents_run=["architect"], agents_failed=["security"],
                   parameter_adjustments=[CLAMP, REASONING])

    class _Prov:
        def close(self) -> None:
            pass

    class _Orch:
        def review(self, *a, **kw):
            return _Result(batch=batch)

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", _Orch)
    monkeypatch.setattr(providers_mod, "get_provider_for", lambda *a, **kw: _Prov())
    monkeypatch.setattr(reviews_mod, "get_review_run_store", lambda: store)
    store.insert(ReviewRun(id="r2", user_id="u1", pr_ref="github:acme/api#11"))

    reviews_mod._run_review_task(
        pr_ref="github:acme/api#11", post_comments=False,
        run_id="r2", user_id="u1", workspace_id="ws1",
    )

    row = store.get("r2")
    assert row.status == ReviewRunStatus.PARTIAL.value
    assert row.parameter_adjustments == _dicts([CLAMP, REASONING])


def test_a_run_that_adjusted_nothing_records_an_empty_list_not_unknown(store):
    store.insert(ReviewRun(id="r3", user_id="u1", pr_ref="github:acme/api#3"))
    record_completed_review(_Result(batch=_batch(agents_run=["architect"])),
                            run_id="r3", store=store)

    row = store.get("r3")
    assert row.parameter_adjustments == [], (
        "[] is 'tracked, nothing adjusted'; None would read as 'nobody looked'"
    )
    assert row.adjustments_count == 0


def test_a_batch_without_the_field_stays_unknown(store):
    """An engine or a double built before the field existed is NOT 'nothing
    was adjusted' — the column stays NULL."""
    batch = SimpleNamespace(
        run_status=ReviewRunStatus.COMPLETE, agents_run=["architect"],
        agents_failed=[], verdict=SimpleNamespace(value="approve"), findings=[],
        critical_count=0, error_count=0, warning_count=0, info_count=0,
        cross_repo_callers=0, elapsed_seconds=1.0, summary="ok",
    )
    store.insert(ReviewRun(id="r4", user_id="u1", pr_ref="github:acme/api#4"))

    record_completed_review(SimpleNamespace(batch=batch, posted=False), run_id="r4", store=store)

    assert store.get("r4").parameter_adjustments is None


# ─── the migration ───────────────────────────────────────────────────


_LEGACY_SCHEMA = """
CREATE TABLE review_runs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pr_ref          TEXT NOT NULL,
    status          TEXT NOT NULL,
    verdict         TEXT NOT NULL DEFAULT 'pending',
    findings_count  INTEGER NOT NULL DEFAULT 0,
    critical        INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    warning         INTEGER NOT NULL DEFAULT 0,
    info            INTEGER NOT NULL DEFAULT 0,
    cross_repo_callers INTEGER NOT NULL DEFAULT 0,
    posted          INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL,
    summary         TEXT NOT NULL DEFAULT '',
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
"""


def _legacy_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO review_runs (id, user_id, pr_ref, status, verdict,"
        " summary, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old", "u1", "github:acme/api#1", "complete", "approve",
         "Looks fine.", "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return path


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(review_runs)")}
    finally:
        conn.close()


def test_a_fresh_database_has_the_column(tmp_path: Path):
    ReviewRunStore(tmp_path / "fresh.db")
    assert "adjustments_json" in _columns(tmp_path / "fresh.db")


def test_the_migration_applies_to_a_database_that_predates_it(tmp_path: Path):
    db = _legacy_db(tmp_path / "legacy.db")
    assert "adjustments_json" not in _columns(db)
    ReviewRunStore(db)
    assert "adjustments_json" in _columns(db)


def test_opening_it_again_is_not_an_error(tmp_path: Path):
    db = _legacy_db(tmp_path / "legacy.db")
    ReviewRunStore(db)
    ReviewRunStore(db)
    assert "adjustments_json" in _columns(db)


def test_a_row_written_before_the_column_is_unknown_not_clean(tmp_path: Path):
    store = ReviewRunStore(_legacy_db(tmp_path / "legacy.db"))
    row = store.get("old")
    assert row.parameter_adjustments is None
    assert row.parameter_adjustments != [], "unknown collapsed into 'nothing adjusted'"
    assert row.adjustments_count == 0


def test_reading_survives_the_column_going_away_again():
    """The reverse direction: an additive ALTER with no down step, so
    "reverses" means the code in front of a table without the column — a
    rollback, a replica behind — reads unknown instead of raising."""
    from src.api.review_runs import _adjustments

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (id TEXT)")
    conn.execute("INSERT INTO r VALUES ('x')")
    row = conn.execute("SELECT * FROM r").fetchone()
    assert _adjustments(row) is None


@pytest.mark.parametrize("stored,expected", [
    (json.dumps([CLAMP.as_dict()]), [CLAMP.as_dict()]),
    (json.dumps([]), []),
    (None, None),
    ("", None),
    ("not json at all", None),
    (json.dumps({"adjustments": []}), None),
    # Items that are not objects are dropped, not fatal.
    (json.dumps([CLAMP.as_dict(), 7, "x"]), [CLAMP.as_dict()]),
])
def test_reading_the_column_never_fails_a_history_request(stored, expected):
    from src.api.review_runs import _adjustments

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (adjustments_json TEXT)")
    conn.execute("INSERT INTO r VALUES (?)", (stored,))
    row = conn.execute("SELECT * FROM r").fetchone()
    assert _adjustments(row) == expected


# ─── the banner says it, from the same list ──────────────────────────


def test_the_banner_names_each_kind_of_adjustment():
    b = _batch(agents_run=["architect", "security", "quality"],
               parameter_adjustments=[CLAMP, REASONING, TEMPERATURE, SWAP])

    banner = b.partial_banner
    lines = [ln for ln in banner.splitlines() if ln.strip()]
    assert len(lines) == 4, banner
    assert all(ln.startswith("⚙ ADJUSTED") for ln in lines), banner
    # Each line carries what was asked, who refused or what it was cut to,
    # and the provider's reason — the three things the operator needs.
    assert "max_output_tokens 100000" in banner and "65535" in banner
    assert "reasoning 'minimal' was refused by gemini/gemini-3.7-flash" in banner
    assert "ran without it" in banner
    assert "Thinking level MINIMAL is not supported" in banner
    assert "temperature 0.1 was refused by anthropic/claude-sonnet-5" in banner
    assert "quality ran on the fallback model gemini/gemini-3.6-flash" in banner
    assert "instead of gemini/gemini-3.7-flash" in banner
    assert "provider quota exhausted" in banner


def test_one_line_per_kind_names_every_agent_it_happened_to():
    """Three agents clamped against the same ceiling is one knob to turn,
    so it is one line naming three agents — not three lines."""
    same_for = [
        ParameterAdjustment(agent=a, parameter="max_output_tokens", requested=100_000,
                            sent=65_535, action="clamped", reason="model ceiling is 65535",
                            model="gemini/gemini-3.7-flash")
        for a in ("architect", "security", "tests")
    ]
    b = _batch(agents_run=["architect", "security", "tests"], parameter_adjustments=same_for)

    lines = [ln for ln in b.partial_banner.splitlines() if ln.strip()]
    assert len(lines) == 1, b.partial_banner
    assert "architect, security, tests" in lines[0]


def test_the_banner_is_derived_from_the_list_not_a_copy():
    b = ReviewBatch(pull_request=_pr(), agents_run=["architect"])
    assert b.partial_banner == ""

    b.parameter_adjustments.append(CLAMP)
    assert "max_output_tokens" in b.partial_banner

    b.parameter_adjustments.clear()
    assert b.partial_banner == "", "the banner outlived the list it claims to read"


def test_the_gap_notice_and_the_adjustments_share_one_banner():
    """A partial run that also adjusted something says both, gap first: the
    missing stage is the bigger fact, and both reach the same reader."""
    b = _batch(agents_run=["architect"], agents_failed=["security"],
               parameter_adjustments=[REASONING])

    assert b.partial_banner.startswith("⚠ PARTIAL REVIEW")
    assert "security" in b.partial_banner
    assert "⚙ ADJUSTED" in b.partial_banner
    assert b.partial_banner.index("⚠ PARTIAL") < b.partial_banner.index("⚙ ADJUSTED")
    assert b.run_status == ReviewRunStatus.PARTIAL


def test_an_adjusted_run_is_still_complete():
    """Every stage answered; the review is not partial, it is not the review
    that was configured. The status stays honest about the first and the
    banner about the second."""
    b = _batch(agents_run=["architect", "security"], parameter_adjustments=[REASONING])
    assert b.run_status == ReviewRunStatus.COMPLETE
    assert b.partial_banner.startswith("⚙ ADJUSTED")


def test_the_notice_is_in_the_summary_once():
    b = ReviewBatch(pull_request=_pr(), agents_run=["architect"],
                    parameter_adjustments=[CLAMP], summary="Original body.")
    b.apply_partial_banner()
    b.apply_partial_banner()
    assert b.summary.count("⚙ ADJUSTED") == 1
    assert b.summary.endswith("Original body.")


def test_the_posted_comment_carries_the_notice():
    """`_format_summary` composes the PR comment from the banner property,
    so what the row says and what the author reads come from one list."""
    from src.review.providers.base import _format_summary

    b = _batch(agents_run=["architect"], parameter_adjustments=[TEMPERATURE])

    comment = _format_summary(b, "<!-- marker -->")
    assert "⚙ ADJUSTED" in comment
    assert "temperature 0.1 was refused by anthropic/claude-sonnet-5" in comment


def test_the_banner_and_the_row_name_the_same_adjustments(store: ReviewRunStore):
    batch = _batch(agents_run=["architect", "security"],
                   parameter_adjustments=[CLAMP, REASONING, TEMPERATURE])
    store.insert(ReviewRun(id="r9", user_id="u1", pr_ref="github:acme/api#9"))
    record_completed_review(_Result(batch=batch), run_id="r9", store=store)

    row = store.get("r9")
    assert row.parameter_adjustments, "the row lost the list"
    for stored in row.parameter_adjustments:
        assert stored["parameter"] in row.summary, (
            f"the banner in the row's summary does not name {stored['parameter']}"
        )
        assert str(stored["requested"]) in row.summary
