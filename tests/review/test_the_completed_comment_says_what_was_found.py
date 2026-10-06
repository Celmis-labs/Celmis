"""The Kodus-style closing of a review: a greeting, "Code Review Completed" with
what was found, the same findings in the PR description, and no finding lost
to a comment the git provider would not place.

  * the greeting is for a PR's first review, a "new changes" line for a later
    push, the plain heading when nobody can say which;
  * the completed comment leads with the verdict and the counts, in the
    repository's review language, and lists the commands only on request;
  * `completed_comment=classic` keeps the layout of earlier releases;
  * stages add blocks through `batch.summary_sections`, shown in the comment
    and/or the description, and never edit the composers;
  * the description carries a findings block; a re-run of the same commit
    rewrites the stamp without a clock time;
  * Bitbucket: a refused inline comment is folded into the summary, a context
    line is anchored by both of its sides, a 429 is waited out once, the
    description PUT carries every field it read.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest

from src.review import issues, markers
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PRActions,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.pr_actions import (
    SUMMARY_END,
    SUMMARY_HEADING,
    SUMMARY_START,
    compose_description,
    description_insights,
    split_description,
)
from src.review.providers import bitbucket as bitbucket_module
from src.review.providers.base import (
    _format_review_pointer,
    _format_started_comment,
    _format_summary,
    _format_unanchored,
    _old_line_for,
)
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.test_a_rerun_does_not_double_the_comments import (
    MARKER,
    _FakeBitbucket,
    _FakeGitHub,
    _FakeGitLab,
    _patch_client,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    GOOD_REPLY,
    _Agent,
    _Client,
    _orch,
    _Provider,
    _run,
    env,  # noqa: F401 — fixture
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import _finding as e2e_finding
from tests.review.test_the_pr_hears_the_review_begin_and_end import _pr as e2e_pr
from tests.review.test_the_pr_says_what_it_does import (
    HUNK,
    _batch,
    _finding,
    _GitHub,
    _pr,
    settings,  # noqa: F401 — fixture
)
from tests.review.test_the_pr_says_what_it_does import _Provider as DescribingProvider

COMPLETED = PRActions(completed_comment="completed")


def _batch_with(findings, *, language=None, actions=COMPLETED, url="") -> ReviewBatch:
    batch = _batch(_pr("github", "o/r", 7, url=url), findings, actions=actions)
    batch.review_language = language
    return batch


def _three_findings() -> list[Finding]:
    return [
        _finding(FindingSeverity.CRITICAL, line=11, title="Token is logged",
                 agent="security", rule_id="sec.log"),
        _finding(FindingSeverity.WARNING, line=12, title="Loop never ends"),
        _finding(FindingSeverity.WARNING, line=13, title="Off by one"),
    ]


# ─── the completed comment ──────────────────────────────────────────


def test_the_completed_comment_says_what_was_found_in_ukrainian() -> None:
    body = _format_summary(_batch_with(_three_findings(), language="uk"), MARKER)

    assert body.startswith(MARKER + "\n## Code Review завершено! 🔥")
    assert "**Знайдено: 3**" in body
    assert "🔴 Критичні 1" in body and "🟡 Попередження 2" in body
    assert "**За категоріями:** Помилка **2** · Безпека **1**" in body
    assert "1. 🔴 **Token is logged** — `src/a.py:11`" in body
    assert "Файлів: **1**" in body and "Коміт: `abcdef1`" in body
    # The change summary has nowhere else to be: the comment holds it.
    assert "Code Review Completed" not in body


def test_the_completed_comment_speaks_english_by_default() -> None:
    body = _format_summary(_batch_with(_three_findings()), MARKER)

    assert "## Code Review Completed! 🔥" in body
    assert "**Found: 3**" in body and "🔴 Critical 1" in body
    assert "**By category:** Bug **2** · Security **1**" in body


def test_a_clean_review_says_so_and_a_review_that_did_not_look_does_not() -> None:
    clean = _format_summary(_batch_with([], language="uk"), MARKER)
    assert "_Зауважень не знайдено._" in clean

    nothing_ran = _batch(_pr("github", "o/r", 7), [], actions=COMPLETED, agents_run=())
    assert "No issues detected" not in _format_summary(nothing_ran, MARKER)


def test_the_top_findings_are_five_and_the_rest_are_counted() -> None:
    findings = [_finding(line=10 + i, title=f"Finding {i}") for i in range(8)]
    body = _format_summary(_batch_with(findings), MARKER)
    assert "5. " in body and "6. " not in body
    assert "…and 3 more in the inline comments" in body


def test_the_commands_guide_follows_its_setting(settings) -> None:  # noqa: F811
    off = _format_summary(_batch_with(_three_findings()), MARKER)
    assert "### Commands" not in off

    on = _format_summary(_batch_with(
        _three_findings(), actions=PRActions(completed_comment="completed",
                                             commands_guide_enabled=True)), MARKER)
    assert "### Commands" in on
    assert "`@celmis start-review`" in on and "`@celmis review --force`" in on

    uk = _batch_with(_three_findings(), language="uk", actions=PRActions(
        completed_comment="completed", commands_guide_enabled=True))
    assert "### Команди" in _format_summary(uk, MARKER)


def test_the_commands_guide_does_not_offer_what_the_repository_switched_off(settings) -> None:  # noqa: F811
    import src.review.commands.chat  # noqa: F401 — registers the chat command
    import src.review.commands.remember  # noqa: F401 — registers `remember`

    batch = _batch_with(_three_findings(), actions=PRActions(
        completed_comment="completed", commands_guide_enabled=True,
        guide_commands_off=("chat", "remember")))
    body = _format_summary(batch, MARKER)
    assert "`@celmis start-review`" in body
    assert "remember:" not in body and "to ask a question" not in body


def test_the_guide_describes_a_review_the_way_the_incremental_scope_works(settings) -> None:  # noqa: F811
    body = _format_summary(_batch_with(_three_findings(), actions=PRActions(
        completed_comment="completed", commands_guide_enabled=True)), MARKER)
    assert "since the last review" in body
    assert "ignoring what was already reviewed" in body


def test_the_settings_link_is_there_only_when_the_install_has_an_address(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", "https://celmis.example.com")
    with_url = _format_summary(_batch_with(_three_findings()), MARKER)
    assert re.search(r"\(https://celmis\.example\.com/review-settings\?repo=[^)]+\)", with_url)

    monkeypatch.setattr(get_settings(), "public_base_url", "")
    assert "review-settings" not in _format_summary(_batch_with(_three_findings()), MARKER)


def test_classic_keeps_the_layout_of_earlier_releases() -> None:
    batch = _batch_with(_three_findings(), actions=PRActions(completed_comment="classic"))
    body = _format_summary(batch, MARKER)
    assert "Code Review for PR #7" in body and "### Scope" in body
    assert "Code Review Completed" not in body

    # A batch built by hand never asked for anything else.
    hand_built = _batch(_pr("github", "o/r", 7), _three_findings())
    assert "Code Review for PR #7" in _format_summary(hand_built, MARKER)


def test_a_repository_header_still_replaces_the_completed_title() -> None:
    actions = PRActions(completed_comment="completed",
                        message_finished_header="Done #{pr_number}")
    body = _format_summary(_batch_with(_three_findings(), actions=actions), MARKER)
    assert "Done #7" in body and "Code Review Completed" not in body


def test_the_change_summary_in_the_description_is_pointed_at_not_repeated() -> None:
    batch = _batch_with(_three_findings(), language="uk")
    batch.pr_overview = "Додає кеш."
    batch.summary_in_description = True
    body = _format_summary(batch, MARKER)
    assert "Підсумок змін — в описі pull request" in body
    assert "Додає кеш." not in body

    batch.summary_in_description = False
    assert "Додає кеш." in _format_summary(batch, MARKER)


# ─── summary sections ───────────────────────────────────────────────


def test_a_section_is_shown_where_it_was_asked_for_and_in_its_order() -> None:
    batch = _batch_with(_three_findings())
    batch.add_section("late", "LATE-BLOCK", order=800)
    batch.add_section("early", "EARLY-BLOCK", order=100)
    batch.add_section("only_description", "DESC-ONLY", targets={"description"})
    batch.add_section("only_comment", "COMMENT-ONLY", targets={"comment"}, order=300)

    comment = _format_summary(batch, MARKER)
    assert comment.index("EARLY-BLOCK") < comment.index("COMMENT-ONLY") < comment.index("LATE-BLOCK")
    assert "DESC-ONLY" not in comment

    description = description_insights(batch)
    assert "EARLY-BLOCK" in description and "DESC-ONLY" in description
    assert "COMMENT-ONLY" not in description


def test_a_section_is_replaced_by_its_key_and_removed_by_an_empty_text() -> None:
    batch = _batch_with([])
    batch.add_section("k", "first")
    batch.add_section("k", "second")
    assert [s.markdown for s in batch.sections_for("comment")] == ["second"]
    batch.add_section("k", "  ")
    assert batch.sections_for("comment") == []


@pytest.mark.parametrize("layout", ["classic", "completed"])
def test_the_classic_and_the_completed_comment_both_show_the_sections(layout) -> None:
    batch = _batch_with(_three_findings(), actions=PRActions(completed_comment=layout))
    batch.add_section("x", "FROM-A-STAGE")
    assert "FROM-A-STAGE" in _format_summary(batch, MARKER)
    batch.rich_summary = True
    assert "FROM-A-STAGE" in _format_summary(batch, MARKER)


# ─── the greeting ───────────────────────────────────────────────────


def test_the_first_review_greets_and_a_later_push_says_the_review_is_updated() -> None:
    pr = _pr("github", "o/r", 7)
    first = _format_started_comment(pr, agents=["defect"], started_at="t",
                                    first_review=True)
    assert "👋 Hi! I'm Celmis. Starting the review of commit `abcdef1` (1 file)…" in first
    later = _format_started_comment(pr, agents=["defect"], started_at="t",
                                    first_review=False, language="uk")
    assert "Нові зміни — оновлюю рев'ю коміту `abcdef1` (1 файл)…" in later
    unknown = _format_started_comment(pr, agents=["defect"], started_at="t")
    assert "Celmis is reviewing this PR…" in unknown


def test_the_greeting_follows_the_review_language_and_plurals() -> None:
    pr = _pr("github", "o/r", 7)
    pr.hunks = [HUNK] * 3
    pr.hunks = [
        Hunk(file_path=f"f{i}.py", old_file_path=f"f{i}.py", old_start=1, old_count=1,
             new_start=1, new_count=1, content="@@ -1 +1 @@\n x\n") for i in range(5)
    ]
    body = _format_started_comment(pr, agents=[], started_at="t", first_review=True,
                                   language="uk")
    assert "Привіт! Я Celmis. Починаю рев'ю коміту `abcdef1` (5 файлів)…" in body


def test_a_custom_started_message_still_wins_over_the_greeting() -> None:
    body = _format_started_comment(_pr("github", "o/r", 7), agents=[], started_at="t",
                                   template="On it: {commit}", first_review=True)
    assert "On it: abcdef1" in body and "Hi! I'm Celmis" not in body


def _placeholder_during_review(monkeypatch, count) -> str:
    fake = _FakeGitHub()
    seen: list[str] = []
    monkeypatch.setattr(issues, "pr_review_count", lambda **kw: count)
    agent = _Agent(findings=[e2e_finding()],
                   probe=lambda: seen.extend(fake.bodies(fake.issue)))
    orch = _orch(monkeypatch, agents=[agent], client=_Client(GOOD_REPLY))
    _run(orch, _Provider(fake, e2e_pr()))
    [placeholder] = seen
    return placeholder


def test_the_orchestrator_greets_a_pr_nobody_reviewed_yet(env, monkeypatch) -> None:  # noqa: F811
    assert "Hi! I'm Celmis" in _placeholder_during_review(monkeypatch, 0)


def test_the_orchestrator_says_new_changes_for_a_pr_it_reviewed_before(env, monkeypatch) -> None:  # noqa: F811
    assert "New changes" in _placeholder_during_review(monkeypatch, 2)


def test_a_database_that_cannot_answer_costs_only_the_greeting(env, monkeypatch) -> None:  # noqa: F811
    def broken(**kw):
        raise RuntimeError("no database")

    fake = _FakeGitHub()
    seen: list[str] = []
    monkeypatch.setattr(issues, "pr_review_count", broken)
    agent = _Agent(probe=lambda: seen.extend(fake.bodies(fake.issue)))
    result = _run(_orch(monkeypatch, agents=[agent]), _Provider(fake, e2e_pr()))
    assert result.posted is True
    assert "Celmis is reviewing this PR…" in seen[0]


# ─── the findings block of the description ──────────────────────────


def _described(language=None) -> ReviewBatch:
    batch = _batch_with(_three_findings(), language=language)
    batch.pr_overview = "Adds a cache."
    batch.walkthrough = {"src/a.py": "wraps the loader"}
    return batch


def test_the_description_says_what_was_found_between_the_overview_and_the_walkthrough() -> None:
    text = description_insights(_described("uk"))
    assert text.startswith(SUMMARY_HEADING)
    assert "### Знайдено" in text and "Усього **3**" in text
    assert "1. 🔴 **Token is logged**" in text
    assert "До 3 залишено коментарями в коді." in text
    assert text.index("Adds a cache.") < text.index("### Знайдено") < text.index("### Огляд змін")


def test_a_description_without_findings_says_nothing_was_found_only_if_something_looked() -> None:
    clean = _described()
    clean.findings = []
    assert "No issues found." in description_insights(clean)

    nothing_ran = _batch(_pr("github", "o/r", 7), [], agents_run=())
    nothing_ran.pr_overview = "Adds a cache."
    assert "### Findings" not in description_insights(nothing_ran)


def test_the_findings_alone_are_enough_to_write_a_description() -> None:
    batch = _batch_with(_three_findings())
    text = description_insights(batch)
    assert "### Findings" in text and "**3** in total" in text


def test_the_description_block_fits_the_limit_and_keeps_the_findings_longest() -> None:
    batch = _described()
    batch.walkthrough = {f"src/f{i}.py": "x" * 150 for i in range(40)}
    batch.pull_request.hunks = [
        Hunk(file_path=f"src/f{i}.py", old_file_path=f"src/f{i}.py", old_start=1,
             old_count=1, new_start=1, new_count=1, content="@@ -1 +1 @@\n x\n")
        for i in range(40)
    ]
    out = compose_description(
        "Author's words.", insights=description_insights(batch), commit="abcdef1234567890",
        existing_mode="append", new_commits_mode="replace", limit=5_000,
    )
    assert out is not None and len(out) <= 5_000
    assert out.startswith("Author's words.")
    assert "### Findings" in out and "Token is logged" in out


# ─── the stamp ──────────────────────────────────────────────────────


def _compose(current, commit, when, **kw):
    return compose_description(
        current, insights="## 🤖 Celmis summary\n\nover", commit=commit,
        existing_mode="append", new_commits_mode="replace", now=when, **kw)


def test_a_new_commit_is_stamped_with_the_time_and_a_rerun_of_it_without() -> None:
    t1 = datetime(2026, 10, 6, 12, 3, tzinfo=UTC)
    t2 = datetime(2026, 10, 6, 15, 40, tzinfo=UTC)
    first = _compose("Mine.", "abcdef1234567890", t1)
    assert "2026-10-06 12:03 UTC" in first
    assert "<sub>" not in first, "Bitbucket would show the tag as text"

    second = _compose(first, "abcdef1234567890", t2)
    assert "12:03" not in second and "15:40" not in second
    assert "2026-10-06" in second

    # Settled: the next re-run writes exactly the same text, so a provider that
    # reports a description change as an event is not woken by it again.
    assert _compose(second, "abcdef1234567890", datetime(2026, 10, 6, 23, 59, tzinfo=UTC)) == second

    third = _compose(second, "1111111deadbeef0", t2)
    assert "15:40 UTC" in third


def test_the_stamp_and_the_update_heading_follow_the_language() -> None:
    t1 = datetime(2026, 10, 6, 12, 3, tzinfo=UTC)
    uk = compose_description(
        "", insights="## 🤖 Celmis summary\n\nover", commit="abcdef1234567890",
        existing_mode="append", new_commits_mode="append", now=t1, language="uk")
    assert "Підсумок Celmis · коміт `abcdef1`" in uk
    later = compose_description(
        uk, insights="## 🤖 Celmis summary\n\nover2", commit="2222222deadbeef0",
        existing_mode="append", new_commits_mode="append", now=t1, language="uk")
    assert "### Оновлення — 2026-10-06 (коміт `2222222`)" in later
    # The update section is found again by its hidden mark, whatever its language.
    again = compose_description(
        later, insights="## 🤖 Celmis summary\n\nover3", commit="2222222deadbeef0",
        existing_mode="append", new_commits_mode="append", now=t1, language="uk")
    assert again.count("### Оновлення") == 1 and "over3" in again and "over2" not in again


# ─── Bitbucket ──────────────────────────────────────────────────────

BASE = "/2.0/repositories/ws/r/pullrequests/3"


class _Refusing(_FakeBitbucket):
    """Bitbucket that will not anchor a comment on `bad_path`, and answers 429
    to the first `rate_limited` inline POSTs."""

    def __init__(
        self, *, bad_path: str = "", rate_limited: int = 0,
        retry_after: str = "2", **kw,
    ) -> None:
        super().__init__(**kw)
        self.retry_after = retry_after
        self.bad_path = bad_path
        self.rate_limited = rate_limited
        self.inline_payloads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"{BASE}/comments":
            payload = json.loads(request.content)
            inline = payload.get("inline")
            if inline:
                if self.rate_limited > 0:
                    self.rate_limited -= 1
                    return httpx.Response(429, headers={"Retry-After": self.retry_after}, json={})
                self.inline_payloads.append(inline)
                if inline.get("path") == self.bad_path:
                    return httpx.Response(400, json={"error": {"message": "bad anchor"}})
        return super().__call__(request)


def _provider(fake) -> BitbucketPRProvider:
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


def _bb_batch(findings, **kw) -> ReviewBatch:
    batch = _batch(_pr("bitbucket", "ws/r", 3), findings, actions=COMPLETED)
    batch.review_language = kw.get("language")
    return batch


def test_an_unanchorable_bitbucket_finding_lands_in_the_summary(settings) -> None:  # noqa: F811
    fake = _Refusing(bad_path="src/gone.py")
    findings = [
        _finding(line=11, title="Placed fine"),
        _finding(line=11, title="Refused by Bitbucket", file_path="src/gone.py",
                 body="The long explanation that must not get lost."),
    ]
    provider = _provider(fake)
    response = provider.post_review(_bb_batch(findings))
    provider.close()

    assert response["inline_posted"] == 1 and response["inline_failed"] == 1
    summaries = [b for b in fake.bodies() if "Code Review Completed" in b]
    assert len(summaries) == 1
    summary = summaries[0]
    assert "### Findings without a place in the diff" in summary
    assert "Refused by Bitbucket" in summary
    assert "The long explanation that must not get lost." in summary
    assert "Placed fine" in summary.split("### Findings without")[0], "the placed one is not repeated"


def test_the_refused_findings_are_said_in_the_review_language(settings) -> None:  # noqa: F811
    fake = _Refusing(bad_path="src/gone.py")
    provider = _provider(fake)
    provider.post_review(_bb_batch(
        [_finding(file_path="src/gone.py", title="Refused")], language="uk"))
    provider.close()
    assert any("### Зауваження без місця в diff" in b for b in fake.bodies())


def test_a_summary_with_no_refused_finding_has_no_such_block(settings) -> None:  # noqa: F811
    fake = _Refusing()
    provider = _provider(fake)
    provider.post_review(_bb_batch([_finding(line=11)]))
    provider.close()
    assert not any("without a place" in b for b in fake.bodies())


def test_the_unanchored_list_keeps_position_and_explanation_and_is_bounded() -> None:
    long = _finding(title="Big", body="word " * 1000)
    text = _format_unanchored([long], _pr("github", "o/r", 7))
    assert "`src/a.py:11`" in text and text.endswith("…")
    assert len(text) < 1_800
    assert _format_unanchored([], _pr("github", "o/r", 7)) == ""


def test_the_review_pointer_and_the_classic_layout_speak_the_review_language() -> None:
    classic = PRActions(completed_comment="classic")
    uk = _batch_with(_three_findings(), language="uk", actions=classic)
    en = _batch_with(_three_findings(), actions=classic)
    assert _format_review_pointer(uk) != _format_review_pointer(en)
    assert _format_summary(uk, MARKER) != _format_summary(en, MARKER)


def test_a_cut_inside_a_code_fence_does_not_swallow_the_rest_of_the_summary() -> None:
    body = "Explanation first.\n" + "text " * 200 + "\n```python\n" + "x = 1\n" * 150 + "```\n"
    text = _format_unanchored([_finding(title="Fenced", body=body)], _pr("bitbucket", "ws/r", 3))
    fences = [ln for ln in text.splitlines() if ln.strip().startswith("```")]
    assert len(fences) % 2 == 0 and text.endswith("…")

    batch = _batch_with(_three_findings())
    batch.add_section("unanchored", text)
    flavoured = markers.bitbucket_flavour(_format_summary(batch, MARKER))
    assert not any(tag in flavoured for tag in ("<details>", "<summary>", "<sub>"))


def test_a_context_line_is_anchored_by_both_of_its_sides_and_an_added_line_by_one(settings) -> None:  # noqa: F811
    # HUNK: " a" 10/10, "-b" old 11, "+B" new 11, "+C" new 12, " c" 12/13, " d" 13/14
    fake = _Refusing()
    provider = _provider(fake)
    provider.post_review(_bb_batch([
        _finding(line=13, title="on a context line"),
        _finding(line=11, title="on an added line"),
    ]))
    provider.close()
    by_to = {p["to"]: p for p in fake.inline_payloads}
    assert by_to[13] == {"path": "src/a.py", "to": 13, "from": 12}
    assert by_to[11] == {"path": "src/a.py", "to": 11}


def test_the_old_line_of_a_new_line_is_read_from_the_hunk() -> None:
    pr = _pr("bitbucket", "ws/r", 3)
    assert _old_line_for(pr, "src/a.py", 10) == 10
    assert _old_line_for(pr, "src/a.py", 11) is None     # added
    assert _old_line_for(pr, "src/a.py", 12) is None     # added
    assert _old_line_for(pr, "src/a.py", 13) == 12
    assert _old_line_for(pr, "src/a.py", 14) == 13
    assert _old_line_for(pr, "src/a.py", 15) is None     # outside the hunk
    assert _old_line_for(pr, "src/other.py", 10) is None


def test_a_rate_limited_comment_is_waited_for_once_and_then_posted(settings, monkeypatch) -> None:  # noqa: F811
    waits: list[float] = []
    monkeypatch.setattr(bitbucket_module.time, "sleep", waits.append)
    fake = _Refusing(rate_limited=1)
    provider = _provider(fake)
    response = provider.post_review(_bb_batch([_finding(line=11)]))
    provider.close()
    assert waits == [2.0]
    assert response["inline_posted"] == 1 and response["inline_failed"] == 0


@pytest.mark.parametrize("header", ["nan", "inf", "-5", "soon"])
def test_a_malformed_retry_after_does_not_abort_the_review(settings, monkeypatch, header) -> None:  # noqa: F811
    waits: list[float] = []
    monkeypatch.setattr(bitbucket_module.time, "sleep", waits.append)
    fake = _Refusing(rate_limited=1, retry_after=header)
    provider = _provider(fake)
    response = provider.post_review(_bb_batch([_finding(line=11)]))
    provider.close()
    assert len(waits) == 1 and 0.0 <= waits[0] <= 30.0
    assert response["inline_posted"] == 1


def test_a_comment_that_stays_rate_limited_is_folded_not_lost(settings, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(bitbucket_module.time, "sleep", lambda s: None)
    fake = _Refusing(rate_limited=2)
    provider = _provider(fake)
    response = provider.post_review(_bb_batch([_finding(line=11, title="Limited")]))
    provider.close()
    assert response["inline_failed"] == 1
    assert any("Limited" in b and "without a place" in b for b in fake.bodies())


class _PullRequestApi:
    """GET/PUT of one Bitbucket PR, as far as the description write needs."""

    def __init__(self, meta: dict) -> None:
        self.meta = dict(meta)
        self.puts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == BASE
        if request.method == "PUT":
            self.puts.append(json.loads(request.content))
            return httpx.Response(200, json=self.puts[-1])
        return httpx.Response(200, json=self.meta)


def test_the_description_put_carries_every_field_it_read(settings) -> None:  # noqa: F811
    api = _PullRequestApi({
        "title": "Add caching", "description": "Mine.", "draft": True,
        "close_source_branch": True, "reviewers": [{"uuid": "{u1}"}],
    })
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(api))
    result = provider.update_description(
        _pr("bitbucket", "ws/r", 3), lambda cur: cur + "\n\nAdded.")
    assert result["written"] is True
    [put] = api.puts
    assert put["title"] == "Add caching" and put["reviewers"] == [{"uuid": "{u1}"}]
    assert put["draft"] is True and put["close_source_branch"] is True


def test_the_description_put_sends_no_flag_the_pr_did_not_have(settings) -> None:  # noqa: F811
    api = _PullRequestApi({"title": "T", "description": "Mine."})
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(api))
    provider.update_description(_pr("bitbucket", "ws/r", 3), lambda cur: cur + "!")
    assert "draft" not in api.puts[0] and "close_source_branch" not in api.puts[0]


def test_nothing_a_bitbucket_completed_comment_stores_carries_raw_html(settings) -> None:  # noqa: F811
    fake = _Refusing()
    provider = _provider(fake)
    batch = _bb_batch(_three_findings())
    batch.pr_overview = "Adds a cache."
    provider.post_review(batch)
    provider.close()
    stored = "\n".join(fake.bodies())
    assert "Code Review Completed" in stored
    assert not re.search(r"</?(sub|details|summary)>", stored)
    assert "<!--" not in stored


# ─── the whole run ──────────────────────────────────────────────────

POLICY = {"enabled": True, "target_branches": [], "summary_target": "description", "completed_comment": "completed",
          "review_language": "uk"}


def test_a_review_writes_the_findings_into_the_description_and_points_at_them(env, monkeypatch) -> None:  # noqa: F811
    fake = _GitHub()
    monkeypatch.setattr(issues, "pr_review_count", lambda **kw: 0)
    orch = _orch(monkeypatch, agents=[_Agent(findings=[e2e_finding()])],
                 client=_Client(GOOD_REPLY), policy=POLICY)
    result = _run(orch, DescribingProvider(fake, e2e_pr()))

    assert result.provider_response["description"]["written"] is True
    assert "### Знайдено" in fake.pr_body and "Cache never expires" in fake.pr_body
    [summary] = fake.issue
    assert "Code Review завершено!" in summary["body"]
    assert "Підсумок змін — в описі pull request" in summary["body"]
    assert "Cache never expires" in summary["body"]


def test_the_built_in_closing_comment_is_the_completed_one(env, monkeypatch) -> None:  # noqa: F811
    fake = _FakeGitHub()
    orch = _orch(monkeypatch, agents=[_Agent(findings=[e2e_finding()])],
                 client=_Client(GOOD_REPLY), policy={"completed_comment": None})
    _run(orch, _Provider(fake, e2e_pr()))
    [summary] = fake.issue
    assert "Code Review Completed" in summary["body"]


def test_the_verdict_of_a_completed_comment_is_unchanged() -> None:
    batch = _batch_with([_finding(FindingSeverity.CRITICAL, line=11)])
    assert batch.verdict == ReviewVerdict.REQUEST_CHANGES
    assert "CHANGES REQUESTED" in _format_summary(batch, MARKER)


# ─── review fixes ───────────────────────────────────────────────────


class _RefusingGitLab(_FakeGitLab):
    """A GitLab that will not place a discussion on one path."""

    def __init__(self, *, bad_path: str = "", **kw) -> None:
        super().__init__(**kw)
        self.bad_path = bad_path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        is_discussion = (
            request.method == "POST" and str(request.url).split("?")[0].endswith("/discussions"))
        if is_discussion and self._form(request).get("position[new_path]") == self.bad_path:
            return httpx.Response(400, json={"message": "line_code is invalid"})
        return super().__call__(request)


def _gitlab_provider(fake) -> GitLabPRProvider:
    provider = GitLabPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


def test_a_gitlab_discussion_that_is_refused_lands_in_the_summary(settings) -> None:  # noqa: F811
    fake = _RefusingGitLab(bad_path="src/gone.py")
    findings = [
        _finding(line=11, title="Placed fine"),
        _finding(line=11, title="Refused by GitLab", file_path="src/gone.py",
                 body="The long explanation that must not get lost."),
    ]
    batch = _batch(_pr("gitlab", "group/proj", 5), findings, actions=COMPLETED)
    provider = _gitlab_provider(fake)
    response = provider.post_review(batch)
    provider.close()

    assert response["discussions_posted"] == 1 and response["discussions_failed"] == 1
    summaries = [b for b in fake.bodies() if "Code Review Completed" in b]
    assert len(summaries) == 1
    assert "### Findings without a place in the diff" in summaries[0]
    assert "Refused by GitLab" in summaries[0]
    assert "The long explanation that must not get lost." in summaries[0]


def test_a_gitlab_summary_names_the_refused_findings_in_the_review_language(settings) -> None:  # noqa: F811
    fake = _RefusingGitLab(bad_path="src/gone.py")
    batch = _batch(_pr("gitlab", "group/proj", 5),
                   [_finding(file_path="src/gone.py", title="Refused")], actions=COMPLETED)
    batch.review_language = "uk"
    provider = _gitlab_provider(fake)
    provider.post_review(batch)
    provider.close()
    assert any("### Зауваження без місця в diff" in b for b in fake.bodies())


def test_the_details_and_the_footer_of_the_completed_comment_follow_the_review_language() -> None:
    batch = _batch_with(_three_findings(), language="uk")
    batch.elapsed_seconds = 2.0
    text = _format_summary(batch, MARKER)
    assert "Змінено файлів: **1**" in text
    assert "Час аналізу: **2.0s**" in text and "агенти: defect" in text
    assert "Працює на Code Analyzer" in text
    assert "Files changed" not in text and "Analysis time" not in text
    assert "Powered by" not in text


def test_the_details_and_the_footer_stay_english_by_default() -> None:
    batch = _batch_with(_three_findings())
    batch.elapsed_seconds = 2.0
    text = _format_summary(batch, MARKER)
    assert "- Files changed: **1**" in text and "Analysis time: **2.0s** · agents: defect" in text
    assert "Powered by Code Analyzer" in text


def test_a_finding_title_cannot_forge_our_marker_or_carry_a_link_or_a_mention() -> None:
    hostile = "Token <!-- celmis:summary:end --> leaked @victim [x](http://e.vil)"
    batch = _batch_with([_finding(title=hostile)])
    batch.pr_overview = "Adds a cache."
    insights = description_insights(batch)
    assert insights.count(SUMMARY_END) == 0
    assert "<!--" not in insights.split("Token", 1)[1].split("\n", 1)[0]
    assert "http://e.vil" not in insights and "@victim" not in insights
    assert "leaked" in insights, "the readable part of the title is kept"


def test_a_hostile_title_does_not_leave_a_stale_tail_after_two_runs() -> None:
    hostile = "Token <!-- celmis:summary:end --> leaked @victim [x](http://e.vil)"
    batch = _batch_with([_finding(title=hostile)])
    insights = description_insights(batch)
    t1 = datetime(2026, 10, 6, 12, 3, tzinfo=UTC)
    t2 = datetime(2026, 10, 6, 13, 3, tzinfo=UTC)
    first = compose_description("Author text.", insights=insights, commit="a" * 40,
                                existing_mode="append", new_commits_mode="replace", now=t1)
    second = compose_description(first, insights=insights, commit="b" * 40,
                                 existing_mode="append", new_commits_mode="replace", now=t2)
    assert first.count(SUMMARY_END) == 1 and second.count(SUMMARY_END) == 1
    assert second.count(SUMMARY_START) == 1 and second.count("Token") == 1
    assert second.startswith("Author text.")


def test_an_end_marker_quoted_inside_the_block_does_not_end_it_early() -> None:
    inner = f"## 🤖 Celmis summary\n\nquoted: {SUMMARY_END} and more\n"
    text = f"before\n{SUMMARY_START}\n{inner}\n{SUMMARY_END}\nafter"
    before, found, after = split_description(text)
    assert before == "before\n" and after == "\nafter"
    assert found is not None and "and more" in found


def test_a_file_path_cannot_break_out_of_its_code_span() -> None:
    batch = _batch_with([_finding(file_path="src/<!-- celmis:summary:end -->`x.py", line=11)])
    insights = description_insights(batch)
    assert SUMMARY_END not in insights and "<!--" not in insights.split("1.", 1)[1]


def test_the_skipped_files_line_has_the_right_plural_in_both_languages() -> None:
    from src.review.providers.base import _scope_lines

    for n, uk_word in ((1, "файл"), (3, "файли"), (5, "файлів")):
        batch = _batch_with(_three_findings(), language="uk")
        batch.skipped_files = [f"f{i}.lock" for i in range(n)]
        assert f"- Пропущено: {n} {uk_word} (" in _scope_lines(batch, "uk")[-1]
    batch = _batch_with(_three_findings())
    batch.skipped_files = ["a.lock"]
    assert _scope_lines(batch)[-1] == "- Skipped: 1 file (lock/binary/generated/too large)"
