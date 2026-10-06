"""Running the commands: the registry, the built-ins, and the worker's body.

Two entry points:

* `accept` — the receiver's last synchronous step. Resolves the repository's
  command settings, applies the rate limits and claims the comment in the
  ledger; returns the job payload, or None when nothing should run (commands
  off, a duplicate delivery, a limit already announced).
* `execute` — the queue job's body. Checks who is asking, runs the command and
  answers on the pull request. Never raises: the ledger row carries the outcome.

Commands are plain functions registered by name; the guide and `help` list
exactly the ones registered, so a command another module has not shipped is
never advertised.

    from src.review.commands.handlers import register_command, CommandContext

    def thanks(ctx: CommandContext) -> str | None:
        ...                      # ctx.command.args holds the rest of the comment
        ctx.reply("Noted.")
    register_command("thanks", thanks)

A command returns a ledger status (`done` by default) or None.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from src.review import messages
from src.review.commands import gate, ledger
from src.review.commands.chat import chat_command
from src.review.commands.events import CommentEvent
from src.review.commands.parser import (
    CHAT,
    HELP,
    REMEMBER,
    REVIEW,
    START_REVIEW,
    ParsedCommand,
    help_markdown,
)
from src.review.commands.remember import remember_command

logger = logging.getLogger(__name__)

#: Forced (full-history) reviews one pull request may ask for per hour.
FORCED_PER_PR_PER_HOUR = 3


@dataclass
class CommandContext:
    """Everything a command needs; one per executed comment."""

    ev: CommentEvent
    provider: Any
    command: ParsedCommand
    #: `commands_enabled`, `chat_enabled`, `command_permission` as in force.
    settings: dict[str, Any]
    workspace_id: str
    user_id: str
    language: str = "en"
    handle: str = "@celmis"
    row_id: str | None = None
    #: The comment the bot wrote to say it is working (providers without reactions).
    ack_comment_id: str | None = None
    #: The last comment this execution posted.
    reply_id: str | None = None
    run_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def t(self, key: str, **kw: object) -> str:
        return messages.t(key, self.language, **kw)

    def reply(self, text: str) -> str | None:
        """Answer in the thread of the comment that asked."""
        self.reply_id = self.provider.post_reply(self.ev, text)
        return self.reply_id

    def acknowledge(self, key: str = "command.ack_review") -> None:
        """Let the person see the comment arrived: a reaction where the
        provider has them, a short reply (edited when the work ends) elsewhere.
        Never raises."""
        try:
            if self.provider.acknowledge(self.ev):
                return
            self.ack_comment_id = self.provider.post_reply(
                self.ev, self.t(key, actor=self.ev.actor_name or "someone"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("command_ack_post_failed provider=%s err=%s",
                           self.ev.provider, type(exc).__name__)


CommandFn = Callable[[CommandContext], "str | None"]

_REGISTRY: dict[str, CommandFn] = {}


def register_command(name: str, fn: CommandFn) -> None:
    """Make `name` a command the bot runs (and lists in its guide)."""
    _REGISTRY[name] = fn


def available_commands() -> list[str]:
    """The registered command names, in registration order."""
    return list(_REGISTRY)


# ─── The receiver's half ─────────────────────────────────────────


def accept(
    ev: CommentEvent, command: ParsedCommand, *, workspace_id: str, user_id: str,
    engine=None,
) -> dict | None:
    """Settings, rate limits and the ledger claim; the job payload or None.

    Blocking (database); the receiver runs it in a thread.
    """
    from src.review.review_defaults import command_settings_for_repo
    from src.review.settings import get_review_settings

    cfg = command_settings_for_repo(ev.provider, ev.repo)
    if not cfg["commands_enabled"]:
        logger.info("command_ignored reason=commands_disabled provider=%s repo=%s",
                    ev.provider, ev.repo)
        return None
    if command.name == CHAT and (not cfg["chat_enabled"] or CHAT not in _REGISTRY):
        # Until the chat step registers its handler, a casual "thanks @celmis"
        # gets no answer (an explicit command word still does).
        return None

    rs = get_review_settings()
    status, announce = ledger.CLAIMED, False
    # A stranger's refused commands cost the bot no reply budget: they must not
    # use up what the pull request's maintainers share.
    over_pr = ledger.count_recent(
        workspace_id, provider=ev.provider, repo=ev.repo, pr_number=ev.pr_number,
        without=(ledger.DENIED,), engine=engine) >= rs.command_replies_per_pr_per_hour
    over_actor = bool(ev.actor_id) and ledger.count_recent(
        workspace_id, provider=ev.provider, actor_id=ev.actor_id, engine=engine,
    ) >= rs.commands_per_actor_per_hour
    # A forced review is a full-history LLM run: it has a small budget of its own.
    over_force = command.force and ledger.count_recent(
        workspace_id, provider=ev.provider, repo=ev.repo, pr_number=ev.pr_number,
        forced=True, without=(ledger.DENIED,), engine=engine) >= FORCED_PER_PR_PER_HOUR
    if over_pr or over_actor or over_force:
        status = ledger.RATE_LIMITED
        # Said once per window; every later one is only written down.
        announce = ledger.count_recent(
            workspace_id, provider=ev.provider, repo=ev.repo, pr_number=ev.pr_number,
            status=ledger.RATE_LIMITED, engine=engine) == 0

    row_id = ledger.claim(
        workspace_id, ev.provider, ev.repo, ev.pr_number, ev.comment_id,
        command=command.name, args=command.args, force=command.force,
        parent_id=ev.parent_id, event_key=ev.event_key, actor_id=ev.actor_id,
        actor_name=ev.actor_name, status=status, engine=engine,
    )
    if row_id is None:
        logger.info("command_dropped reason=already_handled provider=%s repo=%s pr=%d",
                    ev.provider, ev.repo, ev.pr_number)
        return None
    if status == ledger.RATE_LIMITED and not announce:
        return None
    event = asdict(ev)
    event["actor_ids"] = list(ev.actor_ids)
    return {
        "event": event, "command": asdict(command),
        "workspace_id": workspace_id, "user_id": user_id, "ledger_id": row_id,
        "settings": cfg, "rate_limited": status == ledger.RATE_LIMITED,
    }


# ─── The worker's half ───────────────────────────────────────────


def _event_of(raw: dict) -> CommentEvent:
    data = dict(raw)
    data["actor_ids"] = tuple(data.get("actor_ids") or ())
    return CommentEvent(**data)


def _language(provider: str, repo: str, workspace_id: str) -> str:
    from src.review.review_defaults import settings_for_repo

    try:
        override = settings_for_repo(provider, repo, ("review_language",))["review_language"]
    except Exception:  # noqa: BLE001
        override = None
    return messages.resolve_language(override, workspace_id)


def execute(p: dict, *, provider=None) -> None:
    """Run one accepted command and answer it. Never raises."""
    from src.review import providers as providers_mod
    from src.review.settings import get_review_settings

    row_id = p.get("ledger_id")
    try:
        ev = _event_of(p["event"])
        command = ParsedCommand(**p["command"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_payload_unreadable err=%s", type(exc).__name__)
        ledger.finish(row_id, ledger.FAILED, error="unreadable job payload")
        return
    workspace_id = str(p.get("workspace_id") or "default")
    user_id = str(p.get("user_id") or "default")
    settings = dict(p.get("settings") or {})

    own = provider is not None
    try:
        provider = provider or providers_mod.get_provider_for(
            ev.provider, user_id=user_id, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_no_provider provider=%s err=%s", ev.provider,
                       type(exc).__name__)
        ledger.finish(row_id, ledger.FAILED, error="no usable provider connection")
        return

    ctx = CommandContext(
        ev=ev, provider=provider, command=command, settings=settings,
        workspace_id=workspace_id, user_id=user_id,
        language=_language(ev.provider, ev.repo, workspace_id),
        handle=get_review_settings().bot_handle, row_id=row_id,
    )
    status, error = ledger.DONE, None
    try:
        status = _run(ctx, p)
    except Exception as exc:  # noqa: BLE001
        status, error = ledger.FAILED, type(exc).__name__
        logger.exception("command_failed command=%s provider=%s repo=%s pr=%d",
                         command.name, ev.provider, ev.repo, ev.pr_number)
        try:
            ctx.reply(ctx.t("command.failed"))
        except Exception:  # noqa: BLE001
            logger.warning("command_failure_reply_failed")
    finally:
        ledger.finish(row_id, status, error=error, reply_comment_id=ctx.reply_id,
                      run_id=ctx.run_id)
        if not own:
            with contextlib.suppress(Exception):
                provider.close()


def _run(ctx: CommandContext, p: dict) -> str:
    ev = ctx.ev

    # The bot's own words, once more, in case the receiver was bypassed.
    reason = gate.own_text_reason(ev)
    if reason:
        logger.info("command_ignored reason=%s provider=%s repo=%s", reason,
                    ev.provider, ev.repo)
        return ledger.IGNORED

    if p.get("rate_limited"):
        ctx.reply(ctx.t("command.rate_limited"))
        return ledger.RATE_LIMITED

    verdict = gate.authorize(ev, ctx.provider, str(ctx.settings.get("command_permission")))
    if not verdict.allowed:
        # Answered once per person and pull request per window.
        first = ledger.count_recent(
            ctx.workspace_id, provider=ev.provider, repo=ev.repo,
            pr_number=ev.pr_number, actor_id=ev.actor_id, status=ledger.DENIED,
        ) == 0
        if first:
            ctx.reply(ctx.t("command.denied", actor=ev.actor_name or "you"))
        return ledger.DENIED

    fn = _REGISTRY.get(ctx.command.name)
    if fn is None:
        ctx.reply(
            ctx.t("command.unavailable", command=ctx.command.name) + "\n\n"
            + help_markdown(ctx.handle, ctx.language, available=available_commands()))
        return ledger.DONE
    return fn(ctx) or ledger.DONE


# ─── Built-in commands ───────────────────────────────────────────


def _help(ctx: CommandContext) -> str | None:
    chat_on = ctx.settings.get("chat_enabled", True) is not False
    ctx.reply(help_markdown(
        ctx.handle, ctx.language,
        available=[name for name in available_commands() if chat_on or name != CHAT]))
    return None


def _start_review(ctx: CommandContext) -> str | None:
    """`start-review`, `review`, `review --force`.

    The review is queued exactly as the Resume button queues it. A person asked,
    so the draft, title and cadence gates do not apply (the request is an
    explicit trigger); a switched-off reviewer still is (`gate_enabled`), and
    the review says so in its answer. `--force` is the full-history request:
    the incremental review (`scope.decide_scope`) reads `request.scope == "full"` as "review every
    file, ignore the baseline".
    """
    from src.review import pr_state
    from src.review.dispatch import enqueue_review_run, execute_review
    from src.review.scope import ReviewRequest

    ev, force = ctx.ev, ctx.command.force
    if ev.pr_state != "open":
        ctx.reply(ctx.t("command.pr_closed", state=ev.pr_state))
        return None

    ctx.acknowledge()
    # A paused PR is resumed first: `resume=True` makes the review cover every
    # push that was skipped while it was paused.
    resumed = False
    try:
        resumed = pr_state.resume(ctx.workspace_id, ev.provider, ev.repo, ev.pr_number)
    except Exception as exc:  # noqa: BLE001 — the review does not depend on it
        logger.warning("command_resume_failed pr=%d err=%s", ev.pr_number,
                       type(exc).__name__)
    request = ReviewRequest(
        trigger="command", force=force, scope="full" if force else None, resume=resumed,
    )
    ack = {
        "event": asdict(ev) | {"actor_ids": list(ev.actor_ids)},
        "ack_comment_id": ctx.ack_comment_id, "language": ctx.language,
    }
    queued = enqueue_review_run(
        ev.provider, ev.repo, ev.pr_number, user_id=ctx.user_id,
        workspace_id=ctx.workspace_id, source="command", request=request,
        extra={"command_ack": ack},
    )
    ctx.run_id = queued.run_id
    if queued.status == "duplicate":
        _settle_ack(ctx, ctx.t("command.already_queued"))
    elif queued.status == "inline":
        # No queue: the review runs here, and answers the comment itself.
        execute_review(queued.payload)
    return None


def _settle_ack(ctx: CommandContext, text: str) -> None:
    """Replace the "working on it" note with `text`, or post `text` when there
    is no such note."""
    if ctx.ack_comment_id and ctx.provider.update_comment(
            ctx.ev.repo, ctx.ev.pr_number, ctx.ack_comment_id, text, kind=ctx.ev.kind):
        return
    ctx.reply(text)


def finish_ack(p: dict, provider, *, result=None, failed: bool = False) -> None:
    """The review a comment asked for has ended: say how.

    Called by `dispatch.execute_review` while its provider is open. A review
    that ran leaves its own summary on the pull request, so a provider with
    reactions says nothing more; a note we posted ourselves is edited to point
    there. A review that did not run (a gate skipped it, or it failed) is
    always explained in the thread.
    """
    ack = p.get("command_ack")
    if not isinstance(ack, dict):
        return
    from src.review.models import ReviewVerdict

    ev = _event_of(ack["event"])
    lang = str(ack.get("language") or "en")
    batch = getattr(result, "batch", None)
    skipped = batch is not None and batch.verdict == ReviewVerdict.SKIPPED
    if failed:
        text = messages.t("command.failed", lang)
    elif skipped:
        summary = str(getattr(batch, "summary", "") or "").strip()
        reason = summary.split("—", 1)[-1].strip() or summary
        text = messages.t("command.done_skipped", lang, reason=reason[:300])
    else:
        text = messages.t("command.done", lang)
    note = ack.get("ack_comment_id")
    if note and provider.update_comment(ev.repo, ev.pr_number, str(note), text, kind=ev.kind):
        return
    if skipped or failed or note:
        provider.post_reply(ev, text)


register_command(START_REVIEW, _start_review)
register_command(REVIEW, _start_review)
register_command(HELP, _help)

register_command(REMEMBER, remember_command)
register_command(CHAT, chat_command)

# `business-logic` registers from the task-context package (the requirements
# check); `chat` from its own module (the chat step). Until a command is
# registered, a comment asking for it is answered with "not available" and the
# guide does not list it.
from src.review.task_context.command import register as _register_business_logic  # noqa: E402

_register_business_logic()
