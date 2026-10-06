"""`@celmis -v business-logic [<task>]` as a command of the PR command dispatcher.

The dispatcher (`src/review/commands/handlers.py`) owns everything around a
command: the webhook, who may command the bot, the once-only ledger, the rate
limits, the reaction. A command is a function of its `CommandContext`; this
module is the business-logic one, and `register()` puts it in the registry:

    from src.review.task_context.command import register
    register()

It is a separate call, not an import side effect, so that importing the task
context never pulls the command package in, and the command package decides
when the registry is complete (the guide lists what is registered).

The handler runs in the queue worker, so it may take as long as a review step:
it fetches the pull request (with its diff), resolves the repository's policy,
runs `on_demand.run_business_logic_check` and answers in the thread of the
comment. A provider that has no reactions was told "checking…" with a note; the
note is edited into the answer.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The command's name in the registry and in the guide.
NAME = "business-logic"

#: Longest answer posted (a comment of every provider takes more).
MAX_REPLY_CHARS = 30000


def _settle(ctx: Any, text: str) -> None:
    """Put `text` where the person will see it: over the "checking…" note when
    the provider needed one, else as a reply in the thread."""
    note = getattr(ctx, "ack_comment_id", None)
    if note:
        try:
            if ctx.provider.update_comment(
                    ctx.ev.repo, ctx.ev.pr_number, note, text, kind=ctx.ev.kind):
                return
        except Exception as exc:  # noqa: BLE001
            logger.info("business_logic_note_not_updated err=%s", type(exc).__name__)
    ctx.reply(text)


def business_logic_command(ctx: Any) -> str | None:
    """Run the check for the comment in `ctx` and answer it. Returns None (the
    dispatcher's "nothing more to say")."""
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.task_context.on_demand import run_business_logic_check

    ev = ctx.ev
    ctx.acknowledge("command.ack_business_logic")
    pr = ctx.provider.fetch_pull_request(ev.repo, ev.pr_number)
    orch = ReviewOrchestrator()
    policy = orch.policy_for(pr, ctx.workspace_id)
    result = run_business_logic_check(
        pr, str(getattr(ctx.command, "args", "") or ""),
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, policy=policy,
        provider=ctx.provider, orchestrator=orch, language=ctx.language)
    # The answer carries the model's wording about text a stranger wrote (a task,
    # a diff): it gets the same treatment as a chat answer before it is posted as us.
    from src.review.commands.chat import clean_answer

    _settle(ctx, clean_answer(result.markdown, handle=ctx.handle, limit=MAX_REPLY_CHARS))
    return None


def register() -> None:
    """Add `business-logic` to the command registry (idempotent)."""
    from src.review.commands.handlers import register_command

    register_command(NAME, business_logic_command)
