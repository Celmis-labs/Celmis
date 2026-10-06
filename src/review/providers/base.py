"""PullRequestProvider abstract interface."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime

from src.review import messages
from src.review.markers import has_marker
from src.review.models import (
    Finding,
    HunkSide,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.pr_summary import sanitize_prose
from src.review.scope import CommitInfo  # noqa: F401 — the provider API's commit type

logger = logging.getLogger(__name__)


class PullRequestProviderError(Exception):
    """Provider operation failed (auth/network/api error)."""


class EmptyDiffError(PullRequestProviderError):
    """The provider lists changed files but sent no diff, even after retries.

    Carries the pull request as far as it could be read (`pr`: metadata, empty
    diff, `reported_files` set) so the caller can say so ON the pull request
    instead of failing where nobody on it can see. `files` is None when the
    provider could not even say how many files the pull request changes.
    """

    def __init__(self, message: str, *, pr: PullRequest | None = None,
                 files: int | None = 0) -> None:
        super().__init__(message)
        self.pr = pr
        self.files = files


@dataclass(frozen=True)
class OurThread:
    """One comment thread of ours on a PR, as `our_inline_threads` lists it.

    `comment_id` is the root comment; `thread_id` is what resolving needs
    (GitHub's GraphQL node id, GitLab's discussion id; None on Bitbucket,
    which resolves by comment id). `fingerprint` is the 16-hex token of the
    finding marker, None for a comment that has none (an older review).
    """

    comment_id: int | str
    path: str
    line: int | None = None
    side: str = "RIGHT"
    resolved: bool = False
    thread_id: str | None = None
    fingerprint: str | None = None
    sha: str | None = None
    #: Somebody else replied: such a thread is a conversation, not a leftover.
    replied: bool = False
    #: `line` is where the code stands NOW (GitHub's `line`), not where the
    #: comment was created (GitLab, Bitbucket, GitHub's `originalLine`).
    line_is_current: bool = False


@dataclass(frozen=True)
class PostedComment:
    """An inline comment this run created, for whoever follows it later
    (the learning loop links replies and reactions back to the finding).

    `fingerprint` is the 16-hex token in the comment's finding marker;
    `finding_key` the full issue fingerprint (`issues.fingerprint`), the key
    the issue ledger uses.
    """

    comment_id: int | str
    path: str
    line: int
    fingerprint: str
    finding_key: str
    #: What a reply to this comment names instead of its id (GitLab: the
    #: discussion id); None where the comment id is that name.
    thread_id: str | None = None


@dataclass(frozen=True)
class ThreadMessage:
    """One comment of a conversation, as `get_thread` lists it.

    `text` is the comment as written (a hidden marker revealed, nothing
    removed); the reader decides what to drop. `ours` is True only when this
    provider's token wrote it: authorship, never the marker, because a quote
    reply copies the marker into somebody else's comment.
    """

    comment_id: str
    author: str
    text: str
    ours: bool = False
    #: Where the comment is anchored on the diff; None for a plain comment.
    path: str | None = None
    line: int | None = None
    created_at: str | None = None


def trim_thread(found: list[ThreadMessage], limit: int) -> list[ThreadMessage]:
    """At most `limit` messages: the root, then the latest ones."""
    limit = max(1, int(limit))
    if len(found) <= limit:
        return found
    return [found[0], *found[-(limit - 1):]] if limit > 1 else found[-1:]


def finding_fingerprint(finding: Finding) -> tuple[str, str]:
    """(16-hex marker token, full issue fingerprint) of a finding."""
    from src.review.issues import fingerprint

    full = fingerprint(finding.rule_id, finding.file_path, finding.title)
    return full[:16], full


def finding_marker_for(finding: Finding, head_sha: str | None) -> str:
    """The `celmis:finding` marker line of an inline comment. The commit is
    informational (the fingerprint is what a later run matches on), so a head
    that is not a 12-hex commit id is written as twelve zeros rather than
    leaving the comment without a marker."""
    from src.review.markers import finding_marker

    short = (head_sha or "").lower()[:12]
    if not re.fullmatch(r"[0-9a-f]{12}", short):
        short = "0" * 12
    return finding_marker(finding_fingerprint(finding)[0], short)


@dataclass
class IncrementalPost:
    """What an incremental `post_review` knows before it posts: our earlier
    threads sorted into outdated and standing, and the findings it will not
    post inline (already on the PR, or on the old side, which an increment
    cannot anchor)."""

    threads: list[OurThread]
    to_resolve: list[OurThread]
    open_threads: list[OurThread]
    resolved_before: int = 0
    demoted: list[Finding] = None  # type: ignore[assignment]
    duplicates: int = 0
    #: False when the provider could not list our threads (no dedupe, no resolve).
    listed: bool = True

    def __post_init__(self) -> None:
        if self.demoted is None:
            self.demoted = []


def begin_incremental_post(
    provider: PullRequestProvider, batch: ReviewBatch, marker: str,
) -> IncrementalPost | None:
    """Plan an incremental post, or None for a full review (nothing changes).

    Lists our earlier threads once and sorts them: a thread on code the new
    commits removed is outdated (resolved after posting), the rest stay open
    and are what `select` compares new findings with.
    """
    pr = batch.pull_request
    if pr.scope is None:
        return None
    from src.review import scope as scope_mod

    listed = True
    try:
        threads = provider.our_inline_threads(pr, marker)
    except Exception as exc:  # noqa: BLE001 — an incremental problem never fails a review
        logger.warning("incremental_threads_failed err=%s", type(exc).__name__)
        threads = None
    if threads is None:
        threads, listed = [], False
    plan = scope_mod.plan_threads(
        threads, pr.scope.removed_lines, pr.scope.deleted_files,
        base_sha=pr.scope.base_sha)
    # A thread somebody replied in is a conversation, not a leftover: it stays
    # open whatever happened to the line.
    to_resolve = [t for t in plan.resolve if not t.replied]
    kept = list(plan.keep_open) + [t for t in plan.resolve if t.replied]
    # What the new findings are compared with is where the code stands now: a
    # thread posted at the last reviewed commit is moved through the increment.
    kept = [_at_head(t, pr) for t in kept]
    return IncrementalPost(
        threads=threads, to_resolve=to_resolve, open_threads=kept,
        resolved_before=plan.resolved_before, listed=listed,
    )


def _at_head(thread: OurThread, pr: PullRequest) -> OurThread:
    """`thread` with its line carried from the last reviewed commit to the
    head, when it can be (see `scope.base_line`); else as is."""
    from src.review import scope as scope_mod

    base = scope_mod.base_line(thread, pr.scope.base_sha)
    if base is None:
        return thread
    moved = scope_mod.map_old_line(pr.hunks, thread.path, base)
    if moved is None or moved == thread.line:
        return thread
    return replace(thread, line=moved, line_is_current=True)


def incremental_skip(state: IncrementalPost | None):
    """The `skip` callable for `ReviewBatch.inline_findings`, or None for a
    full review. Counts into `state` what it drops."""
    if state is None:
        return None
    from src.review import scope as scope_mod

    def skip(finding: Finding) -> bool:
        if finding.side == HunkSide.LEFT:
            # The old side of an increment is the old side of the last
            # reviewed commit, not of the PR's target: no valid anchor.
            state.demoted.append(finding)
            return True
        fp = finding_fingerprint(finding)[0]
        if scope_mod.already_posted(fp, finding.file_path, finding.line, state.open_threads):
            state.duplicates += 1
            return True
        return False

    return skip


def finish_incremental_post(
    provider: PullRequestProvider, batch: ReviewBatch, state: IncrementalPost | None,
    *, posted: int, refused: list[Finding] | None = None,
) -> dict:
    """After the inline comments are up: resolve the outdated threads, put the
    banner (and the cumulative line) in the summary, fold the findings that
    could not be anchored into it. Returns the response keys to merge.
    Never raises."""
    if state is None:
        return {}
    pr = batch.pull_request
    language = batch.review_language
    resolution = {"resolved": 0, "failed": 0, "unsupported": 0}
    if state.to_resolve:
        try:
            # Before the call: the webhook for a closed thread can be faster
            # than the reply to the call that closed it.
            from src.review.learning import resolve as learning_resolve

            learning_resolve.note_closed_by_us(
                pr.provider, pr.repo, pr.number, [t.comment_id for t in state.to_resolve])
        except Exception:  # noqa: BLE001 — a missing note only costs a weak signal
            logger.debug("note_closed_by_us_failed")
        try:
            resolution.update(provider.resolve_threads(pr, state.to_resolve))
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_threads_failed err=%s", type(exc).__name__)
            resolution["failed"] = len(state.to_resolve)
    standing = len(state.open_threads) + len(state.to_resolve) - resolution["resolved"]
    batch.add_section(
        "incremental", incremental_banner(pr, language, open_threads=standing + posted,
                                          resolved=state.resolved_before + resolution["resolved"],
                                          with_threads=state.listed),
        order=50, targets={"comment"})
    left_out = list(state.demoted) + list(refused or [])
    if left_out:
        batch.add_section(
            "unanchored", _format_unanchored(left_out, pr, language),
            order=900, targets={"comment"})
    return {
        "incremental": True,
        "threads_resolved": resolution["resolved"],
        "threads_resolve_failed": resolution["failed"],
        "threads_resolve_unsupported": resolution["unsupported"],
        "findings_already_posted": state.duplicates,
        "findings_demoted_to_summary": len(state.demoted),
    }


def incremental_banner(
    pr: PullRequest, language: str | None, *, open_threads: int | None = None,
    resolved: int | None = None, with_threads: bool = True,
) -> str:
    """The summary's first block of an incremental review: what was read, and
    how the earlier comments stand."""
    scope = pr.scope
    if scope is None:
        return ""
    files = len({h.file_path for h in pr.hunks})
    text = messages.t(
        "incremental.banner", language,
        commits=messages.tn("incremental.commits", scope.new_commits, language,
                            count=scope.new_commits),
        sha=(scope.base_sha or "")[:7],
        files=messages.tn("started.files_count", files, language, count=files),
    )
    if with_threads and open_threads is not None and resolved is not None:
        text += "\n\n" + messages.t(
            "incremental.threads", language, open=open_threads, resolved=resolved)
    return text


@dataclass(frozen=True)
class PathCommit:
    """One commit of a branch, as far as a path-filtered history says it."""

    sha: str
    subject: str = ""
    #: ISO-8601 as the provider wrote it; None when it did not say.
    date: str | None = None
    url: str | None = None


#: The largest file a head check will read. A file over it is "unreadable",
#: never an empty answer: a prompt cannot hold it and a hash of half of it
#: would call an unchanged file changed.
MAX_FILE_BYTES = 1_000_000


def committed_since(date: str | None, since: datetime | None) -> bool:
    """Is a commit dated `date` (ISO-8601) not older than `since`?

    An unreadable or missing date counts as recent: dropping a commit because
    its date could not be read would hide the very commit that fixed an issue.
    """
    if since is None or not date:
        return True
    try:
        when = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=since.tzinfo)
    return when >= since


@dataclass(frozen=True)
class FileChange:
    """What one commit did to one path: added | modified | deleted | renamed.

    `path` is the path AFTER the commit (the new name for a rename, the old
    one for a deletion); `previous_path` is set for a rename only.
    """

    status: str
    path: str
    previous_path: str | None = None


class PullRequestProvider(ABC):
    """Provider-agnostic PR ops: fetch + post comments."""

    name: str = ""

    #: The longest PR description this provider stores, for `compose_description`
    #: to fit our block into; None means `pr_actions.DESCRIPTION_MAX_CHARS`.
    description_max_chars: int | None = None

    #: The repository's review language for the notes a provider writes by
    #: itself (`upsert_feedback_comment`); set by the review's lifecycle once
    #: the policy is known. None reads as English.
    review_language: str | None = None

    @staticmethod
    def _expect_ok(resp, what: str):
        """`resp` when it is a 2xx; PullRequestProviderError otherwise.

        The guarded clients never follow a redirect on their own, so a 301/302
        reaches the caller as a response with an EMPTY body. Reading `.text`
        off it was how a moved repository (or Bitbucket's diff endpoint, which
        answers 302) turned into "the pull request has no diff": a quiet skip
        of a PR that had one. A 3xx is therefore an error that names where it
        pointed, not a result.
        """
        code = resp.status_code
        if 200 <= code < 300:
            return resp
        if 300 <= code < 400:
            where = resp.headers.get("location") or "no Location header"
            raise PullRequestProviderError(
                f"{what} answered with a redirect (HTTP {code} to {where[:200]})"
            )
        raise PullRequestProviderError(f"{what} error {code}: {resp.text[:200]}")

    # ─── Reading the target branch (the issues backlog) ──────────
    #
    # Non-abstract on purpose: a provider that cannot read a branch answers
    # with an error, and the backlog check then calls the issue UNREADABLE and
    # leaves it open — never fixed. All four take the full repository name as
    # the review calls it (`owner/name`, `workspace/slug`, `group/project`).

    def branch_head_sha(self, repo: str, branch: str) -> str:
        """The sha `branch` points at now. Raises PullRequestProviderError on
        any failure — a missing branch included: the caller must not mistake
        "could not ask" for "nothing there"."""
        raise PullRequestProviderError(
            f"{self.name or 'this provider'} cannot read a branch head")

    def read_file_at(self, repo: str, ref: str, path: str) -> str | None:
        """The text of `path` at `ref` (a sha or a branch); None when the file
        does not exist there. Raises PullRequestProviderError when it could
        not be read (no scope, a rate limit, too large, an outage)."""
        raise PullRequestProviderError(
            f"{self.name or 'this provider'} cannot read a file")

    def commits_touching(
        self, repo: str, ref: str, path: str, *,
        since: datetime | None = None, limit: int = 10,
    ) -> list[PathCommit]:
        """The commits on `ref` that touched `path`, newest first, none older
        than `since`, at most `limit`."""
        raise PullRequestProviderError(
            f"{self.name or 'this provider'} cannot list a file's commits")

    def file_change_in_commit(
        self, repo: str, sha: str, path: str,
    ) -> FileChange | None:
        """What commit `sha` did to `path` (None when it did not touch it).
        Tells a deletion or a rename from an edit — the one thing a missing
        file at the branch head cannot say by itself."""
        raise PullRequestProviderError(
            f"{self.name or 'this provider'} cannot read a commit's files")

    def commit_url(self, repo: str, sha: str) -> str | None:
        """A web link to a commit, or None."""
        return None

    def list_comment_reactions(
        self, repo: str, pr_number: int, comment_id: str,
    ) -> list[tuple[str, str]]:
        """The thumbs on one review comment as (user, "up" | "down") pairs, a
        person at most once per direction. Raises PullRequestProviderError when
        the provider has no such reactions or the read failed — the learning
        poll then simply skips that comment."""
        raise PullRequestProviderError(
            f"{self.name or 'this provider'} cannot list reactions")

    @abstractmethod
    def fetch_pull_request(
        self, repo: str, pr_number: int,
    ) -> PullRequest:
        """Fetch PR metadata + raw diff + parse hunks."""

    @abstractmethod
    def post_review(
        self, batch: ReviewBatch, *, dry_run: bool = False,
    ) -> dict:
        """Submit review (batched comments + summary).

        Returns a dict with the provider-specific response (review_id,
        comment_ids). If dry_run=True — just simulate without an actual
        API call.
        """

    @abstractmethod
    def find_existing_review_comment(
        self, repo: str, pr_number: int, marker: str,
    ) -> int | None:
        """Find existing summary comment with marker — for idempotent updates.

        Returns comment_id if found (for PATCH); None — create a new one.
        """

    def find_marked_comment_ids(
        self, repo: str, pr_number: int, marker: str,
    ) -> list[int]:
        """Every comment of OURS carrying the marker — summary AND inline.

        Authorship is part of the contract, not just the marker: all three
        implementations match author AND marker, because "Quote reply" copies
        the quoted comment's raw markdown — marker included — into a human's
        rebuttal. Defaults to nothing so a provider that has not implemented
        it keeps today's behaviour rather than silently deleting the wrong
        things.
        """
        return []

    # ─── Incremental review (all optional: None / [] keeps a double working,
    # and every failure means "review the whole PR") ─────────────────────

    def list_pr_commits(self, repo: str, pr_number: int) -> list[CommitInfo] | None:
        """The commits of the PR with their parents, or None when this provider
        cannot say (or the list was cut short: a truncated list proves nothing
        about ancestry)."""
        return None

    def fetch_incremental_diff(
        self, repo: str, pr_number: int, base_sha: str, head_sha: str,
    ) -> str | None:
        """The unified diff between two commits of the PR, or None when it
        could not be read (an empty string is a real, empty answer)."""
        return None

    def our_inline_threads(self, pr: PullRequest, marker: str) -> list[OurThread] | None:
        """The inline comment threads of OURS on the PR (marker AND author),
        or None when they could not be listed."""
        return None

    def resolve_threads(self, pr: PullRequest, threads: list[OurThread]) -> dict[str, int]:
        """Mark threads resolved. Returns {"resolved", "failed", "unsupported"}
        counts. Never raises: an outdated thread left open is harmless."""
        return {"resolved": 0, "failed": 0, "unsupported": len(threads)}

    # ─── Conversation: what the comment commands need ───────────
    #
    # Non-abstract on purpose: a provider that has not implemented them keeps
    # every other behaviour, and the command receiver answers "not supported"
    # instead of crashing. Each reply carries the chat marker (never the review
    # marker, so a push does not clean it up).

    def viewer_ids(self) -> frozenset[str]:
        """The stable ids this token posts as; empty when unknown."""
        return frozenset()

    def post_reply(self, ev, body: str) -> str | None:
        """Answer the comment `ev` (a `CommentEvent`) in its own thread; the new
        comment's id. Raises PullRequestProviderError when the write fails."""
        raise PullRequestProviderError(f"{self.name or 'this provider'} cannot reply to comments")

    def update_comment(
        self, repo: str, pr_number: int, comment_id: str, body: str, *, kind: str = "issue",
    ) -> bool:
        """Rewrite a comment of ours (the "working on it" note, once it is done)."""
        return False

    def get_thread(self, ev, limit: int = 30) -> list[ThreadMessage]:
        """The conversation `ev` belongs to, oldest first, root first, `ev`'s own
        comment included; at most `limit` messages (the root and the latest).
        Empty when the provider cannot tell or the read fails: a chat answer
        without its thread is worse than one with it, never an error."""
        return []

    def acknowledge(self, ev) -> bool:
        """React to the comment `ev` so its author sees it was received. False
        when the provider has no reactions (the caller replies instead)."""
        return False

    def actor_permission(
        self, repo: str, *, actor_id: str = "", actor_name: str = "",
    ) -> str:
        """`write`, `read`, `none` — or `unknown` when the provider cannot say."""
        return "unknown"

    def pr_participants(self, repo: str, pr_number: int) -> frozenset[str]:
        """Ids and handles of the PR's author, reviewers and assignees."""
        return frozenset()

    #: The id of the lifecycle comment this provider instance posted or
    #: adopted for the current review — "🔄 reviewing…" first, the final
    #: summary later. `post_review` rewrites THIS comment instead of picking
    #: one by age, so the placeholder and the summary are one comment even
    #: when `replace_on_synchronize` is off and nothing else is upserted.
    _status_comment_id: int | None = None

    def upsert_status_comment(
        self, pr: PullRequest, body: str, *, create: bool = True,
        only_if_in_progress: bool = False,
    ) -> int | None:
        """Write `body` into the persistent, marked summary comment.

        The same comment `post_review` later fills with the final summary:
        the one this instance already wrote, else (when re-runs replace) the
        oldest marked top-level comment of ours, else — only when `create` —
        a new one. `create=False` is for the skip and failure paths, which
        must rewrite a placeholder that exists and never start a thread on a
        pull request nobody is reviewing.

        `only_if_in_progress` is the skip path's guard: the comment is
        rewritten only when it is this instance's own placeholder or an
        existing comment of ours that is STILL a placeholder (carries
        `STATUS_IN_PROGRESS_MARK`, e.g. left by a killed run). A finished
        summary of an earlier commit is never overwritten by a skip.

        Returns the comment id, or None when nothing was written. The default
        writes nothing, so a provider (or a test double) that has not
        implemented it keeps today's behaviour. Implementations may raise;
        the orchestrator treats every failure here as non-fatal.
        """
        return None

    def _our_summary_comments(
        self, pr: PullRequest, marker: str,
    ) -> list[tuple[int, str]]:
        """(id, body) of every top-level marked comment of OURS, oldest first.

        Authorship as in `find_marked_comment_ids`. Defaults to nothing.
        """
        return []

    def _write_top_level_comment(
        self, pr: PullRequest, body: str, existing_id: int | None,
    ) -> int | None:
        """Update `existing_id` or post a new top-level comment. Default: nothing."""
        return None

    def _comment_marker(self) -> str:
        """The review marker, read the way this provider's module reads it."""
        from src.review.settings import get_review_settings

        return get_review_settings().comment_marker

    def upsert_feedback_comment(
        self, pr: PullRequest, reason: str, language: str | None = None,
    ) -> int | None:
        """The brief "not reviewed: <reason>" note, written at most once per PR.

        For a review skipped or blocked before any placeholder existed, when
        the repository's `status_feedback` is on. It carries the review marker
        AND `STATUS_FEEDBACK_MARK`: a later skip finds it by the second and
        rewrites it in place (never a second note), and a later real review
        adopts or cleans it up like any other marked comment of ours. Only a
        note of ours that IS a feedback note is ever rewritten here — a
        finished summary of an earlier commit is not touched.
        """
        marker = self._comment_marker()
        existing = [
            cid for cid, text in self._our_summary_comments(pr, marker)
            if has_marker(text, STATUS_FEEDBACK_MARK)
        ]
        return self._write_top_level_comment(
            pr, _format_feedback_comment(
                pr, reason=reason, marker=marker,
                language=language or self.review_language),
            existing[-1] if existing else None,
        )

    def update_description(self, pr: PullRequest, transform) -> dict:
        """Rewrite the pull request's description through `transform`.

        `transform(current) -> str | None` gets the description as the
        provider holds it NOW (re-read, so an edit made while the review ran
        is not overwritten) and returns the new text, or None to leave it.
        Returns {"written": bool, "error": str | None}. Default: unsupported.
        """
        return {"written": False, "error": "not supported by this provider"}

    def fetch_commit_messages(self, pr: PullRequest, limit: int = 50) -> list[str]:
        """The full messages of the pull request's commits, newest first, at
        most `limit`. Where teams also write the task key ("PROJ-6066 fix
        cutting") when the title does not. Never raises; [] when the provider
        cannot say."""
        return []

    def _status_target(
        self, pr: PullRequest, marker: str, *, replace: bool, create: bool,
        only_if_in_progress: bool,
    ) -> tuple[bool, int | None]:
        """(write?, id to update or None for a new comment) — shared by all
        three `upsert_status_comment` implementations."""
        if self._status_comment_id is not None:
            return True, self._status_comment_id
        if only_if_in_progress:
            # Listed regardless of `replace_on_synchronize`: a placeholder a
            # killed run left behind is stale in either mode. The newest one
            # wins — with history kept, older finished summaries are records.
            for cid, text in reversed(self._our_summary_comments(pr, marker)):
                if has_marker(text, STATUS_IN_PROGRESS_MARK):
                    return True, cid
            return False, None
        existing = (
            self.find_existing_review_comment(pr.repo, pr.number, marker)
            if replace else None
        )
        if existing is None and not create:
            return False, None
        return True, existing


def _with_marker(body: str, marker: str) -> str:
    """`body` carrying `marker` on its first line — added only when missing.

    The marker is what every later run finds the persistent comment by, so a
    lifecycle body without it would be a comment no re-run can ever replace.
    """
    if not marker or marker in body:
        return body
    return f"{marker}\n{body}"


def _count(value: object) -> int | None:
    """A provider's "how many files" as a non-negative int, None when it is
    missing or not a number (GitLab sends `changes_count` as a string, and
    "1000+" for a huge MR — that stays unknown, never a wrong small number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# ─── Factory ─────────────────────────────────────────────────────


def get_provider_for(
    provider_name: str, *, user_id: str = "default", workspace_id: str = "default",
) -> PullRequestProvider:
    """Return PR provider instance by name.

    Args:
        provider_name: 'github' | 'gitlab' | 'bitbucket'
        user_id: legacy credential owner (default transition tenant only).
        workspace_id: the tenant whose git token to use. The provider resolves
                 strictly this workspace's `ws:{id}` slot (see resolve_git_credential),
                 so a review can never post/clone under another tenant's PAT.

    Raises ValueError if the provider is unknown.
    """
    name = provider_name.lower().strip()
    if name == "github":
        from src.review.providers.github import GitHubPRProvider
        return GitHubPRProvider(user_id=user_id, workspace_id=workspace_id)
    if name == "gitlab":
        from src.review.providers.gitlab import GitLabPRProvider
        return GitLabPRProvider(user_id=user_id, workspace_id=workspace_id)
    if name == "bitbucket":
        from src.review.providers.bitbucket import BitbucketPRProvider
        return BitbucketPRProvider(user_id=user_id, workspace_id=workspace_id)
    raise ValueError(
        f"Unknown PR provider '{provider_name}'. "
        f"Allowed: github | gitlab | bitbucket"
    )


# ─── Severity → emoji helper (cross-provider) ────────────────────


_SEVERITY_EMOJI = {
    "critical": "🔴",
    "error": "🟠",
    "warning": "🟡",
    "info": "💡",
}


def _severity_emoji(severity: str) -> str:
    return _SEVERITY_EMOJI.get(severity.lower(), "•")


#: How a provider renders `Finding.suggested_code` when it may be committed.
SUGGESTION_GITHUB = "github"
SUGGESTION_GITLAB = "gitlab"


def _fence_for(*texts: str) -> str:
    """A backtick fence longer than any run of backticks inside `texts`."""
    import re

    longest = max((len(m) for t in texts for m in re.findall(r"`+", t or "")), default=0)
    return "`" * max(3, longest + 1)


def _suggestion_parts(
    finding: Finding, *, committable: str | None = None,
    original: list[str] | None = None,
) -> list[str]:
    """The fix part of an inline comment.

    `committable` is set by the provider only when the repository switched
    `committable_suggestions` on AND the anchor is exact (see
    `_committable_span`): GitHub then gets a ```suggestion block, GitLab a
    ```suggestion:-0+N block whose N is the number of lines BELOW the anchored
    one that the replacement also covers. Anything else — a hint, code on a
    provider or anchor that cannot take a one-click commit, Bitbucket — is a
    plain block a human reads: a ```diff when the replaced lines are known,
    plain code otherwise. A hint is never rendered as a committable block:
    structural rules ship prose like "=== / !==", and committing that is a
    broken file.
    """
    parts: list[str] = []
    code = finding.suggested_code
    hint = finding.suggestion
    if code is not None:
        start = finding.line
        end = max(start, int(finding.suggested_end_line or start))
        fence = _fence_for(code, *(original or []))
        parts.append("")
        if committable == SUGGESTION_GITHUB:
            parts += [f"{fence}suggestion", code, fence]
        elif committable == SUGGESTION_GITLAB:
            parts += [f"{fence}suggestion:-0+{end - start}", code, fence]
        elif original:
            parts.append("**Suggested change:**")
            parts.append("")
            parts.append(f"{fence}diff")
            parts += [f"-{ln}" for ln in original]
            parts += [f"+{ln}" for ln in code.split("\n")]
            parts.append(fence)
        else:
            parts.append("**Suggested change:**")
            parts.append("")
            parts += [fence, code, fence]
    if hint and hint.strip() != (code or "").strip():
        fence = _fence_for(hint)
        parts.append("")
        parts.append("**Suggestion:**")
        parts.append("")
        parts += [fence, hint, fence]
    return parts


def _new_side_text(pr: PullRequest) -> dict[tuple[str, int], str]:
    """(path, new-file line) → that line's text, for every line a hunk shows.

    Read off the hunk bodies: context and added lines exist in the new file,
    removed lines do not. Used to show what a suggested change replaces.
    """
    out: dict[tuple[str, int], str] = {}
    for hunk in pr.anchor_hunks:
        if not hunk.file_path or hunk.is_binary:
            continue
        n = hunk.new_start
        rows = (hunk.content or "").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        for raw in rows:
            if raw.startswith("@@") or raw.startswith("\\") or raw.startswith("-"):
                continue
            # "" is a blank context line whose leading space was trimmed.
            out[(hunk.file_path, n)] = raw[1:]
            n += 1
    return out


def _original_lines(
    pr_lines: dict[tuple[str, int], str], finding: Finding,
) -> list[str] | None:
    """The new-file lines a suggested replacement covers, or None if unseen."""
    if finding.suggested_code is None:
        return None
    end = max(finding.line, int(finding.suggested_end_line or finding.line))
    found = [pr_lines.get((finding.file_path, n)) for n in range(finding.line, end + 1)]
    if any(t is None for t in found):
        return None
    return [t for t in found if t is not None]


def _committable_span(
    finding: Finding, anchored_line: int,
    ranges: dict[tuple[str, str], list[tuple[int, int]]],
) -> tuple[int, int] | None:
    """(start, end) when a suggestion can be a one-click commit, else None.

    Three conditions, all about not committing over the wrong lines: the
    finding carries `suggested_code`; the anchor was NOT moved by
    `_snap_to_span` (a moved anchor would apply the replacement to the line
    it was moved to); and the whole range sits on the new side inside ONE
    span the diff carries, which is where GitHub and GitLab accept it.
    """
    if finding.suggested_code is None:
        return None
    if getattr(finding, "side", HunkSide.RIGHT) != HunkSide.RIGHT:
        return None
    start = finding.line
    end = max(start, int(finding.suggested_end_line or start))
    if anchored_line != start:
        return None
    for lo, hi in ranges.get((finding.file_path, "RIGHT"), []):
        if lo <= start and end <= hi:
            return start, end
    return None


def _committable_enabled(batch: ReviewBatch) -> bool:
    actions = getattr(batch, "pr_actions", None)
    return bool(getattr(actions, "committable_suggestions", False))


def _format_finding_body(
    finding: Finding, marker: str = "", *, committable: str | None = None,
    original: list[str] | None = None, head_sha: str | None = None,
) -> str:
    """Markdown body for an inline comment — universal cross-provider.

    `marker` is the same idempotency token the summary comment carries. Only
    the summary had one, so `replace_on_synchronize` deleted the summary and
    left every inline comment behind — and Bitbucket fires pullrequest:updated
    on every push, so a branch pushed five times collected five full sets of
    inline comments, up to max_inline_comments each.
    """
    parts: list[str] = []
    if getattr(finding, "reasoning", ""):
        # The FIRST line, before the title: the evidence the agent wrote
        # down before it wrote the claim. It went after the body at first
        # (e01531d), and the benchmark image that measured the verifier
        # predated even that — 0 of 77 posted comments carried a Why: line.
        # The judge reads the whole comment and matches on the issue it
        # names, and a human decides in the first line whether to read the
        # second; both get the derivation before the conclusion.
        parts.append(f"*Why:* {finding.reasoning}")
        parts.append("")
    emoji = _severity_emoji(finding.severity.value)
    if finding.title:
        parts.append(f"**{emoji} {finding.title}**")
    else:
        parts.append(f"**{emoji} {finding.severity.value.upper()}**")
    parts.append("")
    parts.append(finding.body or "(no details)")

    parts.extend(_suggestion_parts(finding, committable=committable, original=original))

    # The footer always renders: confidence is the agent's own number and
    # belongs where telemetry belongs — visible, last, and never the thing
    # the reader was asked to weigh the finding by.
    meta = []
    if getattr(finding, "rule", ""):
        meta.append(f"review rule: **{finding.rule}**")
    if finding.agent:
        meta.append(f"agent: `{finding.agent}`")
    if finding.rule_id:
        meta.append(f"rule: `{finding.rule_id}`")
    meta.append(f"confidence: {finding.confidence:.2f}")
    parts.append("")
    parts.append(f"<sub>{' · '.join(meta)}</sub>")

    if marker:
        # Invisible in rendered markdown on all three providers. It marks the
        # SHAPE of the comment, not its authorship: "Quote reply" copies the raw
        # markdown, marker and all, so a human arguing with a finding ends up
        # holding one too. The cleanup therefore pairs this with the posting
        # account (see `_is_ours` in the github/gitlab providers) — a substring
        # match on its own would delete that human's words.
        parts.append("")
        # The finding's own marker: its fingerprint and the commit it was
        # posted on. A later incremental run reads it to tell that this
        # finding is already on the PR (and the learning loop to link a reply
        # back to it). Inside the same marked body, never alone: the
        # authorship check of the marker below still applies to it.
        own = finding_marker_for(finding, head_sha)
        if own:
            parts.append(own)
        parts.append(marker)

    return "\n".join(parts)


_VERDICT_EMOJI = {
    ReviewVerdict.APPROVE: "✅",
    ReviewVerdict.COMMENT: "💬",
    ReviewVerdict.REQUEST_CHANGES: "❌",
    ReviewVerdict.SKIPPED: "⏭️",
}

_VERDICT_KEY = {
    ReviewVerdict.APPROVE: "completed.verdict.approve",
    ReviewVerdict.COMMENT: "completed.verdict.comment",
    ReviewVerdict.REQUEST_CHANGES: "completed.verdict.request_changes",
    # SKIPPED used to fall through both .get() defaults and render as
    # "💬 " — a bare speech bubble above "_No issues detected._" on a PR
    # nothing had reviewed. The banner in the summary carries the why; this
    # line only has to stop impersonating a verdict about the code.
    ReviewVerdict.SKIPPED: "completed.verdict.skipped",
}


def _verdict_line(batch: ReviewBatch, language: str | None = None) -> str:
    """One verdict line, rendered once for every surface that shows it.

    Both the persistent summary comment and GitHub's immutable review body
    print this line. It is a single function because the two used to be the
    same full text and are now two renderings of one review — if the wordings
    could drift, a PR timeline could call a review APPROVED while the summary
    it points at says otherwise.
    """
    emoji = _VERDICT_EMOJI.get(batch.verdict, "💬")
    key = _VERDICT_KEY.get(batch.verdict)
    return f"{emoji} {messages.t(key, language) if key else ''}"


def _format_review_pointer(batch: ReviewBatch, summary_url: str | None = None) -> str:
    """The body of a SUBMITTED review — a pointer to the summary, never the summary.

    A submitted GitHub review is immutable: the delete endpoint covers PENDING
    reviews only, and a dismissal leaves the body on the page. While the review
    body carried the full summary, every re-run therefore stacked one more full
    copy onto the PR timeline — the one duplication no cleanup can reach. The
    full summary now lives ONLY in the persistent comment, which IS updated in
    place; the review body keeps just the verdict (so the timeline stays
    readable at a glance) and one sentence saying where the rest is.

    Trade-off, accepted by the product owner: GitHub's notification email
    quotes the review body, so the email now carries a pointer instead of the
    content. Self-hosted installs rarely have SMTP configured, so the email
    was mostly never sent anyway.

    `summary_url` is the anchor link to the persistent comment when its id is
    already known — every re-run, because the upsert rewrites the oldest
    comment in place and its id is stable. On the FIRST run the comment does
    not exist until after the review is submitted (a review that fails to post
    must not update the summary first), so the sentence points without a link;
    the comment lands right below the review in the timeline.
    """
    where = (
        f"[the review summary]({summary_url})"
        if summary_url
        else "the review summary comment on this pull request"
    )
    return (
        f"{_verdict_line(batch, batch.review_language)}\n\n"
        f"Full findings and scope are in {where} — one persistent comment, "
        f"updated in place on every run."
    )


_THRESHOLD_KEY = {
    "critical": "threshold.critical",
    "error": "threshold.error",
    "warning": "threshold.warning",
}


def _posting_line(batch: ReviewBatch, language: str | None = None) -> str:
    """How many of the counted findings became inline comments, and why not
    the rest.

    The severity counts above are ALL findings. Once a repo raises its comment
    threshold, or a review crosses the inline cap, fewer comments arrive than
    the counts promise — and a reader who counts the threads and gets a
    different number concludes the tool lost some. So the gap is said, with
    its cause: "3 shown inline · 7 below the warning threshold (kept in
    Celmis)". Empty when every finding was posted.
    """
    from src.review.settings import get_review_settings

    total = len(batch.findings)
    below = batch.below_threshold_count
    postable = total - below
    cap = batch.inline_cap(int(get_review_settings().max_inline_comments))
    # Counted by the method the providers post with, so findings a review
    # rule exempts from the cap (`ReviewBatch.bypasses_filters`) are counted
    # as shown rather than as "over the limit".
    shown = len(batch.inline_findings(cap))
    over_cap = postable - shown
    if not below and not over_cap:
        return ""
    # "up to": this text is composed before the comments are sent, and a
    # provider can still refuse some of them one by one (GitLab, Bitbucket
    # post per finding). The number is what was SELECTED, said as such.
    parts = [messages.t("completed.posting_shown", language, n=shown)]
    if below:
        label = messages.t(
            _THRESHOLD_KEY.get(str(batch.comment_min_severity or "").lower(),
                               "threshold.fallback"), language)
        parts.append(messages.t("completed.posting_below", language, n=below, label=label))
    if over_cap:
        parts.append(messages.t("completed.posting_over", language, n=over_cap, cap=cap))
    return "_" + " · ".join(parts) + "_"


def _files(n: int, language: str | None = None) -> str:
    if messages.normalise_language(language) == "uk":
        return {"one": "файл", "few": "файли"}.get(messages.plural_form(n, "uk"), "файлів")
    return "file" if n == 1 else "files"


def _format_summary(batch: ReviewBatch, marker: str) -> str:
    """Top-level summary comment markdown — universal for all 3 providers.

    Two shapes. The compact one is what every comment looked like before the
    lifecycle work and stays the default for a hand-built batch. The rich one
    (`batch.rich_summary`, switched on by the orchestrator from the repo
    policy's `summary_enabled`) is Kodus-shaped: a natural-language summary,
    a per-file walkthrough table, findings by severity and by source with the
    top ones listed, and the scope/telemetry folded into <details>.
    """
    actions = getattr(batch, "pr_actions", None)
    if getattr(actions, "completed_comment", "classic") == "completed":
        return _format_completed_comment(batch, marker)
    if getattr(batch, "rich_summary", False):
        return _format_rich_summary(batch, marker)
    lines: list[str] = []
    lines.append(marker)
    lines.append(_summary_header(batch))
    lines.append("")

    # The gap notice, before anything else can be read on its own. This
    # function composes the posted comment from findings, scope and telemetry
    # and for one whole wave read neither `summary` nor the banner — so a
    # review in which nothing ran told the run row and the notification
    # "FAILED" while showing the pull-request author "💬 _No issues
    # detected._". It reads `partial_banner` (the property), not `summary`:
    # the early-skip paths write free prose into `summary` that belongs to
    # the run row, and the claude_code engine writes its own error there.
    # The property derives from the same `agents_run`/`agents_failed` state
    # the row persists, so the comment and the row cannot name different gaps.
    banner = batch.partial_banner
    if banner:
        lines.append(banner.strip())
        lines.append("")

    lines.append(_verdict_line(batch, batch.review_language))
    lines.append("")

    # Severity summary
    if batch.findings:
        lines.append("### Findings")
        lines.append("")
        lines.extend(_severity_count_lines(batch))
        lines.append("")
        posting = _posting_line(batch, batch.review_language)
        if posting:
            lines.append(posting)
            lines.append("")
    elif batch.agents_run:
        # "_No issues detected._" is a claim that something looked and found
        # the code clean. It used to print unconditionally on zero findings,
        # which put it under the header of reviews in which every agent
        # failed — or none was ever dispatched. Gated on `agents_run` so it
        # is unreachable when nothing ran; the banner above explains those
        # runs instead.
        lines.append("_No issues detected._")
        lines.append("")

    lines.extend(_section_lines(batch, "comment"))

    # PR scope
    lines.append("### Scope")
    lines.extend(_scope_lines(batch))
    lines.append("")

    # Telemetry
    perf = _performance_line(batch)
    if perf:
        lines.append("### Performance")
        lines.append(perf)
        lines.append("")

    lines.append("---")
    lines.append(f"<sub>Powered by Code Analyzer · {_provenance(batch)}</sub>")
    return "\n".join(lines)


def _summary_header(batch: ReviewBatch) -> str:
    """The summary's first visible line: the repository's own
    `message_finished_header` when it set one, else the built-in heading."""
    from src.review.pr_actions import (
        HEADER_TEMPLATE_MAX_CHARS,
        render_template,
        template_values,
    )

    pr = batch.pull_request
    actions = getattr(batch, "pr_actions", None)
    custom = render_template(
        getattr(actions, "message_finished_header", None),
        template_values(pr, list(batch.agents_run)),
        limit=HEADER_TEMPLATE_MAX_CHARS,
    )
    return custom or f"## 🤖 Code Review for PR #{pr.number}"


def _severity_count_lines(batch: ReviewBatch) -> list[str]:
    lines: list[str] = []
    if batch.critical_count:
        lines.append(f"- 🔴 **Critical:** {batch.critical_count}")
    if batch.error_count:
        lines.append(f"- 🟠 **Error:** {batch.error_count}")
    if batch.warning_count:
        lines.append(f"- 🟡 **Warning:** {batch.warning_count}")
    if batch.info_count:
        lines.append(f"- 💡 **Info:** {batch.info_count}")
    return lines


def _scope_lines(batch: ReviewBatch, language: str | None = None) -> list[str]:
    pr = batch.pull_request
    lines = [
        messages.t("scope.files", language, n=len(pr.changed_files)),
        messages.t("scope.lines", language,
                   added=pr.total_added_lines, removed=pr.total_removed_lines),
    ]
    if batch.cross_repo_callers:
        lines.append(messages.t("scope.callers", language, n=batch.cross_repo_callers))
    if batch.skipped_files:
        # Two causes with two owners: the install's skip lists and size limit,
        # and this repository's own ignore globs (tagged by the orchestrator).
        by_glob = sum(1 for p in batch.skipped_files
                      if str(p).endswith(" (ignore glob)"))
        other = len(batch.skipped_files) - by_glob
        if other:
            lines.append(messages.t("scope.skipped", language, n=other,
                                    files=_files(other, language)))
        if by_glob:
            lines.append(messages.t("scope.ignored", language, n=by_glob,
                                    files=_files(by_glob, language)))
    # An agent that had nothing to check — said here, folded away, rather
    # than in the banner: it is not a gap in the review, and the author who
    # wonders why the business-logic check said nothing finds the answer.
    for agent, why in (getattr(batch, "skip_reasons", None) or {}).items():
        lines.append(messages.t("scope.not_run", language, agent=agent,
                                why=_md_cell(why, 200)))
    return lines


def _performance_line(batch: ReviewBatch, language: str | None = None) -> str:
    if not batch.elapsed_seconds:
        return ""
    parts = [
        messages.t("perf.time", language, seconds=f"{batch.elapsed_seconds:.1f}"),
        # "agents: none" and not "agents: " — this line is reachable for
        # skipped/failed runs now that they post a real comment.
        messages.t("perf.agents", language,
                   agents=", ".join(batch.agents_run) or messages.t("perf.none", language)),
    ]
    # Only when there are any. The Claude Code engine bills by
    # subscription and never populates these, so every review it produced
    # printed "tokens: 0/0" beside a real $0.21 — a number that reads as a
    # measurement and is an absent field. A missing line is honest; a zero
    # is not.
    if batch.tokens_in or batch.tokens_out:
        parts.append(messages.t("perf.tokens", language,
                                tin=f"{batch.tokens_in:,}", tout=f"{batch.tokens_out:,}"))
    return "- " + " · ".join(parts)


# ─── The Kodus-style summary ─────────────────────────────────────────

#: Rows in the walkthrough table before the rest collapse into "+N more".
WALKTHROUGH_MAX_FILES = 30
#: Findings listed by title under the counts; the inline comments carry the rest.
TOP_FINDINGS_MAX = 10

_SEVERITY_LABEL = {
    "critical": "Critical", "error": "Error", "warning": "Warning", "info": "Info",
}


def _md_cell(text: str, limit: int = 200) -> str:
    """One table cell / one line: whitespace collapsed, pipes escaped, capped."""
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat.replace("|", "\\|")


def _file_stats(pr: PullRequest) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for h in pr.hunks:
        added, removed = stats.get(h.file_path, (0, 0))
        stats[h.file_path] = (added + h.added_lines, removed + h.removed_lines)
    return stats


def _blob_link(pr: PullRequest, path: str, line: int) -> str | None:
    """A link to `path` at `line` in the head commit, when the URL shape is known.

    Built from the pull request's own web URL, so it never points at a host the
    review did not come from: GitHub `/blob/<sha>/<path>#L<n>`, GitLab
    `/-/blob/<sha>/<path>#L<n>`, Bitbucket `/src/<sha>/<path>#lines-<n>`.
    None when the URL or the head sha is missing — the caller prints plain
    text, which is the "where the provider supports it" half of the rule.
    """
    url = (pr.url or "").strip()
    sha = (pr.head_sha or "").strip()
    if not url or not sha or not path or line <= 0:
        return None
    from urllib.parse import quote

    safe_path = quote(path, safe="/")
    if pr.provider == "github" and "/pull/" in url:
        base = url.split("/pull/", 1)[0]
        return f"{base}/blob/{sha}/{safe_path}#L{line}"
    if pr.provider == "gitlab" and "/-/merge_requests/" in url:
        base = url.split("/-/merge_requests/", 1)[0]
        return f"{base}/-/blob/{sha}/{safe_path}#L{line}"
    if pr.provider == "bitbucket" and "/pull-requests/" in url:
        base = url.split("/pull-requests/", 1)[0]
        return f"{base}/src/{sha}/{safe_path}#lines-{line}"
    return None


def _finding_location(pr: PullRequest, finding: Finding) -> str:
    # A path comes from the diff; a backtick or angle bracket in it could break
    # out of the code span or forge a marker comment.
    safe_path = re.sub(r"[`<>]", "_", str(finding.file_path))
    label = f"`{safe_path}:{finding.line}`"
    # A LEFT-side finding sits on a line that no longer exists in the head
    # commit, so a link to the head blob would land on the wrong code.
    if getattr(finding, "side", HunkSide.RIGHT) != HunkSide.RIGHT:
        return label
    link = _blob_link(pr, finding.file_path, finding.line)
    return f"[{label}]({link})" if link else label


def _category_of(finding: Finding) -> str:
    """The category a reader sees — Bug, Security, Performance, Business
    logic, … — from the one agent→category map (`src.review.categories`)."""
    from src.review.categories import finding_category

    return finding_category(finding)


def _walkthrough_lines(batch: ReviewBatch, language: str | None = None) -> list[str]:
    pr = batch.pull_request
    walkthrough = getattr(batch, "walkthrough", None) or {}
    if not walkthrough:
        return []
    stats = _file_stats(pr)
    files = pr.changed_files
    shown = files[:WALKTHROUGH_MAX_FILES]
    lines = [
        messages.t("walk.title", language),
        "",
        f"| {messages.t('walk.file', language)} | +/- | {messages.t('walk.change', language)} |",
        "|---|---|---|",
    ]
    for path in shown:
        added, removed = stats.get(path, (0, 0))
        lines.append(
            f"| `{_md_cell(path, 120)}` | +{added} / -{removed} | "
            f"{_md_cell(walkthrough.get(path) or '—')} |"
        )
    more = len(files) - len(shown)
    if more > 0:
        lines.append(f"| {messages.tn('walk.more', more, language, count=more)} | | |")
    lines.append("")
    return lines


def _rich_findings_lines(batch: ReviewBatch) -> list[str]:
    pr = batch.pull_request
    lines = ["### Findings", ""]
    if not batch.findings:
        if batch.agents_run:
            lines.append("_No issues detected._")
            lines.append("")
            return lines
        # Same rule as the compact form: "no issues" is a claim that
        # something looked. Nothing did, and the banner above says why.
        return []

    sev = [
        f"{_severity_emoji(level)} {_SEVERITY_LABEL[level]}: **{count}**"
        for level, count in (
            ("critical", batch.critical_count), ("error", batch.error_count),
            ("warning", batch.warning_count), ("info", batch.info_count),
        )
        if count
    ]
    lines.append("**By severity:** " + " · ".join(sev))
    by_cat: dict[str, int] = {}
    for f in batch.findings:
        cat = _category_of(f)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    lines.append("")
    lines.append("**By category:** " + " · ".join(
        f"{cat}: **{n}**"
        for cat, n in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
    ))
    lines.append("")

    postable = batch.postable_findings
    top = postable[:TOP_FINDINGS_MAX]
    if top:
        lines.append("**Top findings:**")
        lines.append("")
        for i, f in enumerate(top, 1):
            title = _md_cell(f.title or f.severity.value.upper(), 160)
            lines.append(
                f"{i}. {_severity_emoji(f.severity.value)} **{title}** — "
                f"{_finding_location(pr, f)}"
            )
        rest = len(postable) - len(top)
        if rest > 0:
            lines.append("")
            lines.append(f"_…and {rest} more in the inline comments._")
        lines.append("")
    posting = _posting_line(batch, batch.review_language)
    if posting:
        lines.append(posting)
        lines.append("")
    return lines


def _section_lines(batch: ReviewBatch, target: str) -> list[str]:
    """The `batch.summary_sections` shown on `target`, each followed by a blank line."""
    lines: list[str] = []
    for section in batch.sections_for(target):
        lines.append(section.markdown)
        lines.append("")
    return lines


def _category_label(category: str, language: str | None) -> str:
    key = f"category.{category}"
    return messages.t(key, language) if key in messages.EN else category


def _severity_breakdown(batch: ReviewBatch, language: str | None) -> str:
    """'🔴 Critical 1 · 🟠 Error 2' — only the levels that have findings."""
    return " · ".join(
        f"{_severity_emoji(level)} {messages.t(f'severity.{level}', language)} {count}"
        for level, count in (
            ("critical", batch.critical_count), ("error", batch.error_count),
            ("warning", batch.warning_count), ("info", batch.info_count),
        )
        if count
    )


def _category_items(batch: ReviewBatch, language: str | None) -> str:
    by_cat: dict[str, int] = {}
    for f in batch.findings:
        cat = _category_of(f)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return " · ".join(
        f"{_category_label(cat, language)} **{n}**"
        for cat, n in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
    )


#: Findings listed by title in the completed comment and the description.
COMPLETED_TOP_FINDINGS = 5


def _top_findings_lines(batch: ReviewBatch, language: str | None) -> list[str]:
    pr = batch.pull_request
    postable = batch.postable_findings
    top = postable[:COMPLETED_TOP_FINDINGS]
    if not top:
        return []
    lines: list[str] = []
    for i, f in enumerate(top, 1):
        # The title is model output written after reading the author's diff:
        # no marker comment, link, image or @-mention may ride in with it.
        title = _md_cell(
            sanitize_prose(f.title or f.severity.value.upper(), 160), 160)
        lines.append(
            f"{i}. {_severity_emoji(f.severity.value)} **{title}** — "
            f"{_finding_location(pr, f)}"
        )
    rest = len(postable) - len(top)
    if rest > 0:
        lines.append("")
        lines.append(messages.t("completed.more", language, n=rest))
    return lines


def _completed_header(batch: ReviewBatch, language: str | None) -> str:
    """The repository's own `message_finished_header` when it set one, else
    the "Code Review Completed" heading."""
    from src.review.pr_actions import (
        HEADER_TEMPLATE_MAX_CHARS,
        render_template,
        template_values,
    )

    actions = getattr(batch, "pr_actions", None)
    custom = render_template(
        getattr(actions, "message_finished_header", None),
        template_values(batch.pull_request, list(batch.agents_run)),
        limit=HEADER_TEMPLATE_MAX_CHARS,
    )
    return custom or messages.t("completed.title", language)


def _commands_guide_lines(language: str | None, off: tuple[str, ...] = ()) -> list[str]:
    """What the bot can be told in a comment — only the commands this
    installation can run and this repository has not switched off. Shown under
    `commands_guide_enabled`."""
    from src.review.commands.handlers import available_commands
    from src.review.commands.parser import guide_lines
    from src.review.settings import get_review_settings

    return guide_lines(
        get_review_settings().bot_handle, language,
        available=[name for name in available_commands() if name not in off],
    )


def _settings_link(batch: ReviewBatch, language: str | None) -> str:
    """A link to this repository's review settings; "" when the install has
    no usable public address (the link would point nowhere)."""
    from urllib.parse import quote

    try:
        from src.review.webhook_install import public_base_url

        base, _problem = public_base_url()
    except Exception:  # noqa: BLE001 — a link never fails a summary
        return ""
    if not base:
        return ""
    slug = quote(batch.pull_request.local_slug or "", safe="")
    url = f"{base}/review-settings" + (f"?repo={slug}" if slug else "")
    return messages.t("completed.settings", language, url=url)


def _format_completed_comment(batch: ReviewBatch, marker: str) -> str:
    """The "Code Review Completed" comment (`completed_comment="completed"`).

    The same persistent comment the classic summary is, rewritten in place, so
    there is no second thread and nothing for the review to answer to. What it
    leads with is the answer: the verdict, how many findings and of what kind,
    the first few by name. Then the change summary when it is not already in
    the description, the blocks other stages added (`batch.summary_sections`),
    the scope in one line, the technical details folded away, the commands
    (when they exist) and a link to this repository's settings.
    """
    from src.review.models import ReviewRunStatus

    language = batch.review_language
    pr = batch.pull_request
    lines: list[str] = [marker, _completed_header(batch, language), ""]

    if batch.run_status == ReviewRunStatus.FAILED:
        lines.append(_failure_headline(batch, language))
        lines.append("")
    banner = batch.partial_banner
    if banner:
        lines.append(banner.strip())
        lines.append("")
    lines.append(_verdict_line(batch, language))
    lines.append("")

    if batch.findings:
        lines.append(messages.t(
            "completed.found", language, n=len(batch.findings),
            breakdown=_severity_breakdown(batch, language)))
        lines.append("")
        lines.append(messages.t(
            "completed.by_category", language, items=_category_items(batch, language)))
        lines.append("")
        top = _top_findings_lines(batch, language)
        if top:
            lines.append(messages.t("completed.top", language))
            lines.append("")
            lines.extend(top)
            lines.append("")
        posting = _posting_line(batch, language)
        if posting:
            lines.append(posting)
            lines.append("")
    elif batch.agents_run:
        # Same rule as the classic form: "no issues" says something looked.
        lines.append(messages.t("completed.clean", language))
        lines.append("")

    if getattr(batch, "summary_in_description", False):
        lines.append(messages.t("completed.in_description", language))
        lines.append("")
    else:
        overview = (getattr(batch, "pr_overview", "") or "").strip()
        if overview:
            lines.append(messages.t("completed.summary", language))
            lines.append("")
            lines.append(overview)
            lines.append("")
        lines.extend(_walkthrough_lines(batch, language))
    lines.extend(_section_lines(batch, "comment"))

    lines.append(messages.t(
        "completed.scope", language, files=len(pr.changed_files),
        added=pr.total_added_lines, removed=pr.total_removed_lines,
        sha=(pr.head_sha or "")[:7] or "unknown"))
    lines.append("")
    details = [*_scope_lines(batch, language)]
    perf = _performance_line(batch, language)
    if perf:
        details += ["", perf]
    lines.append("<details>")
    lines.append(f"<summary>{messages.t('completed.details', language)}</summary>")
    lines.append("")
    lines.extend(details)
    lines.append("")
    lines.append("</details>")
    lines.append("")

    actions = getattr(batch, "pr_actions", None)
    if getattr(actions, "commands_guide_enabled", False):
        lines.extend(_commands_guide_lines(language, getattr(actions, "guide_commands_off", ())))
    link = _settings_link(batch, language)
    if link:
        lines.append(link)
        lines.append("")

    lines.append("---")
    powered = messages.t("completed.powered", language, provenance=_provenance(batch))
    lines.append(f"<sub>{powered}</sub>")
    return "\n".join(lines)


def _format_unanchored(
    findings: list[Finding], pr: PullRequest, language: str | None = None,
) -> str:
    """Findings the git provider would not attach to a line, as a list for the
    summary — the position kept as text, the explanation kept whole (cut at a
    sane length), so a refused comment costs the reader a click, not the
    finding. Empty when there is nothing to say."""
    if not findings:
        return ""
    lines = [messages.t("unanchored.title", language), "",
             messages.t("unanchored.intro", language), ""]
    for i, f in enumerate(findings, 1):
        # The title is model output written after reading the author's diff:
        # no marker comment, link, image or @-mention may ride in with it.
        title = _md_cell(
            sanitize_prose(f.title or f.severity.value.upper(), 160), 160)
        lines.append(
            f"{i}. {_severity_emoji(f.severity.value)} **{title}** — "
            f"{_finding_location(pr, f)}"
        )
        body = (f.body or "").strip()
        if len(body) > UNANCHORED_BODY_MAX:
            body = _close_open_fence(
                body[: UNANCHORED_BODY_MAX - 1].rstrip()) + "…"
        if body:
            lines.extend("   " + ln if ln.strip() else "" for ln in body.splitlines())
    return "\n".join(lines)


#: How much of one refused finding's explanation the summary keeps.
UNANCHORED_BODY_MAX = 1200


def _close_open_fence(text: str) -> str:
    """Close a code fence a cut left open, so the rest of the summary is not
    swallowed as code (and Bitbucket's flavouring still sees the real tags)."""
    fence = None
    for ln in text.splitlines():
        stripped = ln.strip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
        elif stripped.startswith(fence):
            fence = None
    return text if fence is None else f"{text}\n{fence}\n"


def _failure_headline(batch: ReviewBatch, language: str | None = None) -> str:
    """'❌ Review failed: <reason>' for a run in which no stage completed.

    The reason comes from `agent_errors`, which holds curated sentences only
    (see the orchestrator: never a provider's raw message), so nothing secret
    can reach the pull request through it.
    """
    reasons = sorted({r for a, r in batch.agent_errors.items()
                      if a in batch.agents_failed and r})
    if reasons:
        reason = "; ".join(reasons)
    elif batch.agents_failed:
        reason = f"no review stage completed ({', '.join(batch.agents_failed)})"
    else:
        reason = "no review stage completed"
    return messages.t("status.failed", language, reason=_md_cell(reason, 300))


def _format_rich_summary(batch: ReviewBatch, marker: str) -> str:
    from src.review.models import ReviewRunStatus

    lines: list[str] = [marker, _summary_header(batch), ""]

    if batch.run_status == ReviewRunStatus.FAILED:
        lines.append(_failure_headline(batch, batch.review_language))
        lines.append("")
    banner = batch.partial_banner
    if banner:
        lines.append(banner.strip())
        lines.append("")
    lines.append(_verdict_line(batch, batch.review_language))
    lines.append("")

    if getattr(batch, "summary_in_description", False):
        # The overview and the walkthrough are in the pull request's
        # description; this comment keeps the verdict and the findings.
        lines.append("_The change summary is in the pull request description._")
        lines.append("")
    else:
        overview = (getattr(batch, "pr_overview", "") or "").strip()
        if overview:
            lines.append("### Summary")
            lines.append("")
            lines.append(overview)
            lines.append("")
        lines.extend(_walkthrough_lines(batch, batch.review_language))
    lines.extend(_rich_findings_lines(batch))
    lines.extend(_section_lines(batch, "comment"))

    details = ["**Scope**", "", *_scope_lines(batch)]
    perf = _performance_line(batch)
    if perf:
        details += ["", "**Performance**", "", perf]
    lines.append("<details>")
    lines.append("<summary>Scope &amp; performance</summary>")
    lines.append("")
    lines.extend(details)
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append("---")
    lines.append(f"<sub>Powered by Code Analyzer · {_provenance(batch)}</sub>")
    return "\n".join(lines)


# ─── Lifecycle comments: started / skipped / failed ──────────────────

#: Present only in the placeholder, so a later reader (or a test) can tell
#: "still running" from a finished summary without parsing prose.
STATUS_IN_PROGRESS_MARK = "<!-- celmis:review-status:in-progress -->"


def _format_started_comment(
    pr: PullRequest, *, agents: list[str], started_at: str, marker: str = "",
    template: str | None = None, language: str | None = None,
    first_review: bool | None = None,
) -> str:
    """The "🔄 reviewing…" placeholder posted as soon as a review begins.

    `template` is the repository's `message_started`; when it renders to
    anything it replaces the built-in text. The in-progress mark stays either
    way — it is how a later skip or crash recognises the placeholder.

    `first_review` shapes the built-in text: True greets ("Hi! I'm Celmis…")
    on the PR's first review, False says the review is being updated for new
    commits, None (not known — a hand-built call, a database that could not
    answer) keeps the plain "reviewing" heading.
    """
    from src.review.pr_actions import (
        STARTED_TEMPLATE_MAX_CHARS,
        render_template,
        template_values,
    )

    lines = [marker] if marker else []
    custom = render_template(
        template, template_values(pr, agents), limit=STARTED_TEMPLATE_MAX_CHARS,
    )
    if custom:
        return "\n".join([*lines, STATUS_IN_PROGRESS_MARK, custom])
    sha = (pr.head_sha or "")[:7] or "unknown"
    files = len(pr.changed_files)
    roster = (", ".join(f"`{a}`" for a in agents) if agents
              else messages.t("started.none", language))
    if first_review is None:
        lines += [
            STATUS_IN_PROGRESS_MARK,
            messages.t("started.title", language),
            "",
            f"- {messages.t('started.commit', language)}: `{sha}`",
            f"- {messages.t('started.agents', language)}: {roster}",
            f"- {messages.t('started.files', language)}: **{files}**",
            f"- {messages.t('started.started', language)}: {started_at}",
            "",
            messages.t("started.footer", language),
        ]
        return "\n".join(lines)
    title = messages.t(
        "started.first_title" if first_review else "started.update_title",
        language, sha=sha,
        files=messages.tn("started.files_count", files, language, count=files),
    )
    lines += [
        STATUS_IN_PROGRESS_MARK,
        title,
        "",
        f"- {messages.t('started.agents', language)}: {roster}",
        f"- {messages.t('started.started', language)}: {started_at}",
        "",
        messages.t("started.footer", language),
    ]
    return "\n".join(lines)


def _format_status_comment(
    pr: PullRequest, *, outcome: str, reason: str, marker: str = "",
    language: str | None = None,
) -> str:
    """A terminal state that is not a review: `outcome` is 'skipped' or 'failed'."""
    sha = (pr.head_sha or "")[:7] or "unknown"
    if outcome == "skipped":
        head = messages.t("status.skipped", language, reason=_md_cell(reason, 400))
        tail = messages.t("status.tail_skipped", language)
    else:
        head = messages.t("status.failed", language, reason=_md_cell(reason, 300))
        tail = messages.t("status.tail_failed", language)
    lines = [marker] if marker else []
    lines += [
        messages.t("status.title", language, number=pr.number),
        "",
        head,
        "",
        f"- {messages.t('status.commit', language)}: `{sha}`",
        "",
        tail,
    ]
    return "\n".join(lines)


#: Carried (beside the review marker) by the short note a skipped or blocked
#: review leaves when `status_feedback` is on — the key a re-run finds the
#: note by, so a second skip rewrites it instead of adding another.
STATUS_FEEDBACK_MARK = "<!-- celmis:review-status:feedback -->"


def _format_feedback_comment(
    pr: PullRequest, *, reason: str, marker: str = "", language: str | None = None,
) -> str:
    """The brief note for a review that never started: why, and for which commit."""
    sha = (pr.head_sha or "")[:7] or "unknown"
    lines = [marker] if marker else []
    lines += [
        STATUS_FEEDBACK_MARK,
        messages.t("feedback.note", language, sha=sha, reason=_md_cell(reason, 400)),
    ]
    return "\n".join(lines)


def _provenance(batch: ReviewBatch) -> str:
    """What actually produced this review.

    The footer used to be a constant: "context: tree-sitter graph + cross-repo
    edges + Gemini 3 Pro/Flash". Every review carried it, including the ones
    run entirely by the Claude Code engine with `agents_run=["claude_code"]`
    and `cost_source=claude_code_subscription` — so the line under a Claude
    review named Gemini, and the line under a review with no cross-repo group
    claimed cross-repo edges.

    Provenance is the one part of a machine-written comment a reader uses to
    decide how much to trust the rest. A constant there is not a small lie.
    """
    bits: list[str] = ["tree-sitter graph"]
    if getattr(batch, "cross_repo_callers", 0):
        bits.append("cross-repo edges")
    engine = ", ".join(batch.agents_run) if batch.agents_run else ""
    if engine:
        bits.append(engine)
    return "context: " + " + ".join(bits)


# ─── Anchoring an inline comment to a line the diff carries ──────────
#
# Shared by all three providers. It lived in the GitHub provider, where the
# consequence is loudest — GitHub validates a review as ONE object, so a
# single refused anchor 422s the batch and takes every other finding with it.
# GitLab and Bitbucket post per finding, so there a bad anchor loses one
# comment instead of all of them; it was still lost, and lost silently, with
# nothing but a `failed` counter to show for it.
#
# One helper, three providers, so a fix measured on one of them is not a fix
# for one of them.

def _anchorable_ranges(pr: PullRequest) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """(path, side) -> the inclusive line spans GitHub will accept an anchor on.

    A review comment must sit on a line the diff actually carries. The hunk
    header states that span exactly: `@@ -old_start,old_count +new_start,new_count @@`,
    so the new file's postable lines are new_start .. new_start+new_count-1 —
    context lines included, which is why this is a span and not the set of '+'
    lines.

    A count of 0 contributes nothing: a pure-deletion hunk has no new-side line
    to point at, and a pure-addition hunk has no old-side one.

    LEFT spans are registered under both paths a rename gives the file, because
    the finding names whichever one the agent saw.
    """
    ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for hunk in pr.anchor_hunks:
        if hunk.new_count > 0 and hunk.file_path:
            ranges.setdefault((hunk.file_path, "RIGHT"), []).append(
                (hunk.new_start, hunk.new_start + hunk.new_count - 1),
            )
        if hunk.old_count > 0:
            span = (hunk.old_start, hunk.old_start + hunk.old_count - 1)
            for path in {hunk.file_path, hunk.old_file_path}:
                if path:
                    ranges.setdefault((path, "LEFT"), []).append(span)
    return ranges


def _old_line_for(pr: PullRequest, path: str, new_line: int) -> int | None:
    """The old-file line number of the CONTEXT line at `new_line` of `path`.

    None for an added line (it has no old side) and for a line the diff does
    not carry. Bitbucket wants both coordinates (`from` and `to`) to anchor a
    comment on an unchanged line; with `to` alone it can answer 400 or place
    the comment on the wrong side. Read from the hunk text, so no extra request.
    """
    for hunk in pr.anchor_hunks:
        if hunk.file_path != path or hunk.new_count <= 0:
            continue
        if not hunk.new_start <= new_line < hunk.new_start + hunk.new_count:
            continue
        old, new = hunk.old_start, hunk.new_start
        for raw in hunk.content.split("\n")[1:]:
            if new > new_line or (old - hunk.old_start >= hunk.old_count
                                  and new - hunk.new_start >= hunk.new_count):
                break
            if raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            if raw.startswith("+"):
                if new == new_line:
                    return None
                new += 1
            elif raw.startswith("-"):
                old += 1
            else:  # context; an editor may have stripped its single space
                if new == new_line:
                    return old
                old += 1
                new += 1
    return None


def _snap_to_span(line: int, spans: list[tuple[int, int]]) -> int:
    """The nearest line inside `spans`; `line` itself when it is already in one.

    MEASURED, on the two 14-PR Martian runs (146 findings between them, 69 and
    77): `github:celmis-bench/discourse-graphite#18` is the ONE PR of the 14
    whose findings never reached the pull request — per_pr.json records
    findings 4, posted 0, status "complete". GitHub validates a review as ONE
    object, so the single anchor the API refused (recorded as
    `app/controllers/admin/groups_controller.rb` line 117, on a file of 104
    lines) 422'd the batch and took the other three findings with it.

    It snaps and never drops, and the arithmetic is why. With the golden count
    fixed at 53 the benchmark's F2 is 5*TP / (TP + FP + 212), so one more true
    positive is worth +1.79 F2 and one fewer false positive +0.16 — a factor
    of eleven. A comment is worth posting at any precision above 8%, which no
    snapped anchor is plausibly below. A finding outside the diff is also not
    noise: it is the class architect exists to produce, the untouched caller
    the change breaks, which by design sits outside the changed lines.

    With no span for that file at all — a finding on a file the PR does not
    touch — the line is returned unchanged: there is nowhere to snap TO, and
    the 422 fallback folds it into the summary rather than losing it.

    Ties go to the lower line so the same finding lands in the same place on
    every re-run; a comment that moves between runs is a comment that looks new.
    """
    best_distance: int | None = None
    best_line = line
    for start, end in spans:
        candidate = start if line < start else (end if line > end else line)
        distance = abs(candidate - line)
        if best_distance is None or distance < best_distance or (
            distance == best_distance and candidate < best_line
        ):
            best_distance = distance
            best_line = candidate
    return best_line
