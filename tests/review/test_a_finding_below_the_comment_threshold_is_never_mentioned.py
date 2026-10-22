"""A finding below the comment threshold is stored and never mentioned.

THE DEFECT. A repository set the lowest severity worth a comment to "error".
The review found a warning and a hint, posted neither, and the summary said:

    COMMENT — there are remarks worth a look
    Found: 2 (Warning 1 · Hint 1) · by category: Performance 1 · Structure 1
    up to 0 shown inline · 2 below the comment threshold — recorded, not posted

The team read it as "there are warnings, but nobody says which". The threshold
had been applied to the inline comments only; the counts, the categories and
the verdict still read every finding, so a pull request with nothing the author
could see stayed on COMMENT instead of being approved.

THE RULE. Everything the pull request is told — the verdict, the counts, the
listing, the description's findings block, the provider's approval — is
computed from the findings at or above the threshold (`postable_findings`).
The rest are still in `batch.findings` and still stored, and no text on the
pull request counts, categorises or names them, in any language or layout.

What does NOT bend: a critical agent that failed, a blocking compliance rule,
an incomplete run and an install that does not approve at all. Each keeps
refusing an approval at every threshold, and the summary says why in one line.
"""

from __future__ import annotations

import pytest

from src.review.models import (
    Finding,
    FindingSeverity,
    PRActions,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.pr_actions import APPROVE, REQUEST_CHANGES, description_insights, review_decision
from src.review.providers.base import _format_review_pointer, _format_summary
from tests.review.test_a_rerun_does_not_double_the_comments import (
    BOT,
    MARKER,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    GOOD_REPLY,
    _Agent,
    _Client,
    _orch,
    env,  # noqa: F401 — fixture
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import _finding as e2e_finding
from tests.review.test_the_pr_hears_the_review_begin_and_end import _pr as e2e_pr
from tests.review.test_the_pr_says_what_it_does import (
    _Bitbucket,
    _bitbucket,
    _finding,
    _GitHub,
    _github,
    _GitLab,
    _gitlab,
    _pr,
    settings,  # noqa: F401 — fixture
)
from tests.review.test_the_pr_says_what_it_does import _Provider as GitHubOverFake

CRITICAL, ERROR = FindingSeverity.CRITICAL, FindingSeverity.ERROR
WARNING, INFO = FindingSeverity.WARNING, FindingSeverity.INFO

LAYOUTS = ("completed", "classic", "rich")
LANGUAGES = (None, "en", "uk")


def _review(
    pr, severities=(), *, threshold: str | None = "error", language: str | None = None,
    layout: str = "completed", approve: bool = True, agents_run=("defect",),
    agents_failed=(), rules_bypass: bool = False, extra: list[Finding] | None = None,
) -> ReviewBatch:
    """A batch built the way the orchestrator builds one: the threshold is on it
    BEFORE the verdict is computed, then the gap banner, then completion."""
    findings = [
        _finding(severity, line=11 + i, title=f"finding {i}", rule_id=f"defect.r{i}")
        for i, severity in enumerate(severities)
    ] + list(extra or [])
    batch = ReviewBatch(pull_request=pr, findings=findings, agents_run=list(agents_run),
                        agents_failed=list(agents_failed))
    batch.review_language = language
    batch.comment_min_severity = threshold
    batch.rules_bypass_filters = rules_bypass
    batch.pr_actions = PRActions(
        completed_comment="completed" if layout == "completed" else "classic",
        approve_when_clean=approve,
    )
    batch.rich_summary = layout == "rich"
    batch.verdict = batch.compute_verdict()
    batch.apply_partial_banner()
    batch.mark_complete()
    return batch


def _gh_pr():
    return _pr("github", "o/r", 1)


# ─── the verdict ────────────────────────────────────────────────────


@pytest.mark.parametrize("threshold", ["error", "critical"])
def test_warnings_and_hints_under_an_errors_only_threshold_are_an_approval(threshold) -> None:
    batch = _review(_gh_pr(), [WARNING, WARNING, INFO], threshold=threshold)
    assert batch.verdict == ReviewVerdict.APPROVE
    assert len(batch.findings) == 3, "they are stored, only the pull request is not told"
    assert batch.below_threshold_count == 3
    assert review_decision(batch) == APPROVE


def test_three_errors_under_a_critical_only_threshold_are_not_a_block() -> None:
    """Weighed over what the pull request is told: three errors that are not
    posted cannot make a request for changes nobody can read."""
    batch = _review(_gh_pr(), [ERROR, ERROR, ERROR], threshold="critical")
    assert batch.verdict == ReviewVerdict.APPROVE
    assert review_decision(batch) == APPROVE


def test_only_the_errors_count_when_errors_and_warnings_meet_an_errors_threshold() -> None:
    batch = _review(_gh_pr(), [ERROR, WARNING, WARNING, WARNING], threshold="error")
    assert batch.verdict == ReviewVerdict.COMMENT
    assert [batch.reported_count(s) for s in (CRITICAL, ERROR, WARNING, INFO)] == [0, 1, 0, 0]
    assert review_decision(batch) is None, "a reported error is not a clean review"

    three = _review(_gh_pr(), [ERROR, ERROR, ERROR, WARNING], threshold="error")
    assert three.verdict == ReviewVerdict.REQUEST_CHANGES


@pytest.mark.parametrize(("severities", "expected"), [
    ([], ReviewVerdict.APPROVE),
    ([INFO], ReviewVerdict.APPROVE),
    ([WARNING], ReviewVerdict.COMMENT),
    ([ERROR], ReviewVerdict.COMMENT),
    ([ERROR, ERROR], ReviewVerdict.COMMENT),
    ([ERROR, ERROR, ERROR], ReviewVerdict.REQUEST_CHANGES),
    ([CRITICAL], ReviewVerdict.REQUEST_CHANGES),
    ([WARNING, INFO, CRITICAL], ReviewVerdict.REQUEST_CHANGES),
])
@pytest.mark.parametrize("threshold", [None, "info", "warning"])
def test_a_threshold_that_hides_nothing_judges_as_it_always_did(
    threshold, severities, expected,
) -> None:
    """At "warning" the only level it can hide is info, which never weighed on
    the verdict; at "info" or none nothing is hidden."""
    assert _review(_gh_pr(), severities, threshold=threshold).verdict == expected


def test_a_reported_critical_blocks_at_every_threshold() -> None:
    for threshold in (None, "info", "warning", "error", "critical"):
        batch = _review(_gh_pr(), [CRITICAL, WARNING], threshold=threshold)
        assert batch.verdict == ReviewVerdict.REQUEST_CHANGES, threshold


def test_a_rule_the_operator_exempted_from_the_threshold_is_reported_and_weighs() -> None:
    cited = _finding(WARNING, line=12, title="Cited", rule="No raw SQL")
    batch = _review(_gh_pr(), [WARNING], threshold="error", rules_bypass=True, extra=[cited])
    assert [f.title for f in batch.postable_findings] == ["Cited"]
    assert batch.verdict == ReviewVerdict.COMMENT


# ─── what still forbids an approval ─────────────────────────────────


@pytest.mark.parametrize("threshold", [None, "info", "warning", "error", "critical"])
def test_a_failed_critical_agent_still_blocks_the_approval(threshold) -> None:
    batch = _review(_gh_pr(), [WARNING, INFO], threshold=threshold, agents_failed=("security",))
    assert batch.verdict == ReviewVerdict.COMMENT
    assert review_decision(batch) is None
    for language in LANGUAGES:
        batch.review_language = language
        text = _format_summary(batch, MARKER)
        assert "✅" not in text, f"an approval tick over a review that cannot approve ({language})"


def test_a_failed_critical_agent_is_said_as_an_incomplete_review_not_as_findings() -> None:
    """"Findings to consider" over an empty list is the sentence this fixes."""
    batch = _review(_gh_pr(), [WARNING], threshold="error", agents_failed=("security",))
    english = _format_summary(batch, MARKER)
    assert "NOT APPROVED** — the review is incomplete" in english
    assert "findings to consider" not in english
    batch.review_language = "uk"
    assert "НЕ СХВАЛЕНО** — рев'ю неповне" in _format_summary(batch, MARKER)


def test_a_critical_agent_that_failed_does_not_hide_a_reported_error_from_the_verdict() -> None:
    batch = _review(_gh_pr(), [ERROR, ERROR, ERROR], threshold="error",
                    agents_failed=("security",))
    assert batch.verdict == ReviewVerdict.REQUEST_CHANGES


def test_a_partial_review_is_not_called_an_approval() -> None:
    """A stage that is not critical failed: the verdict stays APPROVE, the run
    is PARTIAL, the provider does not approve it — and so the line does not
    claim it did."""
    batch = _review(_gh_pr(), [WARNING], threshold="error", agents_failed=("quality",))
    assert batch.verdict == ReviewVerdict.APPROVE
    assert review_decision(batch) is None
    assert "NOT APPROVED" in _format_summary(batch, MARKER)


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_blocking_compliance_rule_holds_below_the_threshold_and_is_named_in_one_line(
    language,
) -> None:
    rule = _finding(ERROR, line=12, title="Every route is authenticated", agent="compliance",
                    rule_id="compliance.auth")
    batch = _review(_gh_pr(), [WARNING], threshold="critical", language=language, extra=[rule])
    assert batch.verdict == ReviewVerdict.REQUEST_CHANGES
    assert review_decision(batch) is None
    text = _format_summary(batch, MARKER)
    [line] = [ln for ln in text.splitlines() if "Every route is authenticated" in ln]
    assert line.startswith("Блокує правило відповідності:" if language == "uk"
                           else "Blocked by a compliance rule:")
    assert "finding 0" not in text, "the warning below the threshold is still not named"


def test_a_blocking_compliance_rule_that_is_listed_needs_no_extra_line() -> None:
    rule = _finding(ERROR, line=12, title="Every route is authenticated", agent="compliance",
                    rule_id="compliance.auth")
    batch = _review(_gh_pr(), [], threshold="error", extra=[rule])
    text = _format_summary(batch, MARKER)
    assert text.count("Every route is authenticated") == 1
    assert "Blocked by a compliance rule" not in text


def test_an_install_that_does_not_approve_still_says_the_verdict() -> None:
    batch = _review(_gh_pr(), [WARNING], threshold="error", approve=False)
    assert batch.verdict == ReviewVerdict.APPROVE
    assert review_decision(batch) is None
    assert "APPROVED" in _format_summary(batch, MARKER)


# ─── the summary: nothing below the threshold, in any language or layout ──


def _banned(batch: ReviewBatch) -> list[str]:
    """Words that would mean the summary spoke of the findings below the
    threshold: their severity, their titles, "found", the threshold itself."""
    return [
        "Warning", "Попередження", "Info", "Підказки", "Hint",
        "below", "нижче", "threshold", "порога", "shown inline", "показано",
        "Found", "Знайдено", "finding 0", "finding 1", "finding 2",
        "By category", "категоріями",
    ]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_a_summary_with_nothing_over_the_threshold_does_not_mention_what_is_under_it(
    language, layout,
) -> None:
    batch = _review(_gh_pr(), [WARNING, WARNING, INFO], threshold="error",
                    language=language, layout=layout)
    text = _format_summary(batch, MARKER)
    for word in _banned(batch):
        assert word not in text, f"{word!r} ({language}, {layout}) in:\n{text}"
    assert ("СХВАЛЕНО" if language == "uk" else "APPROVED") in text
    assert "COMMENT" not in text and "КОМЕНТАР" not in text


@pytest.mark.parametrize(("language", "words"), [
    (None, ("Performance", "Structure")),
    ("uk", ("Продуктивність", "Структура")),
])
def test_the_categories_of_the_findings_under_the_threshold_are_not_listed(
    language, words,
) -> None:
    """The screenshot: a performance warning and a structure hint under an
    errors-only threshold came out as "By category: Performance 1 · Structure 1"."""
    hidden = [
        _finding(WARNING, line=12, title="Slow loop", agent="performance"),
        _finding(INFO, line=13, title="Long function", agent="structural"),
    ]
    batch = _review(_gh_pr(), [], threshold="error", language=language, extra=hidden)
    text = _format_summary(batch, MARKER)
    for word in words:
        assert word not in text
    assert "Slow loop" not in text and "Long function" not in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_description_does_not_mention_what_is_under_the_threshold_either(language) -> None:
    batch = _review(_gh_pr(), [WARNING, INFO], threshold="error", language=language)
    batch.pr_overview = "Adds a cache."
    text = description_insights(batch)
    for word in _banned(batch):
        if word in ("Found", "Знайдено"):
            continue  # the block's own heading, followed by "no issues"
        assert word not in text, f"{word!r} in the description:\n{text}"
    assert ("Зауважень не знайдено." if language == "uk" else "No issues found.") in text


def _error_count_line(language: str | None, layout: str) -> str:
    """How each layout writes "one error": the completed comment follows the
    review language, the classic and rich layouts are English."""
    if layout == "completed":
        return "Помилки 1" if language == "uk" else "Error 1"
    return "**Error:** 1" if layout == "classic" else "Error: **1**"


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_only_the_findings_at_or_over_the_threshold_are_counted_and_listed(
    language, layout,
) -> None:
    batch = _review(_gh_pr(), [ERROR, WARNING, WARNING, INFO], threshold="error",
                    language=language, layout=layout)
    text = _format_summary(batch, MARKER)
    if layout != "classic":  # the classic layout counts and lists nothing by title
        assert "finding 0" in text
    for hidden in ("finding 1", "finding 2", "finding 3", "🟡", "💡"):
        assert hidden not in text, f"{hidden!r} ({language}, {layout})"
    assert _error_count_line(language, layout) in text
    description = description_insights(batch)
    assert "finding 0" in description
    assert "finding 1" not in description and "🟡" not in description


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_warning_threshold_counts_warnings_and_errors_and_still_not_hints(language) -> None:
    batch = _review(_gh_pr(), [ERROR, WARNING, INFO], threshold="warning", language=language)
    text = _format_summary(batch, MARKER)
    assert "finding 0" in text and "finding 1" in text and "finding 2" not in text
    assert "💡" not in text
    warning_word = {"uk": "Попередження"}.get(language, "Warning")
    assert f"{warning_word} 1" in text


def test_nothing_ran_and_nothing_over_the_threshold_is_still_not_an_approval() -> None:
    batch = _review(_gh_pr(), [], agents_run=(), agents_failed=("defect", "security"))
    text = _format_summary(batch, MARKER)
    assert batch.verdict == ReviewVerdict.SKIPPED
    assert "APPROVED" not in text


# ─── short ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("language", "verdict", "heading"), [
    (None, "✅ **APPROVED** — no blocking findings · commit `abcdef1`",
     "## Code Review Completed! 🔥"),
    ("uk", "✅ **СХВАЛЕНО** — блокуючих зауважень немає · коміт `abcdef1`",
     "## Code Review завершено! 🔥"),
])
def test_an_approving_summary_is_a_heading_and_one_verdict_line(
    language, verdict, heading,
) -> None:
    batch = _review(_gh_pr(), [WARNING, INFO], threshold="error", language=language)
    batch.pr_actions = PRActions(completed_comment="completed", approve_when_clean=True,
                                 commands_guide_enabled=True)
    assert _format_summary(batch, MARKER).splitlines() == [MARKER, heading, "", verdict]


def test_a_summary_with_findings_is_the_verdict_the_count_and_one_line_each() -> None:
    batch = _review(_gh_pr(), [ERROR, ERROR], threshold="error")
    lines = _format_summary(batch, MARKER).splitlines()
    assert lines[1:] == [
        "## Code Review Completed! 🔥", "",
        "💬 **COMMENT** — findings to consider · commit `abcdef1`", "",
        "**Found: 2** (🟠 Error 2)", "",
        "1. 🟠 **finding 0** — `src/a.py:11`",
        "2. 🟠 **finding 1** — `src/a.py:12`",
    ]


def test_the_review_body_of_a_clean_approval_is_the_verdict_alone() -> None:
    clean = _review(_gh_pr(), [WARNING], threshold="error")
    assert _format_review_pointer(clean) == "✅ **APPROVED** — no blocking findings"
    uk = _review(_gh_pr(), [WARNING], threshold="error", language="uk")
    assert _format_review_pointer(uk) == "✅ **СХВАЛЕНО** — блокуючих зауважень немає"


def test_the_review_body_with_findings_points_at_the_summary_in_the_review_language() -> None:
    batch = _review(_gh_pr(), [ERROR], threshold="error")
    assert _format_review_pointer(batch, "https://example.test/c#1").endswith(
        "Details: [review summary](https://example.test/c#1)")
    assert "summary" in _format_review_pointer(batch).lower()
    batch.review_language = "uk"
    assert _format_review_pointer(batch, "https://example.test/c#1").endswith(
        "Деталі: [підсумок рев'ю](https://example.test/c#1)")


# ─── every provider approves what has nothing over the threshold ────


def _approving(pr, severities=(), **kw) -> ReviewBatch:
    return _review(pr, severities, threshold="error", **kw)


class TestGitHub:
    def test_warnings_under_an_errors_threshold_are_approved_with_no_comment(self, settings) -> None:  # noqa: F811
        fake = _GitHub()
        _github(fake).post_review(_approving(_gh_pr(), [WARNING, WARNING, INFO]))
        [review] = fake.reviews
        assert review["event"] == "APPROVE"
        assert review["comments"] == []
        assert review["body"].endswith("✅ **APPROVED** — no blocking findings")
        [summary] = fake.bodies(fake.issue)
        assert "Warning" not in summary and "below" not in summary

    def test_a_reported_error_is_a_comment_not_an_approval(self, settings) -> None:  # noqa: F811
        fake = _GitHub()
        _github(fake).post_review(_approving(_gh_pr(), [ERROR, WARNING]))
        assert fake.reviews[-1]["event"] == "COMMENT"
        assert len(fake.reviews[-1]["comments"]) == 1

    def test_a_failed_critical_agent_is_not_approved(self, settings) -> None:  # noqa: F811
        fake = _GitHub()
        _github(fake).post_review(_approving(_gh_pr(), [WARNING], agents_failed=("security",)))
        assert fake.reviews[-1]["event"] == "COMMENT"

    def test_a_blocking_compliance_rule_is_not_approved(self, settings) -> None:  # noqa: F811
        fake = _GitHub()
        rule = _finding(ERROR, title="Rule", agent="compliance", rule_id="compliance.x")
        _github(fake).post_review(_review(
            _gh_pr(), [WARNING], threshold="critical", extra=[rule]))
        assert fake.reviews[-1]["event"] == "COMMENT"


class TestGitLab:
    def test_warnings_under_an_errors_threshold_are_approved(self, settings) -> None:  # noqa: F811
        fake = _GitLab()
        pr = _pr("gitlab", "group/proj", 5)
        result = _gitlab(fake).post_review(_approving(pr, [WARNING, INFO]))
        assert fake.approved_by == [BOT]
        assert fake.approval_calls == [("approve", {"sha": "abcdef1234567890"})]
        assert result["review_state"] == {"approval": "approved"}
        assert fake.discussion_bodies == []
        [summary] = [b for b in fake.bodies() if "Code Review" in b]
        assert "Warning" not in summary and "below" not in summary

    def test_a_reported_error_withdraws_an_earlier_approval(self, settings) -> None:  # noqa: F811
        fake = _GitLab()
        fake.approved_by = [BOT]
        _gitlab(fake).post_review(_approving(_pr("gitlab", "group/proj", 5), [ERROR]))
        assert fake.approved_by == []

    def test_a_failed_critical_agent_is_not_approved(self, settings) -> None:  # noqa: F811
        fake = _GitLab()
        _gitlab(fake).post_review(_approving(
            _pr("gitlab", "group/proj", 5), [WARNING], agents_failed=("defect",)))
        assert fake.approved_by == [] and fake.approval_calls == []


class TestBitbucket:
    def test_warnings_under_an_errors_threshold_are_approved(self, settings) -> None:  # noqa: F811
        fake = _Bitbucket()
        _bitbucket(fake).post_review(_approving(_pr("bitbucket", "ws/r", 3), [WARNING, INFO]))
        assert fake.state == "approved"
        assert fake.calls == ["POST approve"]
        summaries = [b for b in fake.bodies() if "Code Review" in b]
        assert len(summaries) == 1
        assert "Warning" not in summaries[0] and "below" not in summaries[0]
        assert not any("inline" in c for c in fake.comments), "no inline comment was posted"

    def test_the_summary_of_the_approval_is_two_lines_on_bitbucket_too(self, settings) -> None:  # noqa: F811
        fake = _Bitbucket()
        batch = _approving(_pr("bitbucket", "ws/r", 3), [WARNING], language="uk")
        _bitbucket(fake).post_review(batch)
        [summary] = [b for b in fake.bodies() if "Code Review" in b]
        shown = [ln for ln in summary.splitlines() if ln.strip() and not ln.startswith("[//]")]
        assert len(shown) == 2, summary

    def test_a_reported_error_is_not_approved(self, settings) -> None:  # noqa: F811
        fake = _Bitbucket()
        _bitbucket(fake).post_review(_approving(_pr("bitbucket", "ws/r", 3), [ERROR]))
        assert fake.state is None and fake.calls == []

    def test_a_failed_critical_agent_is_not_approved(self, settings) -> None:  # noqa: F811
        fake = _Bitbucket()
        _bitbucket(fake).post_review(_approving(
            _pr("bitbucket", "ws/r", 3), [WARNING], agents_failed=("contract",)))
        assert fake.state is None and fake.calls == []


# ─── through the orchestrator ───────────────────────────────────────


POLICY = {"enabled": True, "target_branches": [], "completed_comment": "completed",
          "comment_min_severity": "error", "approve_when_clean": True,
          "review_language": "uk"}


def _warning_agent() -> _Agent:
    return _Agent(findings=[
        e2e_finding(severity=WARNING, title="Loop never ends", line=2),
        e2e_finding(severity=INFO, title="Naming", line=3, agent="structural"),
    ])


def test_a_real_run_at_an_errors_threshold_approves_and_says_nothing_of_the_warnings(
    env, monkeypatch,  # noqa: F811
) -> None:
    fake = _GitHub()
    orch = _orch(monkeypatch, agents=[_warning_agent()], client=_Client(GOOD_REPLY),
                 policy=POLICY)
    result = orch.review("github", "o/r", 1, provider=GitHubOverFake(fake, e2e_pr()))

    assert result.batch.verdict == ReviewVerdict.APPROVE
    assert len(result.batch.findings) == 2, "the findings are stored, not dropped"
    assert fake.reviews[-1]["event"] == "APPROVE" and fake.reviews[-1]["comments"] == []
    [summary] = fake.bodies(fake.issue)
    assert "СХВАЛЕНО" in summary
    for word in ("Попередження", "Підказки", "нижче", "Loop never ends", "Naming", "Знайдено"):
        assert word not in summary


def test_a_real_run_with_an_error_over_the_threshold_comments_and_does_not_approve(
    env, monkeypatch,  # noqa: F811
) -> None:
    fake = _GitHub()
    agent = _Agent(findings=[
        e2e_finding(severity=ERROR, title="Cache never expires", line=2),
        e2e_finding(severity=WARNING, title="Loop never ends", line=3),
    ])
    orch = _orch(monkeypatch, agents=[agent], client=_Client(GOOD_REPLY), policy=POLICY)
    result = orch.review("github", "o/r", 1, provider=GitHubOverFake(fake, e2e_pr()))

    assert result.batch.verdict == ReviewVerdict.COMMENT
    assert fake.reviews[-1]["event"] == "COMMENT" and len(fake.reviews[-1]["comments"]) == 1
    [summary] = fake.bodies(fake.issue)
    assert "Cache never expires" in summary and "Loop never ends" not in summary
    assert "Знайдено: 1" in summary


def test_a_real_run_where_a_critical_stage_died_is_not_approved_at_any_threshold(
    env, monkeypatch,  # noqa: F811
) -> None:
    class _Dead(_Agent):
        def review(self, context):
            raise RuntimeError("provider down")

    fake = _GitHub()
    orch = _orch(monkeypatch, agents=[_warning_agent(), _Dead("security")],
                 client=_Client(GOOD_REPLY), policy=POLICY)
    result = orch.review("github", "o/r", 1, provider=GitHubOverFake(fake, e2e_pr()))

    assert result.batch.failed_critical_agents == ["security"]
    assert result.batch.verdict == ReviewVerdict.COMMENT
    assert fake.reviews[-1]["event"] == "COMMENT"
    [summary] = fake.bodies(fake.issue)
    assert "НЕ СХВАЛЕНО" in summary and "Loop never ends" not in summary


def test_the_request_changes_decision_is_unchanged() -> None:
    batch = _review(_gh_pr(), [CRITICAL], threshold="critical")
    batch.pr_actions = PRActions(request_changes_on_critical=True)
    assert review_decision(batch) == REQUEST_CHANGES


# ─── the other places a count is read ───────────────────────────────


def test_the_channel_notification_counts_what_the_pull_request_was_told() -> None:
    from src.review.orchestrator import _notification_counts, _severity_for_verdict

    batch = _review(_gh_pr(), [WARNING, WARNING, INFO], threshold="error")
    assert _notification_counts(batch) == (
        "**0** critical · **0** error · **0** warn · **0** info")
    assert _severity_for_verdict(batch) == "info"

    shown = _review(_gh_pr(), [ERROR, WARNING], threshold="error")
    assert _notification_counts(shown) == (
        "**0** critical · **1** error · **0** warn · **0** info")
    assert _severity_for_verdict(shown) == "warn"
