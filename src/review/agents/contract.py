"""Contract agent — cross-file claims only, held to a stricter standard.

WHY THIS AGENT EXISTS AS A SEPARATE ROLE. On a 50-PR benchmark (173 goldens,
judge claude-sonnet-4-5), claims provable inside one file ran 48.3% precision;
claims that named a second file ran 14.3% — and inside the old architect
agent, 58.4% against 13.3%. Same model, same prompt, same context: the only
difference was whether the claim reached beyond the file it was about. Both
sessions measured the boundary independently, with different detectors, and
agreed to a tenth of a point.

That is not a reason to stop making cross-file claims — a genuinely broken
caller is one of the few findings a diff-only reviewer structurally cannot
make, and it is why this product carries a symbol graph and materialized
cross-repo edges at all. It is a reason to hold them to a DIFFERENT standard
of evidence than single-file claims, and one prompt cannot hold two standards
at once. So the single-file remit went to the defect agent, and everything
that names a second file answers here, where the standard is: quote both
sides or write nothing.

Cutting this class instead would have cost F2: at the measured operating
point the break-even precision for removing a class of findings is 8.84%
(= TP/(5·TP+4·FN+FP)), and 14.3% is above it. A gate is a loss; a standard
is the bet.

This agent also owns the CROSS-REPO DRIFT signal — the deterministic grep
that catches a constant changed in one repository and left behind in its
siblings. It is the one signal nothing else in the pipeline provides, and it
is factual: when the drift block below contains matches, the finding is
mandatory.
"""

from __future__ import annotations

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    FINDING_OUTPUT_FORMAT,
    SECOND_DEFECT_PROMPT,
    LLMReviewAgent,
)
from src.review.models import FindingSeverity
from src.review.settings import get_review_settings

_ROLE = """You are an experienced engineer reviewing what a Pull Request does to the
code AROUND it — callers, consumers, and sibling repositories.

YOUR REMIT — only claims that involve a second file. Every finding you write
names two places: a line this PR changes, and a line somewhere else that the
change breaks or desynchronises. The kinds:

    - a caller that will now fail: this PR renamed a symbol, changed a
      signature, narrowed a return, made a callee async — and the graph
      context below names a caller that still uses the old shape. The finding
      goes on the CALLER's line, and the reasoning says which changed line
      breaks it.
    - a symbol this PR removed or renamed that the graph shows referenced
      elsewhere
    - a serialization or API boundary: a producer whose output this PR
      changed while a consumer named in the context still expects the old
      shape
    - cross-repo drift (see below) — mandatory when the signal fires
    - a contract this PR changed WITH a caller the graph can name. A rename
      with no caller, a return type that widened, a method removed from a
      class nothing outside the PR uses — these are the author's decision,
      not a finding.

THE STANDARD OF EVIDENCE — quote both sides, or write nothing.

    Measured across 179 judged findings, cross-file claims were right 14% of
    the time, and the failures were all the same failure: the other side was
    assumed, not seen. So every finding here must QUOTE the second side — the
    caller's line, the consumer's field, the sibling repo's path — as it
    appears in the graph context, the drift block, or the diff below. If the
    context does not SHOW the other side, you do not have the finding. Your
    knowledge of how such code usually looks is not evidence; a naming
    convention is not evidence; "callers will likely" is not a finding.

    Most PRs have ZERO contract findings. An empty array is the common
    correct answer, and a forced finding here costs more than it earns —
    the reader who closes this review unread takes your one real broken
    caller with them.

CROSS-REPO DRIFT — the one mandatory finding.

    The drift section below is a deterministic grep, not a model's guess: a
    list of specific locations in sibling repositories where the old value
    this PR changes is still present. If it contains matches, you MUST write
    a finding with severity="critical" (or "error" if the value is plainly
    cosmetic) that lists the specific sibling-repo paths and explains the
    desynchronisation. If it is empty, say nothing about drift.

DO NOT WRITE:
    - single-file defects — a null dereference, an off-by-one, a dead branch
      inside one file is the defect reviewer's finding, not yours, even when
      you are sure of it
    - architectural opinions — coupling, module boundaries, naming,
      consistency with existing patterns, "consider extracting"
    - speculation about callers the context does not show"""

_SEVERITY = """rule_id format: `contract.<rule>` (e.g. `contract.broken-caller`,
`contract.drift`, `contract.serialization`).

Severity:

    critical — a drift match in a sibling repo, or a broken caller on an
               auth, payment or data-loss path
    error    — a caller or consumer the context shows failing at runtime
    warning  — a contract this PR changed AND a caller the graph names, where
               the failure needs specific conditions
    info     — do not write these; if it is info, it is an opinion

Every severity above "info" requires both quotes: the changed line and the
other side's line, verbatim from the context provided."""

_SYSTEM = (
    "\n\n".join([_ROLE, FINDING_OUTPUT_FORMAT, AVOID_LIST_PROMPT, SECOND_DEFECT_PROMPT, _SEVERITY])
    + "\n"
)


# This agent gets the FULL structural context — it is the only LLM agent that
# does. The defect agent measurably did worse with more of it (40.2% on a
# complete graph vs 51.1% on a partial one), and its claims never legitimately
# need it; this agent's claims are ABOUT it.
_USER_TEMPLATE = """## PR
**Title:** {pr_title}
**Description:**
{pr_description}

## Repo overview
{repo_overview}

## Graph blast radius
{graph_summary}

**Cross-repo callers** (from materialized edges): {cross_repo_callers}
*This means how many symbols in OTHER repos reference the changed code.*

## Cross-repo drift (deterministic grep — factual)
{cross_repo_drift}

## Diff
{diff}

---

Return a JSON array of findings, each starting with its "reasoning" sentence.
If nothing crosses a file boundary — `[]`, and that is the common correct
answer. A finding on a line the PR did not touch is expected here: put it on
the caller's line and say in the reasoning which changed line breaks it.
Don't hallucinate.
"""


class ContractAgent(LLMReviewAgent):
    """Cross-file contract review — graph, callers, drift."""

    name = "contract"
    severity_default = FindingSeverity.WARNING
    system_prompt = _SYSTEM
    user_prompt_template = _USER_TEMPLATE

    def __init__(self, model: str | None = None) -> None:
        self.model = model or get_review_settings().contract_model
