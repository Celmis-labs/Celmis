"""Review categories: which agent's finding is which kind of finding.

Kodus groups a review into categories — Bug, Performance, Security, Business
logic — each with its own prompt. Celmis groups it into agents, each with its
own prompt, and the two line up one-to-one where it matters. This module is
the ONE spelling of that line-up on the server; `web/lib/review-categories.ts`
is the other half, and a test holds the two together.

The category is derived from `Finding.agent` (the provenance every finding
already carries) rather than stored beside it: one fact, one home. A finding
several agents agreed on carries their names comma-joined
(`"defect,security"`, see the verifier's merges); it is filed under the
first named agent that has a category, which is the same agent whose title
and body the merged comment shows.
"""

from __future__ import annotations

#: agent → the category label a reader sees. Insertion order is the order
#: the summary and the UI list categories in when counts tie.
AGENT_CATEGORY: dict[str, str] = {
    "defect": "Bug",
    "contract": "Contract",
    "security": "Security",
    "performance": "Performance",
    "business_logic": "Business logic",
    "compliance": "Compliance",
    "breaking_change": "Breaking change",
    "structural": "Structure",
    "cve": "Dependencies",
}

#: What a finding with no known agent is filed under, keyed by its rule id's
#: prefix — a Claude Code engine finding, a hand-built batch, a row written
#: by an agent that has since been retired.
_RULE_PREFIX_CATEGORY: dict[str, str] = {
    "defect": "Bug",
    "quality": "Bug",
    "arch": "Contract",
    "contract": "Contract",
    "sec": "Security",
    "security": "Security",
    "perf": "Performance",
    "logic": "Business logic",
    "compliance": "Compliance",
    "breaking": "Breaking change",
    "cve": "Dependencies",
    "ghsa": "Dependencies",
}

OTHER = "Other"


def category_for_agent(agent: str | None) -> str | None:
    """The category of one agent name (or a comma-joined list), or None."""
    for name in (agent or "").split(","):
        label = AGENT_CATEGORY.get(name.strip().lower())
        if label:
            return label
    return None


def category_of(agent: str | None, rule_id: str | None = None) -> str:
    """The category label of a finding: by its agent, else by its rule id's
    prefix, else "Other"."""
    label = category_for_agent(agent)
    if label:
        return label
    prefix = (rule_id or "").split(".", 1)[0].strip().lower()
    return _RULE_PREFIX_CATEGORY.get(prefix, OTHER)


def finding_category(finding) -> str:
    """`category_of` for anything shaped like a `Finding`."""
    return category_of(getattr(finding, "agent", None), getattr(finding, "rule_id", None))


__all__ = [
    "AGENT_CATEGORY",
    "OTHER",
    "category_for_agent",
    "category_of",
    "finding_category",
]
