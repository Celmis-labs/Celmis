"""Render a repo policy's custom rules into prompt text, per agent.

One renderer for the two readers that need it — the orchestrator, which hands
the text to the agents of a real review, and the prompt-preview endpoint, which
shows an operator what those agents will be told. Two copies of this loop is
how the preview and the review came to disagree about what a rule says.

A rule (a `folder_rules` entry) is `{pattern, prompt}` and, optionally,

  * `title`         — a short name, printed as the rule's heading;
  * `severity_hint` — info | warning | error | critical: the severity the
                      agent should report a violation at;
  * `agents`        — the agents the rule is for. Empty or absent = every
                      agent, which is what every row written before these
                      fields existed means.

A rule addressed to some agents reaches ONLY their prompts. The untargeted
rules (and the repo-level prompt template) stay one shared block, exactly as
before, so a policy that uses none of the new fields renders byte-for-byte as
it did.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

#: The severities a rule may hint at, least to most severe.
SEVERITY_HINTS: tuple[str, ...] = ("info", "warning", "error", "critical")

REPO_RULES_HEADING = "**Repo-level rules (from admin panel):**"


@dataclass
class RenderedRules:
    """The rules of one policy, split by who they are for."""

    #: Prompt template + every rule addressed to all agents.
    shared: str = ""
    #: agent → the rules addressed to it by name (shared ones NOT repeated).
    per_agent: dict[str, str] = field(default_factory=dict)
    #: Every agent-targeted rule, once each — what a single-reviewer engine
    #: (Claude Code) needs on top of `shared`, since it plays every agent.
    targeted: str = ""
    #: `shared` and `targeted` together.
    everything: str = ""


def rule_agents(rule: dict) -> list[str]:
    """The agents a stored rule names; [] means every agent."""
    raw = rule.get("agents") or []
    if not isinstance(raw, list):
        return []
    return [str(a).strip() for a in raw if str(a).strip()]


def _render_one(rule: dict, matched: list[str] | None) -> str | None:
    pattern = str(rule.get("pattern") or "").strip()
    prompt = str(rule.get("prompt") or "").strip()
    if not pattern or not prompt:
        return None
    title = str(rule.get("title") or "").strip()
    hint = str(rule.get("severity_hint") or "").strip().lower()
    if hint not in SEVERITY_HINTS:
        hint = ""

    where = f"`{pattern}`"
    if matched:
        preview = ", ".join(matched[:3])
        if len(matched) > 3:
            preview += f", …(+{len(matched) - 3})"
        where += f" (matches: {preview})"

    if not title and not hint:
        # The exact shape every pre-existing rule has always rendered in.
        return f"**Folder rule — {where}:**\n{prompt}"

    head = f"**Rule — {title}** ({where})" if title else f"**Folder rule — {where}**"
    body = prompt
    if hint:
        body += (
            f"\nReport a violation of this rule with severity `{hint}`."
        )
    return f"{head}:\n{body}"


def render_policy_rules(
    policy: dict | None,
    changed_files: list[str] | None,
    *,
    match_files: bool = True,
) -> RenderedRules:
    """Split `policy`'s prompt template and rules into prompt blocks.

    `match_files=False` renders every rule whether or not a file matches —
    what the preview does, since it has no pull request to match against.
    """
    out = RenderedRules()
    if not policy:
        return out

    shared: list[str] = []
    targeted: list[str] = []
    everything: list[str] = []
    per_agent: dict[str, list[str]] = {}

    base = str(policy.get("prompt_template") or "").strip()
    if base:
        block = f"{REPO_RULES_HEADING}\n{base}"
        shared.append(block)
        everything.append(block)

    changed = list(changed_files or [])
    for rule in policy.get("folder_rules") or []:
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("pattern") or "").strip()
        matched: list[str] | None = None
        if match_files:
            matched = [f for f in changed if fnmatch.fnmatch(f, pattern)]
            if not matched:
                continue
        block = _render_one(rule, matched)
        if block is None:
            continue
        everything.append(block)
        targets = rule_agents(rule)
        if not targets:
            shared.append(block)
        else:
            targeted.append(block)
            for agent in dict.fromkeys(targets):
                per_agent.setdefault(agent, []).append(block)

    out.shared = "\n\n".join(shared)
    out.per_agent = {a: "\n\n".join(blocks) for a, blocks in per_agent.items()}
    out.targeted = "\n\n".join(targeted)
    out.everything = "\n\n".join(everything)
    return out


__all__ = [
    "REPO_RULES_HEADING",
    "RenderedRules",
    "SEVERITY_HINTS",
    "render_policy_rules",
    "rule_agents",
]
