"""Guided tours: the answer that points at the control instead of describing it.

A how-to answer can end with a short block naming the controls it talks about:

    [[guide]]
    review-agent-toggle:security | Open "Model & limits" for this agent.
    review-agent-max-output:security | This is the output token cap.
    [[/guide]]

The browser walks the person there: opens the page, scrolls to each control,
dims the rest and explains it in a small popover. What the model writes is
only ever a *name* from `TARGETS` plus a sentence; the page to open and the
opener to click come from this table, never from the model, and a name that is
not in it is dropped. A made-up target would otherwise start a tour that
highlights nothing, which is worse than no tour.

Every id here has a `data-tour="<id>"` attribute in `web/` — a test keeps the
two in step, so moving a control means moving the attribute, not editing prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Agents with a per-agent model row (`REVIEW_AGENTS` in web/lib/api.ts).
AGENTS: tuple[str, ...] = (
    "defect", "contract", "security", "performance", "business_logic",
    "verifier", "compliance",
)

MAX_STEPS = 6
MAX_TEXT = 220


@dataclass(frozen=True)
class Target:
    """One control a tour may point at.

    `route` may contain `{agent}`; such a target is addressed as `id:agent`.
    `opener` is the id (same `:agent` suffix) of the control that unfolds the
    container this one lives in, clicked when the target is not on screen.
    """

    id: str
    route: str
    what: str
    opener: str | None = None

    @property
    def per_agent(self) -> bool:
        return "{agent}" in self.route


_AGENT_ROUTE = "/review-settings?section=categories&agent={agent}"

TARGETS: dict[str, Target] = {t.id: t for t in (
    Target("review-agent-toggle", _AGENT_ROUTE,
           'the "Model & limits" button of one review agent (unfolds its model row)'),
    Target("review-agent-model", _AGENT_ROUTE,
           "the model picker of one review agent", "review-agent-toggle"),
    Target("review-agent-max-output", _AGENT_ROUTE,
           "the max output tokens field of one review agent", "review-agent-toggle"),
    Target("review-agent-reasoning", _AGENT_ROUTE,
           "the reasoning level select of one review agent", "review-agent-toggle"),
    Target("review-agent-test", _AGENT_ROUTE,
           'the "Test" button of one review agent', "review-agent-toggle"),
    Target("llm-review-card", "/settings/llm",
           "the PR Review base card: provider, model and fallback every agent inherits"),
    Target("llm-base-reasoning", "/settings/llm",
           "the base reasoning level of the PR Review card"),
    Target("llm-base-test", "/settings/llm",
           'the "Test" button of the PR Review base card'),
    Target("llm-apply-all", "/settings/llm",
           'the "Apply to all agents" button under the base reasoning level'),
    Target("llm-review-agents", "/settings/llm",
           "the card listing every review agent with its own model and limits"),
    Target("llm-embeddings-card", "/settings/llm",
           "the embeddings card: the model that indexes code for search"),
    Target("llm-provider-keys", "/settings/llm",
           "the provider keys card: API keys for each LLM provider"),
    Target("llm-chat-card", "/settings/llm",
           "the Chat card: the model behind Ask the code"),
    Target("llm-agent-card", "/settings/llm",
           "the Agent card: the model behind this Celmis agent"),
)}


def resolve_target(raw: object) -> dict[str, str] | None:
    """`{"target", "route"[, "opener"]}` for a known target id, else None."""
    if not isinstance(raw, str):
        return None
    base, sep, agent = raw.strip().partition(":")
    spec = TARGETS.get(base)
    if spec is None:
        return None
    if spec.per_agent != bool(sep) or (sep and agent not in AGENTS):
        return None
    out = {
        "target": f"{base}:{agent}" if sep else base,
        "route": spec.route.format(agent=agent) if sep else spec.route,
    }
    if spec.opener:
        out["opener"] = f"{spec.opener}:{agent}"
    return out


_BLOCK = re.compile(r"\[\[guide\]\](.*?)(?:\[\[/guide\]\]|\Z)", re.DOTALL | re.IGNORECASE)
_PARTIAL_OPEN = re.compile(r"\[{1,2}(?:g(?:u(?:i(?:d(?:e)?)?)?)?)?\]{0,2}\s*\Z", re.IGNORECASE)
_MARKUP = re.compile(r"\[([^\]]*)\]\([^)]*\)|[`*_<>]|\[\[|\]\]")


def _clean(text: str) -> str:
    text = _MARKUP.sub(lambda m: m.group(1) or "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]


def parse_steps(block: str) -> list[dict[str, str]]:
    """The valid, de-duplicated steps of one block, at most `MAX_STEPS`."""
    steps: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in block.splitlines():
        ident, sep, text = line.strip().lstrip("-* ").partition("|")
        resolved = resolve_target(ident) if sep else None
        text = _clean(text)
        if resolved is None or not text or resolved["target"] in seen:
            continue
        seen.add(resolved["target"])
        steps.append({**resolved, "text": text})
        if len(steps) == MAX_STEPS:
            break
    return steps


def extract_tour(text: str) -> tuple[str, list[dict[str, str]]]:
    """(the answer without the block, the valid steps in it)."""
    steps: list[dict[str, str]] = []
    for m in _BLOCK.finditer(text or ""):
        steps = steps or parse_steps(m.group(1))
    return _BLOCK.sub("", text or "").rstrip(), steps


def strip_partial(text: str) -> str:
    """A sentence still being written, minus a block that has started.

    Streamed text reaches the screen before `extract_tour` sees the whole
    answer, so a half-typed `[[gui` must not flash in the bubble.
    """
    text = _BLOCK.sub("", text or "")
    return _PARTIAL_OPEN.sub("", text).rstrip()


def tour_prompt() -> str:
    """The part of the system prompt that teaches the block and lists targets."""
    lines = [
        f"- {t.id}{':<agent>' if t.per_agent else ''}: {t.what}"
        for t in TARGETS.values()
    ]
    return (
        "\n\nShow-me tours. When the answer is about WHERE a control is or HOW "
        "to change it and the control is in the list below, end the answer "
        "with a block that makes the app walk the person to it:\n"
        "[[guide]]\n<target> | <one short sentence, in the answer's language, "
        "telling what to do there>\n[[/guide]]\n"
        f"One line per control, in the order to visit them, at most {MAX_STEPS}. "
        "If a control is inside a folded container, list the control that "
        "unfolds it first. Per-agent targets are written id:agent with agent "
        f"one of {', '.join(AGENTS)}. Use ONLY these ids, copied exactly — "
        "anything else is discarded. No block when no listed control fits.\n"
        "Targets:\n" + "\n".join(lines) + "\n"
    )
