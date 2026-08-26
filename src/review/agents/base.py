"""ReviewAgent abstract interface."""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any

# Same rule as errors.py below: capabilities.py imports litellm inside its
# probes, so the adjustment shape costs nothing at import.
from src.llm.capabilities import (
    ADJUST_SWAPPED,
    PARAM_MODEL,
    ParameterAdjustment,
)

# Safe at module scope: errors.py is pure (re + dataclasses) and imports the
# provider SDKs inside `classify`, so nothing here pays for litellm at import.
from src.llm.errors import (
    MAX_HINT,
    REFUSED,
    THROTTLED,
    TRANSIENT,
    UNRECOGNISED,
    ProviderFailure,
    classify,
    retry_after_seconds,
)

# One source of truth, shared with documentation generation. Two copies of
# this list drift, and a language offered for review comments but not for
# documentation is a dropdown that means something different depending on
# which page you opened.
from src.llm.prompts.language import LANGUAGE_NAMES as _REVIEW_LANG_NAMES
from src.review.models import Finding, FindingSeverity, HunkSide, PullRequest
from src.review.settings import AgentLLMSettings, resolve_agent_llm

logger = logging.getLogger(__name__)


class AgentReplyUnreadable(RuntimeError):
    """The model's reply did not contain the JSON array the protocol demands.

    Distinct from "the model found nothing": the prompt says *If nothing is
    found — `[]`*, so an absent or unparseable array is a protocol violation,
    not a clean bill of health. It has to reach `AgentRunResult.error`, because
    an agent that returns no findings and no error is counted as having run,
    and a review where every agent "ran" with zero findings is APPROVE.
    """


class AgentRefused(AgentReplyUnreadable):
    """The model answered, and the answer was a polite no.

    Measured on a benchmark run against real forks: the security agent failed
    ~11% of runs with `agent_no_json` — on the first attempt AND on the
    corrective one. Intercepting the call inside the container showed why: a
    200 carrying 248 characters of prose, no JSON, no safety flag, no error
    status —

        Sorry, I cannot fulfill your request to analyze or identify
        vulnerabilities in specific code snippets. You can search online
        for secure coding guidelines…

    The same PR re-run by hand answered with valid JSON and a real finding,
    so the refusal is probabilistic, not a property of the input. It was
    filed under AgentReplyUnreadable — true, it IS unreadable — and so got
    the corrective resend, which asks again identically and was measured to
    get the identical refusal. Telling a refusal apart from a truncated array
    is what lets the ladder spend its second call where a different answer is
    possible (a different model, or a differently framed question) instead.

    Still a subclass, on purpose: every `except AgentReplyUnreadable` that
    does not know about refusals keeps catching them, so a refusal can never
    become the silent approval the parent class exists to prevent. `str(exc)`
    is the model's own sentence — redacted and clipped where it was built —
    so it can travel into `AgentRunResult.error`: the run record should say
    what the model said, not that something was unreadable.
    """


# Reasoning models narrate before they answer, and the narration contains
# brackets. Stripped before the scan, never after.
_REASONING_BLOCK = re.compile(
    r"<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)

#: How much of an unreadable reply the WARNING line carries. ~300 characters
#: is the whole of a refusal and the shape of anything else — enough for an
#: operator to tell "the model said no" from "the array was cut off" without
#: intercepting the call, which is what it took the first time (the line read
#: `agent_no_json agent=security` and what the model said was stored nowhere).
#: The full text goes to DEBUG. Redacted BEFORE it is cut — see `_redact_reply`.
_REPLY_PREVIEW_CHARS = 300

#: Longer than this is not a refusal, it is an essay — and an essay, whatever
#: it says, is not what `_REFUSAL_SHAPES` is for.
_REFUSAL_MAX_CHARS = 600

#: The shapes a refusal takes. Three, and deliberately no more:
#:
#:   1. regret + cannot/can't/unable/not able/won't + the verb of the task
#:      (fulfill/help/assist/analyze/comply/provide/review/proceed), in either
#:      order — the measured one: "Sorry, I cannot fulfill your request to
#:      analyze or identify vulnerabilities in specific code snippets."
#:   2. the bare "I can't help with that" family.
#:   3. a policy sentence: "…violates our content policy", "against my
#:      guidelines".
#:
#: `looks_like_refusal` gates them first: the reply must be short, and must
#: contain no `[` or `{` — a truncated array is EXACTLY the case the
#: corrective resend exists for, and a finding whose body happens to say
#: "sorry, cannot help" lives inside brackets. Matched against the RAW reply,
#: never the redacted one: redaction inserts `[REDACTED:…]`, and the bracket
#: gate would then reject the one refusal that echoed a secret.
#:
#: What would make it wrong. A refusal written in the workspace's review
#: language rather than English — the shapes are English, so such a reply is
#: plain-unreadable and gets the corrective resend, as before. A refusal
#: wrapped in JSON (`{"error": "…"}`) or longer than _REFUSAL_MAX_CHARS — same
#: fallthrough. And the inverse: a short apology that is NOT a policy refusal
#: ("Sorry, I cannot analyze an empty diff") is treated as one, which costs a
#: re-framed or fallback call in place of a corrective one — neither of which
#: would have read an empty diff either. Narrow on purpose: a miss costs what
#: it cost before this class existed; a false positive would skip the budget
#: doubling that rescues a truncated array.
_REFUSAL_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?=.*\b(?:sorry|apologi[sz]e|regret|unfortunately)\b)"
        r".*?\b(?:cannot|can'?t|unable\s+to|not\s+able\s+to|won'?t|will\s+not)\b"
        r".{0,40}?\b(?:fulfill?|help|assist|analy[sz]e|comply|provide|review|proceed)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bI(?:\s+am|'?m)?\s+(?:cannot|can'?t|not\s+able\s+to|unable\s+to)\s+"
        r"(?:help|assist)\s+(?:you\s+)?with\s+(?:that|this)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:violates?|goes\s+against|against|not\s+(?:allowed|permitted)\s+(?:by|under))\s+"
        r"(?:my|our|the|its)\s+(?:\w+\s+){0,2}(?:polic(?:y|ies)|guidelines|principles)\b",
        re.IGNORECASE,
    ),
)


def looks_like_refusal(text: str) -> bool:
    """True when a reply with no findings array in it is the model declining.

    Consulted only after `_extract_json_array` found nothing, so the question
    is never "is this a refusal or an answer" but "is this a refusal or a
    truncated/garbled answer" — and only the second kind is worth a
    corrective resend. The gates and the shapes are `_REFUSAL_SHAPES`.
    """
    body = _REASONING_BLOCK.sub(" ", text or "").strip()
    if not body or len(body) > _REFUSAL_MAX_CHARS:
        return False
    if "[" in body or "{" in body:
        return False
    return any(shape.search(body) for shape in _REFUSAL_SHAPES)


def _clip(text: str, limit: int) -> str:
    """Whitespace flattened, cut with an ellipsis. Cut AFTER redaction, always."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit - 1]}…"


def _redact_reply(agent: str, text: str) -> str:
    """Model output made safe for a log line or a run record.

    The reply is the model's, and the model had the diff — a reply that quotes
    it can quote the secret somebody committed. So it goes through the same
    redactor the prompt went through (`LLMClient.generate` runs it over
    `code_context`; the embedder over every chunk) before it touches a log
    line — and through it WHOLE, before any cut: a key cut at character 300
    is half a key, which the key regexes no longer match and the log then
    keeps. `mode="code"`, all stages, although the text is mostly prose: the
    redactor prefers 'markdown' for model output because detect-secrets
    false-positives on headings, and a false positive in a DIAGNOSTIC LINE
    costs nothing while a miss is a secret in the log. Fail-closed like every
    other caller: a redactor that raises withholds the text, it does not wave
    it through.
    """
    from src.security.redactor import redact

    try:
        safe, _ = redact(text, source_hint=f"review_reply:{agent}", mode="code")
    except Exception as exc:  # noqa: BLE001 — fail-closed: no text beats unredacted text
        logger.warning("agent_reply_redaction_failed agent=%s err=%s", agent, exc)
        return "[reply withheld: redaction failed]"
    return safe


def balanced_json_span(text: str, opener: str) -> str | None:
    r"""Return the first balanced `opener`…closer span, or None.

    A counter, not a regex. `re.search(r"(\[.*\])", text, re.DOTALL)` spans
    from the first bracket ANYWHERE to the last bracket ANYWHERE: one line of
    prose after the array, or a single `[a]` inside a reasoning trace, and the
    captured text is not JSON. Quoted brackets are not counted — a finding
    whose message contains "]" used to be able to end the array early.
    """
    closer = {"[": "]", "{": "}"}[opener]
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _agent_error_text(exc: BaseException, failure: ProviderFailure) -> str:
    """What lands in `AgentRunResult.error` — a sentence, not a stack fragment.

    An UNRECOGNISED failure keeps `str(exc)` verbatim, deliberately. Every
    phrase in the reason table was written for a failure we understand;
    inventing one for a failure we do not would dress a brand-new provider
    error up as a familiar one, and whoever reads the run record next would
    stop looking. A failure we cannot name should look unnamed.

    A REFUSED failure carries the model's own sentence after the table's: "the
    model refused" alone would send the reader back to the log this record
    exists to spare them, and the sentence was redacted and clipped where the
    exception was built (`AgentRefused`), so it is safe to travel.
    """
    if failure.disposition == REFUSED:
        return f"{failure.reason}: {exc}"
    return str(exc) if failure.disposition == UNRECOGNISED else failure.reason


@dataclass
class AgentContext:
    """Per-PR context bundle for agent invocation.

    `graph_summary` — human-readable text that includes the changed symbols +
    their callers/callees + the cross-repo blast radius. Pre-computed by the
    orchestrator so as to avoid LLM-side graph queries.
    """

    pull_request: PullRequest
    graph_summary: str = ""           # textual blast-radius summary
    #: The three-line rendering of the same graph for the agents the full
    #: one would drown (quality, tests): which changed symbols the rest of
    #: the code depends on, and nothing else. Same lookup, same run.
    graph_brief: str = ""
    #: Why there is no graph when there is none — a ParameterAdjustment the
    #: orchestrator appends to the batch. None when the graph answered in full.
    graph_note: ParameterAdjustment | None = None
    cross_repo_callers_count: int = 0
    repo_overview: str = ""            # existing PRD overview (vault) — optional
    style_guide: str = ""              # repo-level conventions (from CLAUDE.md etc.)
    cross_repo_drift: str = ""        # semantic config drift signal (Stage 7)
    custom_rules: str = ""            # per-repo NL rules + folder rules (Stage 10)
    # Stage 11 (BYOK / multi-provider). If `llm_client` is None the agent falls
    # back to the legacy Gemini singleton — preserved so nothing breaks during
    # rollout when a policy is not yet set for a repo.
    user_id: str = "default"
    workspace_id: str = "default"          # tenant — selects ws:{id} keys/config/prompts
    llm_client: object | None = None       # LLMClient — Any to avoid import cycle
    #: agent → its resolved model / output ceiling / reasoning level. This was
    #: `model_by_agent: dict[str, str]` and carried the model alone; the output
    #: ceiling was a single global and the reasoning level reached nothing at
    #: all. One object rather than three maps because the agent that doubles
    #: its own ceiling on a corrective retry has to see all three together.
    #: Empty means nobody resolved anything — every agent falls to its floor.
    agent_llm: dict[str, AgentLLMSettings] = field(default_factory=dict)
    # Per-repo per-agent full system_prompt overrides (Stage 12). Takes precedence
    # over global /admin/agents override, which in turn overrides the agent default.
    repo_agent_prompts: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    """Output of one agent invocation."""

    agent: str
    findings: list[Finding] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    #: WHY it failed, as one of `src.llm.errors`' codes.
    #:
    #: `error` is the sentence for a record; this is the fact a caller can
    #: switch on. It exists because the sentence could not be shown to the
    #: reader of the pull request: `_agent_error_text` keeps `str(exc)`
    #: verbatim for an UNRECOGNISED failure — correct for a run record an
    #: authenticated operator reads, and not something to paste into a public
    #: comment, since a provider's message is the one thing this module exists
    #: to keep out of a user's face.
    #:
    #: With a code the banner can print the CURATED sentence for a failure we
    #: understand and stay generic for one we do not, instead of telling every
    #: reader to "check server logs" — an instruction a SaaS user cannot
    #: follow, for a timeout, a quota, a rejected key and a refusal alike.
    error_code: str | None = None
    #: Parts of this agent's own work that did not happen — and it is the
    #: agent, not the orchestrator, that knows.
    #:
    #: `agents_failed` answers "did the agent answer?", and for a multi-pass
    #: agent that is too coarse. `DefectAgent` reads the diff twice; the
    #: second pass is worth roughly 8pp of recall and is allowed to fail
    #: without taking the review with it, because pass one's findings are real
    #: whatever happens next. Correct — and until now completely silent: a
    #: review that looked once published as COMPLETE, with nothing in
    #: `agents_failed` and an APPROVE available to it, byte-identical to one
    #: that looked twice.
    #:
    #: Not the failed list, deliberately. Putting "defect" there would make a
    #: critical finder read as absent and refuse an approval its findings
    #: support. What is missing is a STAGE inside the agent, so it is named as
    #: one, and the orchestrator folds it into `agents_skipped`.
    #:
    #: A map and not a list because the stage names are the keys and the
    #: reasons are the values, in one place: `error` and `error_code` next to
    #: each other are two fields that can disagree, and the same fact stored
    #: twice is how they get to. The value is a sentence fit to publish —
    #: `curated_reason`, never a provider's own message.
    skipped_stages: dict[str, str] = field(default_factory=dict)
    # Stage 11 — cost accounting per call
    cost_usd: float | None = None
    cost_source: str | None = None  # 'openrouter_actual' | 'litellm_estimate' | 'unknown'
    model_used: str | None = None
    #: The model's own output ceiling, when it was lower than the configured
    #: one and the call was cut down to it. None when nothing was clamped.
    #: Carried on the run record because a configured value the provider would
    #: have rejected is a CONFIGURATION mistake, and the only alternative to
    #: surfacing it here is discovering it hours later as an inference failure
    #: whose message never names the number that caused it.
    max_output_tokens_clamped_to: int | None = None
    #: True when the workspace's fallback model was called — whether or not it
    #: answered. `model_used` says WHICH model spoke last; this flag is what
    #: lets the API say "the primary did not" without string-comparing model
    #: ids that gateways and the gemini/ prefixing both rewrite.
    fallback_used: bool = False
    #: Every parameter Celmis changed on this agent's way to an answer — the
    #: ceiling clamp and the two refusals above as the client reports them,
    #: plus the model swap this class makes itself — in the one shape the run
    #: record and the PR comment read. The three scalar fields above each say
    #: one of these facts in their own words and reach the API through nothing;
    #: this list is what the orchestrator merges onto the batch, from failed
    #: agents too, because an agent that was clamped and then died still made
    #: the adjustment.
    parameter_adjustments: list[ParameterAdjustment] = field(default_factory=list)
    #: Reply items that were shaped like findings and were not: no `reasoning`
    #: sentence, or a file this PR does not touch. Counted at parse time
    #: (`LLMReviewAgent._dict_to_finding`) so a run whose agent produced ten
    #: claims and backed two reads as "2 findings, 8 dropped" rather than as
    #: a quiet review. The orchestrator sums this onto the batch the way it
    #: sums `parameter_adjustments`.
    dropped_no_evidence: int = 0
    #: Reply items whose reasoning sentence complained that a test is
    #: missing, reaches too little, or mocks the thing away, instead of
    #: naming a defect (`reads_as_a_coverage_claim`). Its own counter rather
    #: than a share of the one above: the two refusals mean different things
    #: to whoever reads the run, and on runG2 this one would have taken 8 of
    #: 69 comments, all eight of them on the judge's false-positive list.
    dropped_coverage_claim: int = 0

    def __post_init__(self) -> None:
        # `_parse_findings` hands the count back riding on the list itself
        # (`ParsedFindings`), so the retry ladder — which builds this result
        # at four places and knows nothing about evidence — carries it
        # without a change at any of them. An explicit argument still wins.
        if not self.dropped_no_evidence:
            self.dropped_no_evidence = int(
                getattr(self.findings, "dropped_no_evidence", 0) or 0
            )
        if not self.dropped_coverage_claim:
            self.dropped_coverage_claim = int(
                getattr(self.findings, "dropped_coverage_claim", 0) or 0
            )


class ReviewAgent(ABC):
    """Single-responsibility agent (architect / security / quality / tests / verifier)."""

    name: str = ""
    model: str = ""  # Gemini model identifier
    severity_default: FindingSeverity = FindingSeverity.WARNING

    @abstractmethod
    def review(self, context: AgentContext) -> AgentRunResult:
        """Run a review pass — returns findings."""


# ─── Effective system prompt composition ────────────────────────────
#
# Resolution order (highest priority wins as the BASE), then extras append:
#
#   1. Per-repo per-agent override (RepoReviewPolicy.agent_prompt_overrides[name])
#   2. Global per-agent override (/admin/agents — credentials row)
#   3. Agent's built-in default (self.system_prompt)
#
# Appends (always applied on top of whichever base was chosen):
#   a. Workspace-wide `system_prompt_extras` from /settings/llm
#   b. Per-repo NL rules (custom_rules) — was architect-only, now every agent


def _review_model(workspace_id: str):
    """The review profile's model, for the case where no gateway is routing.

    `build_llm_client` returns the gateway deployment when one is configured
    and otherwise asks this. Without it the client has no model at all and
    every call fails before sending a token — which is precisely the outage
    documented in src/llm/client.py, reproduced here by omission.
    """
    def _pick(_agent: str | None = None) -> str | None:
        try:
            from src.llm.profiles import resolve_profile
            return resolve_profile("review", workspace_id).model
        except Exception:  # noqa: BLE001
            return None
    return _pick


def agent_llm_settings(context: AgentContext, agent: str) -> AgentLLMSettings:
    """This agent's model / output ceiling / reasoning level.

    Prefers what the orchestrator already resolved. Falls back to resolving it
    here from the workspace config, because the review call sites all have two
    branches — an injected client and one they build themselves — and the
    second is the one nobody looks at. That is not a hypothetical: the
    verifier's self-built branch went on inheriting litellm's retry default
    for months after the injected branch was fixed, and reached Google
    directly for longer than that. A per-agent ceiling honoured on one branch
    and not the other would be the same bug in a new coat.

    The fallback deliberately leaves `model` as None. The caller passes it
    straight to `LLMClient.generate(model=…)`, where an explicit model wins
    over the client's own `resolve_model` — and that resolver is the
    gateway-aware one: on a routed workspace it returns the LiteLLM deployment,
    and naming a bare model here instead would send the call looking for a raw
    provider key the tenant does not hold. Which is the outage written up in
    `build_llm_client`. The ceiling and the reasoning level have no such
    second resolver, so those two do come from here.

    `max_output_tokens` always comes back with a number in it. An unreachable
    workspace config costs the OVERRIDE, never the ceiling: falling through to
    None there would hand the call an uncapped budget, which is the failure
    this whole change exists to make impossible to reach by accident.
    """
    resolved = (context.agent_llm or {}).get(agent)
    if resolved is not None:
        return resolved
    try:
        from src.api.routers.llm import _load_workspace_config
        workspace_cfg = _load_workspace_config(context.workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace_llm_config_unavailable agent=%s err=%s", agent, exc)
        workspace_cfg = {}
    return replace(
        resolve_agent_llm(agent, workspace_cfg=workspace_cfg), model=None,
    )


#: (fallback, primary) pairs already complained about. A fallback that names
#: the primary is a standing configuration mistake, not an event — the same
#: dedup capabilities.py applies to its clamp warnings, for the same reason:
#: four agents per review, every review, and a WARNING nobody can finish
#: reading is a WARNING nobody reads.
_FALLBACK_IS_PRIMARY_LOGGED: set[tuple[str, str]] = set()


def _llm_timeout() -> float:
    """The per-call deadline, read fresh so an operator's change takes effect.

    Read through a function, and not captured at import, for the reason every
    other number in this file is: a value copied once is a value that stops
    tracking the setting it came from.
    """
    from src.review.settings import get_review_settings
    return float(get_review_settings().llm_timeout_seconds)


def _llm_timeout_retry_factor() -> float:
    """How much longer the retry gets after a call died on the deadline.

    Read, not written. `* 2` sat here as a literal — a number without a name,
    which is the defect this module spent a day removing, written the same
    afternoon three commits after the argument for why an unreachable 120 was
    the whole problem."""
    from src.review.settings import get_review_settings
    return float(get_review_settings().llm_timeout_retry_factor)


def _usable_fallback_model(agent_llm: AgentLLMSettings) -> str | None:
    """The workspace's fallback model, unless it names the primary.

    A "fallback" that resolves to the model that just failed is a third
    attempt at the same thing wearing a different setting — exactly what the
    two-call ceiling exists to forbid — so it is dropped here rather than
    spent. Compared through `resolve_litellm_model` because the two layers can
    spell one model two ways ("gemini-3-pro" vs "gemini/gemini-3-pro") and a
    string compare would wave the duplicate through. A primary of None (a
    gateway-routed workspace, where the client's resolver picks the model)
    cannot be compared, and the configured fallback is honoured as written.
    """
    fallback = (agent_llm.fallback_model or "").strip() or None
    if fallback is None or agent_llm.model is None:
        return fallback
    # In-function like the module's other cross-package imports; the module
    # itself is capability-table-free string logic, so this stays cheap.
    from src.llm.capabilities import resolve_litellm_model

    if resolve_litellm_model(fallback) != resolve_litellm_model(agent_llm.model):
        return fallback
    key = (fallback, agent_llm.model)
    if key not in _FALLBACK_IS_PRIMARY_LOGGED:
        _FALLBACK_IS_PRIMARY_LOGGED.add(key)
        logger.warning(
            "review_fallback_model_is_the_primary fallback=%s primary=%s — "
            "ignored: retrying the model that just failed is a third attempt, "
            "not a fallback", fallback, agent_llm.model,
        )
    return None


def _review_language_instruction(workspace_id: str) -> str:
    """Language directive appended to every review prompt when the workspace
    picked a non-English output language."""
    try:
        from src.api.routers.llm import _load_workspace_config
        code = str(_load_workspace_config(workspace_id).get("review_language") or "en")
    except Exception:  # noqa: BLE001
        return ""
    if code == "en":
        return ""
    name = _REVIEW_LANG_NAMES.get(code, code)
    return (
        f"\n\nIMPORTANT: Write ALL review output — finding titles, bodies, "
        f"summaries and suggestions' explanations — in {name}. Keep code, "
        f"identifiers and file paths as-is."
    )


def _compose_effective_system_prompt(
    *,
    agent_name: str,
    default_system: str,
    context: AgentContext,
) -> str:
    # 1) Per-repo per-agent override wins.
    repo_override = (context.repo_agent_prompts or {}).get(agent_name, "").strip()
    if repo_override:
        base = repo_override
    else:
        # 2) Workspace-level /admin/agents override.
        try:
            from src.api.routers.agents import get_effective_system_prompt
            base = get_effective_system_prompt(agent_name, context.workspace_id) or default_system
        except Exception:  # noqa: BLE001
            base = default_system

    parts = [base.rstrip()]

    # a) Workspace-wide append.
    try:
        from src.api.routers.llm import _load_workspace_config
        extras = (_load_workspace_config(context.workspace_id).get("system_prompt_extras") or "").strip()
    except Exception:  # noqa: BLE001
        extras = ""
    if extras:
        parts.append("**Workspace-wide rules:**\n" + extras)

    # b) Per-repo custom rules — surfaced to every agent, not just architect.
    rules = (context.custom_rules or "").strip()
    if rules:
        parts.append(rules)

    lang = _review_language_instruction(context.workspace_id)

    if lang:

        parts.append(lang.strip())


    # The contract the parser enforces has to be in every prompt the parser
    # reads from. An override that replaced the default prompt wholesale —
    # per-repo or from /admin/agents — was written before `reasoning` was
    # required, and without this every finding it produced would be dropped
    # at parse time for want of a sentence nobody asked the model for: a
    # review lost over a filter. "reasoning" anywhere in the override means
    # it asks in its own words; otherwise the shared shape is appended.
    if '"reasoning"' not in base:
        parts.append(FINDING_OUTPUT_FORMAT.strip())

    return "\n\n".join(parts)


# ─── The shape every LLM agent answers in ────────────────────────────
#
# reasoning → finding → confidence, in that order, for all four agents. The
# order is what the Martian benchmark rewards: its judge reads the whole
# comment and asks whether it names the same underlying issue as the human
# reviewer did, so the derivation sentence is the part that gets matched —
# and a model that has to write "cfg can be nil on line 42; dereferenced on
# 47" BEFORE it may claim "possible nil dereference" produces fewer claims it
# cannot back. The leader on that benchmark reports a 51% fall in false
# positives with no loss of recall from exactly this structure. Confidence
# comes last and is the agent's own number: telemetry, carried on the
# finding and printed in the comment footer, never a threshold.
#
# One constant, four prompts. The architect carried its own copy first
# (e01531d); security paraphrased it; quality and tests said "like the
# others" and did not show the shape at all — three wordings of one
# contract, and the parser enforces only one of them.
FINDING_OUTPUT_FORMAT = """Output: a JSON array of findings and nothing around it. Each finding, fields IN THIS ORDER:
{
    "reasoning": "<ONE sentence, written FIRST: the line, the value, and what happens when the code runs — e.g. 'cfg can be nil on line 42; dereferenced without a check on line 47'>",
    "file": "path/to/file.py",
    "line": <integer, 1-indexed, in the new file>,
    "severity": "critical" | "error" | "warning" | "info",
    "title": "<one line>",
    "body": "<markdown — the full explanation>",
    "rule_id": "<agent>.<rule>",
    "suggestion": "<optional replacement code>",
    "confidence": <0.0-1.0, your own estimate, written LAST>
}

The order is the point: the reasoning sentence you cannot finish is the finding you do not have — write it before the title, and if it will not come, write nothing. A finding with no "reasoning", or on a file this PR does not change, is discarded unread.
If nothing is found, reply exactly []."""

@dataclass(frozen=True)
class AvoidedCategory:
    """One kind of comment no agent may write, and the rule ids it would
    have carried — the name the prompt uses and the names the deny-list
    uses, side by side so neither can be renamed without the other."""

    what: str
    rule_ids: tuple[str, ...]


# Comment categories no agent may produce. Named here ONCE and spliced into
# all four prompts, so they are not generated and then filtered — the
# cheapest false positive is the one never written (Augment tunes its system
# prompt this way and has no post-filter at all). Downstream, the
# deterministic prefilter hides the same things by rule id —
# `ReviewSettings.suppressed_rules` — because a prompt is followed most of
# the time and a gate every time. The two lists must not drift: every rule
# id that setting hides is listed here under the category that forbids it,
# and a test holds the two together. A category the prompt forbids but the
# filter lets through is a comment the model writes one time in twenty; a
# rule the filter hides but the prompt still asks for is tokens spent on a
# comment nobody will see.
AVOIDED_CATEGORIES: dict[str, AvoidedCategory] = {
    # `quality.naming` was in this tuple until the measurement took it out.
    # cal.com#10600's golden — "The exported function TwoFactor handles backup
    # codes and is in BackupCode.tsx. Inconsistent naming." — was a TRUE
    # POSITIVE in the BASE run, written twice (quality as `quality.naming`,
    # architect as `style.component_naming`), and is a FALSE NEGATIVE in
    # runG2, the run in which this avoid-list first reached architect
    # (5adc53f). Those are the only two naming comments either run produced:
    # 2 comments, 1 true positive, 0 false positives. The deny-list was never
    # what blocked it — `ReviewSettings.suppressed_rules` does not contain
    # `quality.naming` — so a rule id listed here bans in the prompt what the
    # filter would happily post, and that is pure loss. `quality.style` stays:
    # formatting is what a formatter settles.
    "style": AvoidedCategory(
        "formatting, import order, quotes, line length, or a name you would "
        "merely spell differently — anything a formatter or a linter settles "
        "without a reviewer. This does NOT cover a name that CONTRADICTS "
        "what the thing is or does: an exported symbol whose name says one "
        "thing while its file or its behaviour says another is a defect no "
        "linter can see — write that one, naming both halves",
        ("quality.style", "defect.style"),
    ),
    "tests-unnamed": AvoidedCategory(
        "\"consider adding tests\" or \"this has no test\" without naming the "
        "uncovered branch and what would silently pass if it were wrong",
        ("tests.no-coverage", "defect.no-coverage"),
    ),
    "todo": AvoidedCategory(
        "TODO / FIXME / HACK comments, whether or not they cite a ticket",
        ("quality.todo", "defect.todo"),
    ),
    "typing": AvoidedCategory(
        "type-annotation nits — a missing hint, Optional versus | None, a "
        "type that could be narrower",
        ("quality.typing", "defect.typing"),
    ),
    "duplication": AvoidedCategory(
        "two similar blocks that could share a helper — unless one of them "
        "is already wrong, which is a defect, not duplication",
        ("quality.duplication", "defect.duplication"),
    ),
    "magic-numbers": AvoidedCategory(
        "a literal that \"should be a constant\" — unless it is the wrong "
        "literal, which is a defect",
        ("quality.magic_numbers", "defect.magic_numbers"),
    ),
    "maintainability": AvoidedCategory(
        "\"hard to read\", \"could be simpler\", \"consider extracting\" — "
        "taste with no wrong result attached",
        ("quality.maintainability", "defect.maintainability"),
    ),
}

AVOID_LIST_PROMPT = (
    "DO NOT WRITE these categories of comment, even when true, and never emit "
    "the rule ids in brackets. A comment the author closes unread costs the "
    "three findings next to it their reader:\n"
    + "\n".join(
        f"    - {name} ({', '.join(cat.rule_ids)}): {cat.what}"
        for name, cat in AVOIDED_CATEGORIES.items()
    )
)


# ─── The second defect on a line already commented ───────────────────
#
# MEASURED, not supposed. Across five benchmark runs and 302 findings, the
# number of times one agent wrote TWO findings on the SAME line is ZERO. Not
# rare — none. Sixty-two lines drew comments from different agents, so the
# constraint is not "one comment per line" globally; it is that having written
# about a line, an agent treats that line as spent and moves on.
#
# That costs real goldens. TWO audited misses sit on a line we DID comment,
# carrying a second, unrelated defect, and both were checked against the diff
# rather than against a description of it:
#
#   topic_embed.rb:13        we said the parameter is mutated in place; the
#                            golden says the receiver can be nil AND the url
#                            is interpolated unescaped
#   next-auth-options.ts:144 we said TOCTOU on single-use consumption; the
#                            golden says the indexOf on that same line is
#                            case-sensitive. `indexOf` is on new-file line 144
#                            of that PR's diff — counted, not assumed.
#
# A THIRD position was dropped from this list after it was written, and the
# reason matters more than the position. `embed.html.erb:11` looked like the
# best target of the three — it draws a comment in four runs of five. The
# golden there is the postMessage targetOrigin one, which a headless-Chrome
# test across two origins DISPROVED: the message is delivered. We then matched
# it in one run by asserting a DOMException that does not occur. Aiming a
# second comment at that line would be teaching the reviewer to elaborate on
# an error it is already rewarded for.
#
# WHY THE LINE AND NOT THE HUNK. The wider phrasing — reread the hunk — would
# cover seven of eight audited positions instead of two, and was rejected on
# the background: "two findings within twenty lines, same agent, same file"
# happened 5 times in those 302, about once per run. A line-scoped instruction
# has a background of zero, so a single extra pair is a signal; a hunk-scoped
# one needs four before it clears noise. Widen it only with the line-scoped
# version measured first.
#
# MEASURED AFTER SHIPPING, AND IT FOUND NOTHING. The first benchmark run
# carrying this block wrote 67 findings and produced ZERO lines with two
# findings from one agent — the same zero as the 302-finding background. Both
# counts were made twice, by two sessions, parsing the `agent:` footer rather
# than looking for an agent's name in the prose (the prose spelling attributes
# a security comment to `quality` whenever the word "security checks" appears
# earlier in the body, and did, on the very line this block is about).
#
# That zero is NOT a refutation, and the arithmetic says why. With 0 events in
# 67 findings the 95% upper bound on the rate is 3/67 = 4.5%; the background's
# is 3/302 = 1.0%. The run therefore rules out an effect that fires on more
# than about one finding in twenty, and rules out nothing smaller. The effect
# this block was designed for is smaller by construction: the two lines it
# targets draw a comment in 2 of 5 runs and 1 of 5, roughly 0.6 opportunities
# per run out of 67 findings — 0.9%. P(seeing zero | the block works perfectly
# at that rate) is 55%. A coin flip.
#
# So it stays, and the reason is not optimism. Removing it is also an unmeasured
# perturbation of a constant that feeds four prompts, and the last time this
# constant was edited the security agent's output moved by five findings for
# reasons unrelated to the edit. Between two unmeasured changes, keep the one
# whose target — an agent walking past a second defect on a line it has already
# commented — is documented in the code above with the diffs to prove it.
#
# What WOULD measure it: not another single run. Either enough runs that 0.9%
# becomes visible (roughly 300 findings, five runs), or a counter restricted to
# the lines where a second defect is known to exist.
#
# The second sentence is not decoration. An instruction to look again, with
# no clamp, is an instruction to produce a second comment — and the cheapest
# second comment is the first one reworded, which at this operating point
# costs a reader and buys nothing.
SECOND_DEFECT_PROMPT = (
    "After you write a finding, read that same line ONCE MORE and ask whether "
    "it carries a SECOND defect of a different kind — a different wrong value, "
    "a different path through it, a different consequence. If it does, write "
    "that as its own separate finding.\n"
    "If it does not, write nothing further about that line. A second comment "
    "restating the first defect in other words is worse than one comment. Two "
    "findings on one line are two findings only when the author would have to "
    "make two different fixes."
)


#: A reasoning sentence that asserts a test is missing, reaches too little,
#: or mocks the thing away, rather than asserting a defect. Six shapes, and
#: they were read off the two bench runs rather than guessed:
#:
#: The `tests` agent wrote 12 of runG2's 69 comments. Eight of them made a
#: claim no other agent made, and the judge scored all eight false; the four
#: that touched a true positive restated a defect architect, quality or
#: security had already found (wrong substring bounds, a hardcoded alias,
#: two undefined dereferences) and none reads like the shapes below. Matched
#: against the published reasoning these six hit exactly those 8 of 69 and
#: none of the other 61. On BASE, which published no reasoning line, matched
#: against the nearest published text — the comment's lead paragraph, a
#: superset of one sentence — they hit 2 of 77, both `tests` comments and
#: both on that run's false-positive list. Against the benchmark's 53 golden
#: comments they hit 0: a human reviewer worth matching does not file bare
#: coverage complaints.
#:
#: Both halves are needed. Six of the eight are the absence shape ("no test
#: exercises", "without test coverage", "neither ... is tested"); two are the
#: over-mocking shape ("poll_feed is mocked in spec/jobs/poll_feed_spec.rb",
#: "perform_retrieve is mocked in all tests"), and an absence-only pattern
#: set lets those two through.
#:
#: Every alternative here carries a test noun of its own — `test`, `spec`,
#: `coverage`, `mocked`, `stubbed`. An earlier draft allowed a bare "not ...
#: covered" and a bare "unverified", and both took collateral: "if group 1
#: does not exist in the test DB" is a defect IN a test, and "the signature
#: is unverified" is a security finding. Four of the six fired on the
#: THE WINDOW IS THREE WORDS AND MUST STAY THERE. It was widened to six to
#: catch "no unit OR end-to-end test", one missed false positive worth 0.16 F2.
#: Measured collateral on 128 real reasoning sentences at the time: zero. That
#: measurement was true and the corpus was too narrow. Six words is enough to
#: span an ordinary English sentence in which "test" means a COMPARISON, not a
#: unit test — "no null check is performed before the test for emptiness",
#: "the loop has no guard, so the emptiness test on line 40 runs on nil" — and
#: those are the sentences the quality agent writes all day. On the benchmark
#: run that shipped the six-word window, quality fell from 9 true positives to
#: 2 and F2 fell 5.71 points against a measured noise floor of 1.78. Absence
#: of evidence in 128 sentences was not evidence of absence for a pattern that
#: fires on a common construction.
#:
#: measured corpus (199 texts: 146 celmis comments + 53 goldens); `fails to
#: cover` fired on none of them and is here as an inflection of the four that
#: did, not as a measured hit.
#:
#: `untested` STANDING ALONE was here and was removed: it fired on nothing in
#: the corpus, and it refuses "Backup code one-time consumption has untested
#: race condition and replay vulnerability" — a real benchmark sentence about
#: a genuine time-of-check defect that merely uses the adjective. A pattern
#: that catches no measured noise and one real defect is a pure loss: at this
#: benchmark's operating point a true positive is worth eleven false
#: positives. The word survives inside the two patterns that require a
#: negation near it, which is where it carried its meaning.
_COVERAGE_CLAIM_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        # "no test exercises X", "without test coverage", "missing tests"
        ("no-test-exists",
         r"\b(?:no|without|lacks?|lacking|missing|absent)\b"
         r"(?:\W+\w+){0,3}\W+(?:tests?|specs?|coverage)\b"),
        # "is not tested", "neither X nor Y is tested"
        ("not-tested",
         r"\b(?:not|never|neither|nor)\b[^.;]{0,120}?\b(?:tested|untested)\b"),
        # "is not covered by any test"
        ("not-covered-by-a-test",
         r"\b(?:not|never|neither|nor|hardly|barely)\b[^.;]{0,120}?"
         r"\bcovered\W+by\b(?:\W+\w+){0,2}\W+(?:tests?|specs?)\b"),
        # the adjective on its own
        # "TestFoo only tests the new-device path", "the spec does not cover"
        ("test-does-too-little",
         r"\b(?:test|spec)\w*\b[^.;]{0,120}?"
         r"\b(?:does\W+not|doesn'?t|fails?\W+to|only)\W+"
         r"(?:tests?|covers?|exercises?|asserts?|verif(?:y|ies))\b"),
        # "X is mocked in spec/foo_spec.rb", "stubbed out in all tests"
        ("over-mocked",
         r"\b(?:mocked|stubbed|faked)\b(?:\W+out)?\W+"
         r"(?:in|across|throughout)\b[^.;]{0,60}?(?:tests?|specs?|suite)\b"),
    )
)


def reads_as_a_coverage_claim(reasoning: str) -> str | None:
    """The shape this reasoning sentence matches, or None.

    The REASONING only, never the body: a body legitimately discusses tests
    when the defect IS in a test, and two of runG2's surviving `tests`
    findings are exactly that — a matcher with inverted logic and a cleanup
    that registers the wrong alias.
    """
    if not reasoning:
        return None
    for label, rx in _COVERAGE_CLAIM_SHAPES:
        if rx.search(reasoning):
            return label
    return None


class CoverageClaimNotAFinding(Exception):
    """A well-formed reply item whose reasoning complains about the tests.

    Agent-independent and rule-id-independent on purpose. The deny-list
    already held `tests.no-coverage` and the avoid-list already forbade
    "tests-unnamed", and both were defeated the same way: the model picks its
    own `rule_id`, and 5adc53f changed the tests prompt's rule-id EXAMPLE to
    `tests.untested-branch` in the very commit that denied `tests.no-coverage`.
    Measured consequence — `hidden.by_rule` is `{}` on all 14 PRs of runG2:
    the deny-list caught nothing while `tests.untested-branch` became the
    run's most common rule at ten comments. A gate keyed on a string the
    model chooses is not a gate, so this one keys on the sentence.

    Kept apart from `FindingWithoutEvidence`: that one refuses a claim with
    no derivation, this one refuses a derivation that is not about a defect.
    Two causes, two counters, because a batch that reports only a total is
    the shape that let the LLM veto delete true positives for five runs
    while reading as a success.
    """

    def __init__(self, shape: str, title: str = "") -> None:
        super().__init__(f"reasoning reads as a coverage claim: {shape}")
        self.shape = shape
        self.title = title


class FindingWithoutEvidence(Exception):
    """A reply item shaped like a finding that is not one.

    Two ways to earn it, both decided at parse time and never by a model: the
    `reasoning` sentence is missing, or the file is one this PR does not
    change. The first is the contract above — a claim with no derivation is
    the claim the prompt told the model not to make. The second is what the
    provider would reject anyway: a comment on a path outside the PR cannot
    be anchored, and a finding on a file the agent never saw the diff of was
    not read off the diff.

    What is deliberately NOT here: "line not in the diff". A change that
    breaks an untouched caller is a finding on the caller's line, and the
    benchmark rewards those; the verifier's DROP rule for them is the one
    the measurement found deleting true positives.
    """

    def __init__(self, why: str, title: str = "") -> None:
        super().__init__(why)
        self.why = why
        self.title = title


class ParsedFindings(list):
    """The findings one reply yielded, plus the count of those it did not.

    A list, so the retry ladder and the orchestrator handle it as the list
    they always had; the counters ride along for `AgentRunResult` to lift.
    Two of them, one per cause: a claim with no derivation, and a derivation
    that complains about the tests instead of naming a defect.
    """

    def __init__(
        self, items=(), *,
        dropped_no_evidence: int = 0,
        dropped_coverage_claim: int = 0,
    ) -> None:
        super().__init__(items)
        self.dropped_no_evidence = dropped_no_evidence
        self.dropped_coverage_claim = dropped_coverage_claim


def changed_files_of(pr: PullRequest | None) -> frozenset[str]:
    """Every path this PR touches, as the diff names them.

    Old and new paths both: a deleted file's finding lands on the old path,
    a rename's on either. `skipped_files` are in too — a binary or oversized
    file is still one the PR changed, and the gate is "outside the PR", not
    "outside what the agent was shown".
    """
    if pr is None:
        return frozenset()
    paths: set[str] = set()
    for hunk in getattr(pr, "hunks", None) or ():
        for path in (hunk.file_path, hunk.old_file_path):
            if path:
                paths.add(path)
    paths.update(p for p in (getattr(pr, "skipped_files", None) or ()) if p)
    return frozenset(paths)


def resolve_changed_file(path: str, changed: frozenset[str]) -> str | None:
    """The PR's own spelling of `path`, or None when the PR does not touch it.

    Models write the path as they saw it and also as they imagine it:
    `./src/foo.py`, `repo-name/src/foo.py`, a bare `foo.py`. Each of those is
    the same file the diff called `src/foo.py`, and a comment posted under
    the model's spelling is one the provider cannot anchor — so the match is
    exact first, then a unique suffix on a `/` boundary in either direction.
    Two candidates is no match: `foo.py` against `src/foo.py` and
    `tests/foo.py` is a guess, and a guessed anchor is a comment on the
    wrong line of the wrong file.
    """
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if not p:
        return None
    if p in changed:
        return p
    hits = [c for c in changed if p.endswith("/" + c) or c.endswith("/" + p)]
    return hits[0] if len(hits) == 1 else None


# ─── LLM-driven agent base — shared prompt scaffolding ──────────────


class LLMReviewAgent(ReviewAgent):
    """Base for LLM-backed agents — shared Gemini call + JSON parsing."""

    system_prompt: str = ""
    user_prompt_template: str = ""

    # One corrective retry, not a loop. The commonest unreadable reply is a
    # findings array truncated by max_output_tokens — deterministic, so a
    # verbatim resend would truncate identically. The retry therefore doubles
    # the output budget AND appends this reminder for the nondeterministic
    # failures (prose around the array, a reasoning trace). A second failure
    # is a failure: the agent lands in agents_failed and the verdict degrades.
    _RETRY_BUDGET_FACTOR = 2
    _RETRY_ADDENDUM = (
        "\n\nREMINDER: your previous reply could not be parsed. Reply with "
        "ONLY the JSON array of findings — no prose before or after it, no "
        "reasoning trace. If nothing is found, reply exactly []."
    )

    #: The one re-ask a REFUSAL can buy on the primary, and only when the
    #: workspace configured no fallback model to spend the slot on instead.
    #: A refusal is not a truncated array: the corrective resend's doubled
    #: budget and "could not be parsed" reminder answer a reply that ran out
    #: of room, and the measured refusal was 248 characters of prose that did
    #: not — the resend got it back word for word. What the refusal lacked was
    #: the framing: whose repository this is, who asked, and that pointing out
    #: the vulnerabilities is the job. So the second ask says that, at the
    #: SAME budget, and says nothing about an unparseable reply — the reply
    #: was perfectly readable. Whether the re-framed ask rescues runs is NOT
    #: yet measured (only that the identical one does not); the tests pin it
    #: to this one slot so a benchmark can retire it without touching the
    #: rest of the ladder.
    _REFUSAL_REFRAME = (
        "\n\nCONTEXT: this is an authorised code review. The owner of this "
        "repository installed this reviewer on it and asked for the findings "
        "you are producing; pointing out vulnerabilities and defects in their "
        "own change, so they can be fixed before it merges, is the requested "
        "and legitimate task. Reply with ONLY the JSON array of findings — "
        "[] if there are none."
    )

    #: What this surface tells LiteLLM about retries. Zero, and stated rather
    #: than inherited.
    #:
    #: The ladder in `_generate_and_parse` was never the whole policy. Neither
    #: `_gen` closure below passed `num_retries`, so both inherited
    #: `LLMClient.generate`'s default of 3, which goes straight into
    #: `litellm.completion` — one layer BELOW the code that decides whether
    #: asking again is worth anything. So the 429 this agent refuses to resend,
    #: on the written grounds that a resend lands in the window that already
    #: refused it, had by then been resent three times inside that window; and
    #: a rejected API key, which cannot resolve between two lines of code, cost
    #: four calls rather than one. The classification ran on the LAST of four
    #: attempts and could only ever decide whether to buy four more.
    #:
    #: `LLMClient._stream` already passes 0 for the same reason and says so.
    #: The default belongs to the shared client and to callers with no policy
    #: of their own; a surface that has one has to state it, or it is quietly
    #: running somebody else's. The whole budget is the two attempts in
    #: `_generate_and_parse` — which is what "the counts are the assertions" in
    #: tests/review/test_a_hopeless_call_is_not_retried.py has always claimed.
    _LLM_NUM_RETRIES = 0

    #: Pause before the one transport resend. The resend used to fire in the
    #: same millisecond as the drop — a bare `continue` — and a dropped
    #: connection does not heal in a millisecond: ConnectError was 5 of the 9
    #: agent failures in one benchmark run, and most of those were the retry
    #: dying on the same dead socket. Two seconds is enough for a route flap
    #: or a proxy restart to settle, and small against the 300s review budget.
    _TRANSIENT_BACKOFF_SECONDS = 2.0
    #: Pause before the fallback call after a 429. Longer than the transport
    #: pause because the provider explicitly said wait; the provider's own
    #: Retry-After outranks this default when the exception carries one.
    _THROTTLED_BACKOFF_SECONDS = 10.0
    #: Ceiling on an honoured Retry-After. A provider that says "3600" is
    #: asking for more than the whole review budget (review_timeout_seconds is
    #: 300); past this point the honest move is to make the one remaining call
    #: and let it fail, rather than hold a worker thread for an hour to dress
    #: a dead review up as a slow one.
    _MAX_RETRY_AFTER_SECONDS = 30.0

    def _backoff_seconds(
        self, failure: ProviderFailure, exc: BaseException,
    ) -> float:
        """How long to wait before the ONE call that follows this failure.

        Backoff decides WHEN that call happens, never whether it happens — the
        attempt budget is owned by `_generate_and_parse` and does not change
        here. Corrective resends never come through this method at all: the
        reply arrived fine, the content was the problem, and waiting buys
        nothing. Nor does the call that follows a refusal, for the same
        reason — the tail of `_generate_and_parse` skips the clock for it.
        """
        if failure.disposition == THROTTLED:
            hinted = retry_after_seconds(exc)
            if hinted is not None:
                return min(hinted, self._MAX_RETRY_AFTER_SECONDS)
            return self._THROTTLED_BACKOFF_SECONDS
        return self._TRANSIENT_BACKOFF_SECONDS

    def _generate_and_parse(
        self,
        generate,  # (system_instruction, max_output_tokens, model_override=None) -> LLM response
        effective_system: str,
        context: AgentContext,
        t0: float,
        *,
        max_output_tokens: int | None = None,
        fallback_model: str | None = None,
        primary_model: str | None = None,
    ) -> AgentRunResult:
        """Drive up to two primary LLM calls — plus at most one fallback —
        and parse the reply.

        Two calls is the PRIMARY's ceiling, not two per problem: a second call
        is spent EITHER correcting an unreadable reply OR resending after a
        transport failure OR re-framing a refused request, never two of those,
        and never twice. `fallback_model`, when the workspace configured one,
        buys exactly ONE further call on that model, and only after the
        primary died THROTTLED, TRANSIENT or REFUSED — the tail of this method
        says why the other endings never engage it.

        A refusal is the one reply that is never re-asked identically: the
        corrective resend was measured to get the refusal back word for word.
        Its second call goes where a different answer is possible — to the
        fallback model when one is configured, otherwise to the primary once
        more with the request re-framed (`_REFUSAL_REFRAME`). One or the
        other, never both, so a refused primary costs two calls either way:
        what an unreadable one costs, and never a third on the primary.

        Tokens and cost are accumulated across EVERY attempt, the fallback
        included — each call was paid for whether or not its reply could be
        read. So are the parameter adjustments each call reports, and the
        swap to `fallback_model` is recorded here as one of them the moment it
        is made — before the fallback answers, because the swap happened
        whether or not it did. `primary_model` is only for naming what the
        swap was FROM on a record; when the caller cannot say (the client's
        resolver picks), the model the primary's own replies named is used.
        """
        if max_output_tokens is None:
            # The floor, and only the floor. `resolve_agent_llm` normally hands
            # this method a resolved per-agent number; None means the caller
            # never went through the resolver — a directly-built AgentContext,
            # or the no-client branch below — and the global default is then
            # the right answer rather than the only one.
            from src.review.settings import get_review_settings
            max_output_tokens = get_review_settings().agent_max_output_tokens

        tokens_in = 0
        tokens_out = 0
        cost_usd: float | None = None
        cost_source = None
        model_used = None
        clamped_to: int | None = None
        adjustments: list[ParameterAdjustment] = []
        last_err: AgentReplyUnreadable | None = None
        # Which kind of second call this is. The corrective retry rewrites the
        # prompt because the REPLY was the problem; a transport retry must not,
        # because there was no reply — doubling the budget and telling the
        # model its last answer was unparseable would be both a lie and dearer.
        corrective = False
        # The other second call that rewrites the prompt: after a refusal, and
        # only when there is no fallback model to go to instead. Kept apart
        # from `corrective` because the two say different things to the model
        # and want different budgets — see the loop head.
        reframed = False

        def _absorb(response: Any) -> str:
            """Book one reply's tokens/cost/model onto the run; return its text.

            One function for the primary attempts and the fallback, because
            accounting that exists in two copies is how a failed agent's spend
            went missing from the ledger last time (see the orchestrator's
            aggregation loop).
            """
            nonlocal tokens_in, tokens_out, cost_usd, cost_source
            nonlocal model_used, clamped_to
            tokens_in += int(getattr(response, "input_tokens", 0) or 0)
            tokens_out += int(getattr(response, "output_tokens", 0) or 0)
            got_cost = getattr(response, "cost_usd", None)
            if got_cost is not None:
                cost_usd = (cost_usd or 0.0) + got_cost
            cost_source = getattr(response, "cost_source", None) or cost_source
            model_used = getattr(response, "model", None) or model_used
            clamped_to = (
                getattr(response, "max_output_tokens_clamped_to", None) or clamped_to
            )
            # Every call's adjustments, not the last one's: a corrective
            # retry that doubled the budget and was clamped is a second
            # clamp, and a doubled number cut to the ceiling is a fact about
            # THAT call.
            adjustments.extend(getattr(response, "parameter_adjustments", None) or ())
            return getattr(response, "text", "") or ""

        # The provider failure that ended the primary, when one did. The loop
        # `break`s here rather than returning so that ONE tail below decides
        # what a dead primary is worth — an error record, or the fallback call.
        exhausted: tuple[BaseException, ProviderFailure] | None = None
        #: A wider deadline for the second attempt, set only when the first
        #: died ON the deadline. None on every other path, so `_gen` falls
        #: back to the configured value and no other failure class silently
        #: buys itself extra time.
        widened: float | None = None

        for attempt in (1, 2):
            system = effective_system
            budget = max_output_tokens
            if corrective:
                system = effective_system + self._RETRY_ADDENDUM
                # Doubled, then handed to the client, which cuts it back to
                # what the model accepts. The doubling is deliberately NOT
                # clamped here: this method knows the number, the client knows
                # the model, and a ceiling computed in two places is how the
                # two stop agreeing. A doubled budget above the model's limit
                # is a 400, so the clamp has to happen — it just happens where
                # the model is known.
                budget = max_output_tokens * self._RETRY_BUDGET_FACTOR
            elif reframed:
                # A refusal is a different question from a truncated array and
                # gets a different second ask: the framing the model lacked,
                # at the SAME budget — the refusal was short prose, room was
                # never the problem — and without the "could not be parsed"
                # reminder, which would tell the model something false about
                # a reply that was perfectly readable.
                system = effective_system + self._REFUSAL_REFRAME
            try:
                # A RESEND INTO THE SAME WALL IS NOT A RETRY. Every other
                # transient failure — a dropped socket, a 5xx, a proxy restart
                # — is answered by asking again on a fresh connection, and the
                # deadline had nothing to do with it. A timeout is the one
                # where the deadline IS the failure: the model was thinking,
                # we stopped listening, and asking the same question of the
                # same model with the same clock is a second guaranteed
                # failure at full price.
                #
                # Doubled once, for the second and last attempt, so a call
                # that merely needed longer gets it while a genuinely stuck
                # one still ends. Worst case is 3× the configured deadline
                # (300 + 600), which is why `llm_timeout_seconds` is required
                # to stay well under the review budget.
                response = generate(system, budget, None, widened)
            except Exception as exc:  # noqa: BLE001
                # What went wrong decides whether asking again is worth
                # anything. Before this, no transport failure was retried at
                # all; the moment one is, the classification is what keeps the
                # retry off the calls that cannot benefit from it.
                failure = classify(exc)
                if failure.disposition == TRANSIENT and attempt == 1:
                    # A dropped connection or a 5xx: the one class a plain
                    # resend fixes. Once, for the same reason the corrective
                    # retry is once — two attempts is a retry, three is a way
                    # to turn somebody else's outage into our bill. And not in
                    # the same millisecond: the resend used to be a bare
                    # `continue`, which put the retry on the same dead socket
                    # it had just died on — ConnectError was 5 of 9 agent
                    # failures in the run that motivated the pause.
                    if failure.code == "local_timeout":
                        widened = _llm_timeout() * _llm_timeout_retry_factor()
                    pause = self._backoff_seconds(failure, exc)
                    logger.warning(
                        "agent_llm_failed agent=%s attempt=%d code=%s "
                        "deadline=%.0fs retrying_in=%.1fs err=%s",
                        self.name, attempt, failure.code, _llm_timeout(),
                        pause, exc,
                    )
                    time.sleep(pause)
                    continue
                # The deadline is on the line because a timeout whose bound
                # is not written down reports a symptom with the cause left
                # out: a reader cannot tell 120 seconds of patience from 900
                # without going to look, and the audit record's parameter
                # block carries the token ceiling and never the time one.
                logger.warning(
                    "agent_llm_failed agent=%s attempt=%d code=%s "
                    "deadline=%.0fs err=%s",
                    self.name, attempt, failure.code, _llm_timeout(), exc,
                )
                # Everything else ends the PRIMARY here. A rejected key, an
                # unknown model or an exhausted quota fails identically on a
                # second call, and a 429 resent to the same model lands inside
                # the window that already refused it — rate-limited is never
                # hammered. Whether anything happens next (an error record, or
                # the one fallback call) is decided once, in the tail below.
                exhausted = (exc, failure)
                break
            text = _absorb(response)
            try:
                findings = self._parse_findings(text, context)
            except AgentRefused as exc:
                # Before its parent class below, or the parent would catch it
                # and spend the corrective resend — the identical re-ask that
                # was measured to get the identical refusal.
                last_err = exc
                if attempt == 1 and fallback_model is None:
                    # The one second call a refusal can buy on the primary,
                    # and only when no fallback exists to buy it instead: a
                    # re-asked question that is actually a different question.
                    reframed = True
                    logger.warning(
                        "agent_model_refused agent=%s attempt=%d next=reframed_retry "
                        "err=%s", self.name, attempt, exc,
                    )
                    continue
                logger.warning(
                    "agent_model_refused agent=%s attempt=%d next=%s err=%s",
                    self.name, attempt,
                    "fallback" if fallback_model is not None else "fail", exc,
                )
                # Ends the PRIMARY the way a provider failure does, through the
                # same disposition vocabulary, so the ONE tail below decides
                # what a refused primary is worth — and can hand it to the
                # fallback model, which a merely unreadable reply never reaches.
                exhausted = (exc, ProviderFailure("model_refused"))
                break
            except AgentReplyUnreadable as exc:
                last_err = exc
                if attempt == 1:
                    # Ask the next call to correct itself. Deliberately not set
                    # on the last attempt: there is no next call to correct,
                    # and this flag also tells the record below whether a
                    # correction was ever actually made.
                    corrective = True
                logger.warning(
                    "agent_reply_unreadable agent=%s attempt=%d err=%s",
                    self.name, attempt, exc,
                )
                continue
            return AgentRunResult(
                agent=self.name, findings=findings,
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
            )
        if exhausted is None:
            # Both attempts produced replies and neither could be read. This
            # ending never engages the fallback: the provider answered, the
            # protocol was the casualty — and a different model answering a
            # prompt the primary already answered twice is not a fix for a
            # parse failure, it is a comparability leak.
            return AgentRunResult(
                agent=self.name,
                error=(
                    f"unreadable reply after a corrective retry: {last_err}"
                    if corrective else f"unreadable reply: {last_err}"
                ),
                error_code="unreadable_reply",
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
            )

        exc, failure = exhausted
        primary_error = _agent_error_text(exc, failure)
        if reframed and failure.disposition == REFUSED:
            # Two refusals, and the second was of a different question. The
            # record says so because the remedy differs: one refusal is
            # weather (the same PR answered fine by hand), two is a model that
            # will not review this change, and the operator's move is a
            # fallback model — which this workspace has not configured, or
            # the re-framed ask would not have been made.
            primary_error = (
                f"{failure.reason}, and again when told the review is "
                f"authorised: {exc}"
            )
        if fallback_model is None or failure.disposition not in (THROTTLED, TRANSIENT, REFUSED):
            # A terminal or unrecognised failure must NOT fall back. A
            # rejected key or an unknown model fails identically on any model
            # — or, worse, the fallback works and quietly papers over a
            # configuration mistake that would then be discovered weeks later,
            # as billing. The reason travels instead: a run record that says
            # "provider quota exhausted" is one a user can act on. A refusal
            # is the opposite case and is in the set above: a different model
            # is precisely the remedy, and there is no comparability objection
            # of the kind that keeps an unreadable reply off the fallback —
            # the primary produced no findings at all.
            return AgentRunResult(
                agent=self.name, error=primary_error,
                error_code=failure.code,
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
            )

        # THROTTLED, TRANSIENT or REFUSED, and the workspace named a second
        # model: one more call, on that model, after the pause the failure
        # class asks for — a 429's own Retry-After when the exception carries
        # one, and nothing at all after a refusal: that reply arrived, the
        # content was the problem, and a pause would only hold the worker.
        # The call is the PRIMARY's configuration untouched: same prompt, same
        # system instruction (never the corrective addendum, never the
        # re-framing — the fallback has not answered unreadably or refused,
        # and telling it that it had would be a lie), same base ceiling and
        # the same reasoning level. Both of the last two are re-fitted to the
        # fallback model where the model is actually known —
        # `clamp_output_tokens` and `reasoning_kwargs` run inside
        # `LLMClient.generate` against the model each call names — so nothing
        # is re-derived here, and a ceiling or vocabulary the fallback's
        # vendor does not share is cut there, not guessed at here.
        pause = 0.0 if failure.disposition == REFUSED else self._backoff_seconds(failure, exc)
        logger.warning(
            "agent_llm_fallback agent=%s code=%s fallback_model=%s waiting=%.1fs",
            self.name, failure.code, fallback_model, pause,
        )
        if pause:
            time.sleep(pause)
        # The swap is an adjustment like the clamp and the two drops, and it
        # is written down BEFORE the fallback is asked: `fallback_used` has
        # always been True on every ending below whether or not the fallback
        # answered, and this record keeps the same rule — the operator asked
        # for one model and a different one was called, full stop. `reason`
        # is the primary's ending, which is the sentence an operator acts on.
        adjustments.append(ParameterAdjustment(
            agent=self.name, parameter=PARAM_MODEL,
            requested=primary_model or model_used, sent=fallback_model,
            action=ADJUST_SWAPPED, reason=primary_error,
            model=primary_model or model_used,
        ))
        try:
            response = generate(effective_system, max_output_tokens, fallback_model)
        except Exception as fallback_exc:  # noqa: BLE001
            fallback_failure = classify(fallback_exc)
            logger.warning(
                "agent_llm_fallback_failed agent=%s code=%s err=%s",
                self.name, fallback_failure.code, fallback_exc,
            )
            # One call was the whole grant — no retries of any kind for the
            # fallback. Both endings travel, because the reader of this record
            # has two different things to fix.
            return AgentRunResult(
                agent=self.name,
                error=(
                    f"{primary_error}; the fallback model failed too: "
                    f"{_agent_error_text(fallback_exc, fallback_failure)}"
                ),
                # The PRIMARY's code: it is the one an operator acts on, and
                # a fallback that also died is a second symptom of the same
                # decision, not a different thing to fix.
                error_code=failure.code,
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
                fallback_used=True,
            )
        text = _absorb(response)
        try:
            findings = self._parse_findings(text, context)
        except AgentRefused as fallback_err:
            # Two models, the same no. Both sentences travel, for the same
            # reason both endings do when the fallback raises: the reader has
            # two things to look at, and "the fallback refused too" is a
            # different fact from "the fallback was unreadable".
            return AgentRunResult(
                agent=self.name,
                error=f"{primary_error}; the fallback model refused too: {fallback_err}",
                error_code=failure.code,
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
                fallback_used=True,
            )
        except AgentReplyUnreadable as fallback_err:
            # No corrective retry for the fallback either — see above.
            return AgentRunResult(
                agent=self.name,
                error=f"unreadable reply from the fallback model: {fallback_err}",
                error_code="unreadable_reply",
                tokens_in=tokens_in, tokens_out=tokens_out,
                elapsed_seconds=time.time() - t0,
                cost_usd=cost_usd, cost_source=cost_source,
                model_used=model_used,
                max_output_tokens_clamped_to=clamped_to,
                parameter_adjustments=adjustments,
                fallback_used=True,
            )
        return AgentRunResult(
            agent=self.name, findings=findings,
            tokens_in=tokens_in, tokens_out=tokens_out,
            elapsed_seconds=time.time() - t0,
            cost_usd=cost_usd, cost_source=cost_source,
            # `_absorb` above took the fallback reply's own model name, so the
            # run record names the model that actually answered — and the flag
            # says the primary was not it.
            model_used=model_used,
            max_output_tokens_clamped_to=clamped_to,
            parameter_adjustments=adjustments,
            fallback_used=True,
        )

    def review(self, context: AgentContext) -> AgentRunResult:
        t0 = time.time()
        prompt = self._build_prompt(context)
        effective_system = _compose_effective_system_prompt(
            agent_name=self.name,
            default_system=self.system_prompt,
            context=context,
        )

        # Stage 11 path — user-scoped LLMClient with per-agent LLM settings.
        if context.llm_client is not None:
            agent_llm = agent_llm_settings(context, self.name)
            model = agent_llm.model
            reasoning = agent_llm.reasoning

            # One closure for the primary attempts AND the fallback call —
            # `model_override` is the only thing the fallback changes, so it
            # cannot drift from the primary in prompt, temperature, reasoning
            # or accounting.
            def _gen(system: str, budget: int, model_override: str | None = None,
                     timeout: float | None = None):
                return context.llm_client.generate(
                    prompt=prompt,
                    model=model_override or model,   # None → LLMClient falls back to resolve_model / model default
                    # The deadline, named. `generate`'s own default is 120s,
                    # which no agent ever overrode and no operator could
                    # change — see `llm_timeout_seconds`.
                    timeout=timeout or _llm_timeout(),
                    agent=self.name,
                    system_instruction=system,
                    mode="review",
                    operation=f"review_{self.name}",
                    repo=context.pull_request.repo_slug,
                    temperature=agent_llm.temperature,
                    max_output_tokens=budget,
                    # The value the workspace configured for THIS agent. The
                    # client drops it when the model cannot take it, so a model
                    # without reasoning gets no parameter rather than a 400 —
                    # and it re-checks against whichever model each call names,
                    # which is what lets the fallback model reuse this value.
                    reasoning=reasoning,
                    num_retries=self._LLM_NUM_RETRIES,
                )

            return self._generate_and_parse(
                _gen, effective_system, context, t0,
                max_output_tokens=agent_llm.max_output_tokens,
                fallback_model=_usable_fallback_model(agent_llm),
                # None when the client's resolver picks — see
                # `agent_llm_settings` — and the record then names the model
                # the primary's own replies carried.
                primary_model=model,
            )

        # No injected client — build one. This used to be a "legacy path
        # (pre-Stage-11), keep working during rollout" that called
        # get_gemini_client() and talked to Google directly; the rollout ended
        # long ago and the branch stayed, quietly bypassing the gateway
        # whenever the orchestrator happened not to inject a client.
        try:
            from src.llm.client import build_llm_client

            pick_model = _review_model(context.workspace_id)
            client = build_llm_client(
                getattr(context, "user_id", None) or "system",
                context.workspace_id,
                surface="review",
                resolve_model=pick_model,
            )
        except Exception as exc:  # noqa: BLE001
            # No retry here on purpose: this raised before a token was sent,
            # and the commonest cause by far — no key configured for the
            # workspace — is not going to resolve between two lines of code.
            failure = classify(exc)
            logger.warning(
                "agent_llm_failed agent=%s code=%s deadline=%.0fs err=%s",
                self.name, failure.code, _llm_timeout(), exc,
            )
            return AgentRunResult(
                agent=self.name, error=_agent_error_text(exc, failure),
                error_code=failure.code,
                elapsed_seconds=time.time() - t0,
            )

        # Same per-agent settings on this branch as on the injected one — see
        # `agent_llm_settings` for why both branches have to carry them.
        agent_llm = agent_llm_settings(context, self.name)

        def _gen(system: str, budget: int, model_override: str | None = None,
                 timeout: float | None = None):
            return client.generate(
                prompt=prompt,
                timeout=timeout or _llm_timeout(),  # same deadline as the injected branch
                # None on the primary attempts — the client's gateway-aware
                # resolver picks the model, see `agent_llm_settings`. The one
                # fallback call names its model explicitly, same as on the
                # injected branch.
                model=model_override,
                agent=self.name,
                mode="review",
                operation=f"review_{self.name}",
                repo=context.pull_request.repo_slug,
                system_instruction=system,
                temperature=agent_llm.temperature,
                max_output_tokens=budget,
                reasoning=agent_llm.reasoning,
                num_retries=self._LLM_NUM_RETRIES,
            )

        return self._generate_and_parse(
            _gen, effective_system, context, t0,
            max_output_tokens=agent_llm.max_output_tokens,
            fallback_model=_usable_fallback_model(agent_llm),
            # The profile's model is the best name this branch has for what
            # the primary was; on a routed workspace the gateway deployment
            # answers instead and the record falls back to the reply's name.
            primary_model=pick_model(self.name),
        )

    def _build_prompt(self, context: AgentContext) -> str:
        pr = context.pull_request
        diff_text = self._format_diff_for_prompt(pr)
        return self.user_prompt_template.format(
            pr_title=pr.title,
            pr_description=pr.description or "(empty)",
            diff=diff_text,
            graph_summary=context.graph_summary or "(no graph context)",
            graph_brief=context.graph_brief or "(no graph context)",
            cross_repo_callers=context.cross_repo_callers_count,
            style_guide=context.style_guide or "(no style guide)",
            repo_overview=context.repo_overview or "(no overview)",
            cross_repo_drift=context.cross_repo_drift or "(no drift signal)",
            custom_rules=context.custom_rules or "(no repo-specific rules configured)",
        )

    @staticmethod
    def _format_diff_for_prompt(pr: PullRequest, max_chars: int = 50_000) -> str:
        """Diff for the LLM — capped so as to avoid overflow."""
        parts: list[str] = []
        used = 0
        for hunk in pr.hunks:
            chunk = (
                f"### {hunk.file_path} (lines {hunk.new_start}-"
                f"{hunk.new_start + hunk.new_count - 1})\n"
                f"```diff\n{hunk.content}\n```\n"
            )
            if used + len(chunk) > max_chars:
                parts.append(f"... (truncated, {len(pr.hunks) - len(parts)} hunks omitted)")
                break
            parts.append(chunk)
            used += len(chunk)
        return "\n".join(parts)

    def _parse_findings(
        self, llm_text: str, context: AgentContext,
    ) -> list[Finding]:
        """Parse LLM response — expects JSON array of findings.

        Robust against being wrapped in markdown ``` blocks. Skip malformed
        entries.
        """
        json_text = self._extract_json_array(llm_text)
        if not json_text:
            preview, full = self._reply_evidence(llm_text)
            if looks_like_refusal(llm_text):
                logger.warning(
                    "agent_model_refused agent=%s reply=%r", self.name, preview,
                )
                raise AgentRefused(_clip(full, MAX_HINT))
            # This line used to read `agent_no_json agent=security`, and what
            # the model had actually said was stored nowhere — an operator
            # could not tell a refusal from a truncated array without
            # intercepting the call, which is what it took. The preview is
            # the evidence; the full text is one level down.
            logger.warning("agent_no_json agent=%s reply=%r", self.name, preview)
            raise AgentReplyUnreadable("no JSON array in the reply")

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            preview, _ = self._reply_evidence(llm_text)
            logger.warning(
                "agent_json_parse_failed agent=%s err=%s reply=%r",
                self.name, exc, preview,
            )
            raise AgentReplyUnreadable(f"reply is not valid JSON: {exc}") from exc

        if not isinstance(data, list):
            preview, _ = self._reply_evidence(llm_text)
            logger.warning(
                "agent_reply_not_an_array agent=%s reply=%r", self.name, preview,
            )
            raise AgentReplyUnreadable(
                f"reply is a {type(data).__name__}, not the array of findings"
            )

        changed = changed_files_of(getattr(context, "pull_request", None))
        out = ParsedFindings()
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                f = self._dict_to_finding(item, changed)
            except CoverageClaimNotAFinding as exc:
                out.dropped_coverage_claim += 1
                logger.debug(
                    "finding_dropped_coverage_claim agent=%s shape=%s title=%r",
                    self.name, exc.shape, exc.title,
                )
                continue
            except FindingWithoutEvidence as exc:
                out.dropped_no_evidence += 1
                logger.debug(
                    "finding_dropped_no_evidence agent=%s why=%s title=%r",
                    self.name, exc.why, exc.title,
                )
                continue
            except (KeyError, ValueError) as exc:
                logger.debug("malformed_finding agent=%s err=%s", self.name, exc)
                continue
            if f is not None:
                out.append(f)
        if out.dropped_no_evidence and not out:
            # Every claim in the reply went unbacked. One such reply is a
            # model ignoring the shape; the same line on every run of one
            # agent is a prompt override that stopped asking for evidence
            # (see `_compose_effective_system_prompt`), and that is worth
            # more than a debug line per finding.
            logger.warning(
                "agent_all_findings_dropped_no_evidence agent=%s dropped=%d",
                self.name, out.dropped_no_evidence,
            )
        if out.dropped_coverage_claim:
            # INFO, not DEBUG: on runG2 this would have fired on five of the
            # fourteen PRs, and an operator comparing two runs needs to see
            # WHERE the comment count fell without turning on debug logging.
            logger.info(
                "agent_coverage_claims_refused agent=%s dropped=%d",
                self.name, out.dropped_coverage_claim,
            )
        return out

    def _reply_evidence(self, llm_text: str) -> tuple[str, str]:
        """(preview, full) of an unreadable reply, both redacted.

        The preview is what the WARNING line carries; the full text is logged
        at DEBUG right here, so every unreadable reply leaves exactly one such
        line whichever branch of `_parse_findings` gave up on it. The prompt
        is never part of either — it is the customer's diff, and the reply is
        evidence enough.
        """
        full = _redact_reply(self.name, llm_text)
        logger.debug("agent_reply_unreadable_full agent=%s reply=%r", self.name, full)
        return _clip(full, _REPLY_PREVIEW_CHARS), full

    @staticmethod
    def _extract_json_array(text: str) -> str | None:
        """Find a '[...]' JSON array in text, even if wrapped in ``` blocks."""
        if not text:
            return None
        # Narration first: a bracket inside <think> is not the answer.
        text = _REASONING_BLOCK.sub(" ", text)
        # Try fenced code blocks first
        fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if fence:
            return fence.group(1)
        return balanced_json_span(text, "[")

    def _dict_to_finding(
        self, data: dict[str, Any], changed_files: frozenset[str] | None = None,
    ) -> Finding | None:
        """One reply item → a Finding, None when malformed, and
        `FindingWithoutEvidence` when it is well-formed and unbacked, and
        `CoverageClaimNotAFinding` when its reasoning complains about the
        tests instead of naming a defect.

        The two endings are kept apart on purpose: a malformed item (no file,
        no line) is a parse problem and stays a debug line; an unbacked one
        is a CLAIM the model made and could not support, and the count of
        those is what tells a run "two findings" from "two findings and
        eight guesses". `changed_files` empty means the caller has no PR to
        gate against — a directly-built context, the unit tests — and the
        file gate stands down rather than drop everything it cannot check.
        """
        file_path = data.get("file") or data.get("file_path") or ""
        line = data.get("line") or 0
        if not isinstance(file_path, str) or not file_path:
            return None
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            return None
        if line_int < 1:
            return None

        title = str(data.get("title") or "")
        reasoning = str(data.get("reasoning") or "").strip()
        if not reasoning:
            raise FindingWithoutEvidence("no reasoning", title)
        shape = reads_as_a_coverage_claim(reasoning)
        if shape is not None:
            raise CoverageClaimNotAFinding(shape, title)
        if changed_files:
            resolved = resolve_changed_file(file_path, changed_files)
            if resolved is None:
                raise FindingWithoutEvidence(f"file not in the PR: {file_path}", title)
            file_path = resolved

        severity_raw = str(data.get("severity") or self.severity_default.value).lower()
        try:
            severity = FindingSeverity(severity_raw)
        except ValueError:
            severity = self.severity_default

        confidence_raw = data.get("confidence", 0.7)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = 0.7

        return Finding(
            file_path=file_path,
            line=line_int,
            side=HunkSide.RIGHT,  # default — most comments land on new lines
            severity=severity,
            title=title,
            body=str(data.get("body") or data.get("message") or ""),
            reasoning=reasoning,
            suggestion=data.get("suggestion") if isinstance(data.get("suggestion"), str) else None,
            agent=self.name,
            rule_id=str(data.get("rule_id") or data.get("rule") or f"{self.name}.unknown"),
            confidence=confidence,
        )
