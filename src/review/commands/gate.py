"""Who the bot listens to, and when it must keep quiet.

Two questions, asked at different times:

* `own_text_reason` — is this comment the bot's own, or another bot's? Asked
  before anything is recorded: the bot answering itself (or two bots answering
  each other) is the one failure that fills a pull request with comments, so
  it is decided by the bot marker and the author's type, never by a counter
  alone. The token owner's own marker-free comments are a person's.
* `authorize` — may THIS person command the bot on THIS repository? Asked by
  the worker, where talking to the provider is allowed.

`command_permission` (inheritable setting):

* `repo_access`  (built-in) a private repository: whoever can comment. A public
                 repository: an owner, member or collaborator, a person with
                 write access, or a participant of the pull request.
* `participants` the pull request's author, reviewers and assignees only.
* `anyone`       whoever can comment.

A provider that cannot tell (`unknown`) never grants more than the
participants rule does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.review import markers
from src.review.commands.events import GITHUB_MEMBER_ASSOCIATIONS, CommentEvent

REPO_ACCESS = "repo_access"
PARTICIPANTS = "participants"
ANYONE = "anyone"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    #: Why not (for the ledger and the log), "" when allowed.
    reason: str = ""


def own_text_reason(ev: CommentEvent) -> str:
    """Why the bot must not act on this comment, or "" when it may.

    Decided by the text's marker, with authorship only secondary: the review
    token is usually one person's account, so a marker-free comment written
    through it is that person talking (they may command the bot, teach it and
    give feedback). Every bot write carries a marker, and a person who quotes
    us puts `>` in front of it, so a quoted marker never matches.
    """
    if ev.actor_is_bot:
        return "author is a bot"
    if markers.is_bot_text(ev.body):
        return "carries a bot marker"
    return ""


def authorize(ev: CommentEvent, provider, mode: str) -> Verdict:
    """May this commenter command the bot? See the module docstring."""
    mode = mode if mode in (REPO_ACCESS, PARTICIPANTS, ANYONE) else REPO_ACCESS
    if mode == ANYONE:
        return Verdict(True)
    ids = {i for i in (ev.actor_id, *ev.actor_ids) if i}
    if not ids:
        return Verdict(False, "the author could not be identified")

    if mode == REPO_ACCESS:
        # Who can comment on a private repository already has access to it.
        if ev.repo_private is True:
            return Verdict(True)
        if ev.actor_assoc in GITHUB_MEMBER_ASSOCIATIONS:
            return Verdict(True)
        try:
            level = provider.actor_permission(
                ev.repo, actor_id=ev.actor_id, actor_name=ev.actor_name)
        except Exception:  # noqa: BLE001 — unknown, not allowed
            level = "unknown"
        if level == "write":
            return Verdict(True)

    try:
        people = provider.pr_participants(ev.repo, ev.pr_number)
    except Exception:  # noqa: BLE001
        people = frozenset()
    if ids & set(people):
        return Verdict(True)
    return Verdict(False, "not allowed to command the reviewer here")


def person_may_teach(
    provider, *, repo: str, pr_number: int, mode: str, actor_id: str = "",
    actor_name: str = "", assoc: str = "", repo_private: bool | None = None,
) -> bool:
    """`authorize` for a signal that is not a comment: a thumb, a resolved
    thread. The same `command_permission` rule decides who may teach the
    reviewer, so on a public repository a stranger's thumbs-down is not
    feedback. A person who cannot be identified is not allowed."""
    ev = CommentEvent(
        provider=getattr(provider, "name", "") or "", repo=repo, pr_number=int(pr_number),
        comment_id="", body="", actor_id=actor_id, actor_name=actor_name or actor_id,
        actor_assoc=assoc, repo_private=repo_private)
    return authorize(ev, provider, mode).allowed


def teaching_check(provider, *, repo: str, pr_number: int, mode: str) -> Callable[[str], bool]:
    """`person_may_teach` for a handle, remembered per handle (a poll meets the
    same few reactors on every comment)."""
    seen: dict[str, bool] = {}

    def check(user: str) -> bool:
        if user not in seen:
            seen[user] = person_may_teach(
                provider, repo=repo, pr_number=pr_number, mode=mode,
                actor_id=user, actor_name=user)
        return seen[user]

    return check
