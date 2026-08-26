"""How an agent session runs: one agent, or a fan-out.

The difference that matters is subagents. The Agent tool defaults to launching
them in the BACKGROUND, which returns an agentId immediately and delivers the
work in a later turn. That is real parallelism and it is what you want for a
large change — but it only pays off if the runner keeps reading past the first
result frame to collect it (src/agent/runner._read_until_settled).

    standard  — subagents forced to the foreground. Their report comes back
                inline in the tool result, so everything the session does
                appears in one ordered stream. Predictable, cheaper, and the
                right default for "rename this", "fix that test".

    workflow  — subagents may run in the background and several can work at
                once. The runner waits for them after the parent's turn ends.
                For work that spans many files or subsystems.

Model and effort are separate choices — a small change can still deserve Opus,
and a big one can be fine on Sonnet — so the mode does not pin them. It only
supplies a default for each.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STANDARD = "standard"
WORKFLOW = "workflow"

# Values the SDK accepts for `effort`; anything else is rejected upstream.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# ALIASES, not version-pinned ids. The CLI resolves each to the current model:
#
#   --model <model>  Provide an alias for the latest model (e.g. 'fable',
#                    'opus', or 'sonnet') or a model's full name (e.g.
#                    'claude-fable-5').
#
# A pinned list is stale the day a new model ships — this one shipped with
# claude-*-4-5 ids while the CLI was already serving 5. An empty value means
# "let the CLI pick", which is what an unconfigured session gets.
MODEL_ALIASES = ("", "opus", "sonnet", "haiku", "fable")

# A full model id is accepted too, so a brand-new one is usable before anyone
# touches this file. The pattern is the guardrail: a typo still fails at the
# API boundary rather than deep inside the CLI, after the repo is cloned.
MODEL_ID_PATTERN = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+){0,4}$")


def is_valid_model(model: str) -> bool:
    model = (model or "").strip()
    return (not model) or model in MODEL_ALIASES or bool(MODEL_ID_PATTERN.match(model))


@dataclass(frozen=True)
class ModeSpec:
    name: str
    #: May the model launch background (parallel) subagents?
    allow_background_subagents: bool
    max_turns: int
    #: Reasoning effort passed to the SDK.
    effort: str
    #: Model used when the session did not name one.
    default_model: str
    #: Seconds to keep reading after a result frame while tasks are in flight.
    #: Only reachable in workflow mode — standard mode has no background task
    #: to wait for — but bounded in both so a stuck task cannot eat the
    #: session's wall clock.
    tail_wait_seconds: int


# `max_turns` reaches the CLI as --max-turns, and the CLI's own help says it
# "will early exit the conversation after the specified number of turns" —
# CONVERSATION, not query. With one message per session that ceiling was never
# approached. A session is a conversation now, so a ten-message chat with a few
# tool calls each walks straight into it, and the runner reports a turn-limit
# result as a failed session. Raised to match what a conversation costs.
_SPECS = {
    STANDARD: ModeSpec(
        name=STANDARD,
        allow_background_subagents=False,
        max_turns=400,
        effort="medium",
        default_model="",
        tail_wait_seconds=2 * 60,
    ),
    WORKFLOW: ModeSpec(
        name=WORKFLOW,
        allow_background_subagents=True,
        max_turns=800,
        effort="high",
        default_model="opus",
        tail_wait_seconds=15 * 60,
    ),
}


def get_spec(mode: str | None) -> ModeSpec:
    """Never raises — an unknown mode degrades to the safe one."""
    return _SPECS.get((mode or "").strip().lower(), _SPECS[STANDARD])


def is_valid_mode(mode: str) -> bool:
    return mode in _SPECS


__all__ = [
    "STANDARD", "WORKFLOW", "MODEL_ALIASES", "EFFORT_LEVELS",
    "ModeSpec", "get_spec", "is_valid_mode", "is_valid_model",
]
