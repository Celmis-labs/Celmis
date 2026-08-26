"""ReviewOrchestrator — full PR review pipeline.

Composes:
    1. Provider — fetch PR + diff
    2. Graph context build — uses the existing FalkorDB graph + cross-repo edges
    3. Multi-agent run (parallel via ThreadPoolExecutor — every LLM call is I/O bound)
    4. Prefilter (always: rule deny-list, dedup, near-duplicate clustering,
       confidence floor, severity sort) + the LLM veto (by policy)
    5. ReviewBatch assembly + verdict computation
    6. Provider — post review (or dry-run)

Designed for local CLI use (Phase 17.6) + webhook handler (Phase 17b).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from src.review.agents import (
    AgentContext,
    ContractAgent,
    CveAgent,
    DefectAgent,
    SecurityAgent,
    StructuralAgent,
    VerifierAgent,
)
from src.review.agents.base import AgentRunResult, LLMReviewAgent, ReviewAgent
from src.review.graph_context import build_graph_context
from src.review.models import Finding, PullRequest, ReviewBatch, ReviewVerdict
from src.review.providers import get_provider_for
from src.review.providers.base import (
    PullRequestProvider,
    PullRequestProviderError,
)
from src.review.settings import (
    AgentLLMSettings,
    ReviewSettings,
    get_review_settings,
)

logger = logging.getLogger(__name__)


@dataclass
class ReviewRunResult:
    batch: ReviewBatch
    posted: bool
    provider_response: dict


class _BudgetExhausted(Exception):
    """The review passed its wall-clock budget; remaining stages stand down.

    Its own type, and not TimeoutError, for one reason: the stages below catch
    broad exceptions and record a FAILURE. A stage the orchestrator decided not
    to run is skipped, not fallen over, and `agents_skipped` exists precisely
    so a history row can tell those apart. Raising anything they would treat as
    a fault would report an operator's budget as an outage.
    """


class ReviewOrchestrator:
    """End-to-end PR review."""

    def __init__(
        self,
        settings: ReviewSettings | None = None,
        *,
        agents: list[ReviewAgent] | None = None,
        verifier: VerifierAgent | None = None,
    ) -> None:
        self.settings = settings or get_review_settings()
        # `is not None`, not `or`: an empty roster is a roster. `agents or
        # …` read "the caller wants no agents" as "the caller said nothing"
        # and silently dispatched the full default five — the absent-vs-empty
        # confusion this codebase keeps finding, here turning an explicit
        # decision into its opposite.
        self.agents = self._default_agents() if agents is None else list(agents)
        self.verifier = verifier or VerifierAgent()
        #: The drift report from the last review, as data. Kept so the caller
        #: can persist it: the finding is deterministic, and routing it to the
        #: user only through a model's summary is the one thing this product
        #: should never do with it.
        self._last_drift_facts: dict | None = None

    @staticmethod
    def _default_agents() -> list[ReviewAgent]:
        # NB: the names below are what a policy's `disabled_agents` may hold —
        # keep `TOGGLEABLE_AGENTS` in src/api/routers/review_policies.py in sync.
        return [
            DefectAgent(),
            ContractAgent(),
            SecurityAgent(),
            StructuralAgent(),
            # Deterministic like StructuralAgent — no LLM, no tokens. Reads
            # pr.hunks, pr.raw_diff and pr.skipped_files only. Fails into
            # agents_failed when blind or timed out; a PR without dependency
            # changes is a clean zero with no scanner run. NOT in
            # _CRITICAL_AGENTS on purpose: an install without the osv binary
            # and offline would otherwise degrade EVERY review to COMMENT —
            # the partial banner already names the missing signal.
            CveAgent(),
        ]

    def review(
        self,
        provider_name: str,
        repo: str,
        pr_number: int,
        *,
        dry_run: bool = False,
        post_comments: bool = True,
        provider: PullRequestProvider | None = None,
        user_id: str = "default",
        workspace_id: str = "default",
    ) -> ReviewRunResult:
        """Run full review pipeline.

        Args:
            provider_name: 'github' | 'gitlab' | 'bitbucket'
            repo: 'owner/name'
            pr_number: PR number / MR iid / Bitbucket PR id
            dry_run: simulate without actual comment posting (still computes findings)
            post_comments: whether to post comments (false → just return batch)
            provider: optional pre-built provider (for tests)
        """
        t0 = time.time()
        # Parallelism gauge for the resource sampler — how many reviews run
        # at this moment is exactly what capacity docs need.
        from src.ops.telemetry import review_finished, review_started
        review_started()
        try:
            return self._review_impl(
                provider_name, repo, pr_number, dry_run=dry_run,
                post_comments=post_comments, provider=provider,
                user_id=user_id, workspace_id=workspace_id, t0=t0,
            )
        finally:
            review_finished()

    def _review_impl(
        self, provider_name: str, repo: str, pr_number: int, *,
        dry_run: bool, post_comments: bool, provider,
        user_id: str, workspace_id: str, t0: float,
    ) -> ReviewRunResult:
        if provider is None:
            provider = get_provider_for(
                provider_name, user_id=user_id, workspace_id=workspace_id,
            )

        try:
            pr = provider.fetch_pull_request(repo, pr_number)
        except PullRequestProviderError:
            raise
        finally:
            pass  # provider closed by caller

        # ── Load per-repo policy (Stage 10) ──
        policy = self._load_policy(pr.local_slug)

        batch = ReviewBatch(pull_request=pr)

        # Hard skip — policy disabled for this repo
        if policy is not None and not policy["enabled"]:
            logger.info("review_skipped reason=policy_disabled pr=%d repo=%s",
                        pr.number, pr.repo_slug)
            batch.summary = (
                "Review skipped — AI reviewer is disabled for this repo "
                "(see /admin/review-policies)."
            )
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        # Hard skip — base branch not in target list
        if (policy is not None
                and policy["target_branches"]
                and pr.base_ref
                and pr.base_ref not in policy["target_branches"]):
            logger.info(
                "review_skipped reason=branch_not_targeted pr=%d base=%s allowed=%s",
                pr.number, pr.base_ref, policy["target_branches"],
            )
            batch.summary = (
                f"Review skipped — base branch '{pr.base_ref}' is not in the "
                f"configured target list {policy['target_branches']}."
            )
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        # ── Build agent context (passes custom_rules from policy + matching folder_rules) ──
        context = self._build_context(
            pr, policy=policy, user_id=user_id, workspace_id=workspace_id,
        )

        batch.cross_repo_callers = context.cross_repo_callers_count
        # What the graph could not say rides the same list as a dropped
        # temperature: `adjustments_notice` prints it in the PR banner and
        # the run row persists it, so a review without a blast radius no
        # longer reads like one with an empty radius. On the benchmark this
        # was measured on, 161 runs had no graph and nothing anywhere said so.
        if context.graph_note is not None:
            batch.parameter_adjustments.append(context.graph_note)

        # ── Skip empty/draft/binary PRs ──
        if pr.is_draft:
            logger.info("review_skipped reason=draft pr=%d", pr.number)
            batch.summary = "PR is draft — review skipped."
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        # A diff too large to review, refused as a refusal rather than
        # silently truncated. `max_diff_size_bytes` said "skip review if
        # larger" in its own comment and was read by NOTHING — so an
        # oversized PR went all the way to the agents, where
        # `_format_diff_for_prompt` cut it to 50k characters and said so
        # nowhere the reader could see. A review of the first fifth of a
        # change, presented as a review of the change.
        cap = int(getattr(self.settings, "max_diff_size_bytes", 0) or 0)
        # Bytes, because the setting says bytes. `len()` on a str counts code
        # points, so a diff of non-ASCII source — Cyrillic identifiers, CJK
        # strings, an emoji in a test fixture — would measure up to four times
        # under its real size and slip past a cap set for the transport that
        # actually carries it.
        raw_len = len((pr.raw_diff or "").encode("utf-8"))
        if cap and raw_len > cap:
            reason = (
                f"Diff is {raw_len:,} bytes, over the {cap:,}-byte limit for a "
                f"single review (REVIEW_MAX_DIFF_SIZE_BYTES). This pull request "
                f"has NOT been reviewed — split it, or raise the limit."
            )
            logger.info("review_skipped reason=diff_too_large pr=%d bytes=%d cap=%d",
                        pr.number, raw_len, cap)
            batch.summary = reason
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        if not pr.hunks:
            if not pr.raw_diff or not pr.raw_diff.strip():
                reason = "PR has no diff content (empty change-set)."
            elif pr.skipped_files:
                # Show first 20 skipped paths so user understands why
                preview = "\n".join(f"  - {p}" for p in pr.skipped_files[:20])
                more = (
                    f"\n  …and {len(pr.skipped_files) - 20} more"
                    if len(pr.skipped_files) > 20 else ""
                )
                reason = (
                    f"All {len(pr.skipped_files)} changed files were filtered "
                    f"out by skip patterns (lockfiles, binaries, build dirs, etc.):"
                    f"\n{preview}{more}"
                )
            else:
                reason = "Diff parser returned no hunks (parser couldn't read the diff format)."
            logger.info(
                "review_skipped reason=no_hunks pr=%d skipped=%d diff_bytes=%d",
                pr.number, len(pr.skipped_files), len(pr.raw_diff),
            )
            batch.summary = reason
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        # ── Engine selection (workspace setting): the platform around the
        # review (policy gates, posting, persistence, verdict) is identical;
        # only the "brain" differs.
        #   api          → 5-agent LiteLLM pipeline (BYOK keys + chosen models)
        #   claude_code  → one headless Claude Code run (subscription token,
        #                  or the workspace's Anthropic API key as fallback)
        engine = "api"
        try:
            from src.api.routers.llm import _load_workspace_config
            engine = str(_load_workspace_config(workspace_id).get("review_engine") or "api")
        except Exception:  # noqa: BLE001
            pass

        if engine == "claude_code":
            # DRIFT REACHES THIS ENGINE TOO, and it did not.
            #
            # `_build_context` above already ran the detector and put the
            # result on `context.cross_repo_drift` — the grep happened, the
            # facts were recorded, `drift_json` was written. The engine simply
            # was not handed the block. So a workspace on the Claude Code
            # engine reviewed a change to a shared constant with no idea the
            # constant was shared, while the run reported drift facts the
            # reviewer never saw.
            #
            # Worse than not running it: the run record says a drift check
            # happened, because one did.
            from src.review.claude_engine import run_claude_review
            cr = run_claude_review(
                pr, user_id=user_id, workspace_id=workspace_id,
                custom_rules=context.custom_rules,
                graph_summary=context.graph_summary,
                cross_repo_drift=context.cross_repo_drift,
            )
            if cr.error:
                logger.warning("claude_engine_failed pr=%d err=%s", pr.number, cr.error)
                # Recorded on the batch so the API can name the stage that did
                # not run. This engine IS the review, so a failure here leaves
                # `agents_run` empty and the row comes out FAILED, not
                # PARTIAL — there is no other stage for it to be partial to.
                # No `apply_partial_banner()` here on purpose: the message
                # assigned below already names the one stage there is and
                # carries the provider's own error, which the generic banner
                # would replace with less.
                batch.agents_failed.append("claude_code")
                batch.summary = f"⚠ Claude Code review failed: {cr.error}"
            else:
                batch.findings = cr.findings
                batch.summary = cr.summary
                batch.agents_run.append("claude_code")
            # Outside the branch on purpose: `run_claude_review` reports the
            # turns it had already paid for when it gives up mid-session, and
            # assigning the cost only in the success arm threw exactly that
            # away — the same leak as the agent loop below, in the engine that
            # bills per session rather than per token.
            batch.cost_usd = cr.cost_usd
            # The engine now says WHOSE money ran the review — claude_code_subscription
            # or claude_code_api_key — and overwriting that with the flat engine
            # name here was the one line that made the distinction invisible.
            if not (cr.cost_source or "").startswith("claude_code"):
                batch.cost_source = "claude_code"
            else:
                batch.cost_source = cr.cost_source
            batch.verdict = batch.compute_verdict()
            batch.mark_complete()
            logger.info(
                "review_complete engine=claude_code pr=%d status=%s findings=%d "
                "verdict=%s turns=%d",
                pr.number, batch.run_status.value, len(batch.findings),
                batch.verdict.value, cr.turns,
            )
            return self._finish_review(
                batch, pr, provider, provider_name,
                dry_run=dry_run, post_comments=post_comments,
                user_id=user_id, workspace_id=workspace_id, t0=t0,
            )

        # ── Run agents in parallel (minus the ones the policy switched off) ──
        disabled_agents = {
            str(a).strip().lower()
            for a in ((policy or {}).get("disabled_agents") or [])
        }
        # A name that no longer names an agent is SAID, not silently ignored.
        # Policies written before the restructure may still carry "architect",
        # "quality" or "tests"; matching them against nothing would quietly
        # turn "I switched the architect off" into "everything runs". They are
        # deliberately NOT mapped onto the successor agents either — a policy
        # that disabled the old tests sidecar must not disable the main
        # finder that inherited its remit. The operator re-decides.
        roster = {a.name for a in self.agents} | {"verifier"}
        unknown = disabled_agents - roster
        if unknown:
            logger.warning(
                "disabled_agents_unknown names=%s roster=%s — these entries "
                "disable nothing; the agent names changed in the Phase-18 "
                "restructure (architect→contract, quality/tests→defect) and "
                "old names are not mapped onto their successors on purpose",
                sorted(unknown), sorted(roster))
        agent_results = self._run_agents_parallel(
            context, disabled_agents=disabled_agents,
        )

        # Aggregate findings + Stage 11 cost accounting.
        # NB: previously silently `continue`d on error, causing critical-agent
        # failures (e.g. security 500'd) to still yield APPROVE verdicts.
        # We now record `agents_failed` and `compute_verdict()` refuses to
        # approve if a critical agent is missing.
        all_findings: list[Finding] = []
        cost_sources: set[str] = set()
        any_unknown_cost = False
        cost_sum = 0.0
        for r in agent_results:
            # The ledger first, before anything can `continue` past it. A
            # failed agent still sent its prompt and still got billed for it;
            # this loop used to skip straight to the next result on `r.error`,
            # so every token an agent spent on its way to failing left no
            # trace in the run's cost. `_generate_and_parse` now spends up to
            # TWO calls on an agent that ends up failing and deliberately sums
            # both — dropping the result here is what turned that honesty into
            # a bigger hole rather than a smaller one.
            batch.tokens_in += r.tokens_in
            batch.tokens_out += r.tokens_out
            if r.cost_usd is not None:
                cost_sum += r.cost_usd
            elif r.error is None or r.tokens_in or r.tokens_out:
                # No cost figure makes the run's total a guess — but only if
                # this agent actually spent something. An agent that failed
                # before a single token left the process (no key configured
                # for the workspace, unknown model) cost exactly nothing, and
                # nothing is a known quantity: poisoning `cost_usd` to None
                # over it would hide the real spend of every other agent.
                any_unknown_cost = True
            if r.cost_source:
                cost_sources.add(r.cost_source)
            # Also before the `continue`, and for the same reason as the
            # ledger: an agent that was clamped, or had its reasoning dropped,
            # or was handed to the fallback model and THEN died still made
            # the adjustment, and the operator reading a FAILED agent's row
            # needs to know the request was not the one they configured.
            batch.parameter_adjustments.extend(
                getattr(r, "parameter_adjustments", None) or ()
            )
            # Claims the parser refused for want of evidence — no reasoning
            # sentence, or a file the PR does not touch. Summed before the
            # `continue` like the adjustments: an agent whose every claim
            # was refused and then failed still refused them.
            batch.dropped_no_evidence += int(
                getattr(r, "dropped_no_evidence", 0) or 0
            )
            # The second parse-time refusal, summed here for the same reason:
            # a reasoning sentence that complained about the tests instead of
            # naming a defect. Kept as its own total so the run record can
            # say which gate the missing comments went through.
            batch.dropped_coverage_claim += int(
                getattr(r, "dropped_coverage_claim", 0) or 0
            )
            # How long the agent took, said out loud. `AgentRunResult` has
            # carried `elapsed_seconds` all along and nothing ever read it, so
            # the only timing this product recorded was the whole review's —
            # and when the per-call deadline turned out to be too short, the
            # number to replace it with had to be argued from review totals
            # instead of measured. A field computed and discarded is a field
            # that was not there.
            logger.info(
                "agent_finished agent=%s elapsed=%.1fs model=%s findings=%d%s",
                r.agent, float(getattr(r, "elapsed_seconds", 0.0) or 0.0),
                getattr(r, "model_used", None) or "-", len(r.findings),
                f" error={r.error}" if r.error else "",
            )
            # A stage inside the agent that did not run — the second pass of
            # a two-pass finder, today. Read before the `continue` like the
            # ledger above it: an agent can skip a stage and then fail, and
            # both are true.
            for stage, why in (getattr(r, "skipped_stages", None) or {}).items():
                if stage not in batch.agents_skipped:
                    batch.agents_skipped.append(stage)
                # The reason rides in the same map the failed agents' do, so
                # the banner has one place to read and cannot name a stage the
                # record says nothing about.
                if why:
                    batch.agent_errors[stage] = why
            if r.error:
                logger.warning("agent_error agent=%s code=%s err=%s",
                               r.agent, getattr(r, "error_code", None) or "-",
                               r.error)
                batch.agents_failed.append(r.agent)
                # The curated sentence, NOT `r.error`. The record's prose keeps
                # a provider's own message verbatim for a failure we cannot
                # name; the banner goes into a pull-request comment, where
                # anyone with access to the repository reads it. A code with a
                # row gets the sentence written for it; one without gets
                # nothing and the banner stays generic.
                from src.llm.errors import curated_reason
                reason = curated_reason(getattr(r, "error_code", None))
                if reason:
                    batch.agent_errors[r.agent] = reason
                continue
            all_findings.extend(r.findings)
            batch.agents_run.append(r.agent)

        # ── Prefilter (always), then the LLM veto (by policy) ──
        #
        # The deterministic half — rule deny-list, exact dedup, near-duplicate
        # clustering, confidence floor, severity sort — runs on EVERY review.
        # It used to live inside `verify()`, so a policy that switched the
        # verifier off also switched off the dedup and the sort:
        # `batch.findings = list(all_findings)` handed the providers an
        # unsorted, un-deduped list for their findings[:max_inline_comments]
        # cap to truncate, and a critical at position 21 fell off while four
        # copies of one warning posted. "verifier" in `disabled_agents` now
        # means exactly "no LLM pass" — never "no dedup".
        #
        # The veto is a stage, not an agent — it runs AFTER them, over their
        # combined output — so the parallel runner's filter never sees it and
        # the check has to live here. When it is off the run reports
        # "verifier" among the skipped names, so a review with no veto is
        # never mistaken for one that was vetted and found everything clean.
        pre = self.verifier.prefilter(
            all_findings,
            suppressed_rules=self._suppressed_rules(policy, self.settings),
        )
        batch.dropped_by_rule = dict(pre.dropped_by_rule)
        batch.dropped_duplicates = pre.dropped_dedup
        batch.dropped_near_duplicates = pre.dropped_near_duplicate
        batch.dropped_low_confidence = pre.dropped_low_confidence
        veto_on, veto_reason = self._verifier_enabled(policy, disabled_agents)
        if not veto_on:
            batch.findings = list(pre.kept)
            batch.agents_skipped.append("verifier")
            logger.info(
                "verifier_llm_skipped reason=%s findings=%d "
                "by_rule=%d dedup=%d near_dup=%d low_conf=%d",
                veto_reason, len(pre.kept), sum(pre.dropped_by_rule.values()),
                pre.dropped_dedup, pre.dropped_near_duplicate,
                pre.dropped_low_confidence,
            )
        else:
            v_result = self.verifier.llm_pass(pre.kept, context)
            batch.findings = v_result.kept
            batch.dropped_by_veto = v_result.dropped_llm_filter
            batch.tokens_in += v_result.tokens_in
            batch.tokens_out += v_result.tokens_out
            if v_result.error:
                # FAILED, not skipped. The two words are the distinction
                # `agents_skipped` was built for: "verifier" lands in the
                # skipped list when a policy switched the veto off — a
                # decision — and here, where it was asked to run and fell
                # over. The batch is the same either way: every finding kept.
                # What differs is whose problem it is.
                #
                # Not critical, so it does not block an approval, and that is
                # right: a veto that did not run leaves MORE findings
                # standing, never fewer. But the review it did not filter is
                # by definition the noisiest one, and the author reading that
                # noise had no way to know it was never filtered.
                logger.warning("verifier_failed reason=%s findings=%d unfiltered",
                               v_result.error, len(v_result.kept))
                batch.agents_failed.append("verifier")

        # ── The wall-clock budget, checked HERE and not enforced by killing
        # anything mid-flight.
        #
        # `timeout_seconds` named itself "total budget per PR review" and was
        # read by no code path at all. Enforcing it as written would have been
        # worse than leaving it dead: measured over 175 real reviews the old
        # 300-second default would have cut 14.3% of them.
        #
        # So the deadline is a STAGE GATE. The agents have already answered by
        # the time it is read; what it can still save is the tail — breaking
        # change, compliance, and the posting round-trip — on a review that has
        # already run long. The findings in hand are kept and posted, the
        # skipped stages are named in `agents_skipped` (which the run row now
        # persists), and the reader is told. A review cut short says so; it
        # does not quietly return fewer findings.
        budget = int(getattr(self.settings, "timeout_seconds", 0) or 0)
        spent = time.time() - t0
        over_budget = bool(budget) and spent > budget
        if over_budget:
            logger.warning(
                "review_over_budget pr=%d spent=%.0fs budget=%ds — skipping the "
                "remaining stages and posting what the agents found",
                pr.number, spent, budget,
            )
            for stage in ("breaking_change", "compliance"):
                if stage not in batch.agents_skipped:
                    batch.agents_skipped.append(stage)
            batch.summary = (
                f"⚠ REVIEW CUT SHORT — this review passed its {budget}s budget "
                f"at {spent:.0f}s, so breaking-change and compliance did not "
                f"run. The findings below are what the agents produced. Raise "
                f"REVIEW_TIMEOUT_SECONDS if this is normal for your repository."
                + ("\n\n" + batch.summary if batch.summary else "")
            )

        # ── Breaking-change detector (Stage 15) — cheap regex + graph
        # calls. Runs BEFORE compliance so its findings can be part of
        # the "one call per matching rule" evaluation if the user writes
        # a compliance rule that references breaking-change severity.
        try:
            if over_budget:
                raise _BudgetExhausted
            from src.review.breaking_change import run_breaking_change
            bc_result = run_breaking_change(context)
            if bc_result.findings:
                batch.findings.extend(bc_result.findings)
                batch.tokens_in += bc_result.tokens_in
                batch.tokens_out += bc_result.tokens_out
                batch.agents_run.append("breaking_change")
        except _BudgetExhausted:
            pass  # already named in agents_skipped and in the banner
        except Exception as exc:  # noqa: BLE001
            logger.warning("breaking_change_failed err=%s", exc)

        # ── Compliance agent (Stage 14) — one LLM call per matching rule.
        # Findings tagged agent="compliance" flow through the verdict
        # layer; any blocking failure downgrades APPROVE to REJECT.
        try:
            if over_budget:
                raise _BudgetExhausted
            from src.review.compliance import run_compliance
            c_result = run_compliance(context)
            batch.findings.extend(c_result.findings)
            batch.tokens_in += c_result.tokens_in
            batch.tokens_out += c_result.tokens_out
            if c_result.findings:
                batch.agents_run.append("compliance")
        except _BudgetExhausted:
            pass  # skipped, not failed — the two are different rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("compliance_agent_failed err=%s", exc)
            batch.agents_failed.append("compliance")

        # Finalise Stage 11 cost. None when any agent had an unknown model.
        batch.cost_usd = None if any_unknown_cost else round(cost_sum, 6)
        batch.cost_source = (
            "unknown" if any_unknown_cost and not cost_sources
            else next(iter(cost_sources)) if len(cost_sources) == 1
            else "mixed"
        )

        # ── Verdict + completion ──
        batch.verdict = batch.compute_verdict()
        # The gap notice for the human reading the pull request. It used to be
        # composed here from a second copy of the critical-agent set, so the
        # prose and the verdict logic were two literals that agreed by luck —
        # and neither of them survived the run, which is why nothing
        # downstream could name the agent that failed. Both now read
        # `batch.agents_failed`, and so does the row we persist.
        batch.apply_partial_banner()
        batch.mark_complete()

        logger.info(
            "review_complete pr=%d status=%s findings=%d verdict=%s elapsed=%.1fs "
            "agents_run=%s agents_failed=%s adjustments=%d",
            pr.number, batch.run_status.value, len(batch.findings),
            batch.verdict.value, batch.elapsed_seconds,
            ",".join(batch.agents_run) or "-",
            ",".join(batch.agents_failed) or "-",
            len(batch.parameter_adjustments),
        )

        return self._finish_review(
            batch, pr, provider, provider_name,
            dry_run=dry_run, post_comments=post_comments,
            user_id=user_id, workspace_id=workspace_id, t0=t0,
        )

    def _finish_review(
        self, batch: ReviewBatch, pr: PullRequest, provider, provider_name: str,
        *, dry_run: bool, post_comments: bool, user_id: str, workspace_id: str,
        t0: float,
    ) -> ReviewRunResult:
        """Shared tail for every engine: reviewer assignment, notifications,
        comment posting. The brain differs; the plumbing doesn't."""
        # ── Auto-reviewer assignment (Stage 16) — via ownership snapshot.
        # Only fires for real posted reviews (not dry-run) so we don't
        # spam @-mentions during test runs.
        if post_comments and not dry_run:
            try:
                from src.review.reviewer_assignment import assign_reviewers_by_ownership
                assign_reviewers_by_ownership(
                    provider=provider_name, repo=pr.repo,
                    # The provider path and the snapshot key are different
                    # addresses for the same repository.
                    repo_slug=pr.local_slug,
                    pr_number=pr.number,
                    changed_files=pr.changed_files,
                    author=pr.author, user_id=user_id, workspace_id=workspace_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_reviewer_failed err=%s", exc)

        # ── Notify via channels (Stage 15). Non-blocking on failure.
        try:
            from src.notifications import notify
            sev = _severity_for_verdict(batch)
            notify(
                workspace_id=workspace_id,
                event="review_complete", repo_slug=pr.repo,
                title=f"Review {batch.verdict.value.upper()} · PR #{pr.number}: {pr.title}",
                body_md=(
                    f"**{batch.critical_count}** critical · "
                    f"**{batch.error_count}** error · "
                    f"**{batch.warning_count}** warn · "
                    f"**{batch.info_count}** info\n\n"
                    + (batch.summary or "")[:1500]
                ),
                severity=sev, link_url=pr.url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("review_notify_failed err=%s", exc)

        # ── Post comments ──
        posted = False
        provider_response: dict = {}
        if post_comments:
            try:
                provider_response = provider.post_review(batch, dry_run=dry_run)
                posted = not dry_run
            except PullRequestProviderError as exc:
                logger.error("review_post_failed err=%s", exc)
                provider_response = {"error": str(exc)}

        return ReviewRunResult(batch=batch, posted=posted, provider_response=provider_response)

    # ─── Agent context build ───────────────────────────────────

    def _build_context(
        self,
        pr: PullRequest,
        *,
        policy: dict | None = None,
        user_id: str = "default",
        workspace_id: str = "default",
    ) -> AgentContext:
        """Build AgentContext with graph blast radius + cross-repo callers.

        Strategy:
            1. `build_graph_context` — the changed symbols of EVERY changed file,
               their callers, the cross-repo edges into the changed files
               (src/review/graph_context.py), as a full summary for the
               agents that reason about impact and a brief for the rest
            2. No graph — agents still work with the diff, and the context
               carries a note the run record and the PR banner print, so a
               review without a blast radius never reads like one with an
               empty radius
            3. Stage 10 — inject per-repo policy (`custom_rules`)
        """
        graph = build_graph_context(pr, workspace_id=workspace_id)
        drift_md = self._build_cross_repo_drift(pr, workspace_id=workspace_id)
        custom_rules = self._build_custom_rules(pr, policy)
        mcp_evidence = self._build_mcp_evidence(pr, user_id=user_id)
        if mcp_evidence:
            # Prepend MCP evidence to custom_rules so every agent sees it
            # (custom_rules already broadcast via base._compose_effective_system_prompt).
            custom_rules = (
                mcp_evidence + ("\n\n" + custom_rules if custom_rules else "")
            )
        llm_client, agent_llm = self._build_llm_client(user_id, workspace_id, policy)

        return AgentContext(
            pull_request=pr,
            graph_summary=graph.summary,
            graph_brief=graph.brief,
            graph_note=graph.note,
            cross_repo_callers_count=graph.cross_repo_callers_count,
            repo_overview=self._load_repo_overview(pr),
            style_guide=self._load_style_guide(pr),
            cross_repo_drift=drift_md,
            custom_rules=custom_rules,
            user_id=user_id,
            workspace_id=workspace_id,
            llm_client=llm_client,
            agent_llm=agent_llm,
            repo_agent_prompts=dict((policy or {}).get("agent_prompt_overrides") or {}),
        )

    # ─── Stage 11: BYOK LLM client + per-agent model resolution ──

    def _build_llm_client(
        self,
        user_id: str,
        workspace_id: str,
        policy: dict | None,
    ) -> tuple[object | None, dict[str, AgentLLMSettings]]:
        """Build a user-scoped LLMClient + the per-agent LLM settings map.

        Returns (None, {}) if litellm is unavailable — agents fall back to
        the legacy Gemini path so nothing breaks during rollout.

        The map used to hold model strings only. It holds an `AgentLLMSettings`
        now because the output ceiling and the reasoning level travel with the
        model and are resolved by the same chain — one resolver, one place to
        look, rather than three maps that agree by hand. The client's
        `resolve_model` still takes a model string: it is the transport's
        interface and none of its business where the string came from.
        """
        try:
            from src.llm.client import build_llm_client
            from src.review.settings import (
                REVIEW_AGENTS,
                get_review_settings,
                resolve_agent_llm,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm_client_unavailable err=%s", exc)
            return None, {}

        rs = get_review_settings()

        # Stage 11 (Kodus-style consolidated LLM config): workspace-wide
        # model is set on /settings/llm. The full fall-through per agent is
        # documented at `resolve_agent_llm`.
        try:
            from src.api.routers.llm import _load_workspace_config
            workspace_cfg = _load_workspace_config(workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("workspace_llm_config_unavailable err=%s", exc)
            workspace_cfg = {}

        # `compliance` is in this map now. It never was, and `resolve_model`'s
        # default arm quietly handed it the env architect model — so a
        # workspace that had chosen a model on /settings/llm got that model
        # for five agents and something else for the sixth, with nothing
        # anywhere saying so.
        by_agent: dict[str, AgentLLMSettings] = {
            agent: resolve_agent_llm(
                agent, policy=policy, workspace_cfg=workspace_cfg, settings=rs,
            )
            for agent in REVIEW_AGENTS
        }
        try:
            client = build_llm_client(
                user_id=user_id,
                workspace_id=workspace_id,
                resolve_model=lambda agent: (
                    (by_agent.get(agent) or AgentLLMSettings()).model
                    or rs.defect_model
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_client_build_failed err=%s", exc)
            return None, {}
        return client, by_agent

    # ─── Stage 10: per-repo policy loading + rules formatting ──

    def _load_policy(self, repo_slug: str) -> dict | None:
        """Synchronous policy fetch — runs from a sync context inside the
        review orchestrator (which is itself called from a thread pool by
        FastAPI). Returns None if no row exists (= use defaults)."""
        import os

        try:
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import Session

            from src.db.models import RepoReviewPolicy

            raw_url = (os.environ.get("DATABASE_URL") or "").strip()
            if not raw_url:
                return None
            sync_url = (
                raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
                       .replace("postgresql://", "postgresql+psycopg://")
                if "+psycopg" not in raw_url else raw_url
            )
            engine = create_engine(sync_url, pool_pre_ping=True)
            try:
                with Session(engine) as s:
                    row = s.execute(
                        select(RepoReviewPolicy).where(
                            RepoReviewPolicy.repo_slug == repo_slug,
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        return None
                    return {
                        "enabled": bool(row.enabled),
                        "prompt_template": row.prompt_template or "",
                        "target_branches": list(row.target_branches or []),
                        "folder_rules": list(row.folder_rules or []),
                        # Stage 11 — per-agent model overrides (None → workspace default)
                        "architect_model": row.architect_model,
                        "security_model": row.security_model,
                        "quality_model": row.quality_model,
                        "tests_model": row.tests_model,
                        "verifier_model": row.verifier_model,
                        # Stage 12 — per-repo per-agent system_prompt overrides.
                        "agent_prompt_overrides": dict(row.agent_prompt_overrides or {}),
                        # Per-repo per-agent LLM knobs — {architect:
                        # {max_output_tokens?, reasoning?}, …}, the top layer
                        # of `resolve_agent_llm`. No `model` key: the model of
                        # this layer is the flat `<agent>_model` columns above
                        # and nothing else, which is why the resolver reads it
                        # from there alone. NULL for every row written before
                        # the column existed, and NULL means inherit.
                        "agents": dict(row.agent_llm_overrides or {}),
                        # Stage 13 — MCP evidence sources.
                        "mcp_sources": list(row.mcp_sources or []),
                        # Per-repo agent kill-switch — these never run.
                        "disabled_agents": list(row.disabled_agents or []),
                        # The prefilter's rule deny-list for this repo. NULL
                        # (every row written before the column existed) means
                        # inherit `ReviewSettings.suppressed_rules`; a list —
                        # an empty one included — REPLACES it. The two are
                        # different answers, so None is kept as None here
                        # rather than flattened into [] the way the lists
                        # above are.
                        "suppressed_rules": (
                            None if row.suppressed_rules is None
                            else list(row.suppressed_rules)
                        ),
                        # Whether the model's veto runs here. NULL — every row
                        # written before the column existed — means inherit
                        # REVIEW_VERIFIER_ENABLED, which is off. True and
                        # False are both this repository's own decision, so
                        # None is kept as None rather than flattened into a
                        # boolean the operator never chose. See
                        # `_verifier_enabled`.
                        "verifier_enabled": row.verifier_enabled,
                    }
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy_load_failed repo=%s err=%s", repo_slug, exc)
            return None

    def _verifier_enabled(
        self, policy: dict | None, disabled_agents: set[str],
    ) -> tuple[bool, str]:
        """Does the model's veto run for this repository, and on whose say-so.

        OFF unless something says otherwise. It shipped ON, and not by
        decision: the only way to switch it off was to name "verifier" in
        `disabled_agents`, and an unconfigured repository names nothing. A
        default that exists only because its opposite was unsayable is not a
        default — it is the shape of the deny-list leaking into the product.

        Three answers, most specific first:

          * "verifier" in `disabled_agents` — the old spelling of "off". It
            still wins over everything, because installations that found it
            (this one's seven benchmark repositories among them) wrote it
            meaning off, and a rename must not silently turn their answer
            around.
          * `policy.verifier_enabled` — this repository decided. True and
            False are both decisions; NULL is not one.
          * `REVIEW_VERIFIER_ENABLED` — the install default, itself False.

        The reason travels with the answer so the log line says whose
        decision it was, rather than reporting three different situations
        with one word.

        Only the model's veto. The deterministic prefilter — dedup,
        near-duplicate clustering, the rule deny-list, the confidence floor,
        the severity sort — runs on every review either way, and used to be
        switched off along with it. That was its own defect and is not this
        one.
        """
        if "verifier" in disabled_agents:
            return False, "disabled_by_policy"
        decided = (policy or {}).get("verifier_enabled")
        if decided is not None:
            return bool(decided), (
                "enabled_by_policy" if decided else "disabled_by_policy"
            )
        on = bool(getattr(self.settings, "verifier_enabled", False))
        return on, "enabled_by_install" if on else "off_by_default"

    @staticmethod
    def _suppressed_rules(
        policy: dict | None, settings: ReviewSettings,
    ) -> frozenset[str]:
        """The rule ids the prefilter hides for this review.

        The policy's list replaces the code default outright — there is no
        merge, so a repo can narrow the set as well as widen it. `None` (no
        policy, or a row that never set one) is the default; `[]` is "hide
        nothing", which the merge would have been unable to say.
        """
        override = (policy or {}).get("suppressed_rules")
        if override is None:
            return frozenset(settings.suppressed_rules)
        return frozenset(
            str(rule).strip() for rule in override if str(rule).strip()
        )

    # (severity_for_verdict — helper for the notify call above)

    def _build_custom_rules(
        self,
        pr: PullRequest,
        policy: dict | None,
    ) -> str:
        """Render the policy's natural-language rules + matching folder rules
        into a single block injected into the architect prompt."""
        if not policy:
            return ""
        import fnmatch

        parts: list[str] = []
        base = (policy.get("prompt_template") or "").strip()
        if base:
            parts.append("**Repo-level rules (from admin panel):**\n" + base)

        # Match folder rules against changed file paths.
        changed = pr.changed_files or []
        for fr in policy.get("folder_rules", []):
            pat = fr.get("pattern", "")
            prompt = (fr.get("prompt") or "").strip()
            if not pat or not prompt:
                continue
            matched = [f for f in changed if fnmatch.fnmatch(f, pat)]
            if matched:
                preview = ", ".join(matched[:3])
                if len(matched) > 3:
                    preview += f", …(+{len(matched) - 3})"
                parts.append(
                    f"**Folder rule — `{pat}` (matches: {preview}):**\n{prompt}"
                )
        return "\n\n".join(parts)

    def _build_mcp_evidence(
        self, pr: PullRequest, *, user_id: str,
    ) -> str:
        """Fetch MCP evidence from every configured source that triggers
        on this PR. Returns an ``<external_untrusted>`` block or ""."""
        try:
            from src.mcp_client import build_evidence_block
            return build_evidence_block(
                repo_slug=pr.repo,
                pr_title=pr.title,
                pr_description=pr.description or "",
                changed_files=pr.changed_files,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp_evidence_failed err=%s", exc)
            return ""

    def _build_cross_repo_drift(
        self, pr: PullRequest, *, workspace_id: str = "default",
    ) -> str:
        """Run semantic drift detector (Stage 7). Graceful — empty on failure.

        Graceful, but no longer SILENT. This logged at `debug`, which on a
        production box means nowhere: a grep that stopped working, a group that
        stopped resolving, a diff format that changed, and the review simply
        proceeds without the one signal nothing else provides. No error, no
        warning, no row anywhere — the feature stops existing and looks
        healthy doing it.

        That is the same shape as `noData = OK` on a monitor: absence of
        signal presenting as health. For the check that catches a constant
        changed in one repository and not its siblings — where the failure is
        silently divergent embeddings, days later — it is the wrong default.
        """
        try:
            from src.review.cross_repo_drift import detect_drift
            report = detect_drift(pr, workspace_id=workspace_id)
            # Keep the FACTS alongside the prose. The markdown goes to the
            # architect; the structure goes to the run record, so the UI can
            # show the finding itself rather than the model's account of it.
            self._last_drift_facts = report.to_facts() if report.has_drift else None
            return report.to_markdown()
        except Exception as exc:  # noqa: BLE001
            self._last_drift_facts = None
            logger.warning(
                "drift_detector_failed repo=%s pr=%s err=%s — this review has "
                "NO cross-repo drift signal", pr.repo, pr.number, exc,
                exc_info=True,
            )
            return ""

    def _load_repo_overview(self, pr: PullRequest) -> str:
        """Load the existing PRD overview from the vault if there is one."""
        try:
            from src.config import get_settings
            settings = get_settings()
            overview_path = (
                settings.repo_vault_path(pr.local_slug) / "_overview.md"
            )
            if overview_path.exists():
                content = overview_path.read_text(encoding="utf-8")
                # Limit to ~3000 chars
                return content[:3000]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _load_style_guide(self, pr: PullRequest) -> str:
        """Load CLAUDE.md / .style or other repo conventions if they exist."""
        try:
            from src.config import get_settings
            settings = get_settings()
            repo_path = settings.repo_path(pr.local_slug)
            for candidate in ("CLAUDE.md", ".cursorrules", "CONTRIBUTING.md"):
                p = repo_path / candidate
                if p.exists():
                    return p.read_text(encoding="utf-8")[:3000]
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ─── Parallel agent execution ──────────────────────────────

    def _run_agents_parallel(
        self,
        context: AgentContext,
        *,
        disabled_agents: set[str] | None = None,
    ) -> list[AgentRunResult]:
        """Run all agents in parallel — LLM calls are I/O bound, and at most
        `settings.agent_concurrency` of them run at once (see the pool split
        below for why the deterministic agents do not count).

        Agents named in `disabled_agents` (per-repo policy) are never
        dispatched: no LLM call, no tokens, no findings. They are also NOT
        recorded in `agents_failed`, so switching off e.g. `security` does not
        degrade the verdict the way a crashed agent does.
        """
        skip = disabled_agents or set()
        active = [a for a in self.agents if a.name not in skip]
        for a in self.agents:
            if a.name in skip:
                logger.warning("agent_skipped_by_policy agent=%s", a.name)
        if not active:
            return []
        # Two pools, one bound. max_workers used to be len(active): six
        # agents, six simultaneous provider connections per PR — benchmarked
        # as the source of ConnectError on a weak uplink (5 of 9 agent
        # failures in one run) and of 503s from Gemini. The bound is on
        # PROVIDER connections, so only the LLM-backed agents count against
        # it: Structural and CVE never open one — ast-grep and osv-scanner
        # are local subprocesses — and seating them in the bounded pool would
        # let a slow osv scan silently shrink the LLM bound below the
        # configured number, while sizing the pool up to admit them would
        # hand a finished scanner's slot to a fourth simultaneous LLM call.
        # The split is by class because LLMReviewAgent IS the "calls a
        # provider" contract — a future LLM agent inherits the bound with the
        # retry machinery. `max(1, …)` fails closed: a zero or negative
        # setting must wedge the bound at serial, never wedge the review.
        llm_agents = [a for a in active if isinstance(a, LLMReviewAgent)]
        local_agents = [a for a in active if not isinstance(a, LLMReviewAgent)]
        concurrency = max(1, int(self.settings.agent_concurrency))
        results: list[AgentRunResult] = []
        with ThreadPoolExecutor(max_workers=concurrency) as llm_pool, \
                ThreadPoolExecutor(max_workers=max(1, len(local_agents))) as local_pool:
            futures = {llm_pool.submit(a.review, context): a for a in llm_agents}
            futures.update(
                {local_pool.submit(a.review, context): a for a in local_agents}
            )
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    res = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "agent_unhandled_exception agent=%s err=%s",
                        agent.name, exc,
                    )
                    res = AgentRunResult(agent=agent.name, error=str(exc))
                results.append(res)
        return results


def _severity_for_verdict(batch) -> str:
    """Map review verdict → notifier severity string."""
    from src.review.models import ReviewVerdict
    v = getattr(batch, "verdict", None)
    if v == ReviewVerdict.REQUEST_CHANGES:
        return "error" if batch.critical_count == 0 else "critical"
    if v == ReviewVerdict.COMMENT and (batch.warning_count or batch.error_count):
        return "warn"
    return "info"
