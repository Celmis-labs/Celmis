"""One defect copied into four files is one comment, and only then.

The shape, from the 14-PR Martian bench run recorded on 2026-08-22 (TP 24 /
FP 31 / FN 29, judge claude-sonnet-4-5, which reads the comment text and
never sees a severity). On one PR `defect.unintended_form_submit` was posted
three times, in three files, about one and the same missing `type="button"`;
the judge read four comment texts out of them and matched at most one golden,
so the rest were false positives. `sec.cwe-862` did the same three times on
another PR. The in-file pass could not touch either: it buckets by
(file_path, rule_id), and a bucket is one file.

So there is now a second pass bucketed by rule_id alone, judged at
CROSS_FILE_JACCARD — deliberately STRICTER than the in-file threshold,
because two findings in one file are already known to be about one piece of
code and two in different files are not.

The half that matters under F2 (recall weighted 4x precision) is what this
must NOT do: four genuinely different bugs under one rule keep their four
comments, and a proven finding is never folded into a copy in another file.
The tests below are the fixtures for both halves; the recall claim itself was
checked by replaying this pass over that run's recorded findings, where all
24 of the judge's true positives came through still posted.
"""
from __future__ import annotations

import random

from src.review.agents.verifier import (
    CROSS_FILE_JACCARD,
    NEAR_DUPLICATE_JACCARD,
    prefilter,
    title_jaccard,
)
from src.review.models import Finding, FindingSeverity

LOGIN = "apps/web/pages/auth/login.tsx"
DISABLE = "apps/web/components/settings/DisableTwoFactorModal.tsx"
ENABLE = "apps/web/components/settings/EnableTwoFactorModal.tsx"
BACKUP = "apps/web/components/auth/BackupCode.tsx"


def _f(file, line, *, rule, title, body="", severity=FindingSeverity.WARNING,
       agent="architect", confidence=0.8, evidence_kind="inferred") -> Finding:
    return Finding(
        file_path=file, line=line, rule_id=rule, title=title, body=body,
        severity=severity, agent=agent, confidence=confidence,
        evidence_kind=evidence_kind,
    )


ONE_TITLE = 'Button without `type="button"` submits the enclosing form'


def _one_defect_four_files() -> list[Finding]:
    """The case the pass is built for: one rule, one wording, four files."""
    return [
        _f(LOGIN, 105, rule="defect.unintended_form_submit", title=ONE_TITLE,
           body="the login body"),
        _f(DISABLE, 112, rule="defect.unintended_form_submit", title=ONE_TITLE,
           body="the disable body"),
        _f(ENABLE, 283, rule="defect.unintended_form_submit", title=ONE_TITLE,
           body="the enable body", severity=FindingSeverity.ERROR, agent="quality"),
        _f(BACKUP, 41, rule="defect.unintended_form_submit", title=ONE_TITLE,
           body="the backup body"),
    ]


def test_four_files_one_title_become_one_comment_naming_every_location():
    result = prefilter(_one_defect_four_files())

    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 3
    merged = result.kept[0]
    # Anchored at the first file in the stable order, not the first thread home.
    assert (merged.file_path, merged.line) == (BACKUP, 41)
    assert merged.severity is FindingSeverity.ERROR, "the worst any copy claimed"
    assert merged.body.startswith("the backup body"), "the anchor's body, not a pile"
    for file, line in ((LOGIN, 105), (DISABLE, 112), (ENABLE, 283), (BACKUP, 41)):
        assert f"{file}:{line}" in merged.body, (
            f"{file}:{line} is not in the comment the judge will read"
        )
    assert merged.agent == "architect,quality"


def test_a_cross_file_merge_does_not_say_also_at_line_about_another_file():
    """The in-file sentence is "Also at lines 88, 120." — posted on a comment
    that is attached to one file, where a bare line number is unambiguous.
    Reused across files it would be a claim about the wrong file."""
    merged = prefilter(_one_defect_four_files()).kept[0]

    assert "Also at line" not in merged.body
    assert merged.body.rstrip().endswith(".")


def test_the_same_defect_down_one_file_still_reads_the_old_way():
    """The in-file wording is load-bearing and unchanged: same rule, same
    title, one file — line numbers, no paths."""
    result = prefilter([
        _f(LOGIN, 42, rule="defect.unintended_form_submit", title=ONE_TITLE,
           body="first body"),
        _f(LOGIN, 88, rule="defect.unintended_form_submit", title=ONE_TITLE),
        _f(LOGIN, 120, rule="defect.unintended_form_submit", title=ONE_TITLE),
    ])

    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 2
    assert result.kept[0].body == "first body\n\nAlso at lines 88, 120."


def test_four_different_titles_under_one_rule_stay_four_comments():
    """The guard that makes this safe to run on every review. A model that
    found four different bugs writes four different titles, and F2 weights
    recall four times precision — folding these would cost three true
    positives to save three false ones, which is a loss, not a win."""
    result = prefilter([
        _f("packages/core/EventManager.ts", 343, rule="sec.cwe-862",
           title="Attendee can delete any booking by id"),
        _f("packages/core/booking.ts", 74, rule="sec.cwe-862",
           title="Webhook secret compared with =="),
        _f("packages/trpc/router.ts", 12, rule="sec.cwe-862",
           title="Team invite accepts an arbitrary role"),
        _f("apps/web/api/me.ts", 9, rule="sec.cwe-862",
           title="Profile endpoint returns another user's email"),
    ])

    assert len(result.kept) == 4
    assert result.dropped_near_duplicate == 0


def test_the_cross_file_test_is_stricter_than_the_in_file_one():
    """Not an assertion about a number in the source — a pair scored between
    the two thresholds folds when it sits in one file and does not when it
    sits in two."""
    assert CROSS_FILE_JACCARD > NEAR_DUPLICATE_JACCARD

    a = "Missing authorization check on the booking update"
    b = "Missing authorization check on the invite delete"
    score = title_jaccard(a, b)
    assert NEAR_DUPLICATE_JACCARD <= score < CROSS_FILE_JACCARD, (
        f"fixture no longer sits between the thresholds (scored {score})"
    )

    same_file = prefilter([
        _f("a.ts", 10, rule="sec.cwe-862", title=a),
        _f("a.ts", 40, rule="sec.cwe-862", title=b),
    ])
    assert len(same_file.kept) == 1, "in one file this pair is one defect twice"

    two_files = prefilter([
        _f("a.ts", 10, rule="sec.cwe-862", title=a),
        _f("b.ts", 40, rule="sec.cwe-862", title=b),
    ])
    assert len(two_files.kept) == 2, "in two files it is not proof of one defect"


def test_a_proven_finding_is_never_folded_into_a_copy_in_another_file():
    """`src/review/structural.py` builds every match of a rule with
    `title=rule.title`, one constant string per rule — so an identical title
    across four files is a property of the template, not evidence that the
    four are one defect. Four `console.log`s are four lines to delete."""
    rule = dict(rule="structural.console-log",
                title="`console.log` left in code",
                evidence_kind="proven", agent="structural", confidence=1.0)
    result = prefilter([
        _f("apps/web/a.ts", 10, **rule),
        _f("apps/web/b.ts", 22, **rule),
        _f("apps/web/c.ts", 3, **rule),
        _f("apps/web/d.ts", 91, **rule),
    ])

    assert len(result.kept) == 4
    assert result.dropped_near_duplicate == 0
    assert all(f.is_proven for f in result.kept)


def test_a_proven_finding_still_folds_down_one_file_and_stays_proven():
    """The in-file behaviour is untouched: within a file the anchor is not
    in doubt, so holding proven findings out of the CROSS-file pass must not
    also stop them folding down one file. That the merged finding is still
    proven when only a LATER member was is pinned separately, below."""
    cve = dict(rule="sec.cve-GHSA-1", agent="cve", confidence=1.0,
               evidence_kind="proven",
               title="lodash 4.17.15 carries known vulnerability GHSA-1")
    result = prefilter([
        _f("package-lock.json", 4102, **cve),
        _f("package-lock.json", 9000, **cve),
    ])

    assert len(result.kept) == 1
    assert result.kept[0].is_proven


def test_untitled_findings_are_never_folded_across_files_either():
    """Findings that shipped no title stay apart. `title_jaccard` already
    scores an empty title 0.0 against anything; this pins that the cross-file
    bucket does not route around it. It is the common case, not a corner one:
    on the measured run every `quality.*` finding came out with an empty
    title and its whole claim in the body."""
    result = prefilter([
        _f("a.ts", 1, rule="quality.async", title=""),
        _f("b.ts", 2, rule="quality.async", title=""),
        _f("c.ts", 3, rule="quality.async", title=""),
    ])
    assert len(result.kept) == 3
    assert result.dropped_near_duplicate == 0


def test_findings_without_a_rule_id_are_not_all_one_bucket():
    """The cross-file bucket IS the rule_id. An empty one would put every
    rule-less finding in the review into a single bucket, where one title
    match would fold two unrelated files together."""
    title = "Unchecked result of parse"
    result = prefilter([
        _f("a.ts", 1, rule="", title=title),
        _f("b.ts", 2, rule="", title=title),
    ])
    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 0


def test_the_counter_reports_the_in_file_and_cross_file_folds_together():
    """`dropped_near_duplicate` is what the run record shows as "near
    duplicates"; a fold this pass performs and does not count is a comment
    that vanished with no line in the record saying where it went."""
    findings = [
        # one defect twice down one file  -> 1 fold
        _f(LOGIN, 42, rule="defect.unintended_form_submit", title=ONE_TITLE),
        _f(LOGIN, 88, rule="defect.unintended_form_submit", title=ONE_TITLE),
        # ... and once in each of two others -> 2 more folds
        _f(DISABLE, 112, rule="defect.unintended_form_submit", title=ONE_TITLE),
        _f(ENABLE, 283, rule="defect.unintended_form_submit", title=ONE_TITLE),
        # untouched
        _f(BACKUP, 7, rule="quality.naming", title="Component name does not match file"),
    ]
    result = prefilter(findings)

    assert result.dropped_near_duplicate == 3
    assert len(result.kept) == len(findings) - result.dropped_near_duplicate == 2


def test_the_cross_file_fold_does_not_depend_on_the_order_agents_finished_in():
    """The orchestrator collects agent results `as_completed`. Which copy
    becomes the surviving comment — and therefore which body a human reads —
    must not be decided by which thread returned first."""
    findings = _one_defect_four_files() + [
        _f("packages/core/EventManager.ts", 343, rule="sec.cwe-862",
           title="Attendee can delete any booking by id"),
        _f("packages/core/booking.ts", 74, rule="sec.cwe-862",
           title="Webhook secret compared with =="),
        _f(BACKUP, 7, rule="quality.naming", title="Component name does not match file",
           severity=FindingSeverity.INFO),
    ]
    expected = None
    for seed in range(8):
        shuffled = list(findings)
        random.Random(seed).shuffle(shuffled)
        result = prefilter(shuffled)
        got = [
            (f.file_path, f.line, f.rule_id, f.title, f.body, f.severity, f.agent)
            for f in result.kept
        ]
        if expected is None:
            expected = got
        assert got == expected, f"order of arrival changed the review (seed {seed})"
        assert result.dropped_near_duplicate == 3


# ─── The recorded shapes, verbatim from the measured run ──────────────
#
# Titles copied out of `findings_json` for the runs timestamped
# 2026-08-22T11:42..11:45. Kept as fixtures because they are the actual
# wording a real model produced for one defect in several files, which is
# harder to get right than any synthetic pair.


def test_the_recorded_form_submit_copies_fold_as_far_as_the_wording_allows():
    """Three comments, one missing `type="button"`, three files. Two of the
    three titles agree at 0.615 and fold; the third describes the same defect
    in words that overlap the others at 0.35-0.38, and a threshold low enough
    to catch it would be looser than the in-file one. So this run loses one
    of the four false positives here, not three — the measured number, not
    the hoped-for one."""
    result = prefilter([
        _f(LOGIN, 105, rule="defect.unintended_form_submit",
           title='TwoFactorFooter buttons submit login form when clicked due to '
                 'missing `type="button"`'),
        _f(DISABLE, 112, rule="defect.unintended_form_submit",
           title='Lost access button submits 2FA disable form due to missing '
                 '`type="button"`'),
        _f(ENABLE, 283, rule="defect.unintended_form_submit",
           title='Download button submits 2FA enable form due to missing '
                 '`type="button"`'),
    ])

    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 1
    merged = next(f for f in result.kept if f.file_path == DISABLE)
    assert f"{DISABLE}:112" in merged.body
    assert f"{ENABLE}:283" in merged.body


def test_the_recorded_cwe_862_copies_fold_across_the_two_files():
    """Same run, second PR: one authorisation gap reported at three call
    sites. The judge counted all three as false positives, so nothing is at
    risk here — and the two that fold are the two whose titles agree at
    0.636."""
    EM = "packages/core/EventManager.ts"
    CANCEL = "packages/features/bookings/lib/handleCancelBooking.ts"
    result = prefilter([
        _f(EM, 343, rule="sec.cwe-862",
           title="Broken Access Control: Unchecked credential retrieval by ID "
                 "in EventManager"),
        _f(EM, 516, rule="sec.cwe-862",
           title="Broken Access Control: Unchecked credential retrieval when "
                 "updating calendar events"),
        _f(CANCEL, 432, rule="sec.cwe-862",
           title="Broken Access Control: Unchecked credential retrieval in "
                 "handleCancelBooking"),
    ])

    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 1
    merged = next(f for f in result.kept if f.line == 343)
    assert f"{EM}:343" in merged.body
    assert f"{CANCEL}:432" in merged.body


def test_the_recorded_stored_xss_pair_is_two_defects_and_stays_two():
    """The pair this pass must never touch: one rule (`sec.cwe-79`), two
    files, two genuinely different defects — the judge counted the second a
    true positive and the first a false one. They score 0.083 on the title,
    the highest any different-defect pair reached in that run."""
    result = prefilter([
        _f("app/models/post.rb", 133, rule="sec.cwe-79",
           title="HTML Sanitization Bypass Leading to Stored XSS"),
        _f("app/models/topic_embed.rb", 13, rule="sec.cwe-79",
           title="Unescaped URL Interpolation in HTML Content"),
    ])

    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 0


def test_equal_severity_comments_come_out_in_file_and_line_order():
    """The providers post `findings[:max_inline_comments]`, so position is
    what decides which comment a reader never sees. The severity sort is
    stable, so ties keep the order this pass emitted — and until it re-sorted,
    that order was the per-(file, rule) bucket order: one file's rule-R
    findings, then all of its rule-S ones, with the line numbers interleaved.
    """
    result = prefilter([
        _f("a.ts", 1, rule="quality.R", title="first"),
        _f("a.ts", 3, rule="quality.S", title="second"),
        _f("a.ts", 5, rule="quality.R", title="third"),
    ])

    assert [f.line for f in result.kept] == [1, 3, 5]


def test_a_proven_member_makes_the_merged_finding_proven_even_when_it_is_not_first():
    """The anchor of an in-file merge is the lowest line, which need not be
    the member that was PROVEN. Taking the anchor's evidence_kind would turn
    "an ast-grep rule matched, and a model said the same thing higher up the
    file" into an inferred finding — and inferred is exactly what the LLM
    veto in `llm_pass` is allowed to delete. A lookup agreed with by a model
    is still a lookup."""
    title = "Mutable default argument shared across calls"
    result = prefilter([
        _f("app/api.py", 10, rule="structural.mutable-default", title=title,
           agent="architect", evidence_kind="inferred"),
        _f("app/api.py", 88, rule="structural.mutable-default", title=title,
           agent="structural", evidence_kind="proven", confidence=1.0),
    ])

    assert len(result.kept) == 1
    assert result.kept[0].line == 10, "still anchored at the first occurrence"
    assert result.kept[0].is_proven


def test_a_chain_of_similar_titles_does_not_pull_two_files_together():
    """Every fold has to be justified by ONE comparison a reader can repeat:
    a finding joins the cluster whose FIRST member its title resembles. Under
    single-link ("resembles ANY member") a chain would merge the two ends,
    and here the ends score 0.556 — below CROSS_FILE_JACCARD — while each
    link scores 0.75. The three titles are built to that shape on purpose."""
    a = "Unvalidated user id reaches the delete query"
    b = "Unvalidated user id reaches the update query"
    c = "Unvalidated user id reaches the update path"
    assert title_jaccard(a, b) >= CROSS_FILE_JACCARD
    assert title_jaccard(b, c) >= CROSS_FILE_JACCARD
    assert title_jaccard(a, c) < CROSS_FILE_JACCARD

    result = prefilter([
        _f("a.ts", 1, rule="sec.cwe-862", title=a),
        _f("b.ts", 2, rule="sec.cwe-862", title=b),
        _f("c.ts", 3, rule="sec.cwe-862", title=c),
    ])

    assert len(result.kept) == 2, "the chain's ends are not one defect"
    assert result.dropped_near_duplicate == 1


def test_a_defect_twice_in_one_file_and_once_in_another_names_all_three():
    """The two clustering passes compose, and the merged comment has to hold
    up when they do. Merging after each pass produced a body with two
    sentences in it — "Also at line 50." from the in-file pass and a list of
    `path:line` from the cross-file one — where line 50 was the only location
    named without the file it is in. One merge over every member instead."""
    title = "Button without type submits the enclosing form"
    result = prefilter([
        _f("apps/a.tsx", 10, rule="defect.unintended_form_submit", title=title,
           body="the a body"),
        _f("apps/a.tsx", 50, rule="defect.unintended_form_submit", title=title),
        _f("apps/b.tsx", 5, rule="defect.unintended_form_submit", title=title),
    ])

    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 2
    body = result.kept[0].body
    assert body == (
        "the a body\n\nThe same defect is at 3 locations: "
        "apps/a.tsx:10, apps/a.tsx:50, apps/b.tsx:5."
    )


def test_two_agents_on_one_line_are_quoted_in_the_same_order_every_run():
    """The exact-dedup merge keeps both bodies with their provenance, in the
    order it received them — so the stable sort at the top of `prefilter` is
    what stops `as_completed` deciding which agent a reader is quoted first.
    Pinned here because the cross-file pass re-sorts what IT merges, which
    hides the top-level sort from every other assertion in this file."""
    expected = None
    for seed in range(8):
        findings = [
            _f("a.ts", 10, rule="quality.dup", title="Null deref",
               body="reached with a null user", agent="architect"),
            _f("a.ts", 10, rule="quality.dup", title="Null deref",
               body="the guard above returns early", agent="quality"),
        ]
        random.Random(seed).shuffle(findings)
        result = prefilter(findings)

        assert len(result.kept) == 1
        assert result.dropped_dedup == 1
        if expected is None:
            expected = result.kept[0].body
        assert result.kept[0].body == expected, f"arrival order swapped them (seed {seed})"
    assert expected == (
        "[architect] reached with a null user\n\n"
        "[quality] the guard above returns early"
    )


def test_the_locations_are_listed_in_file_and_line_order():
    """Two same-file groups can both join one cross-file cluster while being
    too far apart to have folded with each other in step 3 — 0.615 and 0.667
    against the representative, 0.4 against one another — and then their
    lines arrive interleaved. The list a reader has to scan is sorted, not
    grouped by whichever wording the model used."""
    rep = "Broken access control unchecked credential retrieval by id in EventManager"
    one = "Broken access control unchecked credential retrieval by id when updating calendars"
    other = "Control unchecked credential retrieval by id in EventManager cancel path"
    assert title_jaccard(rep, one) >= CROSS_FILE_JACCARD
    assert title_jaccard(rep, other) >= CROSS_FILE_JACCARD
    assert title_jaccard(one, other) < NEAR_DUPLICATE_JACCARD, "not one group in step 3"

    result = prefilter([
        _f("aa.ts", 5, rule="sec.cwe-862", title=rep),
        _f("bb.ts", 10, rule="sec.cwe-862", title=one),
        _f("bb.ts", 60, rule="sec.cwe-862", title=one),
        _f("bb.ts", 20, rule="sec.cwe-862", title=other),
        _f("bb.ts", 70, rule="sec.cwe-862", title=other),
    ])

    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 4
    assert result.kept[0].body == (
        "The same defect is at 5 locations: "
        "aa.ts:5, bb.ts:10, bb.ts:20, bb.ts:60, bb.ts:70."
    )


def test_a_weak_sibling_does_not_drag_a_strong_finding_under_the_floor():
    """The fold runs at step 3b and the confidence floor at step 4, so the
    merged finding's confidence decides whether the WHOLE cluster is posted.

    Taking the cluster's lowest confidence would delete the anchor's own
    finding because a copy of it in another file was written less certainly —
    a recall loss invented by the merge, on the metric this pass is measured
    against (F2 on the 14-PR Martian bench, recall weighted 4x precision).
    Nothing pinned that before: swapping `max` for `min` in `_merge_cluster`
    left every other test in this file and its neighbour green.
    """
    result = prefilter([
        _f("aa.ts", 5, rule="sec.cwe-862", title=ONE_TITLE, body="the sure one",
           confidence=0.9),
        _f("bb.ts", 9, rule="sec.cwe-862", title=ONE_TITLE, body="the unsure one",
           confidence=0.3),
    ])

    assert len(result.kept) == 1, "the strong finding was dropped with its weak copy"
    assert result.kept[0].confidence == 0.9
    assert result.kept[0].body.startswith("the sure one")
    assert result.dropped_low_confidence == 0
    assert result.dropped_near_duplicate == 1


def test_a_pair_exactly_at_the_threshold_folds():
    """CROSS_FILE_JACCARD is a floor the comparison includes, not a number to
    clear. Three shared tokens out of five is 0.6 exactly, and `>` instead of
    `>=` in `_fold_across_files` changed no other test in this file."""
    a, b = "alpha beta gamma delta", "alpha beta gamma epsilon"
    assert title_jaccard(a, b) == CROSS_FILE_JACCARD

    result = prefilter([
        _f("aa.ts", 5, rule="defect.x", title=a, body="the a body"),
        _f("bb.ts", 7, rule="defect.x", title=b, body="the b body"),
    ])

    assert len(result.kept) == 1
    assert result.kept[0].body == (
        "the a body\n\nThe same defect is at 2 locations: aa.ts:5, bb.ts:7."
    )
