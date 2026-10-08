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

import contextlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from src.llm.capabilities import ParameterAdjustment
from src.review import cadence, messages
from src.review.agents import (
    AgentContext,
    BusinessLogicAgent,
    ContractAgent,
    CveAgent,
    DefectAgent,
    PerformanceAgent,
    SecurityAgent,
    StructuralAgent,
    VerifierAgent,
)
from src.review.agents.base import (
    AgentRunResult,
    LLMReviewAgent,
    ReviewAgent,
    base_instruction_block,
    clamp_base_instruction,
    claude_engine_guidelines,
)
from src.review.branch_patterns import branch_targeted
from src.review.branch_patterns import pass_sentence as branch_pass_sentence
from src.review.branch_patterns import skip_sentence as branch_skip_sentence
from src.review.graph_context import build_graph_context
from src.review.learning import signals as _learning_signals  # noqa: F401  (outcome listener)
from src.review.models import (
    COMPLETED_COMMENTS,
    SUMMARY_EXISTING_DESCRIPTION,
    SUMMARY_ON_NEW_COMMITS,
    SUMMARY_TARGETS,
    Finding,
    PRActions,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.providers import get_provider_for
from src.review.providers.base import (
    EmptyDiffError,
    PullRequestProvider,
    PullRequestProviderError,
)
from src.review.scope import AUTOMATIC, ReviewRequest, title_keyword_match
from src.review.settings import (
    AgentLLMSettings,
    ReviewSettings,
    get_review_settings,
)
from src.review.stages import StageRecorder

logger = logging.getLogger(__name__)


# ─── Stage sentences ──────────────────────────────────────────────────
#
# Every reason on a stage is written here, from fields Celmis owns — never
# from an exception's message or a provider's response body, which can quote
# URLs and credentials. `src.review.stages.scrub` is the second line.


def _quoted_list(items) -> str:
    """['main', 'develop'] — the exact configured values, bounded."""
    vals = [str(x) for x in list(items or [])[:20]]
    more = len(list(items or [])) - len(vals)
    body = ", ".join(f"'{v}'" for v in vals)
    return f"[{body}{', …' if more > 0 else ''}]"


def _provider_label(name: str) -> str:
    return {"github": "GitHub", "gitlab": "GitLab",
            "bitbucket": "Bitbucket"}.get(str(name or "").lower(), str(name or "provider"))


def _fetched_sentence(pr) -> str:
    files = len(getattr(pr, "changed_files", None) or [])
    base = getattr(pr, "base_ref", "") or "?"
    head = getattr(pr, "head_ref", "") or "?"
    return (f"#{getattr(pr, 'number', '?')} fetched: {base} ← {head}, "
            f"{files} changed file{'s' if files != 1 else ''}.")


def _lost_diff_note(files: int | None, language: str | None = None) -> str:
    """The sentence a pull request gets when its diff never arrived.

    Not a skip: the provider itself lists `files` changed files, so there IS
    something to review and nobody has. Said on the PR, because a run that
    fails quietly is how an unreviewed change gets merged as "checked".
    """
    if files is None:
        return messages.t("lost_diff.unknown", language)
    return messages.tn("lost_diff", files, language, files=files)


def _settings_sentence(policy) -> str:
    if policy is None:
        return "No repository policy or workspace defaults; built-in settings apply."
    parts = []
    if isinstance(policy, dict) and policy.get("workspace_id"):
        parts.append("repository policy merged over workspace defaults")
    else:
        parts.append("workspace review defaults")
    disabled = list((policy or {}).get("disabled_agents") or [])
    if disabled:
        parts.append(f"agents off: {', '.join(str(a) for a in disabled[:10])}")
    rules = list((policy or {}).get("folder_rules") or [])
    if rules:
        parts.append(f"{len(rules)} custom rule{'s' if len(rules) != 1 else ''}")
    return "Settings resolved: " + "; ".join(parts) + "."


def _context_sentence(context) -> str:
    callers = int(getattr(context, "cross_repo_callers_count", 0) or 0)
    note = getattr(context, "graph_note", None)
    if note is not None:
        why = getattr(note, "reason", "") or "the code graph is unavailable"
        return f"Reviewed without full graph context: {why}"
    return (f"Code graph context built; {callers} cross-repo "
            f"caller{'s' if callers != 1 else ''} of the changed code.")


#: A model-backed agent that answers a sizeable prompt with next to nothing.
#: `[]` is a valid reply and stays valid; these bounds only decide when the run
#: says out loud that four agents "read" a large diff in one or two tokens each.
TERSE_REPLY_MAX_TOKENS_OUT = 3
TERSE_REPLY_MIN_TOKENS_IN = 3000
TERSE_REPLY_MIN_CHANGED_LINES = 50
#: `ParameterAdjustment.parameter` / action for the note. Not a model
#: parameter; the same road as the graph note (the literal is mirrored in
#: `ReviewBatch.adjustments_notice`, which models.py cannot import from here).
PARAM_AGENT_REPLY = "agent_reply"
ADJUST_TERSE = "terse"


def terse_reply_note(agent_results, pr) -> ParameterAdjustment | None:
    """An adjustment naming agents that answered a big diff with ~nothing.

    Never changes a verdict: the empty reply is accepted as it always was. It
    only makes the run say so, because a lite model without reasoning tends to
    answer `[]` and the review then approves silently. Missing usage (no
    tokens recorded) is "cannot tell", not "terse".
    """
    changed = sum(
        int(getattr(h, "added_lines", 0) or 0) + int(getattr(h, "removed_lines", 0) or 0)
        for h in (getattr(pr, "hunks", None) or ())
    )
    if changed < TERSE_REPLY_MIN_CHANGED_LINES:
        return None
    names: list[str] = []
    for r in agent_results:
        if getattr(r, "error", None) or getattr(r, "findings", None):
            continue
        tin = int(getattr(r, "tokens_in", 0) or 0)
        tout = int(getattr(r, "tokens_out", 0) or 0)
        if tin >= TERSE_REPLY_MIN_TOKENS_IN and 0 < tout <= TERSE_REPLY_MAX_TOKENS_OUT:
            names.append(str(r.agent))
    if not names:
        return None
    return ParameterAdjustment(
        agent=", ".join(names), parameter=PARAM_AGENT_REPLY,
        requested="a reasoned review", sent=f"<= {TERSE_REPLY_MAX_TOKENS_OUT} output tokens",
        action=ADJUST_TERSE,
        reason=(
            "no findings, and the reply was only a few tokens for a "
            f"{changed}-line change; a model without reasoning tends to answer `[]` — "
            "consider a model with reasoning or the per-agent `reasoning` setting "
            "(docs/REVIEW_SETTINGS.md)"
        ),
    )


def _record_agent_stage(stages: StageRecorder, r, *, dispatched_at: float,
                        finished_at: float | None) -> None:
    """One agent's stage: model, tokens, findings, how long it took."""
    from datetime import UTC, datetime

    elapsed = float(getattr(r, "elapsed_seconds", 0.0) or 0.0)
    end = finished_at or time.time()
    start = max(dispatched_at, end - elapsed) if elapsed else dispatched_at
    duration_ms = int(max(0.0, (elapsed if elapsed else end - start)) * 1000)
    model = getattr(r, "model_used", None) or None
    tin = int(getattr(r, "tokens_in", 0) or 0)
    tout = int(getattr(r, "tokens_out", 0) or 0)
    n = len(getattr(r, "findings", None) or [])
    meta = {"model": model, "tokens_in": tin, "tokens_out": tout, "findings": n}
    if getattr(r, "error", None):
        from src.llm.errors import curated_reason

        why = curated_reason(getattr(r, "error_code", None)) or (
            "the agent did not produce an answer")
        reason = f"Failed: {why}" + (f" (model {model})" if model else "")
        status = "failed"
    else:
        reason = (f"{n} finding{'s' if n != 1 else ''}"
                  + (f" · model {model}" if model else " · no model call")
                  + (f" · {tin:,} tokens in / {tout:,} out" if (tin or tout) else ""))
        status = "success"
    stages.add(f"agent:{r.agent}", f"Agent: {r.agent}", status, reason,
               started_at=datetime.fromtimestamp(start, UTC).isoformat(),
               duration_ms=duration_ms, meta=meta)


def _publish_sentence(provider_name: str, batch, response, *, dry_run: bool) -> str:
    label = _provider_label(provider_name)
    if dry_run:
        return f"Dry run: nothing was posted to {label}."
    n = len(getattr(batch, "findings", None) or [])
    tail = ""
    if isinstance(response, dict) and response.get("summary_error"):
        tail = " The summary comment was not delivered."
    return (f"Review posted to {label}: summary and up to {n} inline "
            f"comment{'s' if n != 1 else ''}.{tail}")


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


def _sort_by_severity(findings: list[Finding]) -> list[Finding]:
    """Critical first, info last; stable, so equal severities keep their order."""
    from src.review.models import SEVERITY_RANK, severity_value

    return sorted(
        findings,
        key=lambda f: -SEVERITY_RANK.get(severity_value(f.severity), -1),
    )


def _drop_ignored(findings: list[Finding], globs: list[str]) -> list[Finding]:
    """Findings on a path the repo's ignore globs exclude, removed.

    The agents never see those files' hunks, but `changed_files_of` still
    accepts skipped paths as anchors, so a model that names one is not refused
    at parse time. An ignored path is ignored all the way through.
    """
    if not globs:
        return findings
    from src.review.ignore_globs import path_ignored

    return [f for f in findings if not path_ignored(f.file_path, globs)]


def _cite_review_rules(findings: list[Finding], policy) -> list[Finding]:
    """Keep a finding's `rule` only when it names a review rule in force for
    this review, spelled as that rule's title, and report the finding at the
    rule's severity — the severity is the operator's decision about the rule,
    not the agent's about the line.

    Anything else in the field is cleared: some replies put a free-form rule
    id there, and a citation of a rule nobody wrote must not reach the bypass
    of the comment filters (`ReviewBatch.bypasses_filters`).
    """
    from src.review.models import FindingSeverity
    from src.review.policy_rules import cited_rule, rules_by_title

    rules = (policy or {}).get("review_rules") if isinstance(policy, dict) else None
    titles = rules_by_title(rules)
    for f in findings:
        raw = getattr(f, "rule", "")
        if not raw:
            continue
        rule = cited_rule(raw, titles) if titles else None
        if rule is None:
            f.rule = ""
            continue
        f.rule = str(rule.get("title") or "")
        severity = str(rule.get("severity") or "").lower()
        if severity in {s.value for s in FindingSeverity}:
            f.severity = FindingSeverity(severity)
    return findings


def _policy_value(policy, name: str, default):
    """A policy setting, or `default` when the policy does not decide it.

    The policy is the dict `_load_policy` builds, but test doubles and older
    callers hand in objects too, so both shapes are read. None — no policy, no
    key, a NULL column — always means "inherit", never "off".
    """
    if policy is None:
        return default
    value = (
        policy.get(name) if isinstance(policy, dict) else getattr(policy, name, None)
    )
    return default if value is None else value


def _policy_setting(policy, name: str):
    """A review setting as it is in force: the resolved policy's value, else
    its built-in (`review_defaults.BUILTIN_DEFAULTS`).

    `_policy_value` with the default looked up rather than restated at each
    reader — the 2.3.0 settings (run_on_drafts, approve_when_clean,
    summary_target, …) have no env knob, so the built-in IS the install
    default, and the page that says "inherit → X" reads X from the same map.
    A blank text setting is an inherit, as it is in the resolver.
    """
    from src.review.review_defaults import TEXT_FIELDS, builtin_default

    value = _policy_value(policy, name, None)
    if name in TEXT_FIELDS and isinstance(value, str) and not value.strip():
        value = None
    return builtin_default(name) if value is None else value


def _pr_actions(policy) -> PRActions:
    """The repository's PR-action settings, each read with its built-in default.

    A word outside a setting's vocabulary falls back to the default rather
    than switching the behaviour off or on by accident; a template that is
    not a string is no template.
    """
    def _word(name: str, allowed: tuple[str, ...], default: str) -> str:
        value = str(_policy_value(policy, name, default) or "").strip().lower()
        return value if value in allowed else default

    def _text(name: str) -> str | None:
        value = _policy_value(policy, name, None)
        return value if isinstance(value, str) and value.strip() else None

    return PRActions(
        approve_when_clean=bool(_policy_value(policy, "approve_when_clean", False)),
        request_changes_on_critical=bool(
            _policy_value(policy, "request_changes_on_critical", False)),
        status_feedback=bool(_policy_value(policy, "status_feedback", True)),
        committable_suggestions=bool(
            _policy_value(policy, "committable_suggestions", False)),
        summary_target=_word("summary_target", SUMMARY_TARGETS, "comment"),
        summary_on_new_commits=_word(
            "summary_on_new_commits", SUMMARY_ON_NEW_COMMITS, "replace"),
        summary_existing_description=_word(
            "summary_existing_description", SUMMARY_EXISTING_DESCRIPTION, "append"),
        message_started=_text("message_started"),
        message_finished_header=_text("message_finished_header"),
        completed_comment=_word("completed_comment", COMPLETED_COMMENTS, "completed"),
        # The guide lists commands, so it needs both the guide and the commands.
        commands_guide_enabled=bool(
            _policy_value(policy, "commands_guide_enabled", True)
            and _policy_value(policy, "commands_enabled", True)),
        guide_commands_off=_commands_turned_off(policy),
    )


def _commands_turned_off(policy) -> tuple[str, ...]:
    """The commands whose feature this repository switched off, so the guide
    in the completed comment does not offer what would be refused."""
    from src.review.commands.parser import BUSINESS_LOGIC, CHAT, REMEMBER

    gated = ((CHAT, "chat_enabled"), (REMEMBER, "memories_enabled"),
             (BUSINESS_LOGIC, "task_context_enabled"))
    return tuple(name for name, setting in gated
                 if not _policy_value(policy, setting, True))


def _display_time(iso: str) -> str:
    """'2026-10-04T12:03:11.5+00:00' -> '2026-10-04 12:03 UTC'."""
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _safe_failure_reason(exc: BaseException) -> str:
    """A sentence about a failure that is safe to print on a pull request.

    Never the exception's message: provider errors quote response bodies,
    URLs and occasionally credentials. The curated sentence for a classified
    LLM failure when there is one, otherwise the exception's type alone —
    the full message stays in the log and the run record.
    """
    try:
        from src.llm.errors import classify, curated_reason

        reason = curated_reason(classify(exc).code)
        if reason:
            return reason
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, PullRequestProviderError):
        return "the git provider refused a request (see the review run for details)"
    return f"internal error ({type(exc).__name__}) — see the review run for details"


class _ReviewLifecycle:
    """The PR-side lifecycle comment of one review.

    started  → "🔄 Celmis is reviewing this PR…" (creates or adopts the
               persistent summary comment)
    skipped  → "⏭️ Skipped: <reason>" — only over a comment that is still an
               in-progress placeholder (this run's, or one a killed run left
               behind). A finished summary of an earlier commit is never
               overwritten, and a skipped PR never gets a new thread
    failed   → "❌ Review failed: <reason>" — only over a placeholder THIS run
               posted, so a crash never overwrites a previous good summary
    (success → `post_review` writes the final summary into the same comment)

    Every write is best-effort: a failure is logged and the review goes on.
    Inactive (`post_comments=False` or a dry run) it writes nothing at all.
    """

    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.provider = None
        self.pr: PullRequest | None = None
        #: The placeholder this run wrote, if any.
        self.started_id: int | None = None
        self._language: str | None = None

    @property
    def language(self) -> str | None:
        """The repository's review language, once the policy is known; None
        (English) before that. Handed to the provider as well, whose own
        notes (the "not reviewed" one) are written in it."""
        return self._language

    @language.setter
    def language(self, value: str | None) -> None:
        self._language = value
        if self.provider is not None:
            with contextlib.suppress(AttributeError):  # a provider double with __slots__
                self.provider.review_language = value

    def bind(self, provider, pr: PullRequest) -> None:
        self.provider = provider
        self.pr = pr
        if self._language is not None:
            self.language = self._language

    def _write(self, body: str, *, create: bool, what: str,
               only_if_in_progress: bool = False) -> int | None:
        if not self.active or self.provider is None or self.pr is None:
            return None
        upsert = getattr(self.provider, "upsert_status_comment", None)
        if not callable(upsert):
            return None
        try:
            if only_if_in_progress:
                cid = upsert(self.pr, body, create=create, only_if_in_progress=True)
            else:
                cid = upsert(self.pr, body, create=create)
        except Exception as exc:  # noqa: BLE001 — a status comment never fails a review
            logger.warning(
                "review_status_comment_failed what=%s pr=%s err_type=%s err=%s",
                what, self.pr.number, type(exc).__name__, str(exc)[:200],
            )
            return None
        cid = cid if isinstance(cid, int) and not isinstance(cid, bool) else None
        logger.info("review_status_comment what=%s pr=%s id=%s",
                    what, self.pr.number, cid)
        return cid

    def started(self, agents: list[str], *, started_at: str,
                template: str | None = None,
                first_review: bool | None = None) -> None:
        from src.review.providers.base import _format_started_comment

        if self.pr is None:
            return
        self.started_id = self._write(
            _format_started_comment(self.pr, agents=agents, started_at=started_at,
                                    template=template, language=self.language,
                                    first_review=first_review),
            create=True, what="started",
        )

    def skipped(self, reason: str, *, feedback: bool = False) -> None:
        """Rewrite a placeholder as "⏭️ Skipped"; with `feedback` (the
        repository's `status_feedback`) and no placeholder to rewrite, leave a
        brief marked note instead — upserted, so a re-run never adds a second.
        Off, a skip with no placeholder stays silent, as it always was."""
        from src.review.providers.base import _format_status_comment

        if self.pr is None:
            return
        cid = self._write(
            _format_status_comment(self.pr, outcome="skipped", reason=reason,
                                   language=self.language),
            create=False, what="skipped", only_if_in_progress=True,
        )
        if cid is None and feedback:
            self._feedback(reason)

    def _feedback(self, reason: str) -> bool:
        """Leave the marked status note. True only when the provider confirmed a
        comment — a caller that must not lose the note (the pause notice)
        reads this; every other caller may ignore it."""
        if not self.active or self.provider is None or self.pr is None:
            return False
        upsert = getattr(self.provider, "upsert_feedback_comment", None)
        if not callable(upsert):
            return False
        try:
            cid = upsert(self.pr, reason)
        except Exception as exc:  # noqa: BLE001 — a status note never fails a review
            logger.warning(
                "review_status_feedback_failed pr=%s err_type=%s err=%s",
                self.pr.number, type(exc).__name__, str(exc)[:200],
            )
            return False
        logger.info("review_status_feedback pr=%s id=%s", self.pr.number, cid)
        return cid is not None

    def failed(self, reason: str) -> None:
        from src.review.providers.base import _format_status_comment

        if self.pr is None or self.started_id is None:
            return
        self._write(
            _format_status_comment(self.pr, outcome="failed", reason=reason,
                                   language=self.language),
            create=False, what="failed",
        )
        self.started_id = None


class _PRSummaryJob:
    """The overview/walkthrough LLM call, run beside the engine.

    One worker thread, one call, a deadline on the join. `apply_to` never
    raises and never blocks past the deadline: the summary is decoration and
    the review it decorates is already finished when it is read.
    """

    def __init__(self, executor, future) -> None:
        self._executor = executor
        self._future = future

    @classmethod
    def start(cls, pr: PullRequest, context, *, language, instructions) -> _PRSummaryJob | None:
        try:
            from src.review.agents.base import agent_llm_settings
            from src.review.pr_summary import generate_pr_summary

            client = getattr(context, "llm_client", None)
            if client is None:
                logger.info("review_summary_skipped reason=no_llm_client pr=%s", pr.number)
                return None
            agent_llm = agent_llm_settings(context, "verifier")
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pr-summary")
            future = executor.submit(
                generate_pr_summary, pr, llm_client=client, agent_llm=agent_llm,
                language=language, instructions=instructions,
            )
            return cls(executor, future)
        except Exception as exc:  # noqa: BLE001
            logger.warning("review_summary_not_started err_type=%s err=%s",
                           type(exc).__name__, str(exc)[:200])
            return None

    def apply_to(self, batch: ReviewBatch) -> tuple[bool, str]:
        """Join the summary call. Returns (ok, a sentence for the run's
        "Generate summary" stage)."""
        from src.review.pr_summary import SUMMARY_TIMEOUT_SECONDS

        try:
            res = self._future.result(timeout=SUMMARY_TIMEOUT_SECONDS + 5)
        except Exception as exc:  # noqa: BLE001 — includes the join's own timeout
            logger.warning("review_summary_unavailable pr=%s err_type=%s",
                           batch.pull_request.number, type(exc).__name__)
            return False, (f"The overview call did not answer ({type(exc).__name__}); "
                           "the summary was posted without it.")
        finally:
            self._executor.shutdown(wait=False)
        batch.pr_overview = res.overview
        batch.walkthrough = dict(res.walkthrough)
        # Its tokens are this review's tokens; the ledger already booked the
        # money under `review_summary`. The run's own total takes the cost
        # only when both sides are known — an unknown never poisons a known.
        batch.tokens_in += res.tokens_in
        batch.tokens_out += res.tokens_out
        if res.cost_usd is not None and batch.cost_usd is not None:
            batch.cost_usd = round(batch.cost_usd + res.cost_usd, 6)
        if res.error:
            logger.info("review_summary_degraded pr=%s reason=%s",
                        batch.pull_request.number, res.error)
            return False, ("The overview was degraded; the summary was posted "
                           "without the parts that failed.")
        return True, (f"Overview and walkthrough of {len(batch.walkthrough)} "
                      f"file{'s' if len(batch.walkthrough) != 1 else ''} generated.")


@dataclass
class SingleAgentRun:
    """What `ReviewOrchestrator.run_single_agent` returns: the agent's result
    and the context it ran in (the on-demand check reads the model client and
    the Jira task from it)."""

    result: AgentRunResult
    context: AgentContext


class ReviewOrchestrator:
    """End-to-end PR review."""

    #: Agents in the roster that run only where a policy opts them in, by
    #: name, in `enabled_agents`. Every other agent runs unless a policy
    #: names it in `disabled_agents` — so the built-in default roster is
    #: everything in `_default_agents()` minus this set: performance ON,
    #: business_logic OFF.
    #:
    #: A separate opt-in list rather than a default `disabled_agents`
    #: entry, because that list already holds operators' decisions. A
    #: repository that switched `cve` off before 2.3 stored ["cve"]; reading
    #: that list as "business_logic is on here" would start a new paid model
    #: call on every one of its reviews without anyone having asked for it.
    #: `disabled_agents` still wins over an opt-in: off is off.
    OFF_BY_DEFAULT: frozenset[str] = frozenset({"business_logic"})

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
        self._agent_finished_at: dict[str, float] = {}
        self.last_stages: StageRecorder | None = None

    @staticmethod
    def _default_agents() -> list[ReviewAgent]:
        # NB: the names below are what a policy's `disabled_agents` may hold —
        # keep `TOGGLEABLE_AGENTS` in src/api/routers/review_policies.py in sync.
        return [
            DefectAgent(),
            ContractAgent(),
            SecurityAgent(),
            # The Kodus-category finders (2.3). Both are always in the
            # roster — the prompt preview, the per-repo prompt overrides and
            # the rule targets are all derived from this list — and whether
            # they RUN is `_dormant_agents`: performance by default, business
            # logic only where a policy opts in.
            PerformanceAgent(),
            BusinessLogicAgent(),
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

    @classmethod
    def _dormant_agents(cls, policy, disabled_agents: set[str] | None = None) -> set[str]:
        """The off-by-default agents this policy did not opt in.

        `enabled_agents` is read through `_policy_value`, so a repository, a
        workspace default or nothing at all may decide it; nothing deciding
        means the built-in roster, i.e. every `OFF_BY_DEFAULT` agent sits
        out. An agent named in `disabled_agents` is not reported here — it is
        off by the operator's word, which the caller already logs as such.
        """
        enabled = {
            str(a).strip().lower()
            for a in (_policy_value(policy, "enabled_agents", None) or [])
        }
        return set(cls.OFF_BY_DEFAULT) - enabled - set(disabled_agents or ())

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
        stages: StageRecorder | None = None,
        request: ReviewRequest | None = None,
    ) -> ReviewRunResult:
        """Run full review pipeline.

        Args:
            provider_name: 'github' | 'gitlab' | 'bitbucket'
            repo: 'owner/name'
            pr_number: PR number / MR iid / Bitbucket PR id
            dry_run: simulate without actual comment posting (still computes findings)
            post_comments: whether to post comments (false → just return batch)
            provider: optional pre-built provider (for tests)
            stages: the run's stage recorder (src/review/stages.py); the
                caller owns it and adds "record" and "finished" after this
                returns. None records into a private one (`last_stages`).
            request: what asked for this review (`src.review.scope`). It
                decides which gates apply: a person's request skips the
                draft, title and cadence gates, `force` the target-branch one
                too. None is an automatic trigger — every gate applies.
        """
        t0 = time.time()
        if stages is None:
            stages = StageRecorder()
        #: The stages of the last review, for a caller that did not pass a
        #: recorder in.
        self.last_stages = stages
        # Parallelism gauge for the resource sampler — how many reviews run
        # at this moment is exactly what capacity docs need.
        from src.ops.telemetry import review_finished, review_started
        review_started()
        # The PR-side lifecycle comment ("🔄 reviewing…" → summary). Owned
        # here, outside `_review_impl`, so an exception anywhere inside it
        # still rewrites a placeholder this run posted into "❌ Review
        # failed" instead of leaving the pull request saying "reviewing…"
        # forever.
        lifecycle = _ReviewLifecycle(active=post_comments and not dry_run)
        try:
            return self._review_impl(
                provider_name, repo, pr_number, dry_run=dry_run,
                post_comments=post_comments, provider=provider,
                user_id=user_id, workspace_id=workspace_id, t0=t0,
                lifecycle=lifecycle, stages=stages,
                request=request or AUTOMATIC,
            )
        except Exception as exc:
            reason = _safe_failure_reason(exc)
            # The stage that was under way when it raised is the one that
            # failed — with the same curated sentence the PR gets, never the
            # exception's own text.
            stages.fail_running(reason[:1].upper() + reason[1:])
            lifecycle.failed(reason)
            raise
        finally:
            review_finished()

    def _review_impl(
        self, provider_name: str, repo: str, pr_number: int, *,
        dry_run: bool, post_comments: bool, provider,
        user_id: str, workspace_id: str, t0: float,
        lifecycle: _ReviewLifecycle | None = None,
        stages: StageRecorder | None = None,
        request: ReviewRequest = AUTOMATIC,
    ) -> ReviewRunResult:
        if lifecycle is None:
            lifecycle = _ReviewLifecycle(active=False)
        if stages is None:
            stages = StageRecorder()
        if provider is None:
            provider = get_provider_for(
                provider_name, user_id=user_id, workspace_id=workspace_id,
            )

        stages.begin("fetch_pr", "Fetch pull request")
        try:
            pr = provider.fetch_pull_request(repo, pr_number)
        except EmptyDiffError as exc:
            # The provider lists files and sent no diff, even after retries.
            # The run fails (the queue may retry it) — and says so on the PR.
            if exc.pr is not None:
                lifecycle.bind(provider, exc.pr)
                lifecycle.language = self._language_for(exc.pr, workspace_id)
                lifecycle._feedback(_lost_diff_note(exc.files, lifecycle.language))
            raise
        except PullRequestProviderError:
            raise
        finally:
            pass  # provider closed by caller
        stages.end("fetch_pr", "success", _fetched_sentence(pr), meta={
            "files": len(getattr(pr, "changed_files", None) or []),
            "head_sha": (getattr(pr, "head_sha", "") or "")[:12] or None,
        })

        lifecycle.bind(provider, pr)

        # ── Load per-repo policy (Stage 10), with the workspace review
        # defaults filled in wherever the repository says nothing:
        # repo (non-null) > workspace > install > built-in.
        stages.begin("settings", "Resolve settings")
        policy = self._resolved_policy(pr.local_slug, workspace_id)
        stages.end("settings", "success", _settings_sentence(policy))
        language = messages.resolve_language(
            (policy or {}).get("review_language"), workspace_id)
        lifecycle.language = language

        batch = ReviewBatch(pull_request=pr)
        # The language the bot's own text on the PR is written in (see
        # `src.review.messages`); the model's output follows the same setting
        # through the prompt.
        batch.review_language = language
        batch.comment_min_severity = (policy or {}).get("comment_min_severity")
        # The repo's own inline-comment cap; None leaves the providers on
        # REVIEW_MAX_INLINE_COMMENTS.
        batch.max_inline_comments = (policy or {}).get("max_inline_comments")
        # Findings citing a review rule skip the threshold and the cap when
        # the policy says rules are exempt from them (default: they are not).
        batch.rules_bypass_filters = not bool(
            _policy_value(policy, "apply_filters_to_rules", True))
        # The Kodus-style summary is the default; a repository can keep the
        # compact one. Read with defaults because the columns are newer than
        # most policy rows (and than the code that may have loaded them).
        batch.rich_summary = bool(_policy_value(policy, "summary_enabled", True))
        # Approve / request changes / suggestions / where the summary goes /
        # message templates — what the providers may do beyond commenting.
        actions = batch.pr_actions = _pr_actions(policy)

        # ── Per-repo ignore globs. The diff was parsed inside the provider,
        # before any policy existed, so the repository's own exclusions can
        # only be applied here. What they drop joins `skipped_files`, which
        # keeps the "all changed files were filtered out" skip below true when
        # the globs cover the whole change. `review_diff` is the text the
        # claude_code engine and the size cap read; `pr.raw_diff` itself is
        # left whole because the run row stores it for the diff view.
        ignore_globs = list((policy or {}).get("ignore_globs") or [])
        review_diff = pr.raw_diff or ""
        stages.begin("ignore_globs", "Apply ignore globs")
        skipped_before = len(pr.skipped_files or [])
        if ignore_globs:
            review_diff = self._apply_ignore_globs(pr, ignore_globs)
        # Copied for every run, not only a filtered one: the summary's
        # "Skipped: N files" line reads the batch, and nothing filled it.
        batch.skipped_files = list(pr.skipped_files)
        if ignore_globs:
            dropped = len(pr.skipped_files or []) - skipped_before
            stages.end("ignore_globs", "success",
                       f"{dropped} file{'s' if dropped != 1 else ''} excluded by "
                       f"{len(ignore_globs)} ignore glob"
                       f"{'s' if len(ignore_globs) != 1 else ''}: "
                       f"{_quoted_list(ignore_globs)}.")
        else:
            stages.end("ignore_globs", "skipped",
                       "No ignore globs configured — every changed file is in scope.")

        # Hard skip — policy disabled for this repo
        stages.begin("gate_enabled", "Check review is enabled")
        if policy is not None and not policy["enabled"]:
            logger.info("review_skipped reason=policy_disabled pr=%d repo=%s",
                        pr.number, pr.repo_slug)
            batch.summary = (
                "Review skipped — AI reviewer is disabled for this repo "
                "(see /review-settings)."
            )
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            stages.end("gate_enabled", "skipped",
                       "Review disabled: the AI reviewer is switched off for this "
                       "repository's review policy.", ends_run=True)
            return ReviewRunResult(batch=batch, posted=False, provider_response={})
        stages.end("gate_enabled", "success",
                   "The AI reviewer is enabled for this repository.")

        # Hard skip — the base branch is not one the target patterns cover.
        # Names, globs (`release/*`) and exclusions (`!main`, exclusion wins;
        # negations only = every branch except those): one matcher,
        # src/review/branch_patterns.py, which the webhook's early draft skip
        # asks as well, so the two gates cannot disagree about a branch.
        stages.begin("gate_target_branch", "Validate target branch")
        targets = list((policy or {}).get("target_branches") or [])
        off_target = bool(policy is not None and targets and pr.base_ref
                          and not branch_targeted(pr.base_ref, targets))
        if off_target and not request.bypasses("gate_target_branch"):
            logger.info(
                "review_skipped reason=branch_not_targeted pr=%d base=%s patterns=%s",
                pr.number, pr.base_ref, targets,
            )
            reason = branch_skip_sentence(pr.base_ref, targets)
            batch.summary = f"Review skipped — {reason}"
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            stages.end("gate_target_branch", "skipped", reason,
                       ends_run=True, meta={"target_branch": pr.base_ref})
            lifecycle.skipped(
                f"the base branch `{pr.base_ref}` is not one this repository "
                f"reviews", feedback=actions.status_feedback,
            )
            return ReviewRunResult(batch=batch, posted=False, provider_response={})
        if off_target:
            stages.end("gate_target_branch", "success",
                       f"Base branch '{pr.base_ref}' is outside the target "
                       f"patterns; reviewed anyway — the review was forced.",
                       meta={"target_branch": pr.base_ref})
        else:
            stages.end("gate_target_branch", "success",
                       branch_pass_sentence(pr.base_ref, targets))

        # ── Draft, title, cadence. Before the repository context is built:
        # none of them needs it, and a skipped PR must not pay for a graph
        # query. A person's request (`ReviewRequest.bypasses`) skips all three.
        gated = self._gate_draft_title_cadence(
            pr, batch, policy=policy, actions=actions, lifecycle=lifecycle,
            stages=stages, request=request, workspace_id=workspace_id,
            language=language, provider_name=provider_name,
        )
        if gated is not None:
            return gated

        # ── Review scope: the whole PR, or only what is new since the last
        # reviewed commit. Before the context is built, so a run that has
        # nothing new to read pays for no graph query.
        stages.begin("scope", "Decide review scope")
        scoped = self._decide_scope(
            pr, batch, policy=policy, actions=actions, lifecycle=lifecycle,
            stages=stages, request=request, workspace_id=workspace_id,
            language=language, provider_name=provider_name, provider=provider,
            ignore_globs=ignore_globs,
        )
        if isinstance(scoped, ReviewRunResult):
            return scoped
        review_diff = scoped if scoped is not None else review_diff

        # ── Build agent context (passes custom_rules from policy + matching folder_rules) ──
        # The agents get the PR with the FILTERED diff text. Compliance and
        # breaking-change build their prompts from `raw_diff`, and the CVE
        # agent reads lockfile sections from it: handed the whole text they
        # read — and sent to the model — files the repository said must never
        # be read, and an early ignored section could push the code a rule is
        # about out of a truncated prompt. `batch.pull_request` keeps the
        # whole diff for the run row's diff view.
        agent_pr = pr
        if ignore_globs:
            import dataclasses
            agent_pr = dataclasses.replace(pr, raw_diff=review_diff)
        stages.begin("context", "Repository context")
        context = self._build_context(
            agent_pr, policy=policy, user_id=user_id, workspace_id=workspace_id,
            provider=provider,
        )
        stages.end("context", "success", _context_sentence(context))
        batch.task_context = getattr(context, "task_context", None)

        batch.cross_repo_callers = context.cross_repo_callers_count
        # What the graph could not say rides the same list as a dropped
        # temperature: `adjustments_notice` prints it in the PR banner and
        # the run row persists it, so a review without a blast radius no
        # longer reads like one with an empty radius. On the benchmark this
        # was measured on, 161 runs had no graph and nothing anywhere said so.
        if context.graph_note is not None:
            batch.parameter_adjustments.append(context.graph_note)

        # ── Skip empty/binary PRs ──
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
        raw_len = len(review_diff.encode("utf-8"))
        stages.begin("gate_size", "Check diff size")
        if cap and raw_len > cap:
            stages.end("gate_size", "skipped",
                       f"Diff too large: {raw_len:,} bytes, over the {cap:,}-byte "
                       f"limit (REVIEW_MAX_DIFF_SIZE_BYTES).", ends_run=True,
                       meta={"bytes": raw_len, "limit": cap})
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
            lifecycle.skipped(
                f"the diff is {raw_len:,} bytes, over the {cap:,}-byte limit "
                f"for a single review — split the pull request or raise "
                f"REVIEW_MAX_DIFF_SIZE_BYTES", feedback=actions.status_feedback,
            )
            return ReviewRunResult(batch=batch, posted=False, provider_response={})
        stages.end("gate_size", "success",
                   f"Diff is {raw_len:,} bytes"
                   + (f" (limit {cap:,})." if cap else " (no size limit)."),
                   meta={"bytes": raw_len})

        stages.begin("gate_hunks", "Check reviewable changes")
        # Bitbucket tells an empty PR (a diffstat of 0) from a lost diff; a
        # diffstat it could not read (None) is not an answer, so it is a loss.
        diff_unverified = pr.provider == "bitbucket" and pr.reported_files is None
        if (not pr.hunks and not (pr.raw_diff or "").strip()
                and ((pr.reported_files or 0) > 0 or diff_unverified)):
            # An empty diff for a PR the provider says has files is a diff
            # that was lost on the way, not an empty change-set. Bitbucket
            # answers with a redirect the old client did not follow; a skip
            # here left a pull request "reviewed" with nothing read.
            note = _lost_diff_note(pr.reported_files, language)
            logger.error("review_failed reason=diff_lost pr=%d reported_files=%s",
                         pr.number, pr.reported_files)
            stages.end("gate_hunks", "failed",
                       "The diff is empty and the provider could not say how many "
                       "files changed." if pr.reported_files is None else
                       f"The diff is empty but the provider lists "
                       f"{pr.reported_files} changed file"
                       f"{'' if pr.reported_files == 1 else 's'}.",
                       ends_run=True, meta={"reported_files": pr.reported_files})
            lifecycle._feedback(note)
            raise EmptyDiffError(
                "empty diff for a PR whose changed files are unknown"
                if pr.reported_files is None else
                f"empty diff for a PR with {pr.reported_files} changed files",
                pr=pr, files=pr.reported_files,
            )
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
                    f"out by skip patterns (lockfiles, binaries, build dirs, "
                    f"this repository's ignore globs, etc.):"
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
            if not pr.raw_diff or not pr.raw_diff.strip():
                short = "the pull request has no diff content"
            elif pr.skipped_files:
                short = (
                    f"all {len(pr.skipped_files)} changed "
                    f"{'file was' if len(pr.skipped_files) == 1 else 'files were'} "
                    f"filtered out (lockfiles, binaries, generated files or this "
                    f"repository's ignore globs)"
                )
            else:
                short = "the diff could not be parsed"
            stages.end("gate_hunks", "skipped",
                       "No reviewable changes: " + short + ".", ends_run=True)
            lifecycle.skipped(short, feedback=actions.status_feedback)
            return ReviewRunResult(batch=batch, posted=False, provider_response={})
        hunk_files = len({h.file_path for h in pr.hunks})
        stages.end("gate_hunks", "success",
                   f"{len(pr.hunks)} hunk{'s' if len(pr.hunks) != 1 else ''} in "
                   f"{hunk_files} file{'s' if hunk_files != 1 else ''} to review"
                   + (f"; {len(pr.skipped_files)} skipped."
                      if pr.skipped_files else "."))

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

        # The named-off agents, plus every opt-in agent (built-in off, e.g.
        # business_logic) the effective `enabled_agents` does not switch on —
        # `review_defaults.AGENT_PARTICIPATION_DEFAULTS`. `named_off` is kept
        # apart for the unknown-name warning below: an opt-in agent added by
        # its default is not something an operator wrote.
        from src.review.review_defaults import effective_disabled_agents

        named_off = {
            str(a).strip().lower()
            for a in ((policy or {}).get("disabled_agents") or [])
        }
        disabled_agents = named_off | effective_disabled_agents(
            None, (policy or {}).get("enabled_agents"))
        # The off-by-default agents nobody opted in. Kept apart from
        # `disabled_agents` because that set means "an operator switched this
        # off" — it is logged as a warning and checked for stale names — and
        # this one means "the built-in roster does not run it", which is the
        # normal state of every review and worth nothing louder than debug.
        dormant_agents = self._dormant_agents(policy, disabled_agents)
        # `business_logic_auto = when_task_found`: the agent nobody opted in
        # runs when the pull request names a Jira task that was read. Never
        # against an explicit "off": naming it in `disabled_agents` always
        # wins, here as everywhere else.
        if (
            "business_logic" in disabled_agents
            and "business_logic" not in named_off
            and self._task_auto_enabled(policy, context.task_context)
        ):
            disabled_agents.discard("business_logic")
            logger.info("business_logic_switched_on_by_task pr=%s tasks=%s",
                        pr.number, [t.key for t in context.task_context.tasks])

        # ── "🔄 Celmis is reviewing this PR…" — posted now, after every skip
        # gate (a skipped PR gets no placeholder to take back) and before the
        # engine spends anything, so the author sees the review has begun.
        # Rewritten in place by the final summary: same comment, same id.
        if _policy_value(policy, "started_comment_enabled", True):
            if engine == "claude_code":
                roster = ["claude_code"]
            else:
                roster = [a.name for a in self.agents
                          if a.name not in disabled_agents
                          and a.name not in dormant_agents]
                if self._verifier_enabled(policy, disabled_agents)[0]:
                    roster.append("verifier")
            lifecycle.started(roster, started_at=_display_time(batch.started_at),
                              template=actions.message_started,
                              first_review=self._is_first_review(
                                  pr, provider_name, workspace_id))

        # ── The PR overview + walkthrough: one cheap LLM call, started now so
        # it runs alongside the engine instead of after it. Only when the
        # comment will actually be posted — a dry run has no reader for it.
        summary_job = None
        wants_summary = batch.rich_summary or actions.summary_target == "description"
        if wants_summary and post_comments and not dry_run:
            stages.begin("summary", "Generate summary")
            summary_job = _PRSummaryJob.start(
                self._summary_pr(pr, agent_pr, ignore_globs), context,
                language=_policy_value(policy, "review_language", None),
                instructions=_policy_value(policy, "summary_instructions", None),
            )
            if summary_job is None:
                stages.end("summary", "skipped",
                           "No model client is configured for the overview call.")
        elif not wants_summary:
            stages.add("summary", "Generate summary", "skipped",
                       "The repository uses the compact summary; no overview call.",
                       duration_ms=0)
        else:
            stages.add("summary", "Generate summary", "skipped",
                       "Nothing will be posted on this run (dry run or posting "
                       "off), so no overview was generated.", duration_ms=0)

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
            from src.review.policy_rules import render_policy_rules

            # This engine reads the raw text, not the hunks, so the ignore
            # globs reach it as a diff with those files' sections cut out.
            engine_pr = pr
            if ignore_globs:
                import dataclasses
                engine_pr = dataclasses.replace(pr, raw_diff=review_diff)
            stages.begin("agent:claude_code", "Agent: claude_code")
            cr = run_claude_review(
                engine_pr, user_id=user_id, workspace_id=workspace_id,
                # One reviewer plays every agent, so it gets the rules
                # addressed to particular agents as well as the shared ones.
                custom_rules="\n\n".join(p for p in (
                    # The team's base instruction first, as every API-engine
                    # agent carries it right after its own prompt.
                    base_instruction_block(getattr(context, "base_instruction", "")),
                    # Then every finder's team guidelines, each in its own
                    # delimited block — the same text the API engine's agents
                    # carry after their prompts. Replacement prompts do not
                    # reach this engine (it has one prompt of its own); the
                    # guidelines, which only add, do.
                    claude_engine_guidelines(context, [
                        a.name for a in self.agents
                        if isinstance(a, LLMReviewAgent)
                        and a.name not in disabled_agents
                        and a.name not in dormant_agents
                    ]),
                    context.custom_rules,
                    render_policy_rules(policy, engine_pr.changed_files).targeted,
                ) if p),
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
                stages.end("agent:claude_code", "failed",
                           "The Claude Code review did not complete "
                           f"(after {int(getattr(cr, 'turns', 0) or 0)} turns).")
            else:
                # Sorted worst-first like the agent path's prefilter output,
                # so the providers' inline cap never cuts a critical for a nit.
                batch.findings = _drop_ignored(
                    _sort_by_severity(_cite_review_rules(list(cr.findings), policy)),
                    ignore_globs)
                batch.summary = cr.summary
                batch.agents_run.append("claude_code")
                stages.end("agent:claude_code", "success",
                           f"{len(batch.findings)} finding"
                           f"{'s' if len(batch.findings) != 1 else ''} in "
                           f"{int(getattr(cr, 'turns', 0) or 0)} turns.",
                           meta={"findings": len(batch.findings),
                                 "turns": int(getattr(cr, "turns", 0) or 0)})
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
                lifecycle=lifecycle, summary_job=summary_job,
                context=context, policy=policy, stages=stages,
            )

        # ── Run agents in parallel (minus the ones the policy switched off;
        # `disabled_agents` was read above, before the started comment) ──
        # A name that no longer names an agent is SAID, not silently ignored.
        # Policies written before the restructure may still carry "architect",
        # "quality" or "tests"; matching them against nothing would quietly
        # turn "I switched the architect off" into "everything runs". They are
        # deliberately NOT mapped onto the successor agents either — a policy
        # that disabled the old tests sidecar must not disable the main
        # finder that inherited its remit. The operator re-decides.
        # Names of the participation map (performance, business_logic) are
        # known even before this build dispatches them: switching off an
        # agent that is not running yet is a decision, not a typo.
        from src.review.review_defaults import AGENT_PARTICIPATION_DEFAULTS

        roster = {a.name for a in self.agents} | {"verifier"}
        unknown = named_off - roster - set(AGENT_PARTICIPATION_DEFAULTS)
        if unknown:
            logger.warning(
                "disabled_agents_unknown names=%s roster=%s — these entries "
                "disable nothing; the agent names changed in the Phase-18 "
                "restructure (architect→contract, quality/tests→defect) and "
                "old names are not mapped onto their successors on purpose",
                sorted(unknown), sorted(roster))
        dispatched_at = time.time()
        agent_results = self._run_agents_parallel(
            context, disabled_agents=disabled_agents, dormant_agents=dormant_agents,
        )
        for a in self.agents:
            if a.name in disabled_agents:
                stages.add(f"agent:{a.name}", f"Agent: {a.name}", "skipped",
                           "Switched off by the repository policy."
                           if a.name in named_off else
                           "Off by default; not enabled for this repository.",
                           duration_ms=0)
        for r in agent_results:
            _record_agent_stage(
                stages, r, dispatched_at=dispatched_at,
                finished_at=getattr(self, "_agent_finished_at", {}).get(r.agent),
            )

        terse = terse_reply_note(agent_results, pr)
        if terse is not None:
            batch.parameter_adjustments.append(terse)

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
            elif r.tokens_in or r.tokens_out or (r.error is None and r.model_used):
                # A clean agent counts only if it called a model: cve and
                # structural answer from the code alone (no tokens, no
                # model_used) and blanked the total of every run they were in
                # — every review showed "unknown cost" while the spend ledger
                # held the real figure.
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
            # An agent that chose not to look — the business-logic agent on a
            # pull request that states no intent. Skipped, not failed and not
            # run: it lands in `agents_skipped` with its reason beside it, and
            # NOT in `agent_errors`, whose presence is what makes the summary
            # call a review thinner than a full one. Nothing went wrong.
            skip_reason = getattr(r, "skip_reason", None)
            if skip_reason and not r.error:
                if r.agent not in batch.agents_skipped:
                    batch.agents_skipped.append(r.agent)
                batch.skip_reasons[r.agent] = skip_reason
                logger.info("agent_skipped_itself agent=%s reason=%s",
                            r.agent, skip_reason)
                continue
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
        stages.begin("verifier", "Verifier")
        pre = self.verifier.prefilter(
            all_findings,
            suppressed_rules=self._suppressed_rules(policy, self.settings),
        )
        batch.dropped_by_rule = dict(pre.dropped_by_rule)
        batch.dropped_duplicates = pre.dropped_dedup
        batch.dropped_near_duplicates = pre.dropped_near_duplicate
        batch.dropped_low_confidence = pre.dropped_low_confidence
        # What the team already judged: after the deterministic prefilter, before
        # the model's veto, so the veto never spends tokens on a finding the
        # team has dismissed. `shadow` (the built-in) only reports.
        candidates = self._learned_filter(
            batch, pre.kept, policy=policy, workspace_id=workspace_id,
            pr=pr, stages=stages, provider=provider, provider_name=provider_name)
        veto_on, veto_reason = self._verifier_enabled(policy, disabled_agents)
        if not veto_on:
            batch.findings = list(candidates)
            batch.agents_skipped.append("verifier")
            logger.info(
                "verifier_llm_skipped reason=%s findings=%d "
                "by_rule=%d dedup=%d near_dup=%d low_conf=%d",
                veto_reason, len(candidates), sum(pre.dropped_by_rule.values()),
                pre.dropped_dedup, pre.dropped_near_duplicate,
                pre.dropped_low_confidence,
            )
            stages.end("verifier", "skipped",
                       f"LLM veto off ({veto_reason.replace('_', ' ')}); the "
                       f"deterministic prefilter kept {len(candidates)} of "
                       f"{len(all_findings)} findings.",
                       meta={"kept": len(candidates), "candidates": len(all_findings)})
        else:
            v_result = self.verifier.llm_pass(candidates, context)
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
                stages.end("verifier", "failed",
                           f"The LLM veto did not answer; {len(v_result.kept)} "
                           f"findings were kept unfiltered.",
                           meta={"kept": len(v_result.kept),
                                 "tokens_in": v_result.tokens_in,
                                 "tokens_out": v_result.tokens_out})
            else:
                stages.end("verifier", "success",
                           f"Kept {len(v_result.kept)} of {len(all_findings)} "
                           f"findings ({v_result.dropped_llm_filter} vetoed).",
                           meta={"kept": len(v_result.kept),
                                 "vetoed": v_result.dropped_llm_filter,
                                 "tokens_in": v_result.tokens_in,
                                 "tokens_out": v_result.tokens_out})

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
        stages.begin("breaking_change", "Breaking-change check")
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
            n_bc = len(bc_result.findings or [])
            stages.end("breaking_change", "success",
                       f"{n_bc} breaking change{'s' if n_bc != 1 else ''} found.",
                       meta={"findings": n_bc})
        except _BudgetExhausted:
            # already named in agents_skipped and in the banner
            stages.end("breaking_change", "skipped",
                       f"Skipped: the review passed its {budget}s time budget.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("breaking_change_failed err=%s", exc)
            stages.end("breaking_change", "failed",
                       f"The breaking-change check raised {type(exc).__name__}.")

        # ── Compliance agent (Stage 14) — one LLM call per matching rule.
        # Findings tagged agent="compliance" flow through the verdict
        # layer; any blocking failure downgrades APPROVE to REJECT.
        stages.begin("compliance", "Compliance rules")
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
            n_c = len(c_result.findings or [])
            stages.end("compliance", "success",
                       f"{n_c} compliance finding{'s' if n_c != 1 else ''}.",
                       meta={"findings": n_c, "tokens_in": c_result.tokens_in,
                             "tokens_out": c_result.tokens_out})
        except _BudgetExhausted:
            # skipped, not failed — the two are different rows
            stages.end("compliance", "skipped",
                       f"Skipped: the review passed its {budget}s time budget.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("compliance_agent_failed err=%s", exc)
            batch.agents_failed.append("compliance")
            stages.end("compliance", "failed",
                       f"The compliance check raised {type(exc).__name__}.")

        # ── Worst first, again. Breaking-change and compliance findings were
        # appended AFTER the prefilter's severity sort, so on a review with
        # more findings than `max_inline_comments` a critical breaking change
        # sat at the tail of the list and the providers' cap cut it while
        # twenty warnings posted. A stable sort keeps the prefilter's
        # confidence order inside each severity.
        batch.findings = _drop_ignored(
            _sort_by_severity(_cite_review_rules(batch.findings, policy)),
            ignore_globs)

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
            lifecycle=lifecycle, summary_job=summary_job,
            context=context, policy=policy, stages=stages,
        )

    def _check_requirements(
        self, batch: ReviewBatch, *, context, policy, stages: StageRecorder,
    ) -> None:
        """The "Requirements check" stage. Never raises, never changes the
        verdict. No stage is recorded when it does not apply (no readable
        task, the agent did not run, the setting is off)."""
        task = getattr(batch, "task_context", None)
        if task is None or not task.ok or "business_logic" not in batch.agents_run:
            return
        try:
            from src.review.task_context import checklist
            from src.review.task_context.service import task_settings

            mode = task_settings(policy)["requirements_mode"]
            if mode == "off":
                return
            stages.begin("requirements", "Requirements check")
            if not checklist.criteria_of(task):
                checklist.attach_requirements(batch, task, None, mode=mode)
                stages.end("requirements", "skipped",
                           "Skipped: the task lists no acceptance criteria, so the "
                           "change was held to its description.")
                return
            findings = list(batch.findings)
            error = None
            pr = getattr(context, "pull_request", None) or batch.pull_request
            if mode == "findings":
                rows = checklist.rows_from_findings(
                    task, findings, incremental=getattr(pr, "scope", None) is not None)
            else:
                agent_llm = None
                try:
                    from src.review.agents.base import agent_llm_settings

                    agent_llm = agent_llm_settings(context, "business_logic")
                except Exception:  # noqa: BLE001
                    agent_llm = None
                res = checklist.check_requirements(
                    pr, task, findings, llm_client=getattr(context, "llm_client", None),
                    agent_llm=agent_llm)
                rows, error = res.rows, res.error
                batch.tokens_in += res.tokens_in
                batch.tokens_out += res.tokens_out
                if res.cost_usd is not None and batch.cost_usd is not None:
                    batch.cost_usd = round(batch.cost_usd + res.cost_usd, 6)
                if error:
                    mode = "findings"        # what the comment shows is the findings view
            checklist.attach_requirements(batch, task, rows, mode=mode)
            gaps = sum(1 for r in rows if r.verdict in ("partial", "missing", "contradicts"))
            note = f"{len(rows)} criteria checked, {gaps} with a gap."
            if error:
                note = (f"The model's answer could not be used ({error}); the comment "
                        f"shows the gaps the review reported. {note}")
            stages.end("requirements", "success", note,
                       meta={"criteria": len(rows), "gaps": gaps, "mode": mode})
        except Exception as exc:  # noqa: BLE001 — a checklist never fails a review
            logger.warning("requirements_stage_failed pr=%s err_type=%s",
                           getattr(batch.pull_request, "number", "?"), type(exc).__name__)
            stages.end("requirements", "failed",
                       f"The requirements check raised {type(exc).__name__}.")

    def _resolve_earlier_issues(
        self, batch: ReviewBatch, pr: PullRequest, provider, provider_name: str,
        *, policy, user_id: str, workspace_id: str, stages: StageRecorder,
    ) -> None:
        """The "Resolve earlier issues" stage. Never raises, never changes the
        verdict: an unreadable branch leaves every issue open."""
        stages.begin("resolve_issues", "Resolve earlier issues")
        try:
            from src.review.issue_resolver import (
                IssueSettings,
                attach_earlier_issues,
                plan_review_resolutions,
            )

            cfg = IssueSettings(
                auto_resolve=bool(_policy_setting(policy, "issues_auto_resolve")),
                llm_verify=bool(_policy_setting(policy, "issues_resolve_llm_verify")),
                max_llm=max(0, int(_policy_setting(policy, "issues_resolve_max_llm"))),
                announce=bool(_policy_setting(policy, "issues_announce_resolved")),
            )
            if not cfg.auto_resolve:
                stages.end("resolve_issues", "skipped",
                           "Skipped: automatic resolution of earlier issues is off "
                           "for this repository.")
                return
            found = plan_review_resolutions(
                pr, provider=provider, workspace_id=workspace_id,
                user_id=user_id, settings=cfg)
            attach_earlier_issues(batch, found)
            if found is None:
                stages.end("resolve_issues", "skipped",
                           "Skipped: no earlier issues on the files this pull "
                           "request changes.")
                return
            n = len(found.resolved)
            note = (f"{n} earlier issue{'s' if n != 1 else ''} fixed on the "
                    f"target branch; {found.still_open} still open.")
            if found.unreadable:
                note += f" {found.unreadable} could not be checked."
            stages.end("resolve_issues", "success", note,
                       meta={"resolved": n, "still_open": found.still_open,
                             "unreadable": found.unreadable,
                             "llm_calls": found.llm_calls})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_issues_failed pr=%s err_type=%s",
                           getattr(pr, "number", "?"), type(exc).__name__)
            stages.end("resolve_issues", "failed",
                       f"Checking earlier issues raised {type(exc).__name__}.")

    def _finish_review(
        self, batch: ReviewBatch, pr: PullRequest, provider, provider_name: str,
        *, dry_run: bool, post_comments: bool, user_id: str, workspace_id: str,
        t0: float,
        lifecycle: _ReviewLifecycle | None = None,
        summary_job: _PRSummaryJob | None = None,
        context=None,
        policy=None,
        stages: StageRecorder | None = None,
    ) -> ReviewRunResult:
        """Shared tail for every engine: reviewer assignment, notifications,
        comment posting. The brain differs; the plumbing doesn't."""
        if stages is None:
            stages = StageRecorder()
        # ── The overview + walkthrough the summary comment renders. Joined
        # here, after the engine, with its own short deadline; whatever went
        # wrong, the comment is rendered without the two sections.
        if summary_job is not None:
            outcome = summary_job.apply_to(batch)
            ok, why = outcome if isinstance(outcome, tuple) else (True, "")
            stages.end("summary", "success" if ok else "failed", why)
        # ── Requirements check: the Jira task's acceptance criteria as a
        # checklist in the completed comment. Needs a readable task and the
        # business-logic agent to have run; records nothing otherwise.
        self._check_requirements(batch, context=context, policy=policy, stages=stages)
        # ── Earlier issues: which backlog issues of merged PRs does the
        # target branch's HEAD show as fixed? Reads only; the ledger write is
        # `issues._record`'s, after the run, so a dry run changes nothing.
        self._resolve_earlier_issues(
            batch, pr, provider, provider_name, policy=policy,
            user_id=user_id, workspace_id=workspace_id, stages=stages)
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

        # ── The summary in the PR description (summary_target="description").
        # Written BEFORE the review is posted, so the comment can leave the
        # walkthrough out exactly when the description really holds it.
        description_result: dict | None = None
        if (post_comments and not dry_run
                and batch.pr_actions.summary_target == "description"):
            description_result = self._write_description(
                provider, batch, context=context, policy=policy,
            )

        # ── Post comments ──
        posted = False
        provider_response: dict = {}
        if not post_comments:
            stages.add("publish", "Publish to provider", "skipped",
                       "Posting was not requested for this run; the review is "
                       "kept here only.", duration_ms=0)
        if post_comments:
            stages.begin("publish", "Publish to provider")
            try:
                provider_response = provider.post_review(batch, dry_run=dry_run)
                posted = not dry_run
                stages.end("publish", "success" if posted else "skipped",
                           _publish_sentence(provider_name, batch, provider_response,
                                             dry_run=dry_run))
            except PullRequestProviderError as exc:
                logger.error("review_post_failed err=%s", exc)
                provider_response = {"error": str(exc)}
                stages.end("publish", "failed",
                           f"The {_provider_label(provider_name)} API refused the "
                           f"review; the findings are kept on this run.")
                # The placeholder this run posted would otherwise go on
                # saying "reviewing…" over a review that never arrived. The
                # provider's own words stay in the run record (above); the
                # pull request gets a sentence without them.
                if lifecycle is not None:
                    lifecycle.failed(
                        "the review could not be posted to the pull request "
                        "(the provider refused it)"
                    )
            # A summary that did not arrive is said out loud — Bitbucket and
            # GitLab write per comment and used to report "posted" with the
            # verdict-carrying summary missing.
            summary_error = (
                provider_response.get("summary_error")
                if isinstance(provider_response, dict) else None
            )
            if summary_error:
                logger.error(
                    "review_summary_not_delivered provider=%s pr=%d err=%s",
                    provider_name, pr.number, summary_error,
                )
            if (posted and isinstance(provider_response, dict)
                    and provider_response.get("inline_comments")):
                self._remember_posted(batch, pr, provider_name, provider_response,
                                      policy=policy, workspace_id=workspace_id)

        if description_result is not None and isinstance(provider_response, dict):
            provider_response["description"] = description_result
        return ReviewRunResult(batch=batch, posted=posted, provider_response=provider_response)

    def _write_description(self, provider, batch: ReviewBatch, *, context, policy) -> dict:
        """Put our overview + walkthrough into the PR description. Never raises.

        `summary_existing_description` decides what happens to the author's
        text on the first write, `summary_on_new_commits` what a later run
        does to our block (see `pr_actions.compose_description`). "complement"
        spends ONE short LLM call, booked as `review_summary` like the
        overview; any failure there falls back to "append".
        """
        from src.review.pr_actions import compose_description, description_insights

        pr = batch.pull_request
        actions = batch.pr_actions
        insights = description_insights(batch)
        if not insights:
            logger.info("review_description_skipped reason=no_summary pr=%s", pr.number)
            return {"written": False, "error": "no summary to write"}
        update = getattr(provider, "update_description", None)
        if not callable(update):
            return {"written": False, "error": "not supported by this provider"}

        def complement(author: str, ours: str) -> str | None:
            return self._complement_description(batch, author, ours,
                                                context=context, policy=policy)

        def transform(current: str) -> str | None:
            return compose_description(
                current, insights=insights, commit=pr.head_sha or "",
                existing_mode=actions.summary_existing_description,
                new_commits_mode=actions.summary_on_new_commits,
                complement=complement,
                limit=getattr(provider, "description_max_chars", None),
                language=batch.review_language,
            )

        try:
            result = update(pr, transform)
        except Exception as exc:  # noqa: BLE001 — the description never fails a review
            logger.warning("review_description_failed pr=%s err_type=%s err=%s",
                           pr.number, type(exc).__name__, str(exc)[:200])
            return {"written": False, "error": type(exc).__name__}
        if not isinstance(result, dict):
            result = {"written": False, "error": "unreadable provider answer"}
        # The comment drops its walkthrough only when the description holds
        # one: written now, or left as it was because our block is already
        # there ("nothing" on new commits, or an identical rewrite).
        if result.get("written") or result.get("unchanged"):
            batch.summary_in_description = True
        else:
            logger.warning("review_description_not_written pr=%s err=%s",
                           pr.number, result.get("error"))
        return result

    @staticmethod
    def _complement_description(batch: ReviewBatch, author: str, ours: str,
                                *, context, policy) -> str | None:
        from src.review.pr_summary import complement_description

        client = getattr(context, "llm_client", None)
        if client is None:
            return None
        try:
            from src.review.agents.base import agent_llm_settings

            agent_llm = agent_llm_settings(context, "verifier")
        except Exception:  # noqa: BLE001
            agent_llm = None
        res = complement_description(
            batch.pull_request, author, ours, llm_client=client, agent_llm=agent_llm,
            language=_policy_value(policy, "review_language", None),
        )
        batch.tokens_in += res.tokens_in
        batch.tokens_out += res.tokens_out
        if res.cost_usd is not None and batch.cost_usd is not None:
            batch.cost_usd = round(batch.cost_usd + res.cost_usd, 6)
        if res.error:
            logger.info("review_description_complement_fallback pr=%s reason=%s",
                        batch.pull_request.number, res.error)
        return res.text

    # ─── Agent context build ───────────────────────────────────

    def _build_context(
        self,
        pr: PullRequest,
        *,
        policy: dict | None = None,
        user_id: str = "default",
        workspace_id: str = "default",
        provider=None,
        task_override=None,
    ) -> AgentContext:
        """Build AgentContext with graph blast radius + cross-repo callers.
        `task_override` is a Jira task somebody named (the on-demand check):
        it is used as it is and the pull request's own text is not searched.

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
        drift_md = self._build_cross_repo_drift(
            pr, workspace_id=workspace_id, user_id=user_id)
        custom_rules = self._build_custom_rules(pr, policy)
        agent_custom_rules = self._build_agent_custom_rules(pr, policy)
        mcp_evidence = self._build_mcp_evidence(pr, user_id=user_id)
        if mcp_evidence:
            # Prepend MCP evidence to custom_rules so every agent sees it
            # (custom_rules already broadcast via base._compose_effective_system_prompt).
            custom_rules = (
                mcp_evidence + ("\n\n" + custom_rules if custom_rules else "")
            )
        llm_client, agent_llm = self._build_llm_client(user_id, workspace_id, policy)
        task_context = task_override if task_override is not None else (
            self._build_task_context(
                pr, policy=policy, user_id=user_id, workspace_id=workspace_id,
                provider=provider))

        return AgentContext(
            pull_request=pr,
            task_context=task_context,
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
            repo_agent_guidelines=dict((policy or {}).get("agent_prompt_guidelines") or {}),
            repo_guidelines_extend=list((policy or {}).get("agent_guidelines_extend") or []),
            agent_custom_rules=agent_custom_rules,
            review_language=((policy or {}).get("review_language") or None),
            # How every suggestion is written — the team's base instruction,
            # clamped to its 2000-character cap here so no prompt can carry
            # more whatever the settings layer let through.
            base_instruction=clamp_base_instruction(
                _policy_value(policy, "base_instruction", None)),
        )

    # ─── Jira task context (business-logic agent) ──

    @classmethod
    def _task_context_wanted(cls, policy: dict | None) -> bool:
        """Will the business-logic agent run — or could it, were a task found?

        The Jira read is a network call, so it is made only for a review that
        can use it: the agent is not named off, and it is either opted in or
        `business_logic_auto` lets a found task turn it on.
        """
        from src.review.review_defaults import effective_disabled_agents
        from src.review.task_context.service import task_settings

        named_off = {
            str(a).strip().lower() for a in ((policy or {}).get("disabled_agents") or [])
        }
        if "business_logic" in named_off:
            return False
        cfg = task_settings(policy)
        if not cfg["enabled"]:
            return False
        opted_out = "business_logic" in effective_disabled_agents(
            None, (policy or {}).get("enabled_agents"))
        return not opted_out or cfg["auto"] == "when_task_found"

    @staticmethod
    def _task_auto_enabled(policy: dict | None, task) -> bool:
        from src.review.task_context.service import task_settings

        return bool(
            task is not None and task.ok
            and task_settings(policy)["auto"] == "when_task_found"
        )

    def _build_task_context(
        self, pr: PullRequest, *, policy: dict | None, user_id: str,
        workspace_id: str, provider,
    ):
        """The Jira task(s) the pull request names, or None when this review
        will not use them. Never raises (the resolver reports its own
        failures as a status the agent turns into a skip reason)."""
        if not self._task_context_wanted(policy):
            return None
        from src.review.task_context.service import resolve_task_context

        def commit_messages() -> list[str]:
            fetch = getattr(provider, "fetch_commit_messages", None)
            if fetch is None:
                return []
            try:
                return list(fetch(pr) or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("task_context_commits_failed pr=%s err=%s",
                            getattr(pr, "number", "?"), type(exc).__name__)
                return []

        return resolve_task_context(
            pr, workspace_id=workspace_id, user_id=user_id, policy=policy,
            commit_messages=commit_messages)

    def run_single_agent(
        self, pr: PullRequest, agent_name: str, *, task_override=None,
        policy: dict | None = None, user_id: str = "default",
        workspace_id: str = "default", provider=None,
    ) -> SingleAgentRun:
        """Run ONE agent on `pr`, forced on, and hand back what it found.

        For the on-demand business-logic check (`@celmis -v business-logic`):
        the repository context is built exactly as for a review
        (`_build_context`: graph, rules, memories, the repository's ignore
        globs), the agent runs through `_run_agents_parallel`, and nothing
        else happens — no gates, no verifier, no summary, no comment, nothing
        recorded on the pull request. The policy's switches (`disabled_agents`,
        `enabled_agents`, `business_logic_auto`, `task_project_keys`) do not
        apply: somebody asked for this agent by name. `task_override` is the
        Jira task they named. Never raises for an agent's own failure (it is
        on `result.error`); a context that cannot be built does raise.
        """
        agent_pr = pr
        globs = list((policy or {}).get("ignore_globs") or [])
        if globs:
            import dataclasses

            agent_pr = dataclasses.replace(pr, raw_diff=self._apply_ignore_globs(pr, globs))
        context = self._build_context(
            agent_pr, policy=policy, user_id=user_id, workspace_id=workspace_id,
            provider=provider, task_override=task_override)
        results = self._run_agents_parallel(context, only=agent_name)
        result = results[0] if results else AgentRunResult(
            agent=agent_name, error=f"no agent named {agent_name}")
        changed = {str(p) for p in (pr.changed_files or [])}
        if changed:
            result.findings = [f for f in result.findings if f.file_path in changed]
        return SingleAgentRun(result=result, context=context)

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

    # ─── Per-repo ignore globs ──────────────────────────────────

    @staticmethod
    def _apply_ignore_globs(pr: PullRequest, globs: list[str]) -> str:
        """Drop the hunks of ignored files from `pr`, in place, and return the
        raw diff without their sections.

        Each ignored file is added once to `pr.skipped_files`, tagged so the
        skip message can tell a repo's own exclusion from a lockfile.
        """
        from src.review.ignore_globs import filter_raw_diff, path_ignored

        kept = []
        ignored: list[str] = []
        for h in pr.hunks:
            if (path_ignored(h.file_path, globs)
                    or path_ignored(h.old_file_path or "", globs)):
                if h.file_path not in ignored:
                    ignored.append(h.file_path)
                continue
            kept.append(h)
        if ignored:
            pr.hunks = kept
            pr.skipped_files = list(pr.skipped_files) + [
                f"{p} (ignore glob)" for p in ignored
            ]
            logger.info("ignore_globs_applied pr=%d files=%d globs=%d",
                        pr.number, len(ignored), len(globs))
        return filter_raw_diff(pr.raw_diff or "", globs)

    # ─── Stage 10: per-repo policy loading + rules formatting ──

    @staticmethod
    def _summary_pr(pr: PullRequest, agent_pr: PullRequest, ignore_globs: list[str]) -> PullRequest:
        """The PR the overview is written about: the whole change, also when an
        incremental review's agents read only the new commits."""
        if pr.scope is None:
            return agent_pr
        import dataclasses

        from src.review.ignore_globs import filter_raw_diff

        return dataclasses.replace(
            pr, raw_diff=filter_raw_diff(pr.scope.full_raw_diff, ignore_globs),
            hunks=list(pr.scope.full_hunks),
            skipped_files=list(dict.fromkeys(
                [*pr.scope.full_skipped_files, *pr.skipped_files])),
            scope=None)

    def _decide_scope(
        self, pr: PullRequest, batch: ReviewBatch, *, policy, actions, lifecycle,
        stages: StageRecorder, request: ReviewRequest, workspace_id: str,
        language: str, provider_name: str, provider, ignore_globs: list[str],
    ) -> ReviewRunResult | str | None:
        """The "scope" stage. Returns a ReviewRunResult when the run ends here
        (nothing new to read), the filtered diff text of the increment when
        `pr` was narrowed to it, or None for a whole-PR review.

        Every doubt ends in the whole PR: a review that reads too much is
        merely slow, one that reads too little is wrong.
        """
        from src.review import scope as scope_mod

        setting = str(_policy_setting(policy, "review_scope") or "incremental")
        state = None
        if setting == "incremental" and not request.force and request.scope != "full":
            try:
                from src.review import pr_state

                state = pr_state.load(workspace_id, provider_name, pr.repo, pr.number)
            except Exception as exc:  # noqa: BLE001 — fail open: a whole review
                logger.warning("scope_state_unreadable pr=%s err=%s", pr.number, exc)
        decision = scope_mod.decide_scope(
            setting, request,
            last_reviewed_sha=state.last_reviewed_sha if state else None,
            head_sha=pr.head_sha,
            list_commits=lambda: provider.list_pr_commits(pr.repo, pr.number),
        )
        meta = {"scope": decision.mode, "reason": decision.code}
        batch.scope_mode = decision.mode if decision.mode != "skip" else None

        def _end_quietly(code: str, reason: str, *, base_sha: str | None) -> ReviewRunResult:
            logger.info("review_skipped reason=%s pr=%d", code, pr.number)
            batch.summary = f"Review skipped — {reason}"
            batch.verdict = ReviewVerdict.SKIPPED
            batch.scope_skip = code
            batch.scope_mode = None
            batch.mark_complete()
            stages.end("scope", "skipped", reason, ends_run=True, meta={
                **meta, "reason": code, "base_sha": (base_sha or "")[:12] or None})
            lifecycle.skipped(
                messages.t(f"incremental.skip.{code}", language,
                           sha=((state.last_reviewed_sha if state else None) or "")[:7]),
                feedback=False)
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        if decision.mode == "skip":
            return _end_quietly(decision.code, decision.reason, base_sha=decision.base_sha)

        if decision.mode == "incremental":
            narrowed = self._narrow_to_increment(
                pr, decision, provider=provider, ignore_globs=ignore_globs)
            if narrowed is None:
                # Only files this review never reads (lockfiles, ignore globs):
                # reading the whole PR again would cost a full review for nothing.
                return _end_quietly(
                    "no_reviewable_new_changes",
                    f"the new commits since `{scope_mod._short(decision.base_sha)}` change "
                    f"only files that are not reviewed.", base_sha=decision.base_sha)
            if isinstance(narrowed, str):
                batch.scope_mode = "full"
                # `narrowed` is the reason the increment could not be read.
                stages.end("scope", "success", f"Full review: {narrowed}",
                           meta={**meta, "scope": "full", "reason": "increment_unreadable"})
                return None
            # The whole PR's skipped files, then the increment's own.
            batch.skipped_files = list(dict.fromkeys(
                [*pr.scope.full_skipped_files, *pr.skipped_files]))
            batch.scope_mode = "incremental"
            files = len({h.file_path for h in pr.hunks})
            stages.end("scope", "success",
                       f"{decision.reason} {files} file{'s' if files != 1 else ''} "
                       f"changed in them; the rest of the pull request is not read again.",
                       meta={**meta, "base_sha": (decision.base_sha or "")[:12],
                             "new_commits": decision.new_commits, "files": files})
            return narrowed[0]

        stages.end("scope", "success", decision.reason, meta=meta)
        return None

    def _narrow_to_increment(
        self, pr: PullRequest, decision, *, provider, ignore_globs: list[str],
    ):
        """Swap `pr`'s diff for the increment's, keeping the whole PR on
        `pr.scope`. Returns (filtered_increment_diff,) on success, None when
        the push touches only files nobody reads, else a sentence saying why
        the whole PR is reviewed instead (and `pr` untouched)."""
        from src.review import scope as scope_mod
        from src.review.diff import parse_unified_diff
        from src.review.models import ScopeInfo

        try:
            raw = provider.fetch_incremental_diff(
                pr.repo, pr.number, decision.base_sha, pr.head_sha)
        except Exception as exc:  # noqa: BLE001
            logger.warning("incremental_diff_failed pr=%s err=%s", pr.number,
                           type(exc).__name__)
            raw = None
        if not raw or not raw.strip():
            return "the provider could not return the changes since the last reviewed commit."
        inc_hunks, inc_skipped = parse_unified_diff(raw, settings=self.settings)
        # The files the PR's own diff does not show are the target branch's,
        # pulled in by a merge commit: not the author's, not reviewed.
        kept = scope_mod.restrict_to_pr(inc_hunks, pr.hunks)
        if not kept:
            # Files the whole PR does not read either (skip patterns, ignore
            # globs): a push of only those has nothing to review. Anything else
            # that cannot be tied to the PR is doubt, and doubt means whole.
            ignored = {re.sub(r" \(ignore glob\)$", "", p) for p in pr.skipped_files or []}
            not_ignored = {h.file_path for h in inc_hunks} - ignored
            if (inc_skipped or inc_hunks) and not not_ignored:
                return None
            return "none of the new changes could be tied to this pull request's own diff."
        if scope_mod.same_added_lines(kept, pr.hunks):
            # The increment is the whole PR: either nothing was reviewed before
            # it, or the provider's "since" diff was really a merge-base diff
            # (Bitbucket's `merge=false` is not confirmed live). The read is
            # the same; the comments must not be matched against a wrong base.
            logger.warning("increment_equals_whole_pr pr=%s", pr.number)
            return "the changes since the last reviewed commit are the whole pull request."
        gone = {h.file_path for h in inc_hunks} - {h.file_path for h in kept}
        raw = scope_mod.drop_diff_sections(raw, gone)
        removed, deleted = scope_mod.removed_old_lines(inc_hunks)
        pr.scope = ScopeInfo(
            base_sha=decision.base_sha or "", new_commits=decision.new_commits,
            reason=decision.reason, full_raw_diff=pr.raw_diff,
            full_hunks=list(pr.hunks), removed_lines=removed, deleted_files=deleted,
            full_skipped_files=list(pr.skipped_files or []),
        )
        pr.hunks = kept
        pr.raw_diff = raw
        pr.skipped_files = list(inc_skipped)
        review_diff = raw
        if ignore_globs:
            review_diff = self._apply_ignore_globs(pr, ignore_globs)
        return (review_diff,)

    def _gate_draft_title_cadence(
        self, pr: PullRequest, batch: ReviewBatch, *, policy, actions, lifecycle,
        stages: StageRecorder, request: ReviewRequest, workspace_id: str,
        language: str, provider_name: str,
    ) -> ReviewRunResult | None:
        """The three gates a person's request skips: draft, title keywords,
        cadence. A ReviewRunResult ends the run (the stage already says why);
        None lets it go on.

        Drafts: skipped unless `run_on_drafts` (repo > workspace > built-in
        False) says this repository reviews them. Read through the resolved
        policy like every other gate here, so the webhook's early skip
        (`review_defaults.run_on_drafts_for_repo`) and this one answer the
        same question from the same rows.
        """
        def _skip(key: str, stage_reason: str, summary: str,
                  meta: dict | None = None) -> ReviewRunResult:
            batch.summary = summary
            batch.verdict = ReviewVerdict.SKIPPED
            batch.mark_complete()
            stages.end(key, "skipped", stage_reason, ends_run=True, meta=meta)
            return ReviewRunResult(batch=batch, posted=False, provider_response={})

        # ── gate_draft ──
        stages.begin("gate_draft", "Check draft status")
        if (pr.is_draft and not _policy_setting(policy, "run_on_drafts")
                and not request.bypasses("gate_draft")):
            logger.info("review_skipped reason=draft pr=%d", pr.number)
            result = _skip(
                "gate_draft",
                "Draft: the pull request is a draft; it is reviewed once it is "
                "marked ready.", "PR is draft — review skipped.")
            lifecycle.skipped("the pull request is a draft",
                              feedback=actions.status_feedback)
            return result
        if pr.is_draft and _policy_setting(policy, "run_on_drafts"):
            stages.end("gate_draft", "success",
                       "Draft — reviewed: run_on_drafts is on.")
        elif pr.is_draft:
            stages.end("gate_draft", "success",
                       "Draft — reviewed: the review was asked for explicitly.")
        else:
            stages.end("gate_draft", "success", "The pull request is not a draft.")

        # ── gate_title ──
        stages.begin("gate_title", "Check title keywords")
        keywords = list(_policy_setting(policy, "ignored_title_keywords") or [])
        matched = title_keyword_match(pr.title, keywords)
        if matched and not request.bypasses("gate_title"):
            logger.info("review_skipped reason=title_keyword pr=%d keyword=%s",
                        pr.number, matched)
            sentence = messages.t("gate.title", "en", keyword=matched)
            result = _skip(
                "gate_title", sentence[:1].upper() + sentence[1:] + ".",
                f"Review skipped — {sentence}.", meta={"keyword": matched})
            # No note on the PR: an edit of a title must not answer with a
            # comment, and the run row says why.
            lifecycle.skipped(messages.t("gate.title", language, keyword=matched),
                              feedback=False)
            return result
        if matched:
            stages.end("gate_title", "success",
                       f"The title contains the ignored keyword \"{matched}\"; "
                       f"reviewed anyway — the review was asked for explicitly.")
        elif keywords:
            stages.end("gate_title", "success",
                       f"The title contains none of the {len(keywords)} ignored "
                       f"keyword{'s' if len(keywords) != 1 else ''}.")
        else:
            stages.end("gate_title", "success",
                       "No ignored title keywords configured.")

        # ── gate_cadence ──
        stages.begin("gate_cadence", "Check review cadence")
        cadence_name = str(_policy_setting(policy, "review_cadence") or "automatic")
        pushes = int(_policy_setting(policy, "auto_pause_pushes"))
        minutes = int(_policy_setting(policy, "auto_pause_window_minutes"))
        if request.bypasses("gate_cadence"):
            stages.end("gate_cadence", "success",
                       f"Cadence '{cadence_name}' not applied: the review was "
                       f"asked for explicitly.", meta={"cadence": cadence_name})
            return None
        state = None
        try:
            from src.review import pr_state

            state = pr_state.load(workspace_id, provider_name, pr.repo, pr.number)
        except Exception as exc:  # noqa: BLE001 — fail open: a review is never lost to this table
            logger.warning("cadence_state_unreadable pr=%s err=%s", pr.number, exc)
        decision = cadence.decide(
            cadence_name, bool(state and state.review_paused),
            state.paused_reason if state else None)
        if decision.action == "review":
            if state is not None and state.review_paused:
                # Only a lapsed mechanical pause gets here (a person's holds):
                # forget it, so the badge and a later `auto_pause` stay honest.
                try:
                    from src.review import pr_state

                    pr_state.resume(workspace_id, provider_name, pr.repo, pr.number)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stale_pause_clear_failed pr=%s err=%s", pr.number, exc)
            stages.end("gate_cadence", "success",
                       f"Cadence '{cadence_name}': this pull request is reviewed "
                       f"automatically.", meta={"cadence": cadence_name})
            return None
        handle = cadence.bot_handle()
        reason = state.paused_reason if state else None
        sentence = cadence.gate_reason(decision, "en", handle=handle, pushes=pushes,
                                       minutes=minutes, reason=reason)
        logger.info("review_skipped reason=%s pr=%d cadence=%s", decision.code,
                    pr.number, cadence_name)
        result = _skip(
            "gate_cadence", sentence[:1].upper() + sentence[1:] + ".",
            f"Review skipped — {sentence}.",
            meta={"cadence": cadence_name, "reason": decision.code})
        lifecycle.skipped(
            cadence.gate_reason(decision, language, handle=handle, pushes=pushes,
                                minutes=minutes, reason=reason),
            feedback=False)
        # The one note that says how to resume: once per pause, never when the
        # repository turned status notes off or the run posts nothing.
        if actions.status_feedback and getattr(lifecycle, "active", False):
            try:
                from src.review import pr_state

                if pr_state.claim_notice(workspace_id, provider_name, pr.repo, pr.number):
                    posted = lifecycle._feedback(cadence.notice(
                        decision, language, handle=handle, pushes=pushes,
                        minutes=minutes, reason=reason,
                        last_reviewed_sha=state.last_reviewed_sha if state else None))
                    if not posted:
                        # Give the claim back: the next delivery tries again
                        # instead of leaving the PR paused with no way to resume.
                        pr_state.release_notice(workspace_id, provider_name,
                                                pr.repo, pr.number)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pause_notice_failed pr=%s err=%s", pr.number, exc)
        return result

    @staticmethod
    def _is_first_review(pr: PullRequest, provider_name: str, workspace_id: str) -> bool | None:
        """Has this PR never been reviewed? None when the database cannot say —
        the placeholder then keeps its plain heading instead of guessing."""
        try:
            from src.review import issues

            count = issues.pr_review_count(
                workspace_id=workspace_id, provider=provider_name,
                repo=pr.repo, number=pr.number,
            )
        except Exception:  # noqa: BLE001 — a greeting never fails a review
            return None
        return count == 0

    def _language_for(self, pr: PullRequest, workspace_id: str) -> str:
        """The bot-text language for a PR whose review stopped before the policy
        stage read it; English when the policy cannot be read."""
        try:
            policy = self._resolved_policy(pr.local_slug, workspace_id)
        except Exception:  # noqa: BLE001 — a note about a lost diff must still go out
            policy = None
        return messages.resolve_language((policy or {}).get("review_language"), workspace_id)

    def policy_for(self, pr: PullRequest, workspace_id: str) -> dict | None:
        """The review policy in force for `pr` (repo over workspace over
        install): what a one-off check outside a review reads its settings from."""
        return self._resolved_policy(pr.local_slug, workspace_id)

    def _resolved_policy(self, repo_slug: str, workspace_id: str) -> dict | None:
        """The repo policy merged over the workspace review defaults.

        The defaults are those of the workspace that OWNS the policy row when
        there is one (the row is keyed by slug alone), else of the workspace
        the review runs for.
        """
        from src.review.review_defaults import blank_policy, merge_policy

        policy = self._load_policy(repo_slug)
        owner = policy.get("workspace_id") if isinstance(policy, dict) else None
        merged = merge_policy(
            policy, self._load_workspace_defaults(owner or workspace_id),
        )
        # The review rules in force (/admin/review-rules): the workspace's
        # active rules plus this repository's, a repository rule replacing a
        # workspace rule of the same title. They ride on the policy so the
        # one renderer (`src.review.policy_rules`) and the citation check
        # read them from the same place; no rules → the policy is untouched.
        rules = self._load_review_rules(owner or workspace_id, repo_slug)
        if rules:
            if merged is None:
                merged = blank_policy()
            if isinstance(merged, dict):
                merged = {**merged, "review_rules": rules}
        # The team's memories (/memories): active ones of the workspace, the
        # repository and its directories, unless the repository switched them
        # off. The renderer keeps a directory memory only when a changed file
        # is under it and cuts the whole to the prompt budget.
        if _policy_setting(merged, "memories_enabled"):
            memories = self._load_memories(owner or workspace_id, repo_slug)
            if memories:
                if merged is None:
                    merged = blank_policy()
                if isinstance(merged, dict):
                    merged = {**merged, "memories": memories}
        return merged

    def _load_memories(self, workspace_id: str, repo_slug: str) -> list[dict]:
        """The active team memories of this review. Blocking; never raises."""
        from src.review.memories import load_active_sync

        return load_active_sync(workspace_id, repo_slug)

    def _load_review_rules(self, workspace_id: str, repo_slug: str) -> list[dict]:
        """The active review rules of this review. Blocking; never raises."""
        from src.review.rules_store import load_active_rules_sync

        return load_active_rules_sync(workspace_id, repo_slug)

    def _load_workspace_defaults(self, workspace_id: str) -> dict | None:
        """The workspace's review defaults, or None. Blocking; never raises."""
        from src.review.review_defaults import load_workspace_defaults_sync

        return load_workspace_defaults_sync(workspace_id)

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
                        "workspace_id": row.workspace_id,
                        "enabled": bool(row.enabled),
                        "prompt_template": row.prompt_template or "",
                        # NULL inherits the workspace review defaults
                        # (`_resolved_policy`), so None is kept as None here —
                        # the same for every field below that a workspace
                        # may default.
                        "target_branches": (
                            None if row.target_branches is None
                            else list(row.target_branches)
                        ),
                        "folder_rules": list(row.folder_rules or []),
                        # Stage 11 — per-agent model overrides (None → workspace default)
                        "architect_model": row.architect_model,
                        "security_model": row.security_model,
                        "quality_model": row.quality_model,
                        "tests_model": row.tests_model,
                        "verifier_model": row.verifier_model,
                        # Stage 12 — per-repo per-agent system_prompt overrides.
                        "agent_prompt_overrides": dict(row.agent_prompt_overrides or {}),
                        # 2.3.2 — per-repo per-agent team guidelines (ADDED
                        # to the prompt) and the agents whose guidelines add
                        # to the workspace's instead of replacing them.
                        "agent_prompt_guidelines": dict(
                            getattr(row, "agent_prompt_guidelines", None) or {}),
                        "agent_guidelines_extend": list(
                            getattr(row, "agent_guidelines_extend", None) or []),
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
                        "disabled_agents": (
                            None if row.disabled_agents is None
                            else list(row.disabled_agents)
                        ),
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
                        # Paths this repository's review never reads, and the
                        # lowest severity posted as a PR comment (None = all).
                        # getattr: a row loaded by code older than the
                        # migration simply has neither.
                        "ignore_globs": (
                            None if getattr(row, "ignore_globs", None) is None
                            else list(row.ignore_globs)
                        ),
                        "comment_min_severity": getattr(
                            row, "comment_min_severity", None),
                        # Kodus-style output settings. getattr for the reason
                        # above. NULL is a row that never answered: it
                        # inherits the workspace default, then ON — silence
                        # is never "off" (`_policy_value(..., True)`).
                        "summary_enabled": getattr(row, "summary_enabled", None),
                        "summary_instructions": (
                            getattr(row, "summary_instructions", None) or None),
                        "started_comment_enabled": getattr(
                            row, "started_comment_enabled", None),
                        "review_language": getattr(row, "review_language", None),
                        "max_inline_comments": getattr(row, "max_inline_comments", None),
                        # The 2.3.0 finders' per-repo models — read by
                        # `resolve_agent_llm` as `<agent>_model`, like the five
                        # columns above.
                        "performance_model": getattr(row, "performance_model", None),
                        "business_logic_model": getattr(
                            row, "business_logic_model", None),
                        # The 2.3.0 settings (migration f1a2b3c4d5e6), every
                        # key present and None when the row does not answer,
                        # so `_resolved_policy` fills it from the workspace and
                        # a reader asks `_policy_setting(policy, name)` for the
                        # value in force (built-ins: review_defaults.
                        # BUILTIN_DEFAULTS). Blank text is None here, as in the
                        # resolver: a cleared box inherits.
                        **self._v23_settings(row),
                    }
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy_load_failed repo=%s err=%s", repo_slug, exc)
            return None

    @staticmethod
    def _v23_settings(row) -> dict:
        """The 2.3.0 settings of a policy row, as the policy dict carries
        them: lists copied, blank text None, a missing attribute (a row loaded
        by code older than the migration) None."""
        from src.review.review_defaults import TEXT_FIELDS, V23_FIELDS

        out: dict = {}
        for name in V23_FIELDS:
            value = getattr(row, name, None)
            if isinstance(value, list):
                value = list(value)
            elif name in TEXT_FIELDS and isinstance(value, str) and not value.strip():
                value = None
            out[name] = value
        return out

    def _remember_posted(
        self, batch: ReviewBatch, pr: PullRequest, provider_name: str, response: dict,
        *, policy, workspace_id: str,
    ) -> None:
        """Keep which provider comment each posted finding became, so a reply,
        a thumb or a resolved thread on it can be read as feedback later.
        Never raises."""
        try:
            from src.review.learning import signals

            owner = policy.get("workspace_id") if isinstance(policy, dict) else None
            signals.record_posted(
                owner or workspace_id,
                signals.PRRef(provider_name, pr.repo, int(pr.number)),
                batch.findings, response.get("inline_comments"),
                sha=pr.head_sha, repo_slug=pr.local_slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning_remember_posted_failed err_type=%s", type(exc).__name__)

    def _learned_filter(
        self, batch: ReviewBatch, findings: list, *, policy, workspace_id: str,
        pr: PullRequest, stages: StageRecorder, provider=None, provider_name: str = "",
    ) -> list:
        """Drop (mode `on`) or only count (mode `shadow`, the built-in) the
        findings the team already dismissed on this repository. Never raises;
        a failure keeps every finding."""
        mode = str(_policy_setting(policy, "learning_suppression") or "shadow")
        if mode not in ("shadow", "on") or not findings:
            return list(findings)
        stages.begin("learned_filter", "Learned filter")
        try:
            from src.review.learning import reactions, signals, suppress

            owner = policy.get("workspace_id") if isinstance(policy, dict) else None
            if provider is not None and provider_name:
                # Thumbs given since the last review of this pull request.
                reactions.poll_pr_reactions(
                    owner or workspace_id,
                    signals.PRRef(provider_name, pr.repo, int(pr.number)), provider)
            result = suppress.apply(
                list(findings), workspace_id=owner or workspace_id,
                repo_slug=pr.local_slug, mode=mode, settings=self.settings)
        except Exception as exc:  # noqa: BLE001 — learning never fails a review
            logger.warning("learned_filter_failed err_type=%s", type(exc).__name__)
            stages.end("learned_filter", "failed",
                       "The learned filter could not run; every finding was kept.")
            return list(findings)
        batch.dropped_by_feedback = len(result.hidden)
        batch.would_drop_by_feedback = len(result.would_hide)
        batch.learned_items = list(result.items)
        meta = {"mode": mode, "checked": result.checked,
                "hidden": len(result.hidden), "would_hide": len(result.would_hide)}
        if result.degraded:
            meta["embeddings"] = "unavailable"
        if result.skipped:
            stages.end("learned_filter", "skipped",
                       f"Nothing to apply: {result.skipped}.", meta=meta)
        elif mode == "on":
            stages.end("learned_filter", "success",
                       f"Left out {len(result.hidden)} of {result.checked} findings the "
                       "team had already dismissed.", meta=meta)
        else:
            stages.end("learned_filter", "success",
                       f"Shadow mode: {len(result.would_hide)} of {result.checked} "
                       "findings would be left out; none were.", meta=meta)
        if result.hidden:
            lang = getattr(batch, "review_language", None)
            batch.add_section(
                "learned", messages.tn("learned", len(result.hidden), lang,
                                       count=len(result.hidden)),
                order=600, targets={"comment"})
        return list(result.kept)

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
        """The policy's rules every agent sees: the repo-level prompt template
        plus each matching rule that names no agent in particular.

        Rules addressed to particular agents are `_build_agent_custom_rules`,
        so they reach only those agents' prompts. Both come from
        `src.review.policy_rules`, the renderer the prompt preview uses too.
        """
        from src.review.policy_rules import render_policy_rules

        rendered = render_policy_rules(policy, pr.changed_files)
        if rendered.memories_used:
            from src.review.memories import touch_used_sync

            touch_used_sync(rendered.memories_used)
        return rendered.shared

    def _build_agent_custom_rules(
        self,
        pr: PullRequest,
        policy: dict | None,
    ) -> dict[str, str]:
        """agent → the matching rules addressed to it by name."""
        from src.review.policy_rules import render_policy_rules

        return render_policy_rules(policy, pr.changed_files).per_agent

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
        user_id: str | None = None,
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
            report = detect_drift(pr, workspace_id=workspace_id, user_id=user_id)
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
        dormant_agents: set[str] | None = None,
        only: str | None = None,
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
        dormant = dormant_agents or set()
        self._agent_finished_at: dict[str, float] = {}
        if only is not None:
            # One forced-on agent (`run_single_agent`): the policy's switches
            # do not apply, and the others are not announced as skipped.
            active = [a for a in self.agents if a.name == only]
        else:
            active = [a for a in self.agents
                      if a.name not in skip and a.name not in dormant]
            for a in self.agents:
                if a.name in skip:
                    logger.warning("agent_skipped_by_policy agent=%s", a.name)
                elif a.name in dormant:
                    logger.debug("agent_off_by_default agent=%s", a.name)
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
                # When each agent came back — its stage starts at this time
                # minus its own elapsed seconds, which is when it actually
                # began, not when it was queued for a pool slot.
                self._agent_finished_at[agent.name] = time.time()
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
