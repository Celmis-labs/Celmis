"""The three providers' comment webhooks, as one `CommentEvent`.

The receiver verifies the delivery first (signature or token, then the tenant
binding); only then does it ask one of these extractors what the payload says.
They read, never trust: every field is type-checked, a payload that lacks the
repository, the pull-request number, the comment id or the text is None, and
nothing here raises — a malformed delivery is an ignored delivery.

Which webhook events carry a comment:

* Bitbucket  `pullrequest:comment_created` / `pullrequest:comment_updated`
* GitHub     `issue_comment` (when the issue is a pull request) and
             `pull_request_review_comment`; actions `created` and `edited`
* GitLab     `Note Hook` with `noteable_type == "MergeRequest"`, not a system note
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A GitLab project or group access token acts as `project_<id>_bot` (older
#: versions) or `project_<id>_bot_<random>`; the Note Hook carries no bot flag.
_GITLAB_TOKEN_BOT = re.compile(r"^(project|group)_\d+_bot(_\w+)?$")

GITHUB_COMMENT_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})
GITLAB_NOTE_EVENT = "Note Hook"
BITBUCKET_COMMENT_EVENTS = frozenset({
    "pullrequest:comment_created", "pullrequest:comment_updated",
})

#: GitHub `author_association` values of people who work on the repository.
GITHUB_MEMBER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True)
class CommentEvent:
    """One comment on one pull request, whichever provider it came from."""

    provider: str
    repo: str
    pr_number: int
    comment_id: str
    body: str
    #: The comment this one answers; None for the first comment of a thread.
    parent_id: str | None = None
    #: What a reply must be posted into (GitLab discussion id; GitHub root id).
    thread_id: str | None = None
    #: `issue` — a comment on the pull request itself; `inline` — on the diff.
    kind: str = "issue"
    #: The stable account id and the display handle of whoever wrote it.
    actor_id: str = ""
    actor_name: str = ""
    #: Every stable id the actor is known by (Bitbucket has two).
    actor_ids: tuple[str, ...] = ()
    actor_is_bot: bool = False
    #: GitHub `author_association` (OWNER, MEMBER, ...); "" elsewhere.
    actor_assoc: str = ""
    #: True/False when the payload says; None when it does not.
    repo_private: bool | None = None
    path: str | None = None
    line: int | None = None
    head_sha: str = ""
    base_ref: str = ""
    #: Normalised: open | merged | closed.
    pr_state: str = "open"
    is_draft: bool = False
    pr_title: str = ""
    edited: bool = False
    #: The delivery (`bb:<uuid>`, `gh:<delivery>`) — for the timeline only.
    event_key: str = ""


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _id(value: object) -> str:
    """An id as text; bool is not an id, and neither is an empty value."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, str)):
        return str(value).strip()
    return ""


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _state(raw: object) -> str:
    """open | merged | closed from any provider's pull-request state."""
    value = _text(raw).lower()
    if value in ("merged", "fulfilled"):
        return "merged"
    if value in ("closed", "declined", "rejected", "superseded", "locked"):
        return "closed"
    return "open"


def extract_bitbucket_comment(
    payload: object, event_key: str, *, delivery: str = "",
) -> CommentEvent | None:
    """`pullrequest:comment_created` / `pullrequest:comment_updated`."""
    if event_key not in BITBUCKET_COMMENT_EVENTS or not isinstance(payload, dict):
        return None
    pr = _dict(payload.get("pullrequest"))
    comment = _dict(payload.get("comment"))
    repository = _dict(payload.get("repository"))
    repo = _text(repository.get("full_name"))
    number = _int(pr.get("id"))
    comment_id = _id(comment.get("id"))
    body = _text(_dict(comment.get("content")).get("raw"))
    if not (repo and number > 0 and comment_id and body.strip()):
        return None
    if comment.get("deleted") is True:
        return None
    # `actor` is who triggered the event (for an edit, the editor); the comment's
    # own `user` is its author. Both fall back to each other.
    actor = _dict(payload.get("actor")) or _dict(comment.get("user"))
    ids = tuple(
        i for i in (_id(actor.get("uuid")), _id(actor.get("account_id"))) if i
    )
    parent = _id(_dict(comment.get("parent")).get("id")) or None
    inline = _dict(comment.get("inline"))
    line = _int(inline.get("to")) or _int(inline.get("from")) or None
    return CommentEvent(
        provider="bitbucket", repo=repo, pr_number=number, comment_id=comment_id,
        body=body, parent_id=parent, thread_id=parent,
        kind="inline" if inline else "issue",
        actor_id=ids[0] if ids else "",
        actor_name=_text(actor.get("nickname")) or _text(actor.get("display_name")),
        actor_ids=ids,
        actor_is_bot=_text(actor.get("type")) == "app_user",
        repo_private=(repository.get("is_private")
                      if isinstance(repository.get("is_private"), bool) else None),
        path=_text(inline.get("path")) or None, line=line,
        head_sha=_text(_dict(_dict(pr.get("source")).get("commit")).get("hash")),
        base_ref=_text(_dict(_dict(pr.get("destination")).get("branch")).get("name")),
        pr_state=_state(pr.get("state")),
        is_draft=bool(pr.get("draft")),
        pr_title=_text(pr.get("title")),
        edited=event_key == "pullrequest:comment_updated",
        event_key=f"bb:{delivery}" if delivery else event_key,
    )


def extract_github_comment(
    payload: object, event: str, *, delivery: str = "",
) -> CommentEvent | None:
    """`issue_comment` on a pull request, or `pull_request_review_comment`."""
    if event not in GITHUB_COMMENT_EVENTS or not isinstance(payload, dict):
        return None
    action = _text(payload.get("action"))
    if action not in ("created", "edited"):
        return None
    comment = _dict(payload.get("comment"))
    repository = _dict(payload.get("repository"))
    repo = _text(repository.get("full_name"))
    comment_id = _id(comment.get("id"))
    body = _text(comment.get("body"))
    if event == "issue_comment":
        issue = _dict(payload.get("issue"))
        if not issue.get("pull_request"):
            return None  # a comment on a plain issue
        number = _int(issue.get("number"))
        pr = issue
        head_sha, base_ref = "", ""
        parent = None
        kind, path, line = "issue", None, None
    else:
        pr = _dict(payload.get("pull_request"))
        number = _int(pr.get("number"))
        head_sha = _text(_dict(pr.get("head")).get("sha"))
        base_ref = _text(_dict(pr.get("base")).get("ref"))
        parent = _id(comment.get("in_reply_to_id")) or None
        kind = "inline"
        path = _text(comment.get("path")) or None
        line = _int(comment.get("line")) or _int(comment.get("original_line")) or None
    if not (repo and number > 0 and comment_id and body.strip()):
        return None
    user = _dict(comment.get("user"))
    login = _text(user.get("login"))
    uid = _id(user.get("id"))
    merged = bool(pr.get("merged") or pr.get("merged_at"))
    return CommentEvent(
        provider="github", repo=repo, pr_number=number, comment_id=comment_id,
        body=body, parent_id=parent,
        # A review comment's replies all hang off the thread's first comment.
        thread_id=parent or (comment_id if kind == "inline" else None),
        kind=kind,
        actor_id=uid or login, actor_name=login,
        actor_ids=tuple(i for i in (uid, login) if i),
        actor_is_bot=_text(user.get("type")) == "Bot" or login.endswith("[bot]"),
        actor_assoc=_text(comment.get("author_association")).upper(),
        repo_private=(repository.get("private")
                      if isinstance(repository.get("private"), bool) else None),
        path=path, line=line, head_sha=head_sha, base_ref=base_ref,
        pr_state="merged" if merged else _state(pr.get("state")),
        is_draft=bool(pr.get("draft")),
        pr_title=_text(pr.get("title")),
        edited=action == "edited",
        event_key=f"gh:{delivery}" if delivery else event,
    )


def extract_gitlab_note(payload: object) -> CommentEvent | None:
    """A `Note Hook` on a merge request — never a system note."""
    if not isinstance(payload, dict):
        return None
    attrs = _dict(payload.get("object_attributes"))
    if _text(attrs.get("noteable_type")) != "MergeRequest" or attrs.get("system"):
        return None
    mr = _dict(payload.get("merge_request"))
    project = _dict(payload.get("project"))
    repo = _text(project.get("path_with_namespace"))
    number = _int(mr.get("iid"))
    comment_id = _id(attrs.get("id"))
    body = _text(attrs.get("note"))
    if not (repo and number > 0 and comment_id and body.strip()):
        return None
    user = _dict(payload.get("user"))
    username = _text(user.get("username"))
    uid = _id(user.get("id"))
    level = project.get("visibility_level")
    # 0 is private. Internal (10) is open to every account of the instance, so
    # like public (20) it says nothing about this commenter's access.
    private = (level == 0) if isinstance(level, int) and not isinstance(level, bool) else None
    discussion = _id(attrs.get("discussion_id")) or None
    position = _dict(attrs.get("position"))
    state = _text(mr.get("state")).lower()
    return CommentEvent(
        provider="gitlab", repo=repo, pr_number=number, comment_id=comment_id,
        body=body, parent_id=None, thread_id=discussion,
        kind="inline" if _text(attrs.get("type")) == "DiffNote" else "issue",
        actor_id=uid or username, actor_name=username,
        actor_ids=tuple(i for i in (uid, username) if i),
        actor_is_bot=bool(user.get("bot")) or bool(_GITLAB_TOKEN_BOT.match(username)),
        repo_private=private,
        path=_text(position.get("new_path")) or None,
        line=_int(position.get("new_line")) or None,
        head_sha=_text(_dict(mr.get("last_commit")).get("id")),
        base_ref=_text(mr.get("target_branch")),
        pr_state="merged" if state == "merged" else _state(state),
        is_draft=bool(mr.get("draft") or mr.get("work_in_progress")),
        pr_title=_text(mr.get("title")),
        edited=_text(attrs.get("action")) == "update",
        event_key=f"gl:{repo}:{number}:{comment_id}",
    )
