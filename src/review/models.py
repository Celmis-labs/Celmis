"""PR Review domain model — provider-agnostic.

Universal data structures that abstract over GitHub/GitLab/Bitbucket diffs +
findings + verdicts. Provider-specific code converts raw API responses into
these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

# The one shape every runtime adjustment travels in — see the block above it
# in src/llm/capabilities.py. Imported here, in the domain model, because the
# batch is where the agents' adjustments are merged and where the PR comment
# reads them from; capabilities.py imports litellm only inside its probes, so
# this costs the domain model nothing at import.
from src.llm.capabilities import (
    ADJUST_CLAMPED,
    ADJUST_DROPPED,
    ADJUST_SWAPPED,
    PARAM_MAX_OUTPUT_TOKENS,
    PARAM_MODEL,
    PARAM_REASONING,
    PARAM_TEMPERATURE,
    ParameterAdjustment,
    adjustment_as_dict,
)


class FindingSeverity(StrEnum):
    """Review finding severity (industry standard 4-level)."""

    INFO = "info"          # nitpick / style suggestion
    WARNING = "warning"    # quality issue, not blocker
    ERROR = "error"        # likely bug
    CRITICAL = "critical"  # security / data-loss risk


class ReviewVerdict(StrEnum):
    """Overall PR verdict — analog GitHub review event."""

    APPROVE = "approve"          # all clean, no blockers
    REQUEST_CHANGES = "changes"  # ≥1 critical/error finding
    COMMENT = "comment"          # findings present but not blockers
    SKIPPED = "skipped"          # nothing reviewed (draft / no hunks / all filtered)


class ReviewRunStatus(StrEnum):
    """Lifecycle of a persisted review run.

    These are the strings `ReviewRun.status` (src/api/review_runs.py) has
    always held; PARTIAL and SKIPPED are the additions. Each is deliberately a
    member of the SAME vocabulary rather than a second flag beside it, because
    every consumer that already switches on status — the metrics gauge, the
    history row, a future re-run command — then has to acknowledge the case
    instead of quietly bucketing it with COMPLETE.

    PARTIAL means what Kodus calls PARTIAL_ERROR: the pipeline never aborted,
    every comment it did produce was posted, and a stage is missing from the
    answer. That is not FAILED — a failed run produced no answer at all — and
    it must not be COMPLETE, because "complete" is the word the product uses
    when it has actually looked everywhere.

    FAILED is reached two ways. The pipeline can raise, in which case the row
    is written by the caller's except-branch and carries an `error_message`.
    Or every stage it dispatched can come back empty-handed, which raises
    nothing — the run finishes normally with no signal in it. That second
    shape used to read back as PARTIAL, i.e. "one stage is missing" for a
    review in which no stage was present, so see `ReviewBatch.nothing_ran`.

    SKIPPED means no stage was ever dispatched, so there is no review for this
    row to be the status of. It used to read back as COMPLETE — both rosters
    empty, nothing failed, therefore "complete" — which is how a repository
    whose policy lists every agent in `disabled_agents` put a green tick and
    an APPROVE verdict on every pull request nothing had looked at. It is not
    FAILED, because nothing errored; it must not be COMPLETE for the same
    reason PARTIAL must not be: "complete" is the word the product uses when
    it has actually looked.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


class HunkSide(StrEnum):
    """Diff side — for line numbers context."""

    LEFT = "left"    # removed line — old file
    RIGHT = "right"  # added line — new file (most comments go here)
    BOTH = "both"    # context line


@dataclass
class Hunk:
    """One @@ hunk in a unified diff."""

    file_path: str           # new path (post-change); old path if file deleted
    old_file_path: str       # before change (= file_path for modified files)
    old_start: int           # line in the old file (1-indexed)
    old_count: int
    new_start: int
    new_count: int
    content: str             # full hunk text including the '@@' header
    is_binary: bool = False
    is_new_file: bool = False
    is_deleted_file: bool = False
    is_renamed: bool = False

    @property
    def added_lines(self) -> int:
        """Number of '+' lines — a proxy for review effort."""
        return sum(1 for line in self.content.split("\n") if line.startswith("+") and not line.startswith("+++"))

    @property
    def removed_lines(self) -> int:
        return sum(1 for line in self.content.split("\n") if line.startswith("-") and not line.startswith("---"))


@dataclass
class PullRequest:
    """Provider-agnostic PR representation."""

    provider: str            # 'github' | 'gitlab' | 'bitbucket'
    repo: str                # owner/name or slug
    number: int              # PR number / MR iid / PR id
    title: str
    description: str
    author: str              # login
    base_ref: str            # target branch (e.g. 'main')
    base_sha: str
    head_ref: str            # source branch
    head_sha: str
    state: str               # 'open' | 'closed' | 'merged' | 'draft'
    is_draft: bool = False
    url: str = ""            # web URL for the PR
    hunks: list[Hunk] = field(default_factory=list)
    raw_diff: str = ""       # unified diff text (for debugging / fallback parsing)
    skipped_files: list[str] = field(default_factory=list)
    # Files filtered out during diff parsing (skip_patterns / binary / too-large)

    @property
    def repo_slug(self) -> str:
        """Slug-safe identifier — for CACHE KEYS, not for addresses.

        Always prefixed, so two providers cannot collide in one cache. That is
        not what the rest of the product is keyed on: clones, vaults, graphs,
        review policies and group membership all use `ParsedRepo.slug`, which
        drops the prefix for Bitbucket and Generic. Use `local_slug` to reach
        any of them.
        """
        return f"{self.provider}_{self.repo.replace('/', '-')}"

    @property
    def local_slug(self) -> str:
        """The slug this repository was actually registered and cloned under.

        Asks the canonical parser rather than rebuilding the rule, because the
        rule has an exception — Bitbucket and Generic carry no provider prefix,
        for clones that predate it — and a second copy of a rule with an
        exception is a second chance to miss it. Every Bitbucket review lost
        its policy, its vault, its clone and its group this way: four lookups
        against an address nothing was ever written to, each answering with a
        silent default.
        """
        from src.sync.git_providers import parse_repo_url

        try:
            return parse_repo_url(f"{self.provider}:{self.repo}").slug
        except Exception:  # noqa: BLE001
            return self.repo_slug

    @property
    def total_added_lines(self) -> int:
        return sum(h.added_lines for h in self.hunks)

    @property
    def total_removed_lines(self) -> int:
        return sum(h.removed_lines for h in self.hunks)

    @property
    def changed_files(self) -> list[str]:
        # Preserves order, dedupes
        seen: set[str] = set()
        out: list[str] = []
        for h in self.hunks:
            if h.file_path not in seen:
                seen.add(h.file_path)
                out.append(h.file_path)
        return out


@dataclass
class Finding:
    """One review finding — mapped to a provider comment."""

    file_path: str           # new file path (where comment goes)
    line: int                # 1-indexed line in the new file (or old if deleted line)
    side: HunkSide = HunkSide.RIGHT
    severity: FindingSeverity = FindingSeverity.WARNING
    title: str = ""          # short summary (1-line)
    body: str = ""           # markdown body — full explanation
    suggestion: str | None = None  # optional code suggestion (for GitHub Apply)
    agent: str = ""          # 'architect' | 'security' | 'quality' | 'tests' — provenance
    rule_id: str = ""        # stable ID for dedup (e.g. 'arch.unused-import')
    confidence: float = 0.7  # 0.0-1.0 — used by verifier for FP filtering
    #: The agent's one-sentence derivation, written BEFORE the finding: the
    #: line, the value, and what happens. Borrowed from the tool ranked first
    #: on the Martian benchmark, which reports a 51% drop in false positives
    #: without losing recall from exactly this ordering — a model made to
    #: state its evidence first cannot assert a conclusion it has none for.
    #: Empty means the agent predates the field or had nothing to say.
    reasoning: str = ""
    #: How this finding was arrived at, which is a different question from how
    #: sure we are about it.
    #:
    #: `proven` — produced by reading files. A structural rule that matched, a
    #: constant found hardcoded in a sibling repository, a lock file that does
    #: not match its manifest. It carries a file, a line and the text, and a
    #: reader agrees or disagrees in seconds.
    #: `inferred` — a language model's judgement. Often right, occasionally
    #: not, and never checkable at a glance.
    #:
    #: `confidence` cannot express this. A float mixes "the grep matched" with
    #: "the model felt strongly", and once they share a scale the UI has no
    #: honest way to separate them — which matters because 20% false positives
    #: is where people stop reading a tool at all. Keeping the proven findings
    #: in their own section is what protects them from the inferred ones'
    #: reputation.
    evidence_kind: str = "inferred"

    @property
    def is_proven(self) -> bool:
        return self.evidence_kind == "proven"

    @property
    def dedup_key(self) -> tuple[str, int, str]:
        """Match similar findings across multiple agents."""
        return (self.file_path, self.line, self.rule_id)


@dataclass
class ReviewBatch:
    """All findings + summary for a PR — atomic unit for posting."""

    pull_request: PullRequest
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""        # markdown summary for the top-level PR comment
    verdict: ReviewVerdict = ReviewVerdict.COMMENT

    # Telemetry
    started_at: str = ""
    completed_at: str = ""
    elapsed_seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    agents_run: list[str] = field(default_factory=list)
    agents_failed: list[str] = field(default_factory=list)     # Stage 11 — was silently dropped before
    #: Stages switched off by policy for this run — today only "verifier".
    #: Distinct from `agents_failed` on purpose: a stage nobody asked to run
    #: is not a failure, and a review with no filter must not read as one
    #: that was filtered and found everything clean.
    agents_skipped: list[str] = field(default_factory=list)
    #: Why each failed agent failed — {agent: a sentence a user can act on}.
    #:
    #: The reason existed all along. `classify` produced a curated sentence
    #: per failure code, `AgentRunResult.error` carried it, and the
    #: orchestrator logged it and appended the agent's NAME to
    #: `agents_failed`. So the pull-request comment and the run row rendered
    #: byte-identical text for a timeout, an exhausted quota, a rejected key
    #: and a model that refused — four problems with four different owners and
    #: one sentence: "Check server logs for LLM errors", which is an
    #: instruction a SaaS user cannot follow.
    #:
    #: Keyed by agent because two agents can fail differently in one review,
    #: and "security and defect did not run" over one reason would be a new
    #: way of saying less than we know.
    agent_errors: dict[str, str] = field(default_factory=dict)
    #: rule_id → findings the prefilter's deny-list hid this run
    #: (`ReviewSettings.suppressed_rules`, or the repo policy's own list).
    #: By rule rather than a total, so the record can say WHAT was hidden:
    #: a filter that reports only "dropped 7" is the shape that let the LLM
    #: veto delete true positives for five runs while reading as a success.
    dropped_by_rule: dict[str, int] = field(default_factory=dict)
    #: The rest of what this run hid, by cause, so the record can answer
    #: "where did the other findings go" without the log. Exact copies of
    #: one finding from several agents; near-duplicates clustered into one
    #: comment; findings under the confidence floor; claims the parser
    #: refused for want of evidence (no reasoning, a file the PR does not
    #: touch); and what the LLM veto dropped when the policy ran it.
    dropped_duplicates: int = 0
    dropped_near_duplicates: int = 0
    dropped_low_confidence: int = 0
    dropped_no_evidence: int = 0
    #: Claims whose reasoning sentence complained that a test is missing,
    #: reaches too little, or mocks the thing away, instead of naming a
    #: defect (`reads_as_a_coverage_claim` in agents/base.py). Separate from
    #: `dropped_no_evidence` for the reason the paragraph above gives: on
    #: runG2 this cause alone accounts for 8 of 69 comments, and a record
    #: that folded it into the neighbour could not say so.
    dropped_coverage_claim: int = 0
    dropped_by_veto: int = 0
    skipped_files: list[str] = field(default_factory=list)
    cross_repo_callers: int = 0  # unique cross-repo blast radius
    # Stage 11 (BYOK) — sum across all agents; None if any agent had unknown model.
    cost_usd: float | None = None
    cost_source: str = "unknown"   # 'openrouter_actual' | 'litellm_estimate' | 'unknown' | 'mixed'
    #: Every parameter Celmis changed between what was asked and what was sent,
    #: for every agent of this run — a ceiling clamped to the model max, a
    #: reasoning word or a temperature the provider refused, a fallback model
    #: called. Merged from each AgentRunResult by the orchestrator's
    #: aggregation loop, FAILED agents included: an agent that was clamped and
    #: then died still made the adjustment. The banner below and the run row
    #: both read this one list, so the PR comment cannot name an adjustment
    #: the API says nothing about — the same rule `partial_banner` already
    #: keeps with `agents_failed`.
    parameter_adjustments: list[ParameterAdjustment] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = datetime.now(UTC).isoformat()

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.CRITICAL)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.INFO)

    # Agents whose failure invalidates an APPROVE verdict. If one of the LLM
    # finders never ran, we do not know whether the change is safe — the right
    # answer is COMMENT with a summary that names the missing signal, not a
    # silent APPROVE. This closes the Stage 11 false-negative gap.
    #
    # EVERY LLM FINDER, and the set went stale once already. It read
    # `{"architect", "security"}`, and the Phase-18 restructure retired
    # `architect` — so a review in which `defect` failed had no critical agent
    # in `agents_failed`, took the non-critical banner arm, and
    # `mark_complete` upgraded it to APPROVE. Reproduced:
    #
    #     agents_run=["cve","structural"] agents_failed=["defect","contract"]
    #       → failed_critical_agents []  → verdict APPROVE
    #
    # `defect` produces 60% of every confirmed finding on the benchmark. So
    # the agent that does most of the looking could die on every pull request
    # and the product would answer "approved" — the exact failure this set
    # exists to prevent, reintroduced by renaming its members.
    #
    # `test_a_dead_finder_cannot_approve` derives the expected membership from
    # the orchestrator's own LLM roster rather than restating it, so the next
    # rename cannot go stale silently.
    #
    # `architect` stays, and ONLY architect. Nothing dispatches it any more,
    # so it costs nothing live, and a historical batch rehydrated from a
    # pre-restructure row still marks its critical failure as critical.
    #
    # `quality` and `tests` are deliberately NOT here, and the first draft of
    # this fix had them — caught by `test_a_non_critical_gap_is_still_a_gap`,
    # which is right: those two were never critical, so adding them would
    # retroactively change what an old row MEANT. A quality agent's absence
    # never touched a verdict; a banner saying it did is the same species of
    # untrue claim as the "downgraded from APPROVE to COMMENT" line that test
    # was written to kill. Their remit moved into `defect`, and `defect` is
    # critical on its own account, not by inheritance.
    _CRITICAL_AGENTS = frozenset({
        "defect", "contract", "security",
        "architect",  # retired; was critical, so old rows keep their meaning
    })

    @property
    def failed_critical_agents(self) -> list[str]:
        """The failed agents a verdict actually depends on, in run order.

        The orchestrator used to re-spell `{"architect", "security"}` inline
        when it built the summary banner, so the set that decides the verdict
        and the set the pull request is told about were two literals that
        happened to match.
        """
        return [a for a in self.agents_failed if a in self._CRITICAL_AGENTS]

    @property
    def nothing_ran(self) -> bool:
        """True when every stage this run dispatched failed.

        The question `agents_failed` alone cannot answer. A run with one
        failure out of five has four agents' worth of signal in it; a run with
        five failures out of five has none, and the two were being reported
        with the same word. Read as "did anything answer?", not "did anything
        break?" — which is why it is `agents_run` that is tested, and why a
        deterministic stage such as breaking_change landing in `agents_run`
        is enough to make this False.

        Both lists empty is not this either: nothing was dispatched, so
        nothing can have failed. But it is not therefore fine. The
        early-return skips (draft PR, no hunks, policy off) do produce that
        shape and set `verdict = SKIPPED` themselves before returning — a
        per-repo policy whose `disabled_agents` names the whole roster
        produces the SAME shape while falling all the way through the
        pipeline, and an earlier revision of this docstring claimed the
        early returns covered it. They never did: that fall-through reached
        `compute_verdict` with no findings and came out APPROVE/COMPLETE — a
        green tick on a pull request no agent had looked at. Both-empty is
        its own case, `nothing_dispatched` below, and gets SKIPPED.
        """
        return bool(self.agents_failed) and not self.agents_run

    @property
    def nothing_dispatched(self) -> bool:
        """True when no stage was ever started — both rosters empty.

        Two paths leave this shape. The early-return skips (draft PR, no
        hunks, policy off) write their own summary and verdict and never
        reach the agent loop. And a per-repo policy whose `disabled_agents`
        names every agent makes `_run_agents_parallel` dispatch nothing, so
        the aggregation loop never runs and control falls through the FULL
        pipeline with both lists still empty. Neither is a failure — nothing
        errored — but neither is a review, and reading the shape as "no
        failures, therefore COMPLETE/APPROVE" is exactly how the second path
        used to approve pull requests nothing had read.
        """
        return not self.agents_run and not self.agents_failed

    @property
    def run_status(self) -> ReviewRunStatus:
        """COMPLETE, PARTIAL when a stage is missing, FAILED when all are,
        SKIPPED when none was ever started.

        Any failure, not just a critical one, makes a run PARTIAL: a quality
        agent that never answered still means the review the user is reading
        has a hole in it, and the row is the only place that can still say so
        once the run is over. Policy-disabled agents never reach
        `agents_failed` (see `_run_agents_parallel`), so switching an agent
        off does not make every run partial.

        The FAILED arm is the one this property was missing. It asked only
        whether anything had failed, never whether anything had succeeded, so
        a review in which every single agent errored — an expired API key, a
        provider outage, a quota wall, all of which hit every agent at once —
        came back as "partial", the status the product uses to mean "we
        looked nearly everywhere". Nothing looked anywhere.

        The SKIPPED arm closes the same hole from the other side. A run in
        which nothing was even dispatched — a per-repo policy disabling every
        agent, or an early skip (draft, no hunks, policy off) — has no
        failures in it, so it used to fall through to COMPLETE, and the row
        for a repository nobody reviews read exactly like the row for one
        that was. The `not self.findings` guard keeps this arm on the same
        condition as `compute_verdict`'s SKIPPED arm, so status and verdict
        cannot disagree about whether a review happened.
        """
        if self.nothing_ran:
            return ReviewRunStatus.FAILED
        if self.nothing_dispatched and not self.findings:
            return ReviewRunStatus.SKIPPED
        return (ReviewRunStatus.PARTIAL if self.agents_failed
                else ReviewRunStatus.COMPLETE)

    @property
    def adjustments_notice(self) -> str:
        """One line per adjustment the run carries — "" when it carries none.

        The second half of `partial_banner`. A review whose reasoning level
        was refused, or whose architect ran on the fallback model, is not
        PARTIAL — every stage answered — but it is not the review the
        operator configured either, and until this existed nothing told them:
        the fields that knew lived on LLMResult and AgentRunResult and reached
        neither the PR comment nor the API. Invisible self-healing is how a
        review quietly gets worse and nobody knows which knob to turn.

        Derived from `parameter_adjustments`, the same list the run row
        persists and the API serves, for the reason `partial_banner` gives for
        `agents_failed`: one source, so the comment cannot name an adjustment
        the API says nothing about. One line per KIND — the same adjustment
        made by three agents is one line naming three agents, because an
        operator fixes a ceiling once, not once per agent.
        """
        if not self.parameter_adjustments:
            return ""
        # Group identical adjustments across agents, first-seen order kept.
        # `repr` on the two values only because the key has to be hashable
        # and a requested value may be anything JSON can carry.
        grouped: dict[tuple, tuple[dict, list[str]]] = {}
        for raw in self.parameter_adjustments:
            a = adjustment_as_dict(raw)
            key = (a["parameter"], a["action"], repr(a["requested"]),
                   repr(a["sent"]), a["reason"], a["model"])
            _first, agents = grouped.setdefault(key, (a, []))
            if a["agent"] and a["agent"] not in agents:
                agents.append(str(a["agent"]))
        lines: list[str] = []
        for first, agents in grouped.values():
            parameter, action = first["parameter"], first["action"]
            requested, sent = first["requested"], first["sent"]
            reason, model = first["reason"], first["model"]
            who = ", ".join(agents)
            by = model or "the provider"
            if parameter == PARAM_MAX_OUTPUT_TOKENS and action == ADJUST_CLAMPED:
                line = (
                    f"max_output_tokens {requested} was above what {by} accepts "
                    f"and was cut to {sent}"
                    + (f" for {who}" if who else "")
                )
            elif parameter == PARAM_REASONING and action == ADJUST_DROPPED:
                line = (
                    f"reasoning {requested!r} was refused by {by} and the review "
                    f"ran without it" + (f" for {who}" if who else "")
                )
            elif parameter == PARAM_TEMPERATURE and action == ADJUST_DROPPED:
                line = (
                    f"temperature {requested} was refused by {by} and the review "
                    f"ran without it" + (f" for {who}" if who else "")
                )
            elif parameter == PARAM_MODEL and action == ADJUST_SWAPPED:
                line = (
                    f"{who or 'a stage'} ran on the fallback model {sent} "
                    f"instead of {requested or 'the configured model'}"
                )
            elif parameter == "graph_context":
                # The graph stage, not a model parameter — it rides this list
                # because this list is the road to the row and the banner.
                # A literal, not graph_context.PARAM_GRAPH_CONTEXT: that
                # module imports this one.
                what = "unavailable" if action == "unavailable" else f"partial ({sent})"
                line = f"graph context {what}"
            else:
                # A kind this wording does not know yet still gets said,
                # plainly, rather than dropped on the floor — silence is the
                # bug this property exists to end.
                line = (
                    f"{parameter} {requested!r} was {action} to {sent!r}"
                    + (f" for {who}" if who else "")
                )
            if reason:
                line = f"{line}: {reason}"
            lines.append(f"⚙ ADJUSTED — {line.rstrip('.')}.")
        return "\n".join(lines) + "\n\n"

    @property
    def partial_banner(self) -> str:
        """The gap notice prepended to `summary` — "" when nothing was missed.

        Where it actually surfaces: via `summary` it reaches the run row, the
        notification body and the MCP tool's payload — the places a human
        asks "what happened to that review?" after the fact. And
        `_format_summary` (src/review/providers/base.py) reads this property
        directly when it composes the posted PR comment. It has to read the
        property, not `summary`: for one whole wave it read neither, and a
        review in which nothing ran posted "💬 _No issues detected._" to the
        author while the row and the notification said FAILED.

        It reads the same `agents_failed` list that `run_status` reads and
        that the run row persists, so the banner can no longer name an agent
        the API says nothing about — or stay silent about one it does. And
        it ends with `adjustments_notice`, read off the same run's
        `parameter_adjustments`, under the same rule: what Celmis changed
        behind the operator's back is said where the gap is said, to the
        same reader, from the same record.

        The old wording claimed "Verdict downgraded from APPROVE to COMMENT",
        which was already untrue whenever findings had pushed the verdict to
        REQUEST_CHANGES, and would be untrue again for a non-critical agent
        that never touched the verdict at all.

        Its replacement, "Every other stage completed; its comments are
        below.", had the same disease one layer down: a fixed sentence
        describing a shape the run was assumed to have. Both halves could be
        false at once — the failed agent may have been the only stage that
        ran, and a review that finds nothing has no comments below anything.
        So the fallback now counts `agents_run` and `findings` and says what
        it counted. The critical arm is left alone: "cannot be an approval" is
        a claim about `compute_verdict`, which is right there refusing to
        approve, and it holds however many stages ran.
        """
        return self._gap_notice + self._degraded_notice + self.adjustments_notice

    @property
    def _degraded_notice(self) -> str:
        """A stage that was meant to run, did not, and left the review thinner.

        `agents_skipped` holds three different things by now — a veto the
        installation does not run by default, two tail stages a wall-clock
        budget stood down, and a stage inside an agent that fell over. Only
        the last is news, and the list of names cannot tell them apart.

        What tells them apart is whether we have a REASON. An agent reports
        the stages it could not finish together with why, and nothing else
        writes a reason for a skip: configuration does not have one, because
        nothing went wrong. So "in `agents_skipped` AND in `agent_errors`" is
        the test, and it needs no name matching — which is what would have
        turned this into a literal that stops being true the day a stage is
        renamed.

        Worth a line at all because the second pass of the defect finder is
        worth roughly 8pp of recall. A review that looked once used to publish
        as COMPLETE, with an APPROVE available to it, byte-identical to one
        that looked twice.
        """
        degraded = {
            stage: self.agent_errors[stage]
            for stage in self.agents_skipped
            if stage in self.agent_errors and stage not in self.agents_failed
        }
        if not degraded:
            return ""
        parts = ", ".join(
            f"{stage} ({why})" for stage, why in degraded.items()
        )
        return (
            f"ℹ This review is thinner than a full one: {parts}. The comments "
            f"below are real; there may be fewer of them than usual.\n\n"
        )

    @property
    def _failure_reasons(self) -> str:
        """" Because X." for each failed agent whose reason we can publish.

        What replaced "Check server logs for LLM errors" — an instruction a
        SaaS user cannot follow, printed identically for a timeout, an
        exhausted quota, a rejected key and a model that refused. Four
        problems, four different owners, one sentence.

        Empty when no failure carried a code with a curated row, and the
        banner then simply stops after the count. Silence is the correct
        ending for "we have no sentence for this yet"; the alternative is
        inventing one, which would dress an unknown failure as a familiar one
        and stop the next reader looking — the rule `_agent_error_text`
        already follows for the record.

        One clause per reason, because two agents can fail differently in one
        review and a single shared sentence would be a new way of saying less
        than we know. Grouped by reason rather than listed per agent, because
        three agents stopped by one exhausted quota is one fact, not three.
        """
        grouped: dict[str, list[str]] = {}
        for agent in self.agents_failed:
            reason = self.agent_errors.get(agent)
            if reason:
                grouped.setdefault(reason, []).append(agent)
        return "".join(
            f" {', '.join(agents)}: {reason}."
            for reason, agents in grouped.items()
        )

    @property
    def _gap_notice(self) -> str:
        """The missing-stage half of `partial_banner` — see there."""
        if self.nothing_dispatched and not self.findings:
            # Same condition as `run_status`'s SKIPPED arm and
            # `compute_verdict`'s SKIPPED arm: the three claims ship in one
            # run row and must not disagree. Only one path reaches banner
            # application with an empty roster — a per-repo policy whose
            # `disabled_agents` names every agent, the case that used to fall
            # through to APPROVE/COMPLETE with no banner at all. (The early
            # skips — draft, no hunks, policy off — write their own reason
            # into `summary` and return before any banner is applied, and are
            # never posted.) So the banner can name the one cause an operator
            # can actually act on.
            return (
                "⚠ REVIEW SKIPPED — no agent ran: every agent is disabled "
                "for this repository (see /admin/review-policies). This pull "
                "request has NOT been reviewed. Re-enable at least one agent "
                "to resume reviews.\n\n"
            )
        if not self.agents_failed:
            return ""
        failed = ", ".join(self.agents_failed)
        if self.nothing_ran and not self.findings:
            # Not a partial anything. There is no review under this notice.
            # The `findings` half of the test keeps this arm and
            # `compute_verdict`'s SKIPPED arm on the same condition, so the
            # banner cannot announce an unreviewed PR above a list of
            # comments; anything with findings falls through to the counted
            # wording below, which describes whatever it actually finds.
            return (
                f"⚠ REVIEW FAILED — {failed} did not run, and no other stage "
                f"produced an answer either. This pull request has NOT been "
                f"reviewed.{self._failure_reasons}\n\n"
            )
        blocked = self.failed_critical_agents
        if blocked:
            tail = (
                f"{', '.join(blocked)} "
                f"{'is a critical stage' if len(blocked) == 1 else 'are critical stages'}"
                f", so this review cannot be an approval."
            )
        else:
            ran = len(self.agents_run)
            stages = "stage" if ran == 1 else "stages"
            found = len(self.findings)
            tail = (
                f"{ran} other {stages} completed; "
                f"{'its' if ran == 1 else 'their'} {found} "
                f"{'comment is' if found == 1 else 'comments are'} below."
                if found else
                f"The {ran} other {stages} that ran found nothing to comment on."
            )
        return (
            f"⚠ PARTIAL REVIEW — {failed} "
            f"did not run. {tail}{self._failure_reasons}\n\n"
        )

    def apply_partial_banner(self) -> None:
        """Prepend the gap notice to the summary, at most once."""
        banner = self.partial_banner
        if banner and not (self.summary or "").startswith(banner):
            self.summary = banner + (self.summary or "")

    def compute_verdict(self) -> ReviewVerdict:
        """Auto-set verdict based on findings severity distribution + agent health."""
        # Stage 14: any BLOCKING compliance failure is a hard REJECT — never
        # downgradable to COMMENT/APPROVE even if severity=warning elsewhere.
        blocking_compliance = any(
            f.agent == "compliance"
            and f.severity in {FindingSeverity.CRITICAL, FindingSeverity.ERROR}
            for f in self.findings
        )
        if blocking_compliance:
            return ReviewVerdict.REQUEST_CHANGES
        # Nothing answered, so there is nothing to have a verdict about. Every
        # other member of this enum is a claim about the code: APPROVE says it
        # is clean, COMMENT says there are findings worth reading (and renders
        # "_No issues detected._" underneath when there are none), and
        # REQUEST_CHANGES says we found blockers — which would make our own
        # provider outage into the author's merge block. SKIPPED is the only
        # one that is a claim about the review instead of about the code:
        # "nothing reviewed". Two shapes land here. `nothing_ran`: every
        # dispatched agent errored. `nothing_dispatched`: no agent was ever
        # started, which is what a policy disabling the whole roster looks
        # like from inside the batch — the shape that used to slide past this
        # arm (it has no failures) and come out APPROVE. `run_status` tells
        # them apart: FAILED for the first, SKIPPED for the second. Providers
        # map an unrecognised verdict to a plain non-blocking comment, so the
        # pull request is neither approved nor blocked while the banner
        # explains why.
        if (self.nothing_ran or self.nothing_dispatched) and not self.findings:
            return ReviewVerdict.SKIPPED
        # If any critical agent failed, never approve — degrade to COMMENT so the
        # summary surfaces the miss instead of hiding it.
        if self.failed_critical_agents:
            if self.critical_count > 0 or self.error_count >= 3:
                return ReviewVerdict.REQUEST_CHANGES
            return ReviewVerdict.COMMENT
        if self.critical_count > 0 or self.error_count >= 3:
            return ReviewVerdict.REQUEST_CHANGES
        if self.warning_count > 0 or self.error_count > 0:
            return ReviewVerdict.COMMENT
        return ReviewVerdict.APPROVE

    def mark_complete(self) -> None:
        from time import time
        self.completed_at = datetime.now(UTC).isoformat()
        if self.started_at:
            try:
                t0 = datetime.fromisoformat(self.started_at).timestamp()
                self.elapsed_seconds = time() - t0
            except (ValueError, TypeError):
                pass
        # "COMMENT with nothing to say" normally means APPROVE — but not when
        # compute_verdict() chose COMMENT precisely *because* a critical agent
        # never produced a verdict. Upgrading that case undid the guard three
        # lines of comment above it were written to provide: an agent whose
        # reply could not be read has no findings by definition, so this is
        # exactly the path a broken architect or security run takes.
        if (
            self.verdict == ReviewVerdict.COMMENT
            and not self.findings
            and not self.failed_critical_agents
        ):
            self.verdict = ReviewVerdict.APPROVE
