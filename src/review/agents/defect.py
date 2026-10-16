"""Defect agent — single-file provable defects. The main finder.

THIS AGENT REPLACES THREE. Architect, quality and tests ran as separate LLM
calls, and a 50-PR benchmark (173 goldens, judge claude-sonnet-4-5) measured
what that bought:

  * 29 of 262 generated findings were near-duplicates — the agents retelling
    each other's findings under different rule ids, each retelling billed as
    its own tokens and read by the verifier as its own candidate;
  * the correlation between an agent's breadth and its precision was the
    OPPOSITE of the split's premise: architect, the widest remit (53 distinct
    rules), was the most precise (51.1%); quality, the narrowest (22 rules),
    the least (38.2%). Splitting remits was not buying focus;
  * the boundary that DID separate precision was mechanical, not thematic:
    claims provable inside one file ran 48.3%; claims that named a second
    file ran 14.3%. Both sessions measured it independently with different
    detectors and agreed to a tenth of a point.

So the split is now along that measured line. This agent owns everything
provable within one file — the whole defect list architect carried, quality's
maintainability defects, tests' untested-branch clause. Cross-file claims
belong to the contract agent, which holds them to a stricter standard of
evidence; this agent is FORBIDDEN to make them, so the two standards never
blur inside one prompt.

45 of the 46 missed bug-goldens on that benchmark were single-file, visible
in the diff. Recall lives here.
"""

from __future__ import annotations

import dataclasses
import logging

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    FINDING_OUTPUT_FORMAT,
    SECOND_DEFECT_PROMPT,
    LLMReviewAgent,
)
from src.review.code_context import build_code_context, render_numbered_diff
from src.review.models import FindingSeverity
from src.review.settings import get_review_settings

logger = logging.getLogger(__name__)

_ROLE = """You are an experienced engineer reviewing Pull Request changes.

YOUR WHOLE JOB — defects in the changed lines, provable from the code shown to
you: the numbered hunks, the full enclosing function of each hunk, the call
sites of the changed functions and the same-file callees printed below.

    A defect is something that produces a wrong result, an exception, or
    behaviour the author plainly did not intend, when this code RUNS. It is not
    a risk, not a smell, not a design opinion. Read the diff line by line and
    ask of each one: does this do what it says it does?

    The kinds that hide well, in the order they are missed:
      - a value that is checked, assigned or compared TWICE while a sibling is
        never touched — copy-paste that survived review
      - a branch that can never be taken because an earlier condition already
        covers it, or because the function it tests always returns a value —
        state which earlier line makes it unreachable
      - an off-by-one in a slice, index or range boundary
      - a literal where a variable belongs — a hardcoded locale, a token name
        assigned as the token itself, a default that silently replaces a value
      - a call that assumes one platform: shell flags, path separators, a
        command whose syntax differs between BSD and GNU
      - an argument in the wrong position, a wrong unit, a wrong sign
      - a library used with syntax that is not valid for it, which throws only
        when the line is reached
      - state mutated inside a loop that also decides the loop's exit
      - check-then-act on state another request can reach between the two: a
        count checked then incremented, a set read then replaced, a code
        marked used after it was looked up
      - an async call whose result is never awaited and is then used —
        including a callee this PR made async under a caller it left as it was
      - an exception swallowed bare, a resource opened without a close on the
        error path, a mutable default argument
      - a new branch whose two sides do DIFFERENT things, with nothing
        exercising the new side — name the branch, and what would silently
        pass if it were wrong. "This function has no test" on its own is NOT
        a finding: every large repository has thousands of untested functions
        and the author did not add them today.

HOW TO READ THE DIFF — one changed line at a time, in order, to the end.

    Check every changed line against the shapes above. Each line that matches
    a shape is its own finding, and you are finished when the LAST changed
    line has been checked — not when you have written enough findings to be
    going on with.

    A diff with six defective lines gets six findings. Two defective lines in
    one file are two findings, one per line. Leaving out a line that matched,
    because other findings were already written, is the defect shipping.

THE BOUNDARY — the code shown to you.

    Every claim you make must be provable from the code printed below: the
    hunks (new-side line numbers in the left gutter), the enclosing functions
    (lines marked * are the ones this PR changed), the call sites and the
    callees. Quote the line that proves it. A caller or a callee is fair game
    when its lines are shown; a claim about code that is NOT shown — a file
    you were not given, a function whose body is absent, a sibling repository
    — is not provable from here: it belongs to the contract reviewer, who sees
    the symbol graph. Do not write it, even when you are sure. Claims that
    stay inside what was shown are right far more often than claims that reach
    outside it.

    When the evidence is INFERRED rather than read — you assume what a helper
    returns, how a caller uses a value, what a library does — say so in the
    body and put the confidence at 0.5 or below. Only a defect you can trace
    line by line from shown code earns 0.7 or more.

WHAT COUNTS AS EVIDENCE.

    Write every finding whose reasoning sentence you can finish with a
    concrete wrong outcome. The test is the sentence, not the count: a diff
    with eight of them is a review with eight findings.

    A claim about how a library behaves under the hood — a serializer that
    will reject this type, an isinstance that will miss this subclass, a
    value that is truthy where you expect falsy — is only a finding when a
    line IN THIS DIFF proves it. Your memory of the library is not evidence,
    and this is the largest single class of wrong answer measured on 102
    judged false positives: confident, well-quoted, single-file, and about a
    behaviour nobody checked.

BEFORE YOU CLAIM THAT SOMETHING IS MISSING.

    "Undefined variable", "missing constant", "wrong or out-of-range index",
    "function does not exist", "property never set": each of these is a claim
    about the ABSENCE of a symbol, and an absence can only be shown by
    looking. Report it only when you can say that the symbol is absent from
    the enclosing function and the file context printed below — name where
    you looked. A variable declared in a loop or branch above the hunk, a
    mapping array declared higher in the file, a helper defined elsewhere in
    the file: all of these are common, and none of them is visible in a
    three-line hunk. If the declaration could simply be outside what you were
    shown, you do not have the finding — write nothing. The same goes for a
    "wrong type" claim: before writing it, find the call site in the context
    and check what it actually passes.

DO NOT WRITE:
    - architectural opinions — coupling, module boundaries, "consider
      extracting", "this pattern is inconsistent with"
    - minor optimizations — micro-performance, allocation counts, "could be
      cached", unless the changed code is on a measured hot path named in the
      context
    - stylistic concerns beyond what the repo's own style guide states
    - issues in surrounding, unchanged code"""

# THE BEHAVIOUR-CHANGE LENS.
#
# The first pass reads the diff for the shapes in `_ROLE`, which are all
# properties of the NEW code. They miss the defects that are properties of the
# CHANGE: the new line is perfectly valid, and wrong only because of what it
# replaced. A flipped comparison, a deleted fallback, a status code that used
# to be 200 — read as new code each is plausible. Only the `-`/`+` pair, or a
# `-` line with no `+` partner at all, shows the problem. A reviewer who looks
# only at added lines cannot see that something was REMOVED, and deleted lines
# were measurably where a small model's recall went.
#
# Used twice: in the system prompt, so the first pass applies it, and as the
# question of the second pass (see `_SECOND_PASS`), which used to repeat the
# first pass's question and re-find what pass one already had.
_BEHAVIOUR_LENS = """THE CHANGE LENS — compare what was removed with what replaced it.

    The `-` lines are as much a part of the diff as the `+` lines. For every
    hunk, pair each `-` line with the `+` line that replaced it (or note that
    nothing did) and ask what behaviour CHANGED:
      - POLARITY: a negation added, dropped or doubled (`!x`, `!!x`, `!=`,
        `!==` versus `===`), `<` versus `<=`, `&&` versus `||`, `empty()`
        versus `isset()`, a swapped branch of a ternary or if/else
      - REMOVED SAFETY NET: a deleted fallback, default value, null/empty
        guard, early return, `else` branch, try/catch, validation or
        `continue`. A `-` line with no `+` partner is a deletion — ask what
        the code below it does now that the line is gone
      - RETURN AND STATUS: a changed return value, error code or HTTP status,
        and whether something downstream (the caller, a later assignment, a
        finally block) overwrites or ignores it
      - CALLER/CALLEE SHAPE: a value serialized, stringified or wrapped on
        one side (`JSON.stringify(x)`, a cast, an array of one) while the
        other side — a call site or callee shown below — expects the original
        shape: object versus JSON string, array versus object, string versus
        number
      - CALLEE SEMANTICS: a raw field replaced by an accessor, or the
        reverse, where the callee's body (shown below when it is in the same
        file) does something the raw field does not — a conversion, a default,
        a clamp, a unit
      - COPY-PASTE: a branch duplicated from its sibling in which one
        variable, coordinate or index (x/y, start/end) was not
        swapped

    A change from one valid behaviour to another is not a defect on its own.
    It is a finding when the code shown proves the OLD behaviour was the
    intended one, or that the new behaviour breaks a reader you can quote.
    The evidence rules above are unchanged."""

# A second admissible shape for the `reasoning` sentence.
#
# FINDING_OUTPUT_FORMAT asks for "the line, the value, and what happens when
# the code runs", and the example it gives is a value trace. Since that
# sentence became a parse-time requirement (added e01531d, gated 5adc53f),
# the shape held: on the 14-PR bench subset every finding that carried a
# reasoning sentence named a numbered line in it, 20 of 20, against 11 of 32
# comment bodies in the run before the field existed.
#
# It also cost. Two goldens matched in the run WITHOUT the gate and nobody
# matched in the run WITH it have no wrong value to trace: a `postMessage`
# handed the full referer where the API contract takes an origin, and an
# `Authenticate` that returns ErrDeviceLimitReached where device tagging used
# to be asynchronous. Every value on those lines is the one the author
# intended; the decision is what is wrong, so "the value" the example asks
# for does not exist and the sentence will not finish.
#
# So this block names both shapes and holds them to one standard of evidence:
# a line from this diff, a path that reaches it, and an outcome that differs
# from the intended one. It lives here rather than in FINDING_OUTPUT_FORMAT
# because that constant also feeds SECURITY, where all 13 findings of the
# same run name a numbered line in their reasoning — widening the shared
# contract would spend that shape on this agent's problem.
_REASONING_FORMS = """Two sentence forms satisfy that "reasoning" field. Both name a line this
PR changes, or a line the reasoning says a changed line reaches:

    (a) A VALUE traced through lines — where it comes from, what it can hold
        there, and the line that then uses it wrongly. "cfg can be nil on
        line 42; dereferenced without a check on line 47."

    (b) A BEHAVIOUR traced through a path — the changed line, the path that
        reaches it, and what that path now produces instead of what it
        produced or was meant to produce. "When the refresh handler runs on
        line 88 with an expired token, it returns 401 instead of the renewed
        session it returned before this change."

Form (b) exists because some defects have no wrong value to point at. An
error path that now rejects a caller it used to admit, an API given an
argument its contract does not accept, a call ordered after the thing it was
meant to guard: every value on those lines is the one the author intended,
and the defect is the decision. Do not bend such a finding into form (a),
and do not discard it for failing to fit — write it as (b).

Form (b) is not a licence to speculate. It demands the same three things (a)
does: a specific line, a path you can name, and a Z you read off this diff —
not one you assume. "Might", "may under load" and "could in some
configuration" are not paths. If you cannot say which line, on which path,
produces which wrong outcome, you do not have the finding — write nothing,
exactly as with (a)."""

_SEVERITY = """rule_id format: `defect.<rule>` (e.g. `defect.off-by-one`, `defect.dead-branch`,
`defect.untested-branch`).

Severity — decided by what happens when the code runs, not by how much the
code bothers you:

    critical — data loss, a bypassed check, a crash on a request path
    error    — wrong behaviour on a path that will be taken
    warning  — a real defect on a path that needs specific conditions, or a
               new behaviour-changing branch nothing exercises
    info     — everything else you were tempted to write down

If a finding cannot be stated as "when X runs, Y happens instead of Z", it is
at most "info", and probably should not be written at all. An observation that
is not a defect costs the defects beside it their reader — which is why it is
worth withholding. That is a bar on what qualifies, NOT a limit on how many
findings a review may carry: every line that is genuinely defective is
genuinely a finding, however many of them the diff turns out to contain."""

# Role, then the shared output contract, then this agent's rider on the
# reasoning sentence that contract requires, then the shared avoid-list, then
# this agent's own severities. The two shared constants keep their order and
# their wording, so the parser's contract and the deny-list's categories read
# identically whichever agent the operator opens; _REASONING_FORMS sits
# directly after the contract it widens, because read apart from it the two
# forms have nothing to be forms OF.
_SYSTEM = (
    "\n\n".join([_ROLE, _BEHAVIOUR_LENS, FINDING_OUTPUT_FORMAT, _REASONING_FORMS, AVOID_LIST_PROMPT, SECOND_DEFECT_PROMPT, _SEVERITY])
    + "\n"
)


# THE GRAPH SUMMARY IS BACK, SMALL, AND LABELLED.
#
# It was removed on the strength of an observational number (40.2% precision
# on a complete graph against 51.1% on a partial one) and on this agent's own
# rule that a claim about another file belongs to the contract reviewer. Both
# premises have changed. The rule now reads "provable from the code shown",
# and the summary is where the agent learns which changed symbols have
# callers at all — so it knows when the call-sites section below it is the
# evidence to look for. The 40.2/51.1 gap was never shown to be causal, and
# the old summary was printed whole; here it is cut to `_GRAPH_CHARS`.
#
# It is orientation, not evidence: the prompt says a claim still has to quote
# a shown line. The cross-repo counts and the drift block stay with the
# contract agent.
_GRAPH_CHARS = 4_000

_USER_TEMPLATE = """## PR
**Title:** {pr_title}
**Description:**
{pr_description}

## Diff (new-side line numbers in the left gutter; `-` lines have none)
{diff}

## Symbol graph (orientation only — quote shown lines, not this summary)
{graph_summary}

## Style guide
{style_guide}

---

Return a JSON array of findings, each starting with its "reasoning" sentence.
If nothing is found — `[]`. Every finding names a file this PR changes and a
line in it. Don't hallucinate.
"""


#: What the second pass is told about the first.
#:
#: It asks for the COMPLEMENT, not for more. A second look with no account of
#: the first is a second draw on the same pool: it re-finds what pass one
#: already has, the prefilter collapses the duplicates, and the call is spent
#: for nothing. Told what is already written, the same draw spends itself on
#: the part of the pool that is still open.
#:
#: The permission to answer `[]` is load-bearing and is why this block ends
#: the way it does. A prompt that lists nine findings and asks for more reads
#: as a demand for a tenth, and the cheapest tenth is a restatement — the
#: exact failure the per-line sweep produced when it was given no bar: +0.35
#: findings per PR, none of them defects.
_SECOND_PASS = """

## Already reported on this diff

A first pass over these same changed lines produced the findings below. They
are written and will be posted. Do not repeat them, do not reword them, and do
not comment again on the lines they occupy unless that line carries a
DIFFERENT defect — a different wrong value, a different path, a different
consequence.

{already}

Now read the diff again with a DIFFERENT question. The first pass asked "is
this new code wrong?". Ask instead: "what behaviour did this change alter, and
is the old behaviour the one that was meant?" Apply THE CHANGE LENS above to
every hunk — pair each `-` line with its `+` line, look at every deletion that
has no partner, and check the caller/callee shape against the call sites and
callees shown. Report the defects the first pass did not.

Most second passes find one or two. Some find none, and `[]` is the correct
answer when the first pass was thorough — it is not a failure to agree with
it. Every rule above still applies: a finding whose reasoning sentence you
cannot finish is still not a finding, and a restatement of something already
listed is worse than silence.
"""


class DefectAgent(LLMReviewAgent):
    """Single-file defect review — the main finder."""

    name = "defect"
    severity_default = FindingSeverity.WARNING
    system_prompt = _SYSTEM
    user_prompt_template = _USER_TEMPLATE

    #: Set only for the duration of the second call — see `review`. Instance
    #: state is safe here because every call site builds a fresh
    #: `ReviewOrchestrator`, so one agent object serves one review.
    _already_found: list | None = None

    def __init__(self, model: str | None = None, passes: int | None = None) -> None:
        settings = get_review_settings()
        self.model = model or settings.defect_model
        self.passes = max(1, int(passes if passes is not None else settings.defect_passes))

    #: The context being rendered, and the code-context text already built for
    #: it. Set by `_build_prompt` so `_format_diff_for_prompt` (which the base
    #: class calls with the pull request alone) can reach the provider and the
    #: clone; the cache means the second pass does not run `git` again.
    _ctx = None
    _extra_cache: tuple[int, str] | None = None

    def _code_context_text(self, pr, hunks) -> str:
        """The enclosing-function / callee / call-site sections for `hunks`.

        Built once per pull request and reused by the second pass. Empty when
        the feature is off or nothing could be read; never raises.
        """
        key = id(pr)
        if self._extra_cache is not None and self._extra_cache[0] == key:
            return self._extra_cache[1]
        settings = get_review_settings()
        text = ""
        if settings.code_context_enabled:
            provider = getattr(self._ctx, "file_provider", None)
            text = build_code_context(
                pr, hunks, provider=provider,
                max_chars=settings.code_context_chars,
                max_callsites=settings.code_context_callsites,
            ).render()
        self._extra_cache = (key, text)
        return text

    def _format_diff_for_prompt(  # type: ignore[override]
        self, pr, max_chars: int = 50_000, hunks=None,
    ) -> str:
        """The diff with line numbers, followed by the code it sits in.

        Falls back to the plain rendering on any error or when the feature is
        off: a renderer bug must cost context, never the review.
        """
        plain = LLMReviewAgent._format_diff_for_prompt
        if not get_review_settings().code_context_enabled:
            return plain(pr, max_chars, hunks)
        try:
            use = pr.hunks if hunks is None else hunks
            diff = render_numbered_diff(use, max_chars)
            extra = self._code_context_text(pr, use)
            return diff + ("\n" + extra if extra else "")
        except Exception:  # noqa: BLE001
            logger.warning("defect_code_context_failed", exc_info=True)
            return plain(pr, max_chars, hunks)

    def _build_prompt(self, context) -> str:
        # The graph summary is orientation, not evidence: cut it, so a large
        # monorepo's blast radius cannot crowd out the code itself.
        if len(context.graph_summary or "") > _GRAPH_CHARS:
            context = dataclasses.replace(
                context,
                graph_summary=context.graph_summary[:_GRAPH_CHARS] + "\n... (summary cut)",
            )
        self._ctx = context
        try:
            prompt = super()._build_prompt(context)
        finally:
            self._ctx = None
        if not self._already_found:
            return prompt
        listed = "\n".join(
            f"  - {f.file_path}:{f.line} — {f.title}" for f in self._already_found
        ) or "  (none)"
        return prompt + _SECOND_PASS.format(already=listed)

    def review(self, context):
        """One diff, `passes` reads of it, findings unioned.

        WHY MORE THAN ONE READ. The three finder agents this one replaced took
        66 true positives between them on a 50-PR benchmark; this one takes
        45 — and the shortfall is not judgement, because its precision per
        claim is higher than any of theirs. It is sampling: the same benchmark
        measured 43 of 43 findings lost between two runs of identical code as
        "this time it did not write it". Three agents were three draws on an
        overlapping pool. One agent is one draw, and one draw recovers about
        0.68 of what three took.

        The alternative explanation — that one answer has a natural list
        length, so the agent saw more than it wrote — was tested first, by
        stripping every restraint from the prompt and adding a per-line sweep.
        It produced +0.35 findings per PR, zero new true positives and three
        false ones. The agent was writing what it saw; it just was not seeing
        all of it in one go.

        A FAILED SECOND PASS IS NOT A FAILED REVIEW. Pass one's findings are
        real whatever happens next, so a second call that errors is logged and
        dropped rather than propagated — the alternative is an outage in the
        extra look costing the review the findings it already had.

        Duplicates between passes are expected and are the prefilter's job:
        exact dedup on (file, line, rule) and near-duplicate clustering run
        over the combined list, the same machinery that already merges two
        agents landing on one line.
        """
        first = super().review(context)
        if self.passes < 2 or first.error or context.pull_request is None:
            return first

        self._already_found = list(first.findings)
        try:
            later = super().review(context)
        finally:
            self._already_found = None

        # THE LEDGER FIRST, BEFORE ANYTHING CAN RETURN PAST IT. The failing
        # branch below used to be a bare `return first`, so a second pass that
        # errored left no trace of what it had spent — and it spends the most
        # exactly when it fails: a timeout costs the call, the transient
        # retry, and sometimes a fallback call on top, every one of them
        # billed. `_generate_and_parse` deliberately sums across all of them
        # so a failing agent does not read as a cheap one; dropping the sum
        # here turned that honesty into a bigger hole rather than a smaller
        # one. It is the same rule the orchestrator states for failed agents,
        # in the same words, four files away.
        first.tokens_in += later.tokens_in
        first.tokens_out += later.tokens_out
        first.elapsed_seconds += later.elapsed_seconds
        if later.cost_usd is not None:
            first.cost_usd = (first.cost_usd or 0.0) + later.cost_usd
        elif later.tokens_in or later.tokens_out:
            # Tokens with no price is not zero cost, it is an unknown one, and
            # a run total that silently absorbs it is a guess wearing a number.
            first.cost_source = "unknown"
        # Every parameter Celmis changed on the way to this answer, from the
        # failing pass too — and this was dropped on BOTH branches, so a
        # ceiling clamp or a swap to the fallback model on the second call
        # vanished even when it worked. An operator reading the record needs
        # to know the request was not the one they configured.
        first.parameter_adjustments = list(
            getattr(first, "parameter_adjustments", None) or ()
        ) + list(getattr(later, "parameter_adjustments", None) or ())

        if later.error:
            logger.warning(
                "defect_second_pass_failed pr=%s code=%s err=%s — keeping the "
                "%d findings the first pass produced",
                getattr(context.pull_request, "number", "?"),
                getattr(later, "error_code", None) or "-",
                str(later.error)[:200], len(first.findings),
            )
            # AND SAY SO WHERE IT IS READ. A one-pass review is a review that
            # measured ~8pp less recall than the two-pass one this agent is
            # configured for, and until now it published as COMPLETE with no
            # banner, nothing in `agents_failed` and an APPROVE available to
            # it — indistinguishable from a review that looked twice.
            #
            # NOT `agents_failed`: the agent did not fail. Pass one's findings
            # are real whatever happened next, and putting "defect" in the
            # failed list would make a critical finder read as absent and
            # refuse an approval the findings support. It is the SECOND LOOK
            # that did not happen, so it is named as its own skipped stage.
            from src.llm.errors import curated_reason

            first.skipped_stages = {
                **(getattr(first, "skipped_stages", None) or {}),
                "defect_second_pass": (
                    curated_reason(getattr(later, "error_code", None))
                    or "the second read of the diff did not complete"
                ),
            }
            return first

        first.findings = list(first.findings) + list(later.findings)
        first.dropped_no_evidence = (
            getattr(first, "dropped_no_evidence", 0)
            + getattr(later, "dropped_no_evidence", 0)
        )
        logger.info(
            "defect_two_passes pr=%s first=%d second=%d",
            getattr(context.pull_request, "number", "?"),
            len(first.findings) - len(later.findings), len(later.findings),
        )
        return first
