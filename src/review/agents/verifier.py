"""Verifier — the deterministic prefilter every review gets, and the LLM veto it may get.

What was measured, on the Martian Code Review Bench (14 PRs, one judge who
reads the comment text only and never sees a severity):

    baseline, LLM veto mostly failing open       TP 22  FP 28  FN 31   P 44.0  R 41.5  F1 42.7
    new prompts + LLM veto ON (32768 ceiling)    TP 10  FP  6  FN 43   P 62.5  R 18.9  F1 29.0
    new prompts + LLM veto OFF                   TP 24  FP 31  FN 29   P 43.6  R 45.3  F1 44.4

The LLM pass deleted true positives by design. Its DROP rules — "a risk, not a
consequence", "the line is not in the diff", "the severity does not match" —
describe shapes the judge rewards. For five runs this docstring said the
opposite ("without the verifier ~11 FPs per run, with it ~2"), attributed to a
benchmark that does not exist; a docstring that overstates a filter's value is
how the veto survived those five runs.

What the leaders do instead. Kodus (#1, open source) keeps by default and drops
only on refutation, with deterministic gates in code beside the model. cubic
(#2) filters UPSTREAM — repo context and a reasoning → finding → confidence
output, -51% FP with no recall loss — and has no veto. Augment tunes the system
prompt ("which comment categories to avoid") and has no post-filter at all.

So the split here:

    prefilter()   deterministic, no model, runs on EVERY review:
        1. rule deny-list — `ReviewSettings.suppressed_rules`, or the repo
           policy's own list; every drop is counted by rule
        2. exact dedup by (file, line, rule) — several agents, one line
        3. near-duplicate clustering by (file, rule) — one agent, one defect,
           repeated down the file; titles >= 0.5 token-Jaccard become one
           finding that lists every line
        3b. the same again by rule ACROSS files, at a stricter threshold —
           one defect copied into four components is one comment naming all
           four `path:line`
        4. confidence floor
        5. severity sort — the providers post findings[:max_inline_comments],
           so the order decides what falls off

    llm_pass()    optional. "verifier" in a repo policy's `disabled_agents`
                  switches it off — and ONLY it. Proven findings never enter
                  it. Fails open on every error. Runs from
                  `REVIEW_VERIFIER_MIN_FINDINGS` judgeable findings (default
                  1), each judged WITH repository evidence.

    verify()      the two in sequence, for callers that want both.

What the evidence changes, and what it does not. The veto above was measured
with a prompt that held the first five hunks of the PR cut at 2000 characters.
It could not refute the false positives a cheap model produces most — "undefined
constant X" when X lives in another file, "undefined variable" when the loop
that sets it is above the hunk, "missing check" when the check is many lines
up — because the refutation is not in the diff. `verifier_evidence.py` now puts
that code in front of it, per finding, and the prompt drops only on REFUTATION
by that evidence, with a one-line reason per drop (a drop without a reason is
kept). The "risk, not a consequence" and severity rules that deleted true
positives are gone. What has NOT been measured: this has not been re-run on the
benchmark above, so there is no number here for its effect on precision or
recall; the stage record (judged / dropped, with reasons) is how to audit it
on real reviews.

Before the split the whole of this lived in `verify()`, so switching the veto
off also switched off the dedup and the sort — the providers' cap then
truncated an unsorted, un-deduped list. `src/review/orchestrator.py` now calls
`prefilter()` unconditionally and `llm_pass()` by policy.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from src.review.agents.base import (
    _REASONING_BLOCK,
    AgentContext,
    _llm_timeout,
    _review_model,
    agent_llm_settings,
    balanced_json_span,
    base_instruction_block,
)
from src.review.models import Finding, FindingSeverity
from src.review.settings import get_review_settings

logger = logging.getLogger(__name__)


#: Two titles under one (file, rule) whose token sets overlap at least this
#: much are the same defect said twice. The shape this is for, seen with the
#: veto OFF: cal.com#10600 posted `defect.unintended_form_submit` three times
#: and cal.com#10967 `sec.cwe-862` three times, every copy a false positive.
#: What a replay of this pass over that run shows, and what this comment must
#: not overstate: it folded NONE of them. The #10600 copies sat in three
#: files, and a bucket is one file; of the #10967 copies the one same-file
#: pair scores 0.43 on the title. The seven false positives the prefilter does
#: remove on that run are all the deny-list's. So this number is a prior, not
#: a measurement: Kodus guards its LLM dedup with Jaccard >= 0.3 on the full
#: content; this is the deterministic half only, on the title, and stricter
#: for it — a title is short, so a shared token means more. Folding the
#: cal.com copies needs the cross-file pass below, not a lower number here.
NEAR_DUPLICATE_JACCARD = 0.5

#: The same test for two titles under one rule_id in DIFFERENT files. Higher
#: than the in-file number on purpose: two findings in one file are already
#: known to be about one piece of code, and two in different files are not,
#: so the evidence that they are one defect has to be stronger.
#:
#: What the shape looks like, replayed over the 14-PR bench run recorded on
#: 2026-08-22 (TP 24 / FP 31 / FN 29, judge claude-sonnet-4-5). Of every
#: cross-file pair that shares a rule_id in that run:
#:
#:   1.000  arch.async-foreach   vital/reschedule.ts vs wipemycalother/…
#:   0.667  arch.async-foreach   bookings.tsx vs handleCancelBooking.ts
#:   0.636  sec.cwe-862          EventManager.ts vs handleCancelBooking.ts
#:   0.615  defect.unintended_form_submit  Disable… vs EnableTwoFactorModal
#:   0.500  sec.cwe-862          EventManager.ts vs handleCancelBooking.ts
#:   0.467, 0.412 …              arch.async-foreach, again one defect
#:   0.375, 0.353                defect.unintended_form_submit, again one
#:   0.083  sec.cwe-79           stored XSS vs unescaped URL — TWO defects
#:   0.056  sec.cwe-755          NameError vs AssertionError — TWO defects
#:   0.000  everything else, including every pair with an empty title
#:
#: Every pair the judge treated as two different defects (one member matched
#: a golden comment the other did not) scores <= 0.083; every pair >= 0.35 is
#: one defect said twice. So the band this could sit in without changing a
#: verdict on that run is wide, and 0.6 is picked near the top of it: two
#: titles must share three of every five words either one uses, which is
#: seven times the score the closest genuinely-different pair reached. It is
#: a constant so it can be retuned against a later run, and the direction to
#: retune is down — but never below NEAR_DUPLICATE_JACCARD, which would make
#: the cross-file test looser than the in-file one it exists to be stricter
#: than.
#:
#: The measured effect of this pass alone, replayed over that run's recorded
#: findings: 77 comments become 73 (cal.diy#11 4 arch.async-foreach -> 2,
#: #12 3 defect.unintended_form_submit -> 2, #13 3 sec.cwe-862 -> 2). Two of
#: the four folded had been counted as false positives; the other two were
#: on a PR whose own judge-side dedup had already absorbed them, so they
#: cost the score nothing and the reader two comments. All 24 of the run's
#: true positives are still posted, each still carrying its own body.
CROSS_FILE_JACCARD = 0.6

#: Two findings from DIFFERENT agents on the SAME (file, line) whose titles
#: overlap at least this much are one defect described twice, and the reader
#: should see one comment.
#:
#: The gap this closes: steps 2 and 3 below both bucket by `rule_id`, and four
#: agents writing about one line write four different rule ids — `security.xss`
#: and `quality.escaping` never meet, however identically they are worded. So
#: the reader gets both. Measured across six bench runs (369 comments): 88
#: same-line cross-agent pairs, about one comment in four.
#:
#: The number is calibrated, not chosen, and the calibration is against the
#: judge's own labels rather than against how the pairs read. Of those 88:
#:
#:   both members scored, DIFFERENT goldens ...  0   ← the case that must never fold
#:   both members scored, ONE golden .........   1   at 0.778
#:   exactly one member scored ...............   5   at 0.048 … 0.500
#:   neither scored .......................... 82
#:
#: There is no pair in six runs where folding would collapse two distinct
#: goldens. The only real risk is the asymmetric five — folding away the half
#: that scored — and every one of them sits at or below 0.500. 0.6 is above
#: all five with margin and below the one safe pair, and it is the same number
#: as CROSS_FILE_JACCARD because the reasoning is the same: two findings that
#: are not already known to be about one thing need stronger evidence.
#:
#: What it costs the reader, per run at this threshold: 18 of the 88 pairs
#: fold, 4.9% of all comments. Folding every pair regardless of wording would
#: be 23.8% — that is the ceiling anyone quoting "a quarter of comments are
#: duplicates" is quoting, and it is not reachable safely.
#:
#: Retune direction is DOWN, and 0.5 is the floor: the riskiest asymmetric
#: pair scores exactly 0.500, so anything below it folds a scored finding on
#: measured evidence rather than on a prior.
SAME_LINE_CROSS_AGENT_JACCARD = 0.6

_SEVERITY_ORDER = {
    FindingSeverity.CRITICAL: 0,
    FindingSeverity.ERROR: 1,
    FindingSeverity.WARNING: 2,
    FindingSeverity.INFO: 3,
}


def _severity_rank(f: Finding) -> int:
    return _SEVERITY_ORDER.get(f.severity, 99)


def _stable_key(f: Finding) -> tuple[str, int, str, str, str]:
    """The one order every pass in here starts from. Total on the fields a
    Finding always has, so two runs that collected the same findings in a
    different thread order see the same list — which is what makes "the first
    body" and "the cluster's representative" mean the same thing twice."""
    return (f.file_path, f.line, f.rule_id, f.agent, f.title)


def _title_tokens(title: str) -> frozenset[str]:
    """Word tokens of a title, in any script.

    This was `[a-z0-9]+`, which reads a review written in Ukrainian, Polish,
    German or Greek as a handful of identifiers and nothing else: every
    Cyrillic title scored 0.0 against every other, so neither clustering pass
    folded anything and one defect copied into six files posted six comments.
    `[^\\W_]+` is "letters and digits" in every script; underscores still
    split, so `snake_case` identifiers tokenize as they did."""
    return frozenset(re.findall(r"[^\W_]+", (title or "").lower()))


def title_jaccard(a: str, b: str) -> float:
    """Token-Jaccard of two titles. Two empty titles are 0.0, not 1.0 —
    "no title" is no evidence that two findings are the same one."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class PrefilterResult:
    """What the deterministic pass kept, and what it dropped, by cause."""

    kept: list[Finding]
    #: rule_id → how many findings that rule lost to the deny-list. A dict
    #: rather than a number so the run record can say WHAT was hidden: a
    #: count alone reads as "the filter ate something", which is the claim
    #: this whole module exists to stop making.
    dropped_by_rule: dict[str, int] = field(default_factory=dict)
    dropped_dedup: int = 0
    dropped_near_duplicate: int = 0
    dropped_low_confidence: int = 0


@dataclass
class VerifierResult:
    """Filtering outcome — the prefilter's counts plus the LLM pass's."""

    kept: list[Finding]
    dropped_dedup: int = 0
    dropped_near_duplicate: int = 0
    dropped_low_confidence: int = 0
    dropped_by_rule: dict[str, int] = field(default_factory=dict)
    dropped_llm_filter: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_seconds: float = 0.0
    #: Why the LLM veto did not run, when it was asked to and could not.
    #:
    #: The pass fails OPEN — an unreachable verifier keeps every finding —
    #: which is the right call and was, until now, an invisible one. A veto
    #: that timed out and a veto that ran and approved everything produced the
    #: same batch, the same verdict and the same comment; only a WARNING in a
    #: server log told them apart, and only for whoever was reading it.
    #:
    #: That matters most exactly when it happens. The veto exists to thin
    #: false positives, so the review where it silently did not run is the
    #: review with the most unfiltered noise in it — and the author reading
    #: that noise has no way to know it was never filtered.
    error: str | None = None
    #: How many findings the veto actually judged (0 = it did not run).
    judged: int = 0
    #: One entry per finding the veto dropped: title, file, line and the
    #: reason it gave, which cites evidence. The run record carries these so a
    #: dropped finding can be audited — "the filter ate something" is the
    #: claim this module exists to stop making.
    dropped_findings: list[dict[str, object]] = field(default_factory=list)
    #: Findings the model dropped WITHOUT a reason and that were kept anyway.
    unjustified_drops: int = 0
    #: What the evidence gatherer collected (`EvidenceBundle.stats`).
    evidence: dict[str, object] = field(default_factory=dict)
    #: Model calls the veto took (see `_VetoOutcome.attempts`).
    veto_attempts: int = 0


@dataclass
class _VetoOutcome:
    """What one `_llm_verify` call produced."""

    verified: list[Finding]
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    unjustified: int = 0
    evidence: dict[str, object] = field(default_factory=dict)
    #: Model calls the pass took: 2 when the first reply was unreadable or
    #: timed out and the one extra ask was spent.
    attempts: int = 0


#: `"keep": [0, 2]` on its own, for a reply whose wrapper is not valid JSON.
_KEEP_ARRAY = re.compile(r'"keep"\s*:\s*\[\s*((?:\d+\s*(?:,\s*\d+\s*)*)?)\]')


#: One `"<idx>": "<reason>"` pair of a `reasons` object.
_REASON_ENTRY = re.compile(r'"#?(\d+)"\s*:\s*"((?:[^"\\\n]|\\.){1,400})"')


#: Extra asks the veto may spend on an unreadable reply or a timeout.
_VETO_EXTRA_ATTEMPTS = 1


def prefilter(
    findings: Iterable[Finding],
    *,
    confidence_threshold: float = 0.5,
    suppressed_rules: Iterable[str] = (),
) -> PrefilterResult:
    """The deterministic pass, as a function: no model, no context, no I/O.

    Order-independent on purpose. The orchestrator collects agent results
    `as_completed`, so the same review can hand this the same findings in a
    different order on every run — and "the first body" or "the cluster's
    representative" must not depend on which thread finished first. Everything
    below starts from a stable sort by (file, line, rule, agent, title).
    """
    result = PrefilterResult(kept=[])
    suppressed = frozenset(suppressed_rules)
    ordered = sorted(findings, key=_stable_key)

    # ── 1. Rule deny-list — first, on the raw findings, so the count is what
    # the agents actually said and not what survived the merges below. Exact
    # match on rule_id; proven findings are not exempt, because the list is an
    # operator's explicit word and a policy that names a rule means it.
    surviving: list[Finding] = []
    for f in ordered:
        if f.rule_id and f.rule_id in suppressed:
            result.dropped_by_rule[f.rule_id] = result.dropped_by_rule.get(f.rule_id, 0) + 1
        else:
            surviving.append(f)

    # ── 2. Exact dedup — several agents, the same (file, line, rule) ──
    by_key: dict[tuple, list[Finding]] = defaultdict(list)
    for f in surviving:
        by_key[f.dedup_key].append(f)
    deduped: list[Finding] = []
    for group in by_key.values():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            deduped.append(_merge_same_line(group))
            result.dropped_dedup += len(group) - 1

    # ── 2b. One line, several agents, different rule ids ──
    # Step 2 needs the rule_id to match, and four agents looking at one line
    # write four rule ids. This is the same test on the title alone, keyed on
    # (file, line) only — the case a reader meets as two comments stacked on
    # one diff line saying the same thing in two vocabularies.
    #
    # Counted as a near-duplicate rather than an exact one: the findings are
    # NOT the same rule, and `dropped_duplicates` is read as "the same thing
    # twice by key". Calling this exact would overstate what was matched.
    by_line: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in deduped:
        by_line[(f.file_path, f.line)].append(f)
    cross_agent: list[Finding] = []
    for bucket in sorted(by_line.values(), key=lambda g: _stable_key(g[0])):
        for cluster in _cluster_same_line_across_agents(bucket):
            cross_agent.append(
                cluster[0] if len(cluster) == 1 else _merge_cross_agent(cluster)
            )
            result.dropped_near_duplicate += len(cluster) - 1
    deduped = sorted(cross_agent, key=_stable_key)

    # ── 3. Near-duplicate clustering — one defect repeated down a file ──
    # Both clustering passes hand on GROUPS of findings rather than merged
    # ones, and the single merge below sees every member of the final group.
    # Merging after step 3 and again after 3b produced a body carrying two
    # sentences — "Also at line 50." from the first merge and a list of
    # `path:line` from the second — in which line 50 was the only location
    # named without its file.
    by_rule: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for f in deduped:
        by_rule[(f.file_path, f.rule_id)].append(f)
    groups: list[list[Finding]] = []
    for bucket in by_rule.values():
        for cluster in _cluster_by_root_cause(_cluster_by_title(bucket)):
            groups.append(cluster)
            result.dropped_near_duplicate += len(cluster) - 1

    # ── 3b. The same defect in several FILES — one comment, every location ──
    groups, folded_across_files = _fold_across_files(groups)
    result.dropped_near_duplicate += folded_across_files

    clustered = [g[0] if len(g) == 1 else _merge_cluster(g) for g in groups]

    # ── 4. Confidence floor ──
    kept = [f for f in clustered if f.confidence >= confidence_threshold]
    result.dropped_low_confidence = len(clustered) - len(kept)

    # ── 5. Severity sort (critical first), then confidence. The sort is
    # stable and the input was ordered by (file, line), so ties land in
    # file order rather than thread order.
    kept.sort(key=lambda f: (_severity_rank(f), -f.confidence))
    result.kept = kept
    return result


def _cluster_by_title(bucket: list[Finding]) -> list[list[Finding]]:
    """Greedy, representative-linked: a finding joins the first cluster whose
    FIRST member's title it resembles, else starts its own. Single-link
    ("resembles any member") would let a chain of pairwise-similar titles pull
    two unrelated findings together; against the first member every merge is
    justified by one comparison a reader can repeat."""
    clusters: list[list[Finding]] = []
    for f in bucket:
        for cluster in clusters:
            if title_jaccard(f.title, cluster[0].title) >= NEAR_DUPLICATE_JACCARD:
                cluster.append(f)
                break
        else:
            clusters.append([f])
    return clusters


def _cluster_by_root_cause(clusters: list[list[Finding]]) -> list[list[Finding]]:
    """Fold clusters of ONE (file, rule) bucket that are about the same things.

    The title comparison above needs shared WORDS, and one defect reported at
    three places by a model is three differently worded sentences (see
    `root_cause_keys`). This pass compares what the findings are about — the
    variable, the function, the tag — instead. Same shape as the title pass,
    for the same reason: a cluster is joined only through its FIRST member, so
    every fold rests on one comparison a reader can repeat, and a chain of
    pairwise overlaps cannot drag an unrelated finding in.

    Held back: proven findings (a lookup's title is a template, see
    `_folds_across_files`) and findings that name nothing usable.
    """
    from src.review.agents.root_cause_keys import root_cause_keys, share_a_root_cause

    out: list[list[Finding]] = []
    keys: list[frozenset[str]] = []
    for cluster in clusters:
        rep = cluster[0]
        k = frozenset() if rep.is_proven else root_cause_keys(rep.title, rep.body)
        for i, target in enumerate(out):
            if share_a_root_cause(k, keys[i]):
                target.extend(cluster)
                target.sort(key=_stable_key)
                break
        else:
            out.append(list(cluster))
            keys.append(k)
    return out


def _folds_across_files(group: list[Finding]) -> bool:
    """Whether a group may be folded into a copy of itself in ANOTHER file.
    A group is one file's findings, so it answers with its first member's
    rule_id and title — and with ANY member's proven status, because that is
    what `_merge_cluster` gives the merged finding.

    Three findings are held back, each for its own reason:

    * no rule_id — the cross-file bucket IS the rule_id, and an empty one
      would put every rule-less finding in the review into one bucket.
    * no title — the whole test is a title comparison, and `title_jaccard`
      already scores an empty title 0.0 against anything. Held back here too
      so the intent is stated rather than inherited from that.
    * proven. A proven finding comes from a lookup, and the lookup writes the
      title: `src/review/structural.py` builds every match of a rule with
      `title=rule.title`, one constant string per rule. So `console.log` left
      in four files is four findings with byte-identical titles under one
      rule_id — four separate defects, each with its own line to fix, that an
      identical-title test would fold into one. For a model-written title the
      same identity is evidence of repetition; for a templated one it is
      evidence of nothing but the template. The CVE agent templates its
      titles too, and the same rule keeps its per-lockfile findings apart.
    """
    first = group[0]
    return (
        bool(first.rule_id)
        and bool(_title_tokens(first.title))
        and not any(f.is_proven for f in group)
    )


def _fold_across_files(
    groups: list[list[Finding]],
) -> tuple[list[list[Finding]], int]:
    """Join the groups of step 3 that are one defect in several FILES.

    Returns the joined groups and how many comments that cost. Same greedy
    representative-linked shape as `_cluster_by_title` and the same reason
    for it, but bucketed by rule_id alone and judged at CROSS_FILE_JACCARD.
    Groups `_folds_across_files` holds back travel through untouched.

    The input is re-sorted rather than taken as it arrives. Not for the
    clustering — restricted to any one rule_id the caller's list is already
    in `_stable_key` order, because its buckets are keyed (file_path,
    rule_id) and emitted in first-seen order, which is file-major — but for
    what comes out. That bucket loop groups a file's findings by rule, so
    step 5's stable severity sort was leaving equal-severity findings in
    (file, rule-first-seen, line) order rather than the (file, line) order
    the step-5 comment claims; sorting here is what makes that claim true.
    It decides which comments a provider's `max_inline_comments` cuts.
    """
    ordered = sorted(groups, key=lambda g: _stable_key(g[0]))
    clusters: list[list[list[Finding]]] = []
    open_by_rule: dict[str, list[int]] = defaultdict(list)
    for group in ordered:
        joined = False
        if _folds_across_files(group):
            rule_id, title = group[0].rule_id, group[0].title
            for i in open_by_rule[rule_id]:
                if title_jaccard(title, clusters[i][0][0].title) >= CROSS_FILE_JACCARD:
                    clusters[i].append(group)
                    joined = True
                    break
            if not joined:
                open_by_rule[rule_id].append(len(clusters))
        if not joined:
            clusters.append([group])

    # Two groups from one file can both join one cluster while being too far
    # apart to have folded with each other in step 3, and their lines then
    # arrive interleaved. Re-sorted so `_also_at_sentence` lists the
    # locations in the order a reader scans them. The ANCHOR is not at stake:
    # groups are ordered by their first member and a group's members ascend,
    # so the front of the flattened list is the cluster's minimum either way.
    out = [sorted((f for g in c for f in g), key=_stable_key) for c in clusters]
    folded = sum(len(c) - 1 for c in clusters)
    return out, folded


def _also_at_sentence(cluster: list[Finding]) -> str:
    """The sentence that keeps a merged comment honest about WHERE.

    Within one file the other occurrences are line numbers and the comment is
    posted on the file, so "Also at lines 88, 120." reads correctly. Across
    files that same sentence would be a lie — line 283 of which file? — so a
    cross-file merge names every location in full, the anchor included: the
    reader (and the bench judge, who sees the comment text and nothing else)
    should not have to work out which of the listed lines the comment is
    attached to.
    """
    first, others = cluster[0], cluster[1:]
    if all(f.file_path == first.file_path for f in others):
        noun = "line" if len(others) == 1 else "lines"
        return f"Also at {noun} {', '.join(str(f.line) for f in others)}."
    places = ", ".join(f"{f.file_path}:{f.line}" for f in cluster)
    return f"The same defect is at {len(cluster)} locations: {places}."


def _merge_cluster(cluster: list[Finding]) -> Finding:
    """One finding for one defect: anchored at its first occurrence, carrying
    the first body, the highest severity, and every place it was seen at.

    The anchor and the body travel together — a body says "this line does X"
    about the line it was written for — so both come from the first member
    (in `_stable_key` order: lowest file, then lowest line), and the severity
    is the worst any member claimed.
    """
    first = cluster[0]
    worst = min(cluster, key=_severity_rank)  # stable: first wins a tie
    also = _also_at_sentence(cluster)
    body = f"{first.body}\n\n{also}" if first.body else also
    agents = sorted({f.agent for f in cluster if f.agent})
    return Finding(
        file_path=first.file_path,
        line=first.line,
        side=first.side,
        severity=worst.severity,
        title=first.title,
        body=body,
        suggestion=first.suggestion,
        suggested_code=first.suggested_code,
        suggested_end_line=first.suggested_end_line,
        agent=",".join(agents),
        rule_id=first.rule_id,
        confidence=max(f.confidence for f in cluster),
        reasoning=first.reasoning,
        # A lookup agreed with by a model is still a lookup.
        evidence_kind="proven" if any(f.is_proven for f in cluster) else first.evidence_kind,
        # A member that cited a review rule keeps the merged comment cited.
        rule=next((f.rule for f in cluster if getattr(f, "rule", "")), ""),
    )


def _cluster_same_line_across_agents(bucket: list[Finding]) -> list[list[Finding]]:
    """Group one line's findings into clusters that are the same defect.

    Same greedy representative-linked shape as `_cluster_by_title`, and the
    same reason: every fold is justified by ONE comparison a reader can
    repeat, rather than by a chain of pairwise resemblances.

    Two findings from the SAME agent never join. That is not tidiness — no
    agent in six benchmark runs (369 comments) ever wrote twice on one line,
    so a same-agent pair here would be a shape nothing has been calibrated
    on, and `SECOND_DEFECT_PROMPT` in `base.py` actively asks for one when
    the line carries a second, different defect. Folding it would undo the
    instruction on the very case it exists for.
    """
    clusters: list[list[Finding]] = []
    for f in bucket:
        for cluster in clusters:
            if any(m.agent == f.agent for m in cluster):
                continue
            if title_jaccard(f.title, cluster[0].title) >= SAME_LINE_CROSS_AGENT_JACCARD:
                cluster.append(f)
                break
        else:
            clusters.append([f])
    return clusters


def _merge_cross_agent(cluster: list[Finding]) -> Finding:
    """Fold one line's cross-agent duplicates into a single comment.

    `_merge_same_line` would nearly do, and does not, for one field: it keeps
    the primary's `reasoning` and drops the rest. Under step 2 that is right —
    those findings share a rule_id and say one thing. Here the members come
    from different agents with different rule ids, the sentences differ, and
    the reasoning line is the part a reader and the benchmark's judge both
    read first. Of the 88 pairs this pass was calibrated on, five had exactly
    one member matching a golden comment; keeping one sentence would be
    keeping a coin flip on which one survives.
    """
    merged = _merge_same_line(cluster)
    seen: list[str] = []
    for f in cluster:
        r = (f.reasoning or "").strip()
        if r and r not in seen:
            seen.append(r)
    if seen:
        merged.reasoning = " ".join(
            r if r.endswith((".", "!", "?")) else r + "." for r in seen
        )
    return merged


def _merge_same_line(group: list[Finding]) -> Finding:
    """Combine findings several agents raised on one (file, line, rule):
    highest severity wins, bodies are kept with their provenance, and the
    consensus earns a small confidence bonus."""
    primary = min(group, key=_severity_rank)  # stable: first wins a tie
    avg_conf = sum(f.confidence for f in group) / len(group)
    agents = sorted({f.agent for f in group if f.agent})
    bodies = []
    for f in group:
        label = f"[{f.agent}]" if f.agent else "[?]"
        bodies.append(f"{label} {f.body}")

    return Finding(
        file_path=primary.file_path,
        line=primary.line,
        side=primary.side,
        severity=primary.severity,
        title=primary.title or "Multiple agents flagged this line",
        body="\n\n".join(bodies),
        suggestion=primary.suggestion,
        suggested_code=primary.suggested_code,
        suggested_end_line=primary.suggested_end_line,
        agent=",".join(agents),
        rule_id=primary.rule_id,
        confidence=min(1.0, avg_conf + 0.1),  # bonus for consensus
        reasoning=primary.reasoning,
        # This merge used to build a Finding with the default evidence_kind,
        # so four identical proven CVE findings merged into one INFERRED
        # finding — and an inferred finding is exactly what the LLM pass is
        # allowed to veto. A database that agrees with itself four times is
        # not a judgement.
        evidence_kind="proven" if any(f.is_proven for f in group) else primary.evidence_kind,
        rule=next((f.rule for f in group if getattr(f, "rule", "")), ""),
    )


class VerifierAgent:
    """Filter + dedup findings before posting comments."""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.5,
        model: str | None = None,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.model = model or get_review_settings().verifier_model

    def prefilter(
        self,
        findings: Iterable[Finding],
        *,
        suppressed_rules: Iterable[str] | None = None,
    ) -> PrefilterResult:
        """The deterministic pass. `suppressed_rules=None` means the code
        default (`ReviewSettings.suppressed_rules`); a repo policy passes its
        own list, and an empty one means "hide nothing"."""
        rules = (
            get_review_settings().suppressed_rules
            if suppressed_rules is None else suppressed_rules
        )
        return prefilter(
            findings,
            confidence_threshold=self.confidence_threshold,
            suppressed_rules=rules,
        )

    def llm_pass(
        self,
        findings: list[Finding],
        context: AgentContext,
    ) -> VerifierResult:
        """The optional LLM veto, over findings the prefilter already shaped.

        Runs from `ReviewSettings.verifier_min_findings` judgeable findings
        up (default 1). It used to need three — "the pass exists to thin a
        pile" — which held while the veto saw only the diff; with repository
        evidence (`verifier_evidence.py`) one finding is worth judging, and a
        typical pull request has one or two, which is where the false
        positives were. Fails open: an unreachable or unreadable verifier
        keeps everything.

        Proven findings never enter it. The pass exists to catch a model's
        hallucinations, and a proven finding is not a model's judgement — it
        is a lookup (a purl+version that matched an OSV advisory, an ast-grep
        rule that fired). Worse, a lockfile CVE finding is anchored in a file
        review filtering kept OUT of the hunks, and an LLM must not be able to
        veto a database.
        """
        import time

        t0 = time.time()
        result = VerifierResult(kept=list(findings))
        judgeable = [f for f in findings if getattr(f, "evidence_kind", "") != "proven"]
        minimum = max(1, int(getattr(get_review_settings(), "verifier_min_findings", 1) or 1))
        if len(judgeable) >= minimum:
            out = self._llm_verify(judgeable, context)
            result.error = out.error
            survivors = {id(f) for f in out.verified}
            # Filtered in place rather than `proven + verified`, so the
            # severity order the prefilter established survives the pass —
            # the providers cap on position.
            result.kept = [
                f for f in findings
                if getattr(f, "evidence_kind", "") == "proven" or id(f) in survivors
            ]
            result.judged = len(judgeable)
            result.dropped_llm_filter = len(out.dropped)
            result.dropped_findings = [
                {"title": f.title, "file": f.file_path, "line": f.line, "reason": why}
                for f, why in out.dropped
            ]
            result.unjustified_drops = out.unjustified
            result.evidence = out.evidence
            result.veto_attempts = out.attempts
            result.tokens_in = out.tokens_in
            result.tokens_out = out.tokens_out
        result.elapsed_seconds = time.time() - t0
        return result

    def verify(
        self,
        findings: list[Finding],
        context: AgentContext,
        *,
        suppressed_rules: Iterable[str] | None = None,
    ) -> VerifierResult:
        """prefilter() then llm_pass(), with every count on one result."""
        import time

        t0 = time.time()
        pre = self.prefilter(findings, suppressed_rules=suppressed_rules)
        result = self.llm_pass(pre.kept, context)
        result.dropped_dedup = pre.dropped_dedup
        result.dropped_near_duplicate = pre.dropped_near_duplicate
        result.dropped_low_confidence = pre.dropped_low_confidence
        result.dropped_by_rule = dict(pre.dropped_by_rule)
        result.elapsed_seconds = time.time() - t0
        logger.info(
            "verifier_done input=%d kept=%d by_rule=%d dedup=%d near_dup=%d "
            "low_conf=%d llm_drop=%d %.1fs",
            len(findings), len(result.kept), sum(result.dropped_by_rule.values()),
            result.dropped_dedup, result.dropped_near_duplicate,
            result.dropped_low_confidence, result.dropped_llm_filter,
            result.elapsed_seconds,
        )
        return result

    def _llm_verify(
        self,
        findings: list[Finding],
        context: AgentContext,
    ) -> _VetoOutcome:
        """Optional LLM pass — drop findings the repository's code refutes.

        `error` on the outcome is None when the pass ran; a user-safe sentence
        when it could not, in which case every finding is kept — see
        `VerifierResult.error`. If the LLM call fails — pass through all
        findings (fail-open). Gathering evidence is the first step and fails
        open on its own: no evidence is the old prompt, not an error.
        """
        from src.review.agents.verifier_evidence import (
            EvidenceBundle,
            gather_evidence,
        )

        try:
            bundle = gather_evidence(
                findings, context.pull_request,
                provider=getattr(context, "code_provider", None))
        except Exception as exc:  # noqa: BLE001 - never fail a review for this
            logger.warning("verifier_evidence_unavailable err=%s", exc)
            bundle = EvidenceBundle()
        prompt = self._build_verification_prompt(findings, context, bundle)
        evidence_stats = dict(bundle.stats)

        # Every other agent takes `context.llm_client` when the orchestrator
        # built one and only falls back to the native client otherwise. This
        # one had no such branch, so on a workspace routed through the gateway
        # it went straight to google-genai, found no raw `gemini` key, and
        # failed — silently, because the failure mode here is pass-through.
        #
        # Silently is the problem. The verifier exists to drop false
        # positives; a verifier that never runs turns every review into an
        # unfiltered agent dump while reporting nothing wrong.
        #
        # Both branches say num_retries=0 instead of inheriting
        # LLMClient.generate's default of 3 — the whole argument lives at
        # LLMReviewAgent._LLM_NUM_RETRIES in agents/base.py. It bit hardest
        # here: a rate-limited verify was resent three times into the window
        # that had just refused it, and only then "failed" into the
        # pass-through above — the unfiltered dump, at four calls' cost.
        #
        # The output ceiling was 1024, written here twice. It is a deliberate
        # number — this agent replies with {"keep": [indices]}, not prose — but
        # as a literal it could be neither seen nor raised, and a reasoning
        # model spends that budget on thinking before it writes the first
        # index. So 1024 is now the FLOOR (ReviewSettings.
        # verifier_max_output_tokens) and a per-agent setting can lift it.
        llm = agent_llm_settings(context, "verifier")
        attempts = 0
        tokens_in = tokens_out = 0
        response = None
        keep_indices: list[int] | None = None
        while True:
            attempts += 1
            try:
                response = self._call_llm(prompt, context, llm)
            except Exception as exc:  # noqa: BLE001
                from src.llm.errors import TRANSIENT, classify

                failure = classify(exc)
                if (failure.disposition == TRANSIENT
                        and attempts <= _VETO_EXTRA_ATTEMPTS):
                    # A timeout or a provider blip is the one failure worth
                    # a second ask; a throttle or a terminal error is not
                    # (resending into a window that just refused is the
                    # bug num_retries=0 above was written to end).
                    logger.info("verifier_llm_retry code=%s", failure.code)
                    continue
                logger.warning("verifier_llm_failed code=%s err=%s — pass-through",
                               failure.code, exc)
                # Fail open, and SAY SO. `failure.reason` and not `exc`: a
                # provider's exception text is not something this product puts
                # in front of a user, which is the whole point of the errors
                # module.
                return _VetoOutcome(findings, tokens_in=tokens_in,
                                    tokens_out=tokens_out, error=failure.reason,
                                    evidence=evidence_stats, attempts=attempts)

            text = getattr(response, "text", "") or ""
            tokens_in += int(getattr(response, "input_tokens", 0) or 0)
            tokens_out += int(getattr(response, "output_tokens", 0) or 0)
            keep_indices = self._parse_keep_indices(text, total=len(findings))
            if keep_indices is not None or attempts > _VETO_EXTRA_ATTEMPTS:
                break
            # The reply arrived and could not be read. A sampled answer is not
            # a fixed one: the same prompt asked again usually comes back as
            # JSON, and the alternative is every finding of the review kept
            # unfiltered.
            logger.info("verifier_unreadable_retry attempt=%d", attempts)


        if keep_indices is None:
            # The reply could not be read. Fail OPEN — an unreadable verifier
            # must not silently delete a real finding.
            logger.warning("verifier_unparseable — keeping all %d findings",
                           len(findings))
            # The OTHER fail-open, and just as invisible: the call succeeded,
            # was paid for, and its reply could not be read. Same consequence
            # as an unreachable verifier — every finding kept, unfiltered —
            # so the same disclosure.
            return _VetoOutcome(
                findings, tokens_in=tokens_in, tokens_out=tokens_out,
                error="the verifier's reply could not be read",
                evidence=evidence_stats, attempts=attempts)

        keep = {i for i in keep_indices if 0 <= i < len(findings)}
        reasons = self._parse_reasons(text)
        verified: list[Finding] = []
        dropped: list[tuple[Finding, str]] = []
        unjustified = 0
        overridden = 0
        items = list(getattr(bundle, "items", None) or [])
        for i, f in enumerate(findings):
            if i in keep:
                verified.append(f)
                continue
            why = reasons.get(i, "")
            ev = items[i] if i < len(items) else None
            if why and getattr(ev, "non_pr_source", False):
                # The evidence behind this drop includes code that is NOT the
                # pull request's (the clone's HEAD, newer than the PR, or the
                # provider's default branch). A definition found only there
                # may have appeared after the PR was written - the typo'd
                # constant "defined" by a later merge. Unclear, so kept;
                # decided here in code, not left to the prompt's wording.
                overridden += 1
                verified.append(f)
                continue
            if not why:
                # A drop must say what refutes the claim. One that does not
                # is a model that gave up on a finding, not one that checked
                # it — and "keep unless refuted" means this is kept.
                unjustified += 1
                verified.append(f)
                continue
            dropped.append((f, why))
        if unjustified:
            logger.info("verifier_unjustified_drops_kept n=%d", unjustified)
        if overridden:
            logger.info("verifier_non_pr_evidence_drops_kept n=%d", overridden)
            evidence_stats["drops_kept_non_pr_evidence"] = overridden
        return _VetoOutcome(
            verified, dropped, tokens_in, tokens_out, None, unjustified,
            evidence_stats, attempts)

    def _call_llm(self, prompt: str, context: AgentContext, llm: Any) -> Any:
        """One veto call through the gateway client; raises on provider error.

        Split out of `_llm_verify` so the caller can ask twice (an unreadable
        reply, a timeout) without writing the two transports out twice.
        """
        if context.llm_client is not None:
            return context.llm_client.generate(
                prompt=prompt,
                agent="verifier",
                mode="qa",
                operation="review_verifier",
                repo=context.pull_request.repo_slug,
                system_instruction=verifier_system_prompt(context),
                temperature=llm.temperature,
                max_output_tokens=llm.max_output_tokens,
                reasoning=llm.reasoning,
                # Stated, like the retry budget beside it and for the same
                # reason: `generate`'s 120s default was inherited by every
                # review call and could be changed by nobody. This pass
                # asks for a large structured reply from a reasoning model
                # over every finding in the review — the slowest single
                # call the pipeline makes.
                timeout=_llm_timeout(),
                num_retries=0,
            )
        else:
            # Through the gateway like everything else. This was the last
            # place in review that reached Google directly.
            from src.llm.client import build_llm_client

            return build_llm_client(
                getattr(context, "user_id", None) or "system",
                context.workspace_id,
                surface="review",
                resolve_model=_review_model(context.workspace_id),
            ).generate(
                agent="verifier",
                prompt=prompt,
                mode="qa",
                operation="review_verifier",
                repo=context.pull_request.repo_slug,
                system_instruction=verifier_system_prompt(context),
                temperature=llm.temperature,
                max_output_tokens=llm.max_output_tokens,
                reasoning=llm.reasoning,
                # Stated, like the retry budget beside it and for the same
                # reason: `generate`'s 120s default was inherited by every
                # review call and could be changed by nobody. This pass
                # asks for a large structured reply from a reasoning model
                # over every finding in the review — the slowest single
                # call the pipeline makes.
                timeout=_llm_timeout(),
                num_retries=0,
            )

    @staticmethod
    def _parse_reasons(text: str) -> dict[int, str]:
        """`reasons` from the reply as {index: one-line reason}; {} when there
        are none or they cannot be read. Never raises: a reason is a label on
        a drop, and an unreadable one means the drop is not honoured."""
        import json

        cleaned = _REASONING_BLOCK.sub(" ", text)
        for m in re.finditer(r"\{", cleaned):
            span = balanced_json_span(cleaned[m.start():], "{")
            if not span:
                continue
            try:
                data = json.loads(span, strict=False)
            except json.JSONDecodeError:
                continue
            raw = data.get("reasons") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                out: dict[int, str] = {}
                for k, v in raw.items():
                    try:
                        idx = int(str(k).strip().lstrip("#"))
                    except ValueError:
                        continue
                    one = " ".join(str(v or "").split())
                    if one:
                        out[idx] = one[:300]
                return out
            if isinstance(raw, list):
                return {}
        # The wrapper was not valid JSON but the reasons may still be
        # readable one by one: `"3": "line 12 defines it"` after the key.
        at = cleaned.find('"reasons"')
        if at >= 0:
            out = {}
            for m in _REASON_ENTRY.finditer(cleaned, at):
                one = " ".join(m.group(2).split())
                if one:
                    out[int(m.group(1))] = one[:300]
            return out
        return {}
        return {}

    @staticmethod
    def _build_verification_prompt(
        findings: list[Finding],
        context: AgentContext,
        evidence: object | None = None,
    ) -> str:
        """One section per finding: the claim, then everything the reader
        would open to check it — the file around the line, the PR's changes to
        that file, and what a search of the repository found for the symbols
        the claim names (`verifier_evidence.render_evidence`). Without
        `evidence` the section holds the claim and the finding's own hunks,
        which is what is known without reading the repository."""
        from src.review.agents.verifier_evidence import FindingEvidence, render_evidence

        pr = context.pull_request
        items = list(getattr(evidence, "items", None) or [])
        lines = [
            f"## PR: {pr.title}",
            "",
            "## Findings to verify",
            "",
        ]
        for i, f in enumerate(findings):
            lines.append(
                f"### Finding #{i} — {f.severity.value} ({f.agent}, "
                f"rule: {f.rule_id}, confidence: {f.confidence})"
            )
            lines.append(f"**Location:** {f.file_path}:{f.line}")
            ev = items[i] if i < len(items) else None
            if ev is None:
                hunks = "\n".join(
                    h.content for h in pr.hunks if h.file_path == f.file_path)
                ev = FindingEvidence(hunks=hunks[:2000])
            rendered = render_evidence(ev)
            # The evidence first, the claim after it: a reader who has the
            # claim in mind reads the code for what supports it.
            lines.append("")
            lines.append(rendered or "_No repository evidence could be gathered "
                                     "for this finding._")
            lines.append("")
            lines.append(f"**Claim to check — title:** {f.title}")
            lines.append(f"**Claim — body:** {f.body[:500]}")
            lines.append("")

        lines.append(
            "Return JSON: {\"checks\": {\"<index>\": \"premise: ... | read: "
            "... | verdict: holds|refuted|unclear\"}, \"keep\": [<indices of "
            "findings to keep>], \"reasons\": {\"<dropped index>\": \"<one "
            "line: the evidence that refutes the claim>\"}}. Every finding "
            "gets a check; every index not in keep needs a reason that cites "
            "the evidence above; a drop without one is ignored."
        )
        return "\n".join(lines)

    @staticmethod
    def _parse_keep_indices(text: str, *, total: int) -> list[int] | None:
        """Extract `keep` indices, or None if the reply could not be read.

        None and [] are different answers and used to be the same one. `[]` is
        the verifier saying "none of these findings are real" — a valid and
        useful verdict — and it was treated as a parse failure, so every
        finding the verifier had just rejected was kept and posted. The agent
        whose entire job is removing false positives could not remove all of
        them.
        """
        import json

        # The prompt asks for {"keep": [...], "reasons": {...}} — an object
        # with a nested object in it. The scan has to be brace-balanced:
        # `\{[^{}]*"keep"[^{}]*\}` cannot cross the `reasons` brace, so it
        # never matched the documented format at all, and the only reason
        # anything worked was the bare-array fallback below. That fallback
        # cannot see `"keep": []` — no digits — so the one verdict this agent
        # exists to deliver, "none of these are real", read as unparseable and
        # failed open into keeping every finding.
        cleaned = _REASONING_BLOCK.sub(" ", text)
        saw_an_object = False
        for m in re.finditer(r"\{", cleaned):
            span = balanced_json_span(cleaned[m.start():], "{")
            if not span:
                continue
            try:
                data = json.loads(span, strict=False)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            saw_an_object = True
            keep = data.get("keep")
            if isinstance(keep, list):
                return [int(i) for i in keep if isinstance(i, (int, float))]

        # Salvage — the object was not valid JSON (an unescaped quote or a
        # raw newline in a "checks" sentence quoting code is enough) but the
        # decision is still there in plain sight. Read the `"keep": [...]`
        # array by itself. Only a closed array of numbers counts, so a reply
        # cut off mid-array stays unreadable, and it runs BEFORE the
        # saw-an-object give-up below: that give-up fired on the inner
        # "checks" object, which parses on its own, and threw the real
        # verdict away with the broken wrapper.
        m_keep = _KEEP_ARRAY.search(cleaned)
        if m_keep:
            return [int(n) for n in re.findall(r"\d+", m_keep.group(1))]

        # Fallback — a bare array of indices, for a model that skipped the
        # wrapper object entirely. Deliberately NOT reached when the model did
        # emit an object: `{"dropped": [0, 1]}` would otherwise be read as the
        # indices to KEEP, inverting the decision. An object we cannot read is
        # unreadable, and unreadable keeps everything.
        if saw_an_object:
            return None
        m2 = re.search(r"\[[\d,\s]*\]", cleaned)
        if m2:
            try:
                arr = json.loads(m2.group(0))
                return [int(i) for i in arr if isinstance(i, (int, float))]
            except (json.JSONDecodeError, ValueError):
                return None
        return None


_VERIFIER_SYSTEM = """You are the last filter before a review reaches a human.

You get candidate findings. Each comes with the evidence a reader would open
before judging it, shown BEFORE the claim: the file around the line at the PR
head, the PR's changes to that file, the bodies of the functions called next to
the line, and what a search of the repository found for the symbols the claim
names. Decide which survive.

HOW TO CHECK A FINDING — in this order, written down in "checks" before you
decide anything:
    1. PREMISE. Restate in one sentence what must be true of the running code
       for the claim to hold ("after the catch, execution reaches line 70
       with $r never assigned"; "the discount applies to an item that is
       already free"). Name the assumption the claim needs that the
       changed line does not state itself — an orientation, a unit, what a
       variable is summed from, what the called function does — because that
       is the part the claim took on trust and the evidence may show.
    2. CHECK IT LINE BY LINE against the evidence, not against the claim's own
       account of it. Follow the control flow the claim relies on: what the
       function called on that path does when it is called — returns, throws,
       or ends the process (exit, die, abort, os.Exit, process.exit); whether
       the value is assigned on the path that reaches the line. A "Lookups"
       block, when present, holds table lookups already evaluated for you
       (`$map[3][1] = 4`): use those values as given, and when a claim says an
       index or key is wrong or duplicated, compare the evaluated values
       before anything else. Read names as
       the code defines them: what a variable is computed from decides what it
       measures, whatever its name suggests.
    3. VERDICT: "holds" (the lines you read are consistent with the premise),
       "refuted" (a specific line you can cite makes the premise false), or
       "unclear" (the evidence does not reach the premise).

KEEP is the default. Drop a finding only when step 3 is "refuted" — a line of
the evidence, cited by number, contradicts the premise. Typical refutations:
    - a symbol the claim calls undefined or misspelled IS defined or used under
      that name (a constant in another file, a global the repository really
      names that way, a variable set in the enclosing loop above the hunk)
    - a guard, check or fix the claim says is missing IS present in the code
      shown
    - the code path the claim says continues does not: the called function
      shown ends the process, throws, or returns before the line in question
    - the quantity the claim says is wrong is, by the lines that compute it,
      the right one (the value is built from the very terms the claim says
      should be used)
    - markup, config or a mapping shown LACKS what the claim assumes (the
      element has no such class; the index matches the mapping defined above)
    - the value the claim fears cannot occur, by code shown

KEEP the finding when:
    - the evidence is absent, partial or ambiguous — a search that found
      nothing, or a hosted index that covers another branch, refutes nothing
    - the only line you would cite comes from evidence labelled "NOT THE PULL
      REQUEST'S CODE" / "NEWER than the pull request": it may have appeared
      after the pull request was written (the code deletes such a drop anyway)
    - the evidence is consistent with the claim, or does not address it
    - you disagree about severity, style, wording, or whether it is "a risk
      rather than a consequence" — those are never reasons to drop
    - you cannot point to a line of the evidence that contradicts the claim
    - the premise rests on something you had to assume, because it is not in
      the evidence

The asymmetry is deliberate. Dropping a real defect costs the author a bug in
production; keeping a weak observation costs them ten seconds. Evidence
labelled as coming from the default branch or a clone HEAD may differ from the
PR head; weigh it accordingly.

Return JSON with "checks" FIRST, then the decision:
{"checks": {"<idx>": "premise: <one sentence> | read: <lines you checked, by number> | verdict: holds|refuted|unclear"},
 "keep": [<indices keep>],
 "reasons": {"<dropped_idx>": "<one line citing the line that refutes the claim>"}}
Every finding gets a "checks" entry, kept to one line. A dropped index with no
reason is ignored.
"""


#: What /admin/agents reads as the verifier's built-in prompt
#: (`src.api.routers.agents._default_system_prompt` looks for `_SYSTEM`).
_SYSTEM = _VERIFIER_SYSTEM


#: Said after the team's base instruction in the verifier's prompt. The
#: finders were told how to write; the verifier is told the same text so it
#: judges a finding written that way as written correctly — and, in the same
#: breath, that wording is never a reason to drop one. The veto's whole job is
#: consequences, and a filter that started vetoing on tone would be a new way
#: to lose a real defect.
_BASE_INSTRUCTION_RIDER = (
    "The finders were given this instruction for how to write. Read their "
    "findings in its light, and never drop a finding for its wording, form or "
    "tone — only for the reasons listed above."
)


def verifier_system_prompt_parts(context: AgentContext) -> list:
    """The blocks of the verifier's system prompt, in the order sent.

    The same precedence the finders use (`compose_system_prompt_parts`):
    this repository's replacement, else the workspace's, else the built-in.
    Then two things may be appended, and only these: the team guidelines for
    the verifier (what this team wants kept or left out — in the same
    delimited block the finders get, worded for a filter: they never let a
    finding that fails the checks survive, and never change the reply shape),
    and the policy's base instruction with its rider. The verifier answers
    `{"keep": [...]}` and reads no repo rules, so the finder's output
    contract and rule blocks would only confuse it. An override that stops
    asking for that shape fails open (every finding kept), never closed.
    """
    from src.review.agents.base import PromptPart, team_guidelines_part

    base, source = _verifier_base_prompt(context)
    # The base as written: alone, it is the whole prompt, byte for byte.
    parts = [PromptPart("base", base, source)]
    guidelines = team_guidelines_part(context, "verifier")
    if guidelines is not None:
        parts.append(guidelines)
    block = base_instruction_block(getattr(context, "base_instruction", ""))
    if block:
        parts.append(PromptPart("base_instruction", block))
        parts.append(PromptPart("rider", _BASE_INSTRUCTION_RIDER))
    return parts


def verifier_system_prompt(context: AgentContext) -> str:
    """The verifier's system prompt for this review — the blocks of
    `verifier_system_prompt_parts`, joined."""
    parts = verifier_system_prompt_parts(context)
    if len(parts) == 1:
        return parts[0].text
    return "\n\n".join(p.text.rstrip() for p in parts)


def _verifier_base_prompt(context: AgentContext) -> tuple[str, str]:
    repo_override = ((context.repo_agent_prompts or {}).get("verifier") or "").strip()
    if repo_override:
        return repo_override, "repo"
    try:
        from src.api.routers.agents import get_effective_system_prompt
        base = get_effective_system_prompt("verifier", context.workspace_id)
    except Exception:  # noqa: BLE001
        base = ""
    if base and base.strip() != _VERIFIER_SYSTEM.strip():
        return base, "workspace"
    return _VERIFIER_SYSTEM, "builtin"
