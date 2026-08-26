"""A reviewer who says "there is no test for this" has not found a defect.

Measured on the Martian Code Review Bench, 14 PRs, judge claude-sonnet-4-5,
two runs of the same PRs (BASE F2 44.94, runG2 F2 41.02). With the golden
count fixed at 53, F2 = 5*TP / (TP + FP + 212), so one true positive is worth
eleven false positives and no precision gate pays for itself unless it costs
zero recall.

This one does. In runG2 the `tests` agent wrote 12 of 69 comments and
`tests.untested-branch` was the run's most common rule at ten. Eight of the
twelve made a claim no other agent made and the judge scored every one of
them false; the four that touched a true positive restated a defect
architect or quality had already found. So the eight are removable at zero
recall cost, and F2 would read 5*21 / (21 + 15 + 212) = 42.34 instead of
41.02.

A rule-id deny-list cannot do it. `ReviewSettings.suppressed_rules` already
held `tests.no-coverage` and the avoid-list already forbade "tests-unnamed",
and 5adc53f changed the tests prompt's rule-id EXAMPLE to
`tests.untested-branch` in the same commit that denied `tests.no-coverage`.
`hidden.by_rule` is `{}` on all 14 PRs of runG2 — the deny-list caught
nothing. The gate under test here keys on the reasoning sentence instead,
which is why the tables below are the run's own sentences rather than
invented ones.
"""

from __future__ import annotations

import json
import logging

import pytest

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    AVOIDED_CATEGORIES,
    AgentContext,
    AgentRunResult,
    LLMReviewAgent,
    ReviewAgent,
    reads_as_a_coverage_claim,
)
from src.review.agents.contract import ContractAgent
from src.review.agents.defect import DefectAgent
from src.review.agents.security import SecurityAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import (
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
)
from src.review.orchestrator import ReviewOrchestrator

AGENTS = [DefectAgent, ContractAgent, SecurityAgent]

# ─── the run's own sentences ─────────────────────────────────────────
#
# Every `reasoning` below is copied from a comment celmis actually posted in
# runG2 (benchmark_data_runG2.json, the `*Why:*` line of each celmis comment).
# REFUSED are the eight the judge scored false and no other agent restated;
# KEPT are the four `tests` comments that touched a true positive plus the
# three comments from other agents whose reasoning mentions a test at all —
# the whole collateral surface the gate could take, in one table.

REFUSED = [
    ("no-test-exists",
     "cursor.offset can be negative when self.enable_advanced_features is true "
     "on line 876; executing list(queryset[start_offset:stop]) on line 881 with "
     "negative start_offset raises an AssertionError in Django ORM which is "
     "uncaptured due to missing tests."),
    ("no-test-exists",
     "TagDevice returning ErrDeviceLimitReached on line 45 branches to reject "
     "anonymous authentication while other errors log a warning and proceed, but "
     "no test exercises Authenticate under device limit error conditions, so "
     "silent regressions in auth blocking would go undetected."),
    ("test-does-too-little",
     "count >= s.deviceLimit on line 108 invokes updateDevice to allow existing "
     "devices to update their timestamp when the device limit is reached, but "
     "TestIntegrationBeyondDeviceLimit only tests adding a new device that gets "
     "rejected, so a failure during existing device updates would pass silently."),
    ("over-mocked",
     "User lookup and RSS item parsing run on line 26 of "
     "app/jobs/scheduled/poll_feed.rb, but poll_feed is mocked in "
     "spec/jobs/poll_feed_spec.rb, leaving RSS parsing and TopicEmbed import "
     "unverified."),
    ("over-mocked",
     "SiteSetting.feed_polling_enabled? is checked on line 40 of "
     "lib/topic_retriever.rb, but perform_retrieve is mocked in all tests in "
     "spec/components/topic_retriever_spec.rb, allowing bugs in feed polling or "
     "HTTP retrieval to pass undetected."),
    ("no-test-exists",
     "Line 63 of app/controllers/uploads_controller.rb adds a branch to "
     "automatically downsize images exceeding SiteSetting.max_image_size_kb; "
     "without test coverage for this branch, logic errors in the downsize retry "
     "loop or file size check would silently pass."),
    ("not-tested",
     "req.body.backupCode on line 48 allows disabling 2FA using a backup code "
     "instead of TOTP, but neither successful disabling nor invalid backup code "
     "rejection is tested, allowing authentication bypass or bad error responses "
     "to pass undetected."),
    ("not-covered-by-a-test",
     "credentials.backupCode on line 131 authorizes user login using single-use "
     "backup codes, but neither successful authentication nor invalid code "
     "rejection is covered by any test, allowing broken backup code validation "
     "or failure to consume used codes to pass silently."),
]

KEPT = [
    # tests agent, defect IN a test file — the collateral the gate must not take
    ("tests/AssertEvents.java",
     "Line 482 of "
     "testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/"
     "testsuite/AssertEvents.java uses incorrect substring indices (3..5 instead "
     "of 4..6) and returns false on equality, causing the matcher to return true "
     "when the grant shortcut is incorrect."),
    ("tests/OrganizationCacheTest.java",
     "The cleanup registration on line 380 hardcodes the provider alias 'alias' "
     "instead of using idpRep.getAlias(), leaving created test identity providers "
     "behind after test execution."),
    # tests agent, defect in production code
    ("tests/EventManager.ts",
     "mainHostDestinationCalendar is undefined when evt.destinationCalendar is "
     "empty on line 117; accessing .integration without optional chaining throws "
     "a TypeError."),
    ("tests/CalendarService.ts",
     "externalCalendarId is falsy on line 256; searching for cal.externalId === "
     "externalCalendarId matches undefined, causing selectedCalendar to resolve "
     "to undefined."),
    # every other runG2 comment whose reasoning mentions a test at all
    ("architect/RecoveryAuthnCodes",
     "requestedCode is 1-based, so calling "
     "generatedRecoveryAuthnCodes.get(requestedCode) on line 481 uses a 1-based "
     "index on a 0-indexed list, selecting the wrong code or throwing "
     "IndexOutOfBoundsException."),
    ("security/identityProviders",
     "idp on line 250 is added to identityProviders without being wrapped with "
     "createOrganizationAwareIdentityProviderModel, allowing cached identity "
     "providers to bypass organization-specific access controls."),
    ("quality/OrganizationCacheTest",
     "The string literal \"alias\" is passed to get() on line 380 instead of "
     "idpRep.getAlias(), causing test cleanup to attempt removing a non-existent "
     "IDP and leave created test providers in the realm."),
]


class _Probe(LLMReviewAgent):
    """Not named after a real agent: the base class resolves a canonical
    prompt by agent name, and this test is about the parser, not the prompt."""

    name = "probe"
    severity_default = FindingSeverity.WARNING
    system_prompt = "s"
    user_prompt_template = "{diff}"
    model = "test-model"


def _pr(*paths: str) -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[
            Hunk(
                file_path=p, old_file_path=p,
                old_start=1, old_count=2, new_start=1, new_count=4,
                content="@@ -1,2 +1,4 @@\n line\n+added1\n+added2\n",
            )
            for p in (paths or ("src/foo.py",))
        ],
        skipped_files=[],
    )


def _reply(*reasonings: str, **over: object) -> str:
    items = []
    for i, r in enumerate(reasonings):
        item: dict[str, object] = {
            "reasoning": r, "file": "src/foo.py", "line": 2 + i,
            "severity": "warning", "title": f"t{i}", "body": "b",
            "rule_id": "probe.thing", "confidence": 0.8,
        }
        item.update(over)
        items.append(item)
    return json.dumps(items)


def _parse(reply: str):
    return _Probe()._parse_findings(reply, AgentContext(pull_request=_pr()))


# ─── change 1: the coverage-claim gate ───────────────────────────────


@pytest.mark.parametrize("shape,reasoning", REFUSED, ids=[r[0] for r in REFUSED])
def test_every_coverage_claim_the_run_posted_is_refused(shape, reasoning):
    """All eight. Each was a false positive on the judge's list, and each
    would still be posted today: `hidden.by_rule` was `{}` on every PR."""
    out = _parse(_reply(reasoning))
    assert list(out) == []
    assert out.dropped_coverage_claim == 1
    assert reads_as_a_coverage_claim(reasoning) == shape


@pytest.mark.parametrize("what,reasoning", KEPT, ids=[k[0] for k in KEPT])
def test_a_defect_that_merely_lives_near_a_test_survives(what, reasoning):
    """The collateral this gate must not take. Two of these are defects IN a
    test file — an inverted matcher and a cleanup that removes the wrong
    provider — and the benchmark scored the claims they restate as true."""
    out = _parse(_reply(reasoning))
    assert len(out) == 1, what
    assert out.dropped_coverage_claim == 0
    assert reads_as_a_coverage_claim(reasoning) is None


def test_both_measured_shapes_are_caught_not_only_absence():
    """An absence-only pattern set leaves the two over-mocking claims — 2 of
    the 8 — on the PR. Both shapes, one reply, both refused."""
    absence, mocking = REFUSED[1][1], REFUSED[3][1]
    out = _parse(_reply(absence, mocking))
    assert list(out) == []
    assert out.dropped_coverage_claim == 2


def test_the_gate_ignores_the_rule_id_the_model_chose():
    """The whole point. `tests.no-coverage` was denied and the model answered
    with `tests.untested-branch`; a gate keyed on that string is not a gate."""
    reasoning = REFUSED[1][1]
    for rule_id in ("tests.no-coverage", "tests.untested-branch",
                    "arch.defect", "sec.cwe-754", "quality.bug"):
        out = _parse(_reply(reasoning, rule_id=rule_id))
        assert list(out) == [], rule_id
        assert out.dropped_coverage_claim == 1, rule_id


def test_the_gate_reads_the_reasoning_and_never_the_body():
    """A body legitimately discusses tests when the defect is in a test. The
    reasoning below names a defect; the body is nothing but coverage talk."""
    out = _parse(_reply(
        KEPT[0][1],
        body="No test covers this branch. It is not tested anywhere, the "
             "helper is mocked in spec/foo_spec.rb, and the suite only tests "
             "the happy path.",
    ))
    assert len(out) == 1
    assert out.dropped_coverage_claim == 0


def test_the_two_refusals_are_counted_apart():
    """`dropped_no_evidence` means "made a claim it could not derive";
    `dropped_coverage_claim` means "derived something that is not a defect".
    A batch that reported one total could not say which gate ran."""
    items = json.loads(_reply(KEPT[2][1], REFUSED[1][1]))
    items.append({
        "file": "src/foo.py", "line": 9, "severity": "warning",
        "title": "no reasoning at all", "body": "b", "rule_id": "probe.x",
    })
    out = _parse(json.dumps(items))
    assert len(out) == 1
    assert out.dropped_coverage_claim == 1
    assert out.dropped_no_evidence == 1


def test_the_count_rides_the_list_into_the_run_result():
    """Same route the neighbouring counter takes: the retry ladder builds
    `AgentRunResult` at four places and knows nothing about either gate."""
    out = _parse(_reply(REFUSED[0][1], REFUSED[3][1], KEPT[3][1]))
    result = AgentRunResult(agent="probe", findings=out)
    assert result.dropped_coverage_claim == 2
    assert len(result.findings) == 1


def test_a_run_that_refused_a_coverage_claim_says_so_without_debug_logging(caplog):
    """On runG2 this would have fired on five of the fourteen PRs. An
    operator comparing two runs' comment counts should not need DEBUG to see
    where the difference went."""
    with caplog.at_level(logging.INFO, logger="src.review.agents.base"):
        _parse(_reply(REFUSED[5][1]))
    assert any(
        r.levelno == logging.INFO
        and r.getMessage() == "agent_coverage_claims_refused agent=probe dropped=1"
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_a_clean_reply_is_untouched_by_the_new_gate():
    out = _parse(_reply(*[k[1] for k in KEPT]))
    assert len(out) == len(KEPT)
    assert out.dropped_coverage_claim == 0
    assert out.dropped_no_evidence == 0


# ─── change 2: the style ban stops contradicting itself ──────────────


def _style_bullet() -> str:
    """The composed style line every agent is actually sent."""
    lines = [ln for ln in AVOID_LIST_PROMPT.splitlines() if ln.strip().startswith("- style (")]
    assert len(lines) == 1, lines
    return " ".join(lines[0].split())


def test_a_formatter_settled_nit_is_still_banned_in_every_composed_prompt():
    """The half that earned its place: formatting, import order and line
    length are what a linter settles, and the avoid-list still says so."""
    bullet = _style_bullet()
    for nit in ("formatting", "import order", "quotes", "line length"):
        assert nit in bullet, nit
    for agent in AGENTS:
        assert bullet in " ".join(agent.system_prompt.split()), agent.name


def test_a_name_that_contradicts_the_thing_is_carved_out_of_the_ban():
    """cal.com#10600's golden — an exported `TwoFactor` living in
    `BackupCode.tsx` — was a TRUE POSITIVE in BASE and a false negative in
    runG2, the run in which this avoid-list first reached architect. The
    carve-out has to sit inside the same bullet the ban does, after it, or
    the model reads the ban and stops."""
    bullet = _style_bullet()
    ban = bullet.index("merely spell differently")
    carve = bullet.index("CONTRADICTS")
    assert ban < carve, "the exception must follow the rule it excepts"
    assert "does NOT cover" in bullet
    for agent in AGENTS:
        assert "CONTRADICTS" in agent.system_prompt, agent.name


def test_no_prompt_forbids_a_naming_rule_id_the_filter_would_post():
    """`quality.naming` is not in `ReviewSettings.suppressed_rules`, so the
    prefilter posts it. Listing it here banned in the prompt exactly what the
    filter would have let through — the two naming comments either run
    produced were BASE's, and one of them was a true positive."""
    forbidden = {rid for cat in AVOIDED_CATEGORIES.values() for rid in cat.rule_ids}
    assert "quality.naming" not in forbidden
    assert "quality.style" in forbidden, "formatting keeps its rule id"
    for agent in AGENTS:
        assert "quality.naming" not in agent.system_prompt, agent.name


def test_the_inflection_no_run_happened_to_produce_is_refused_too():
    """Neither run wrote a "fails to cover", so this is NOT a measured hit — it
    is an inflection of the shapes that did fire, and this test is what keeps
    it from being decoration."""
    out = _parse(_reply("The upload spec fails to cover the branch added on line 63."))

    assert list(out) == []
    assert out.dropped_coverage_claim == 1


def test_the_adjective_alone_no_longer_refuses_anything():
    """A bare `untested` shipped here as a third inflection and was removed.

    It matched nothing in the 199-text corpus and it refused "Backup code
    one-time consumption has untested race condition and replay vulnerability"
    — a real benchmark sentence about a time-of-check defect that merely uses
    the adjective. A pattern that catches no measured noise and one real defect
    is a pure loss: a true positive is worth eleven false positives here. The
    word still counts inside the patterns that require a negation beside it.
    """
    survives = _parse(_reply(
        "Backup code consumption on line 48 has an untested race condition "
        "between the lookup and the write."
    ))
    assert len(list(survives)) == 1
    assert survives.dropped_coverage_claim == 0

    refused = _parse(_reply("The downsize retry branch added on line 63 is not tested."))
    assert list(refused) == []
    assert refused.dropped_coverage_claim == 1


# ─── the count reaches the batch ─────────────────────────────────────


class _Canned(ReviewAgent):
    def __init__(self, name: str, **result) -> None:
        self.name = name
        self._result = result

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, **self._result)


class _KeepEverything:
    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):  # pragma: no cover - policy runs it
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
    """The real aggregation loop, the same way `test_the_run_says_what_it_hid`
    drives it — the counter is worth nothing if it stops at the agent."""
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    def _run(*agents) -> ReviewBatch:
        orch = ReviewOrchestrator(agents=list(agents), verifier=_KeepEverything())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        return orch.review(
            "github", "o/r", 1, dry_run=True, post_comments=False,
            provider=_Provider(),
        ).batch

    return _run


def test_the_refusals_are_summed_onto_the_batch(run):
    """Five of runG2's fourteen PRs would carry a non-zero here, and a run
    whose comment count fell by eight has to be able to say why."""
    batch = run(
        _Canned("tests", findings=[], dropped_coverage_claim=3),
        _Canned("quality", findings=[], dropped_coverage_claim=1),
        _Canned("architect", findings=[]),
    )
    assert batch.dropped_coverage_claim == 4


def test_a_failed_agents_refusals_still_count(run):
    """It refused two coverage claims, then died. It refused them all the
    same — the same rule the evidence counter next door already follows."""
    batch = run(
        _Canned("tests", error="boom", dropped_coverage_claim=2),
        _Canned("quality", findings=[], dropped_coverage_claim=1),
    )
    assert "tests" in batch.agents_failed
    assert batch.dropped_coverage_claim == 3


def test_a_batch_that_refused_nothing_reads_zero_not_missing():
    assert ReviewBatch(pull_request=_pr()).dropped_coverage_claim == 0
