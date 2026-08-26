"""Two agents describing one defect on one line produce one comment.

THE GAP THIS CLOSES. `prefilter`'s exact dedup (step 2) and its near-duplicate
clustering (step 3) both bucket by `rule_id`, and four agents looking at one
line write four different rule ids: `security.xss` and `quality.escaping` never
land in the same bucket however identically they are worded. So both reached
the reader, stacked on one diff line.

Measured across six benchmark runs (369 comments): 88 same-line cross-agent
pairs, roughly one comment in four.

WHY THE THRESHOLD IS 0.6 AND NOT LOWER. Calibrated against the judge's own
labels, not against how the pairs read. Of those 88 pairs:

    both scored, DIFFERENT goldens ...  0   the case that must never fold
    both scored, ONE golden .........   1   at 0.778
    exactly one scored ..............   5   at 0.048 … 0.500
    neither scored .................. 82

No pair in six runs would collapse two distinct goldens. The real risk is the
asymmetric five, and all of them sit at or below 0.500 — so 0.6 clears every
one with margin. At 0.6 this folds 18 of the 88 pairs, 4.9% of comments; the
23.8% anyone gets by folding every pair regardless of wording is a ceiling,
not a target.

THE ONE THING IT MUST NOT DO is fold two findings from the SAME agent. No
agent has ever written twice on one line in 369 comments, and
`SECOND_DEFECT_PROMPT` explicitly asks for a second finding when the line
carries a second, different defect. A pass that folded it would undo that
instruction on the exact case it exists for.
"""

from __future__ import annotations

from src.review.agents.verifier import (
    SAME_LINE_CROSS_AGENT_JACCARD,
    prefilter,
)
from src.review.models import Finding, FindingSeverity


def f(*, agent, rule, title, line=11, path="app/views/embed.html.erb",
      body="b", reasoning="", severity=FindingSeverity.WARNING, confidence=0.9):
    return Finding(
        file_path=path, line=line, severity=severity, title=title, body=body,
        agent=agent, rule_id=rule, confidence=confidence, reasoning=reasoning,
    )


#: Titles copied verbatim from benchmark comments rather than invented, so
#: the tests fail if the threshold stops folding what it was calibrated to
#: fold. The first pair is the commonest shape by far: architect and quality
#: produce a BYTE-IDENTICAL title on one line, which happened repeatedly
#: across runs and scores 1.000.
DUP_A = "Unawaited async callback in Array.prototype.forEach"   # runG5+runG7, reschedule.ts:125
DUP_B = "Unawaited async callback in Array.prototype.forEach"   # same line, other agent

#: Two REAL findings on one line that need two different fixes. From runG5,
#: layouts/embed.html.erb:11. They score 0.000 — not one shared token.
DIFF_A = "Invalid targetOrigin passed to postMessage"                # quality
DIFF_B = "Reflected XSS via request.referer in inline script tag"    # security


def test_two_agents_one_wording_become_one_comment():
    """The commonest measured shape: two agents, one line, the same title."""
    kept = prefilter([
        f(agent="architect", rule="arch.async-foreach", title=DUP_A),
        f(agent="quality", rule="quality.async", title=DUP_B),
    ]).kept

    assert len(kept) == 1
    assert set(kept[0].agent.split(",")) == {"architect", "quality"}


def test_both_bodies_survive_the_fold():
    """Merged, not dropped: the reader loses a comment, not a claim."""
    kept = prefilter([
        f(agent="architect", rule="arch.async-foreach", title=DUP_A,
          body="the callback returns a promise nobody awaits"),
        f(agent="quality", rule="quality.async", title=DUP_B,
          body="forEach ignores the returned promise"),
    ]).kept

    assert "nobody awaits" in kept[0].body
    assert "ignores the returned promise" in kept[0].body


def test_both_reasoning_sentences_survive_the_fold():
    """`_merge_same_line` keeps the primary's reasoning and drops the rest.
    That is right for one rule_id said twice and wrong here: the sentences
    differ, and five of the calibration pairs had exactly one member matching
    a golden. Keeping one sentence would be keeping a coin flip."""
    kept = prefilter([
        f(agent="architect", rule="arch.async-foreach", title=DUP_A,
          reasoning="forEach on line 125 drops the promise, so callers resume early"),
        f(agent="quality", rule="quality.async", title=DUP_B,
          reasoning="the async callback is never awaited, leaving errors unhandled"),
    ]).kept

    assert "callers resume early" in kept[0].reasoning
    assert "errors unhandled" in kept[0].reasoning


def test_two_different_defects_on_one_line_stay_two_comments():
    """The whole point of the threshold. Both of these are real findings on
    `embed.html.erb:11` and they need two different fixes."""
    kept = prefilter([
        f(agent="quality", rule="quality.api-misuse", title=DIFF_A),
        f(agent="security", rule="security.xss", title=DIFF_B),
    ]).kept

    assert len(kept) == 2


def test_the_same_agent_is_never_folded_with_itself():
    """`SECOND_DEFECT_PROMPT` asks one agent for a second finding when the
    line carries a second defect. Folding those would undo the instruction on
    the case it exists for — even when the titles are worded alike."""
    kept = prefilter([
        f(agent="security", rule="security.xss", title=DUP_A),
        f(agent="security", rule="security.xss-2", title=DUP_B),
    ]).kept

    assert len(kept) == 2


def test_a_different_line_is_a_different_defect():
    kept = prefilter([
        f(agent="architect", rule="arch.async-foreach", line=125, title=DUP_A),
        f(agent="quality", rule="quality.async", line=190, title=DUP_B),
    ]).kept

    assert len(kept) == 2


def test_the_fold_is_counted_as_a_near_duplicate_not_an_exact_one():
    """`dropped_duplicates` is read as "the same (file, line, rule) twice".
    These are different rules, and saying otherwise overstates the match."""
    r = prefilter([
        f(agent="architect", rule="arch.async-foreach", title=DUP_A),
        f(agent="quality", rule="quality.async", title=DUP_B),
    ])

    assert r.dropped_dedup == 0
    assert r.dropped_near_duplicate == 1


def test_the_threshold_stays_above_the_riskiest_measured_pair():
    """Retune direction is down; 0.5 is the floor. The riskiest asymmetric
    pair in six runs — one member matched a golden, the other did not —
    scores exactly 0.500, so anything at or below it folds a scored finding
    on measured evidence rather than on a prior."""
    assert SAME_LINE_CROSS_AGENT_JACCARD > 0.5


def test_the_severest_finding_leads_the_merged_comment():
    kept = prefilter([
        f(agent="quality", rule="quality.async", severity=FindingSeverity.WARNING,
          title=DUP_B),
        f(agent="architect", rule="arch.async-foreach", severity=FindingSeverity.CRITICAL,
          title=DUP_A),
    ]).kept

    assert kept[0].severity is FindingSeverity.CRITICAL


def test_a_line_nobody_doubled_is_untouched():
    kept = prefilter([
        f(agent="security", rule="security.xss", title=DIFF_B),
        f(agent="quality", rule="quality.dead-code", line=40, title="Unreachable branch"),
    ]).kept

    assert len(kept) == 2
    assert all("," not in (k.agent or "") for k in kept)
