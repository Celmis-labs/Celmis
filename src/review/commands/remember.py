"""`@celmis remember [--repo|--org|--dir[=path]]: <rule>` — teach the reviewer.

The comment's text goes to the team's memories (`src.review.memories.remember`),
which decides whether it is active at once (a trusted person) or waits on the
Memories page for an editor, and whether the team already knows it. This
module only works out WHO is asking and WHERE the rule applies, then says what
happened in the thread. Runs in the command worker (the store blocks).
"""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING

from src.review import memories

if TYPE_CHECKING:
    from src.review.commands.handlers import CommandContext

logger = logging.getLogger(__name__)

#: How much of a stored rule the answer repeats.
_ECHO_CHARS = 200


def _actor(ctx: CommandContext) -> memories.ActorRef:
    """The commenter as the store needs them. `is_token_owner` is true only for
    the provider token's own account, never for "whoever is commenting": the
    owner is always trusted, so a stranger must not pass for it."""
    ev = ctx.ev
    try:
        viewer = frozenset(ctx.provider.viewer_ids() or ())
    except Exception:  # noqa: BLE001 — unknown identity means not the owner
        viewer = frozenset()
    mine = {i for i in (ev.actor_id, *ev.actor_ids) if i}
    return memories.ActorRef(
        provider=ev.provider, external_id=ev.actor_id or None,
        display=ev.actor_name or None, is_token_owner=bool(viewer & mine),
        aliases=tuple(i for i in ev.actor_ids if i and i != ev.actor_id),
    )


def _scope(ctx: CommandContext) -> tuple[str | None, str | None] | str:
    """(repo_slug, path_glob) for the rule, or the message key explaining why the
    request cannot be placed."""
    from src.sync.git_providers import parse_repo_url

    ev, command = ctx.ev, ctx.command
    if command.scope == "org":
        return None, None
    try:
        slug = parse_repo_url(f"{ev.provider}:{ev.repo}").slug
    except Exception:  # noqa: BLE001
        slug = ev.repo.replace("/", "-")
    if command.scope != "dir":
        return slug, None
    path = command.path
    if not path and ev.path:
        # A comment on a line of a file means "this file's directory".
        path = posixpath.dirname(ev.path)
    if not path:
        return "command.remember.dir_needed"
    return slug, path


def remember_command(ctx: CommandContext) -> str | None:
    text = (ctx.command.args or "").strip()
    if not text:
        ctx.reply(ctx.t("command.remember.empty", handle=ctx.handle))
        return None
    placed = _scope(ctx)
    if isinstance(placed, str):
        ctx.reply(ctx.t(placed, handle=ctx.handle))
        return None
    slug, glob = placed
    ev = ctx.ev
    result = memories.remember(
        ctx.workspace_id, text, repo_slug=slug, path_glob=glob, actor=_actor(ctx),
        source=memories.SourceRef(
            provider=ev.provider, repo=ev.repo, pr_number=ev.pr_number,
            comment_id=ev.comment_id),
        origin="command",
    )
    scope = ctx.t(f"command.remember.scope.{memories.scope_of(slug, glob)}")
    shown = (text if len(text) <= _ECHO_CHARS else text[:_ECHO_CHARS] + "…")
    key = {
        "create": "command.remember.created",
        "update": "command.remember.updated",
        "pending": "command.remember.pending",
    }.get(result.action, "command.remember.skipped")
    ctx.reply(ctx.t(key, scope=scope, text=shown, reason=result.reason))
    return None
