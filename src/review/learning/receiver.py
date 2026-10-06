"""The webhook receiver's half of learning: a comment on a pull request that
may answer one of our findings, and a thread somebody resolved.

Both run on a worker thread (they touch the database and, for a comment, the
provider). The receiver has already verified the delivery and bound it to ONE
workspace; nothing here widens that.
"""

from __future__ import annotations

import contextlib
import logging

from src.review.learning import replies, resolve
from src.review.learning import signals as sig

logger = logging.getLogger(__name__)


def learn_from_comment(ev, *, workspace_id: str, user_id: str) -> replies.ReplyResult:
    """Read one comment as feedback on a finding. Never raises.

    The cheap test comes first: a comment that is not a reply, or whose thread
    is not one of our findings, ends after one indexed lookup (no provider
    call, no model). Only the comments that answer a finding build a provider
    connection, ask the repository's `command_permission` whether this person
    may teach the reviewer, and are read.
    """
    try:
        if not replies.is_reply_candidate(ev):
            return replies.ReplyResult(reason="not a human reply")
        pr = sig.PRRef(ev.provider, ev.repo, int(ev.pr_number))
        if replies._find_posted(workspace_id, pr, ev, None) is None:  # noqa: SLF001
            return replies.ReplyResult(reason="not a reply to a finding")

        from src.review import providers as providers_mod
        from src.review.commands import gate
        from src.review.review_defaults import command_settings_for_repo
        from src.review.settings import get_review_settings

        mode = str(command_settings_for_repo(ev.provider, ev.repo).get("command_permission"))
        provider = providers_mod.get_provider_for(
            ev.provider, user_id=user_id, workspace_id=workspace_id)
        try:
            owners = provider.viewer_ids()
            # The same rule that lets a person command the bot: a stranger's
            # reply must not teach it (or cost a model call).
            return replies.handle_comment_event(
                ev, workspace_id=workspace_id, user_id=user_id,
                handle=get_review_settings().bot_handle, viewer_ids=owners,
                permitted=lambda: not gate.own_text_reason(ev)
                and gate.authorize(ev, provider, mode).allowed)
        finally:
            with contextlib.suppress(Exception):
                provider.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_comment_failed provider=%s err_type=%s",
                       getattr(ev, "provider", "?"), type(exc).__name__)
        return replies.ReplyResult(action="error", reason=type(exc).__name__)


def learn_from_thread(ev: resolve.ThreadEvent, *, workspace_id: str, user_id: str = "") -> dict:
    """A resolved / reopened thread as a weak signal. Never raises.

    A person who resolves a thread teaches the reviewer only when the
    repository's `command_permission` lets them: on a public repository any
    account can resolve a thread it can see."""
    def permitted() -> bool:
        from src.review import providers as providers_mod
        from src.review.commands import gate
        from src.review.review_defaults import command_settings_for_repo

        mode = str(command_settings_for_repo(ev.provider, ev.repo).get("command_permission"))
        provider = providers_mod.get_provider_for(
            ev.provider, user_id=user_id, workspace_id=workspace_id)
        try:
            return gate.person_may_teach(
                provider, repo=ev.repo, pr_number=ev.pr_number, mode=mode,
                actor_id=ev.actor_id, actor_name=ev.actor_name,
                repo_private=ev.repo_private)
        finally:
            with contextlib.suppress(Exception):
                provider.close()

    def checked() -> bool:
        try:
            return permitted()
        except Exception as exc:  # noqa: BLE001 — unknown is not allowed
            logger.warning("learning_thread_permission_failed provider=%s err_type=%s",
                           ev.provider, type(exc).__name__)
            return False

    try:
        return resolve.handle_thread_event(ev, workspace_id=workspace_id, permitted=checked)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_thread_failed provider=%s err_type=%s",
                       getattr(ev, "provider", "?"), type(exc).__name__)
        return {"action": "error"}
