"""Multi-agent reviewer (Phase 17.5, restructured Phase 18).

Three LLM finder agents and two deterministic ones, then a prefilter that
always runs (rule deny-list, dedup, near-duplicate clustering, confidence
floor, severity sort) and an LLM veto the policy may switch off.

THREE FINDERS, NOT FIVE. Architect, quality and tests used to be separate
LLM calls. A 50-PR benchmark (173 goldens, judge claude-sonnet-4-5) measured
what the split bought: 29 of 262 generated findings were the agents retelling
each other, and the width of an agent's remit correlated with BETTER
precision, not worse (architect, 53 rules, 51.1%; quality, 22 rules, 38.2%).
The boundary that actually separated good findings from bad was mechanical:
claims provable inside one file ran 48.3%, claims naming a second file 14.3%.
The roster now follows that line:

    Defect    — everything provable inside one file: architect's defect
                list, quality's remit, tests' untested-branch clause.
                The main finder; 45 of 46 missed bug-goldens were here.
    Contract  — everything that names a second file: broken callers,
                serialization boundaries, cross-repo drift. Held to a
                stricter standard: quote both sides or write nothing.
    Security  — unchanged. 50% precision as measured; not touched.
    Structural, CVE — deterministic, no LLM, unchanged.
    Verifier  — dedup + FP filtering across all findings, unchanged.

Each agent — a pure function (Hunk + context) → list[Finding]. Provenance is
tracked in the `Finding.agent` field.
"""

from src.review.agents.base import AgentContext, ReviewAgent
from src.review.agents.contract import ContractAgent
from src.review.agents.cve import CveAgent
from src.review.agents.defect import DefectAgent
from src.review.agents.security import SecurityAgent
from src.review.agents.verifier import VerifierAgent
from src.review.structural import StructuralAgent

__all__ = [
    "AgentContext",
    "ContractAgent",
    "CveAgent",
    "DefectAgent",
    "ReviewAgent",
    "SecurityAgent",
    "StructuralAgent",
    "VerifierAgent",
]
