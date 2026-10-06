"""GitLab MR provider — extends Phase 9 GitLabClient.

API specifics:
    GET /projects/{id}/merge_requests/{iid}              — MR metadata
    GET .../merge_requests/{iid}/raw_diffs               — raw unified diff (new, 2025+)
    GET .../merge_requests/{iid}/versions                — needed for inline positions
    POST .../merge_requests/{iid}/discussions            — inline thread
    GET .../merge_requests/{iid}/discussions             — threads, replies included
    GET .../merge_requests/{iid}/notes                   — every note, inline ones included
    POST .../merge_requests/{iid}/notes                  — top-level summary note
    PUT .../merge_requests/{iid}/notes/{id}              — update existing summary
    DELETE .../merge_requests/{iid}/notes/{id}           — remove a previous run's note
    GET .../merge_requests/{iid}/approvals               — who approved (is it us?)
    POST .../merge_requests/{iid}/approve                — approve_when_clean
    POST .../merge_requests/{iid}/unapprove              — take our approval back
    PUT .../merge_requests/{iid}                         — the summary in the description

GitLab has no "request changes" action an API token can take, so
`request_changes_on_critical` falls back to withdrawing our approval and saying
in the summary note that changes are requested.

Inline position (`position[*]`): base_sha/head_sha/start_sha from the versions
API are required.

The notes endpoint is most of the cleanup story: a diff note (what a discussion
posted against a line actually is) comes back from it alongside the plain notes,
tagged `type: "DiffNote"` and carrying a `position`. So one paginated pass finds
both kinds and one delete path removes either. What that flat list cannot show
is who replied to what — it carries no discussion id — so one extra pass over
the discussions endpoint decides which notes a human reply protects.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import httpx

from src.credentials import resolve_git_credential
from src.http import build_client
from src.review import markers
from src.review.diff import parse_unified_diff
from src.review.markers import has_marker, parse_finding_marker
from src.review.models import (
    Finding,
    HunkSide,
    PullRequest,
    ReviewBatch,
)
from src.review.pr_actions import APPROVE, REQUEST_CHANGES, review_decision
from src.review.providers.base import (
    MAX_FILE_BYTES,
    SUGGESTION_GITLAB,
    CommitInfo,
    FileChange,
    OurThread,
    PathCommit,
    PostedComment,
    PullRequestProvider,
    PullRequestProviderError,
    ThreadMessage,
    _anchorable_ranges,
    _committable_enabled,
    _committable_span,
    _count,
    _format_finding_body,
    _format_summary,
    _format_unanchored,
    _new_side_text,
    _original_lines,
    _snap_to_span,
    _with_marker,
    begin_incremental_post,
    finding_fingerprint,
    finish_incremental_post,
    incremental_skip,
    trim_thread,
)
from src.review.scope import MAX_LISTED_COMMITS
from src.review.settings import get_review_settings
from src.sync.gitlab_instance import (
    API_SUFFIX,
    DEFAULT_INSTANCE,
    GitLabInstance,
    UnsafeGitLabURL,
    instance_for_credential,
    instance_of,
)

logger = logging.getLogger(__name__)


GITLAB_API_BASE = "https://gitlab.com/api/v4"

# The two kinds of note this bot writes. They live in one collection and share
# one delete path, but they are not handled the same way: the inline ones are
# deleted before a re-run, the summary is updated in place.
_INLINE_NOTE = "inline"
_SUMMARY_NOTE = "summary"


class GitLabPRProvider(PullRequestProvider):
    """GitLab Merge Request operations."""

    name = "gitlab"

    def __init__(
        self,
        token: str | None = None,
        *,
        account_label: str = "default",
        user_id: str = "default",
        workspace_id: str = "default",
        api_base: str | None = None,
        instance: GitLabInstance | None = None,
        timeout: float = 30.0,
    ) -> None:
        stored = None
        if token is None:
            stored = resolve_git_credential(
                "gitlab", user_id=user_id, account_label=account_label,
                workspace_id=workspace_id,
            )
            if stored is None:
                raise PullRequestProviderError(
                    f"No GitLab credentials saved for user '{user_id}'. "
                    f"Connect GitLab via the Connections page first."
                )
            token = stored.secret
        self.token = token
        # Which GitLab: explicit instance > explicit api_base > the instance
        # the workspace's credential row names > gitlab.com. The token and the
        # host come from the SAME row, so a self-hosted token can only ever
        # go to its own instance.
        try:
            if instance is None:
                if api_base:
                    instance = instance_of(api_base.rstrip("/").removesuffix(API_SUFFIX))
                elif stored is not None:
                    instance = instance_for_credential(stored)
                else:
                    instance = DEFAULT_INSTANCE
            http_kwargs = instance.http_kwargs()
        except UnsafeGitLabURL as exc:
            raise PullRequestProviderError(
                f"The workspace's GitLab URL cannot be used: {exc}") from exc
        self.instance = instance
        self.api_base = instance.api_base
        #: Who this token posts as. Looked up once, on the first cleanup that
        #: needs it; "" means the lookup failed and nothing may be deleted.
        self._viewer_cache: str | None = None
        # Guarded egress (src/http.py), not a raw httpx.Client. gitlab.com is
        # already on the shipped public allowlist; a self-hosted instance is
        # the client's one extra host, pinned to the address validated just
        # now (src/sync/gitlab_instance.py) — never derived from request data.
        self._http = build_client(
            timeout=timeout,
            **http_kwargs,
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": "code-analyzer/0.1",
            },
        )

    def __enter__(self) -> GitLabPRProvider:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ─── Fetch ───────────────────────────────────────────────────

    def fetch_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        project_path = quote(repo, safe="")

        # 1. Metadata
        meta_resp = self._http.get(
            f"{self.api_base}/projects/{project_path}/merge_requests/{pr_number}",
        )
        if meta_resp.status_code == 404:
            raise PullRequestProviderError(
                f"MR !{pr_number} not found in {repo}"
            )
        # A 301 (project renamed or moved) is an error that names the new
        # location, not an empty answer to parse.
        self._expect_ok(meta_resp, "GitLab API")
        meta = meta_resp.json()

        # 2. Raw diff (newer GitLab — single endpoint)
        # Fallback: the changes endpoint if raw_diffs is not available (legacy)
        diff_resp = self._http.get(
            f"{self.api_base}/projects/{project_path}/merge_requests/{pr_number}/raw_diffs",
            headers={"Accept": "text/plain"},
        )
        if diff_resp.status_code == 404:
            # Fallback: build the diff manually from the changes endpoint
            raw_diff = self._build_diff_from_changes(project_path, pr_number)
        else:
            # Not `>= 400` alone: a 301 gave an empty body here, and an empty
            # diff reads as "nothing to review".
            self._expect_ok(diff_resp, "GitLab raw_diffs")
            raw_diff = diff_resp.text

        settings = get_review_settings()
        hunks, skipped_files = parse_unified_diff(raw_diff, settings=settings)

        return PullRequest(
            provider="gitlab",
            repo=repo,
            number=pr_number,
            title=str(meta.get("title") or ""),
            description=str(meta.get("description") or ""),
            author=str((meta.get("author") or {}).get("username") or ""),
            base_ref=str(meta.get("target_branch") or ""),
            base_sha=str((meta.get("diff_refs") or {}).get("base_sha") or ""),
            head_ref=str(meta.get("source_branch") or ""),
            head_sha=str((meta.get("diff_refs") or {}).get("head_sha")
                          or meta.get("sha") or ""),
            state=str(meta.get("state") or "opened"),
            is_draft=bool(meta.get("draft") or meta.get("work_in_progress", False)),
            url=str(meta.get("web_url") or ""),
            hunks=hunks,
            raw_diff=raw_diff,
            skipped_files=skipped_files,
            reported_files=_count(meta.get("changes_count")),
        )

    def _build_diff_from_changes(
        self, project_path: str, mr_iid: int,
    ) -> str:
        """Fallback for GitLab Self-Managed without the raw_diffs endpoint.

        Uses the `/changes` endpoint + reconstructs a unified diff.
        """
        resp = self._http.get(
            f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}/changes"
        )
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        diffs: list[str] = []
        for change in data.get("changes") or []:
            old = change.get("old_path") or change.get("new_path") or "unknown"
            new = change.get("new_path") or change.get("old_path") or "unknown"
            diffs.append(f"diff --git a/{old} b/{new}")
            diffs.append(f"--- a/{old}")
            diffs.append(f"+++ b/{new}")
            diffs.append(change.get("diff") or "")
        return "\n".join(diffs)

    # ─── Post review ─────────────────────────────────────────────

    def post_review(
        self, batch: ReviewBatch, *, dry_run: bool = False,
    ) -> dict:
        pr = batch.pull_request
        if pr.provider != "gitlab":
            raise PullRequestProviderError(
                f"GitLab provider received non-gitlab PR: {pr.provider}"
            )
        project_path = quote(pr.repo, safe="")
        settings = get_review_settings()

        # 1. Get versions for diff_refs (needed for inline positions)
        versions_resp = self._http.get(
            f"{self.api_base}/projects/{project_path}/merge_requests/{pr.number}/versions"
        )
        if versions_resp.status_code >= 400 or not versions_resp.json():
            base_sha = pr.base_sha
            head_sha = pr.head_sha
            start_sha = pr.base_sha
        else:
            v = versions_resp.json()[0]
            base_sha = v.get("base_commit_sha") or pr.base_sha
            head_sha = v.get("head_commit_sha") or pr.head_sha
            start_sha = v.get("start_commit_sha") or pr.base_sha

        if dry_run:
            return {
                "dry_run": True,
                "findings": len(batch.findings),
                "verdict": batch.verdict.value,
            }

        # 2. Read what the previous run left on this MR
        #
        # Only the summary was ever cleaned up, and the inline discussions —
        # which carry the same marker — stayed: a re-review added a second full
        # set of them, a third added a third. Listed HERE, before the new set
        # goes up, so the ids cannot include the notes we are about to post;
        # deleted after (step 4), so a failure while posting leaves the previous
        # review standing rather than removing it and replacing it with nothing.
        stale: list[tuple[str, int]] = []
        protected: set[int] = set()
        listing_complete = True
        # An incremental review (only the new commits were read) keeps every
        # earlier inline discussion; the summary note is rewritten in place.
        incremental = begin_incremental_post(self, batch, settings.comment_marker)
        if settings.replace_on_synchronize:
            stale, listing_complete = self._marked_notes(
                project_path, pr.number, settings.comment_marker,
            )
            if incremental is not None:
                stale = [x for x in stale if x[0] == _SUMMARY_NOTE]
            if stale and listing_complete:
                # The flat listing cannot see who replied to what (see
                # `_protected_note_ids`) — one extra pass answers it. Skipped
                # when there is nothing to delete, or when the listing already
                # came up partial and the deletes are withheld anyway.
                protected, threads_ok = self._protected_note_ids(
                    project_path, pr.number,
                )
                listing_complete = threads_ok

        # 3. Inline discussions per finding (capped)
        #
        # Anchors are snapped onto a line the diff carries first, same as the
        # GitHub provider. GitLab posts per finding rather than validating the
        # review as one object, so an unanchorable line loses ONE comment
        # instead of the batch — which is why this went unnoticed here. It was
        # still a finding thrown away, recorded as nothing but a `failed`
        # counter.
        ranges = _anchorable_ranges(pr)
        committable = _committable_enabled(batch)
        new_side = _new_side_text(pr)
        posted = 0
        failed = 0
        snapped = 0
        refused: list[Finding] = []
        inline_comments: list[PostedComment] = []
        for finding in batch.inline_findings(
            settings.max_inline_comments, skip=incremental_skip(incremental),
        ):
            side = "RIGHT" if finding.side == HunkSide.RIGHT else "LEFT"
            line = _snap_to_span(
                finding.line, ranges.get((finding.file_path, side), []))
            if line != finding.line:
                snapped += 1
                logger.info(
                    "gitlab_anchor_snapped project=%s mr=%d path=%s from=%d to=%d",
                    project_path, pr.number, finding.file_path,
                    finding.line, line,
                )
            # GitLab anchors a suggestion at ONE line and counts the rest:
            # ```suggestion:-0+N replaces the anchored line and the N below.
            span = _committable_span(finding, line, ranges) if committable else None
            payload = {
                "body": _format_finding_body(
                    finding, settings.comment_marker,
                    committable=SUGGESTION_GITLAB if span else None,
                    original=_original_lines(new_side, finding),
                    head_sha=pr.head_sha,
                ),
                "position[position_type]": "text",
                "position[base_sha]": base_sha,
                "position[head_sha]": head_sha,
                "position[start_sha]": start_sha,
                "position[new_path]": finding.file_path,
                "position[old_path]": finding.file_path,
                "position[new_line]": str(line),
            }
            resp = self._http.post(
                f"{self.api_base}/projects/{project_path}/merge_requests/{pr.number}/discussions",
                data=payload,
            )
            if resp.status_code in (200, 201):
                posted += 1
                note_id = self._first_note_id(resp)
                if note_id is not None:
                    short, full = finding_fingerprint(finding)
                    inline_comments.append(PostedComment(
                        comment_id=note_id, path=finding.file_path, line=line,
                        fingerprint=short, finding_key=full,
                        thread_id=self._discussion_id(resp)))
            else:
                failed += 1
                refused.append(finding)
                logger.warning(
                    "gitlab_discussion_failed status=%d body=%s",
                    resp.status_code, resp.text[:200],
                )
        # A discussion GitLab would not place is not a finding lost: it joins
        # the summary, which is composed below. An incremental run also
        # resolves the discussions its commits made outdated and puts its
        # banner in the summary.
        incremental_response = finish_incremental_post(
            self, batch, incremental, posted=posted, refused=refused)
        if refused and incremental is None:
            batch.add_section(
                "unanchored", _format_unanchored(refused, pr, batch.review_language),
                order=900, targets={"comment"},
            )

        # 4. Drop the previous run's notes, now that this run's are up
        #    The lifecycle comment this run posted ("🔄 reviewing…") is the
        #    note kept and rewritten, so placeholder and summary are one note.
        keep_summary_id, cleanup = self._delete_stale_notes(
            project_path, pr.number, stale,
            protected=protected, listing_complete=listing_complete,
            prefer_keep=self._status_comment_id,
        )

        # 5. Top-level summary note
        decision = review_decision(batch)
        summary_body = _format_summary(batch, marker=settings.comment_marker)
        if decision == REQUEST_CHANGES:
            summary_body += (
                "\n\n> ❌ **Changes requested** — critical findings above. GitLab "
                "has no request-changes action for this token, so Celmis has "
                "withdrawn its approval instead; treat this note as the block."
            )
        summary_id = self._upsert_summary(
            project_path, pr.number, summary_body, keep_summary_id,
        )
        self._status_comment_id = None

        # 6. Approval — given for a clean review, taken back otherwise, only
        #    for a repository that lets reviews approve or block at all.
        review_state: dict = {}
        if batch.pr_actions.manages_review_state:
            review_state = self._apply_approval(
                project_path, pr, approve=decision == APPROVE,
            )

        response = {
            "summary_note_id": summary_id,
            "discussions_posted": posted,
            "discussions_failed": failed,
            # `complete: False` means duplicates may still be on the MR — a
            # caller reporting "review posted" can say so rather than let a
            # half-done cleanup look like a finished one.
            "cleanup": cleanup,
            "review_state": review_state,
            # What this run created, for the learning loop to follow.
            "inline_comments": inline_comments,
            **incremental_response,
        }
        if summary_id is None:
            # The summary note is the only place the verdict lives on GitLab,
            # so a lost one is a delivery failure — `error` becomes the run's
            # `post_error` (PARTIAL), the same contract as Bitbucket.
            why = (
                f"GitLab refused the summary note (HTTP {self._last_summary_status})"
                if self._last_summary_status else
                "the GitLab summary note could not be written"
            )
            logger.error(
                "gitlab_summary_not_delivered project=%s mr=%d discussions_posted=%d "
                "discussions_failed=%d status=%s",
                project_path, pr.number, posted, failed,
                self._last_summary_status or "-",
            )
            response["summary_error"] = why
            response["error"] = why
        return response

    @staticmethod
    def _discussion_id(resp: httpx.Response) -> str | None:
        """The id of the discussion a POST just created: what a reply names."""
        try:
            body = resp.json()
        except ValueError:
            return None
        did = body.get("id") if isinstance(body, dict) else None
        return did if isinstance(did, str) and did else None

    @staticmethod
    def _first_note_id(resp: httpx.Response) -> int | None:
        """The id of the first note of the discussion a POST just created."""
        try:
            body = resp.json()
        except ValueError:
            return None
        notes = body.get("notes") if isinstance(body, dict) else None
        if isinstance(notes, list) and notes and isinstance(notes[0], dict):
            nid = notes[0].get("id")
            return nid if isinstance(nid, int) else None
        return None

    # ─── Incremental review: commits, compare diff, discussions ───

    def _paged(self, url: str) -> list | None:
        """Every item of a paginated list (X-Next-Page), None when a page fails."""
        out: list = []
        seen: set[str] = set()
        nxt: str | None = url
        while nxt:
            if nxt in seen:
                return None
            seen.add(nxt)
            try:
                resp = self._http.get(nxt)
                self._expect_ok(resp, "GitLab API")
                page = resp.json()
            except (PullRequestProviderError, httpx.HTTPError, ValueError) as exc:
                logger.warning("gitlab_list_failed error=%s", exc)
                return None
            if not isinstance(page, list):
                return None
            out.extend(page)
            if len(out) > MAX_LISTED_COMMITS * 8:
                return None
            next_page = (resp.headers.get("X-Next-Page") or "").strip()
            nxt = self._replace_page_param(nxt, next_page) if next_page else None
        return out

    def list_pr_commits(self, repo: str, pr_number: int) -> list[CommitInfo] | None:
        project_path = quote(repo, safe="")
        items = self._paged(
            f"{self.api_base}/projects/{project_path}/merge_requests/{pr_number}"
            f"/commits?per_page=100")
        if items is None or len(items) > MAX_LISTED_COMMITS:
            return None
        out: list[CommitInfo] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                return None
            out.append(CommitInfo(
                sha=str(item["id"]),
                parents=tuple(str(p) for p in item.get("parent_ids") or []),
                message=str(item.get("message") or ""),
                committed_at=item.get("committed_date")))
        return out

    def fetch_incremental_diff(
        self, repo: str, pr_number: int, base_sha: str, head_sha: str,
    ) -> str | None:
        """`repository/compare?straight=true` rebuilt as a unified diff. A
        diff GitLab collapsed or cut as too large cannot be read whole: None."""
        project_path = quote(repo, safe="")
        try:
            resp = self._http.get(
                f"{self.api_base}/projects/{project_path}/repository/compare",
                params={"from": base_sha, "to": head_sha, "straight": "true"})
            self._expect_ok(resp, "GitLab compare")
            body = resp.json()
        except (PullRequestProviderError, httpx.HTTPError, ValueError) as exc:
            logger.warning("gitlab_incremental_diff_failed mr=%s error=%s", pr_number, exc)
            return None
        diffs = body.get("diffs") if isinstance(body, dict) else None
        if not isinstance(diffs, list) or body.get("compare_timeout"):
            return None
        rows: list[str] = []
        for change in diffs:
            if not isinstance(change, dict):
                return None
            if change.get("too_large") or change.get("collapsed"):
                return None
            old = change.get("old_path") or change.get("new_path") or "unknown"
            new = change.get("new_path") or change.get("old_path") or "unknown"
            rows.append(f"diff --git a/{old} b/{new}")
            rows.append("--- /dev/null" if change.get("new_file") else f"--- a/{old}")
            rows.append("+++ /dev/null" if change.get("deleted_file") else f"+++ b/{new}")
            rows.append(change.get("diff") or "")
        return "\n".join(rows)

    def our_inline_threads(self, pr: PullRequest, marker: str) -> list[OurThread] | None:
        """Our diff discussions (first note: marker AND author) with their
        discussion id. None when a page cannot be read."""
        project_path = quote(pr.repo, safe="")
        viewer = self._viewer_username()
        if not viewer:
            return None
        items = self._paged(
            f"{self.api_base}/projects/{project_path}/merge_requests/{pr.number}"
            f"/discussions?per_page=100")
        if items is None:
            return None
        threads: list[OurThread] = []
        for disc in items:
            notes = [n for n in (disc.get("notes") or []) if isinstance(n, dict)] \
                if isinstance(disc, dict) else []
            if not notes:
                continue
            first = notes[0]
            position = first.get("position")
            if (first.get("system") or not isinstance(position, dict)
                    or not self._is_ours(first, marker, viewer)):
                continue
            found = parse_finding_marker(str(first.get("body") or ""))
            new_line, old_line = position.get("new_line"), position.get("old_line")
            line = new_line if isinstance(new_line, int) else old_line
            threads.append(OurThread(
                comment_id=first.get("id") if isinstance(first.get("id"), int) else 0,
                thread_id=str(disc.get("id") or "") or None,
                path=str(position.get("new_path") or position.get("old_path") or ""),
                line=line if isinstance(line, int) else None,
                side="RIGHT" if isinstance(new_line, int) else "LEFT",
                resolved=bool(first.get("resolved")),
                fingerprint=found[0] if found else None,
                sha=found[1] if found else None,
                replied=any(not n.get("system") and not self._authored_by(n, viewer)
                            for n in notes[1:]),
            ))
        return threads

    def resolve_threads(self, pr: PullRequest, threads: list[OurThread]) -> dict[str, int]:
        """`PUT .../discussions/{id}?resolved=true`, one per thread."""
        project_path = quote(pr.repo, safe="")
        out = {"resolved": 0, "failed": 0, "unsupported": 0}
        for th in threads:
            if not th.thread_id:
                out["unsupported"] += 1
                continue
            try:
                resp = self._http.put(
                    f"{self.api_base}/projects/{project_path}/merge_requests/"
                    f"{pr.number}/discussions/{quote(th.thread_id, safe='')}",
                    params={"resolved": "true"})
            except httpx.HTTPError as exc:
                out["failed"] += 1
                logger.warning("gitlab_resolve_error id=%s err=%s", th.thread_id, exc)
                continue
            if resp.status_code in (200, 201):
                out["resolved"] += 1
            elif resp.status_code in (404, 405):
                out["unsupported"] += 1
            else:
                out["failed"] += 1
                logger.warning("gitlab_resolve_failed id=%s status=%d",
                               th.thread_id, resp.status_code)
        return out

    # ─── Lifecycle comment ("🔄 reviewing…" → summary) ──────────

    def upsert_status_comment(
        self, pr: PullRequest, body: str, *, create: bool = True,
        only_if_in_progress: bool = False,
    ) -> int | None:
        """See `PullRequestProvider.upsert_status_comment`."""
        if pr.provider != "gitlab":
            raise PullRequestProviderError(
                f"GitLab provider received non-gitlab PR: {pr.provider}"
            )
        settings = get_review_settings()
        project_path = quote(pr.repo, safe="")
        write, existing = self._status_target(
            pr, settings.comment_marker,
            replace=settings.replace_on_synchronize, create=create,
            only_if_in_progress=only_if_in_progress,
        )
        if not write:
            return None
        nid = self._upsert_summary(
            project_path, pr.number,
            _with_marker(body, settings.comment_marker), existing,
        )
        if nid is not None:
            self._status_comment_id = nid
        return nid

    def _comment_marker(self) -> str:
        return get_review_settings().comment_marker

    def _write_top_level_comment(
        self, pr: PullRequest, body: str, existing_id: int | None,
    ) -> int | None:
        return self._upsert_summary(
            self._project_path(pr.repo), pr.number, body, existing_id,
        )

    # ─── Approval ────────────────────────────────────────────────

    def _approved_by_us(self, project_path: str, mr_iid: int) -> bool | None:
        """Whether this token's user is among the MR's approvers; None = unknown."""
        viewer = self._viewer_username()
        if not viewer:
            return None
        try:
            resp = self._http.get(
                f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}/approvals"
            )
            if resp.status_code >= 400:
                return None
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        for entry in body.get("approved_by") or []:
            user = entry.get("user") if isinstance(entry, dict) else None
            if isinstance(user, dict) and str(user.get("username") or "") == viewer:
                return True
        return False

    def _apply_approval(self, project_path: str, pr: PullRequest, *, approve: bool) -> dict:
        """Approve, or take our approval back. Idempotent; never raises.

        The approvals listing is read first, so a re-run does not approve
        twice (GitLab refuses a second approval from the same user) and a run
        with nothing to take back sends nothing. When that listing cannot be
        read the action is sent anyway and a refusal is only logged. The
        approval is pinned to the reviewed commit (`sha`), so a push that
        landed while the review ran is not approved by it.
        """
        state = self._approved_by_us(project_path, pr.number)
        base = f"{self.api_base}/projects/{project_path}/merge_requests/{pr.number}"
        if approve and state is not True:
            action, url = "approved", f"{base}/approve"
            data = {"sha": pr.head_sha} if pr.head_sha else {}
        elif not approve and state is not False:
            action, url, data = "unapproved", f"{base}/unapprove", {}
        else:
            return {"approval": "unchanged"}
        try:
            resp = self._http.post(url, data=data)
        except httpx.HTTPError as exc:
            logger.warning("gitlab_approval_error action=%s err=%s", action, exc)
            return {"approval": "failed", "action": action}
        # 404 on unapprove: there was no approval of ours, the state we wanted.
        if resp.status_code < 400 or (action == "unapproved" and resp.status_code == 404):
            return {"approval": action}
        logger.warning("gitlab_approval_failed action=%s status=%d body=%s",
                       action, resp.status_code, resp.text[:200])
        return {"approval": "failed", "action": action, "status": resp.status_code}

    # ─── The summary in the merge request description ───────────

    def update_description(self, pr: PullRequest, transform) -> dict:
        """GET the MR, PUT its description with `transform(description)`."""
        url = (f"{self.api_base}/projects/{self._project_path(pr.repo)}"
               f"/merge_requests/{pr.number}")
        try:
            resp = self._http.get(url)
            if resp.status_code >= 400:
                return {"written": False, "error": f"GitLab refused the read (HTTP {resp.status_code})"}
            meta = resp.json()
            current = (str((meta or {}).get("description") or "")
                       if isinstance(meta, dict) else "")
            new = transform(current)
            if new is None or new == current:
                return {"written": False, "error": None, "unchanged": True}
            resp = self._http.put(url, data={"description": new})
        except (httpx.HTTPError, ValueError) as exc:
            return {"written": False, "error": type(exc).__name__}
        if resp.status_code >= 400:
            logger.warning("gitlab_description_put_failed status=%d", resp.status_code)
            return {"written": False, "error": f"GitLab refused the description (HTTP {resp.status_code})"}
        return {"written": True, "error": None}

    def fetch_commit_messages(self, pr: PullRequest, limit: int = 50) -> list[str]:
        url = (f"{self.api_base}/projects/{self._project_path(pr.repo)}"
               f"/merge_requests/{pr.number}/commits")
        try:
            resp = self._http.get(url, params={"per_page": max(1, min(int(limit), 100))})
            if resp.status_code != 200:
                return []
            rows = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        # GitLab lists newest first already.
        return [str(r.get("message") or "") for r in rows
                if isinstance(r, dict) and r.get("message")][:limit]

    def _our_summary_comments(
        self, pr: PullRequest, marker: str,
    ) -> list[tuple[int, str]]:
        bodies: dict[int, str] = {}
        found, _ = self._marked_notes(
            self._project_path(pr.repo), pr.number, marker, bodies=bodies,
        )
        return [(nid, bodies.get(nid, "")) for kind, nid in found
                if kind == _SUMMARY_NOTE]

    # ─── Idempotency: what a previous run left behind ────────────

    def _notes_url(self, project_path: str, mr_iid: int) -> str:
        # Ascending, so the first summary note found is the one the FIRST review
        # created — that is the note kept and updated in place, which is what
        # makes a link to the summary still resolve after the third push.
        return (
            f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}"
            f"/notes?per_page=100&order_by=created_at&sort=asc"
        )

    def _marked_notes(
        self, project_path: str, mr_iid: int, marker: str,
        bodies: dict[int, str] | None = None,
    ) -> tuple[list[tuple[str, int]], bool]:
        """Our notes as (kind, id), and whether the listing finished.

        Matching is `marker` AND authorship — see `_is_ours`. It was the marker
        alone, on the reasoning that only this module writes one; quoting a note
        is what broke that, because it copies the quoted note's RAW markdown and
        the marker is an HTML comment inside it. A reviewer arguing with one of
        our findings therefore writes a note carrying our marker, and the
        marker-only test would have deleted their words on the next run.
        GitLab's own system notes are skipped outright regardless.

        The bool is False when a page could not be read. A cleanup that saw only
        page one and reported success is worse than no cleanup at all: the
        duplicates it missed stay on the MR and nothing says so — and against a
        page size of 100 a busy MR reaches page two easily.
        """
        found: list[tuple[str, int]] = []
        seen: set[str] = set()
        url: str | None = self._notes_url(project_path, mr_iid)
        while url:
            if url in seen:
                # A proxy that echoes the same X-Next-Page would spin forever.
                logger.warning("gitlab_list_notes_loop url=%s", url)
                return found, False
            seen.add(url)
            try:
                resp = self._http.get(url)
                if resp.status_code >= 400:
                    logger.warning(
                        "gitlab_list_notes_failed status=%d", resp.status_code,
                    )
                    return found, False
                page = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("gitlab_list_notes_error err=%s", exc)
                return found, False
            viewer = self._viewer_username()
            if not viewer:
                # Fail closed — see `_viewer_username`.
                return found, False
            for note in page or []:
                if not isinstance(note, dict) or note.get("system"):
                    continue
                if not self._is_ours(note, marker, viewer):
                    continue
                nid = note.get("id")
                if not isinstance(nid, int):
                    continue
                inline = note.get("type") == "DiffNote" or bool(note.get("position"))
                found.append((_INLINE_NOTE if inline else _SUMMARY_NOTE, nid))
                if bodies is not None:
                    bodies[nid] = str(note.get("body") or "")
            # GitLab pagination via the X-Next-Page header
            next_page = (resp.headers.get("X-Next-Page") or "").strip()
            url = self._replace_page_param(url, next_page) if next_page else None
        return found, True

    def _protected_note_ids(
        self, project_path: str, mr_iid: int,
    ) -> tuple[set[int], bool]:
        """Note ids that a foreign reply protects, via one pass of /discussions.

        The flat /notes payload carries no discussion id, so a thread is
        invisible to the listing the cleanup runs on. Nothing inline was ever
        deleted before this cleanup existed, so no reply could be orphaned;
        now that our own inline notes go on every push, deleting a root a
        human answered would tear their reply out of its context. Every note
        in a discussion where any non-system note is not provably ours is
        protected: proof of ours is what LIFTS protection, an unreadable
        author never does.

        The bool is False when a page could not be read — the caller then
        deletes nothing, because the unread pages may hold exactly the reply
        that would have protected a root.
        """
        viewer = self._viewer_username()
        if not viewer:
            return set(), False
        protected: set[int] = set()
        seen: set[str] = set()
        url: str | None = (
            f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}"
            f"/discussions?per_page=100"
        )
        while url:
            if url in seen:
                logger.warning("gitlab_list_discussions_loop url=%s", url)
                return protected, False
            seen.add(url)
            try:
                resp = self._http.get(url)
                if resp.status_code >= 400:
                    logger.warning(
                        "gitlab_list_discussions_failed status=%d",
                        resp.status_code,
                    )
                    return protected, False
                page = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("gitlab_list_discussions_error err=%s", exc)
                return protected, False
            for discussion in page or []:
                if not isinstance(discussion, dict):
                    continue
                notes = [
                    n for n in discussion.get("notes") or [] if isinstance(n, dict)
                ]
                if len(notes) < 2:
                    continue  # nobody replied; nothing here to protect
                # Our own account without any marker of ours is a person
                # speaking through the token, and counts as foreign.
                foreign = any(
                    not n.get("system") and (
                        not self._authored_by(n, viewer)
                        or not markers.is_bot_text(str(n.get("body") or ""))
                    )
                    for n in notes
                )
                if foreign:
                    protected.update(
                        n["id"] for n in notes if isinstance(n.get("id"), int)
                    )
            next_page = (resp.headers.get("X-Next-Page") or "").strip()
            url = self._replace_page_param(url, next_page) if next_page else None
        return protected, True

    def _delete_stale_notes(
        self,
        project_path: str,
        mr_iid: int,
        stale: list[tuple[str, int]],
        *,
        protected: set[int],
        listing_complete: bool,
        prefer_keep: int | None = None,
    ) -> tuple[int | None, dict]:
        """Delete the previous run's notes; return the summary worth keeping.

        The first marked summary note is NOT deleted — it is handed back so the
        caller can PUT the new summary into it (Qodo's persistent-comment
        pattern). Surplus summaries, including the ones the old delete-and-
        repost left behind, go the way of the inline notes.

        Deleting a note this bot authored needs no elevated rights; the previous
        code assumed otherwise and overwrote the summary with "_(superseded by
        new review)_" instead, which left one dead stub per re-run — the same
        pile-up in a quieter costume.

        Two more things are never deleted: a note in a discussion someone
        else spoke in — updating the kept summary is safe, a PUT leaves the
        thread standing, but deleting a note a human answered orphans their
        reply, so it is counted in `kept_threaded` (a decision, not a failure:
        it does not turn `complete` off) — and anything at all when the
        listing did not finish, because the unread pages may hold exactly the
        reply that would have protected it.

        Nothing here raises: a review with duplicate notes beats no review, so a
        failure is logged, counted, and surfaced in the returned stats.
        """
        # `prefer_keep` — the lifecycle note this run already wrote — wins
        # over "the first one found"; a marked summary that is neither is
        # surplus like any other.
        keep_summary_id: int | None = prefer_keep
        deleted = 0
        failed = 0
        kept_threaded = 0
        for kind, nid in stale:
            if kind == _SUMMARY_NOTE and keep_summary_id is None:
                keep_summary_id = nid
                continue
            if kind == _SUMMARY_NOTE and nid == keep_summary_id:
                continue
            if nid in protected:
                kept_threaded += 1
                continue
            if not listing_complete:
                continue
            try:
                resp = self._http.delete(
                    f"{self.api_base}/projects/{project_path}"
                    f"/merge_requests/{mr_iid}/notes/{nid}"
                )
            except httpx.HTTPError as exc:
                failed += 1
                logger.warning(
                    "gitlab_delete_note_error kind=%s id=%d err=%s", kind, nid, exc,
                )
                continue
            # 404 — somebody deleted it first, which is the state we wanted.
            if resp.status_code in (200, 202, 204, 404):
                deleted += 1
            else:
                failed += 1
                logger.warning(
                    "gitlab_delete_note_failed kind=%s id=%d status=%d body=%s",
                    kind, nid, resp.status_code, resp.text[:100],
                )
        return keep_summary_id, {
            "deleted": deleted,
            "failed": failed,
            "kept_threaded": kept_threaded,
            "complete": listing_complete and failed == 0,
        }

    def _upsert_summary(
        self, project_path: str, mr_iid: int, body: str, existing_id: int | None,
    ) -> int | None:
        """PUT the summary note we already own, or POST the first one."""
        self._last_summary_status = None
        if existing_id is not None:
            resp = self._http.put(
                f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}"
                f"/notes/{existing_id}",
                data={"body": body},
            )
            if resp.status_code in (200, 201):
                return existing_id
            logger.warning(
                "gitlab_summary_put_failed id=%d status=%d — posting a new note",
                existing_id, resp.status_code,
            )
        resp = self._http.post(
            f"{self.api_base}/projects/{project_path}/merge_requests/{mr_iid}/notes",
            data={"body": body},
        )
        if resp.status_code in (200, 201):
            nid = resp.json().get("id")
            return nid if isinstance(nid, int) else None
        self._last_summary_status = resp.status_code
        logger.warning("gitlab_summary_post_failed status=%d", resp.status_code)
        return None

    #: HTTP status of the last summary write that failed, for the run record.
    _last_summary_status: int | None = None

    def find_existing_review_comment(
        self, repo: str, pr_number: int, marker: str,
    ) -> int | None:
        """The summary note to update in place — oldest marked top-level one.

        Inline notes are deliberately not eligible: this returns the id the
        caller will PUT a summary into, and writing a summary over a comment
        anchored to line 42 of some file is not an update, it is vandalism.
        """
        project_path = self._project_path(repo)
        found, _ = self._marked_notes(project_path, pr_number, marker)
        for kind, nid in found:
            if kind == _SUMMARY_NOTE:
                return nid
        return None

    def find_marked_comment_ids(
        self, repo: str, pr_number: int, marker: str,
    ) -> list[int]:
        """Every note of ours on this MR — inline discussions and summary."""
        found, _ = self._marked_notes(self._project_path(repo), pr_number, marker)
        return [nid for _, nid in found]

    # ─── Reading the target branch (the issues backlog) ──────────

    def _project_url(self, repo: str) -> str:
        return f"{self.api_base}/projects/{self._project_path(repo)}"

    def branch_head_sha(self, repo: str, branch: str) -> str:
        resp = self._http.get(
            f"{self._project_url(repo)}/repository/branches/{quote(branch, safe='')}")
        self._expect_ok(resp, "GitLab branch")
        sha = str(((resp.json() or {}).get("commit") or {}).get("id") or "")
        if not sha:
            raise PullRequestProviderError(f"GitLab named no commit for branch {branch!r}")
        return sha

    def read_file_at(self, repo: str, ref: str, path: str) -> str | None:
        resp = self._http.get(
            f"{self._project_url(repo)}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": ref},
        )
        if resp.status_code == 404:
            return None
        self._expect_ok(resp, "GitLab file")
        if len(resp.content) > MAX_FILE_BYTES:
            raise PullRequestProviderError(
                f"{path} is larger than {MAX_FILE_BYTES} bytes; not read")
        return resp.content.decode("utf-8", "replace")

    def commits_touching(
        self, repo: str, ref: str, path: str, *,
        since: datetime | None = None, limit: int = 10,
    ) -> list[PathCommit]:
        params: dict[str, object] = {
            "ref_name": ref, "path": path, "per_page": max(1, min(int(limit), 100)),
        }
        if since is not None:
            params["since"] = since.isoformat()
        resp = self._http.get(f"{self._project_url(repo)}/repository/commits",
                              params=params)
        self._expect_ok(resp, "GitLab commits")
        return [
            PathCommit(
                sha=str(c.get("id") or ""),
                subject=str(c.get("title") or c.get("message") or "").strip()[:300],
                date=c.get("committed_date") or c.get("created_at"),
                url=c.get("web_url"),
            )
            for c in (resp.json() or []) if c.get("id")
        ][:limit]

    def file_change_in_commit(
        self, repo: str, sha: str, path: str,
    ) -> FileChange | None:
        resp = self._http.get(
            f"{self._project_url(repo)}/repository/commits/{sha}/diff",
            params={"per_page": 100})
        self._expect_ok(resp, "GitLab commit")
        for d in resp.json() or []:
            new, old = d.get("new_path"), d.get("old_path")
            if path not in (new, old):
                continue
            if d.get("deleted_file"):
                return FileChange("deleted", old or path)
            if d.get("renamed_file") and old == path and new:
                return FileChange("renamed", new, previous_path=old)
            return FileChange("added" if d.get("new_file") else "modified", new or path)
        return None

    def list_comment_reactions(
        self, repo: str, pr_number: int, comment_id: str,
    ) -> list[tuple[str, str]]:
        url = (f"{self.api_base}/projects/{self._project_path(repo)}/merge_requests/"
               f"{int(pr_number)}/notes/{quote(str(comment_id), safe='')}/award_emoji")
        try:
            resp = self._http.get(url, params={"per_page": 100})
            rows = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            raise PullRequestProviderError(f"GitLab reactions: {type(exc).__name__}") from exc
        if rows is None:
            raise PullRequestProviderError(f"GitLab reactions error {resp.status_code}")
        out: list[tuple[str, str]] = []
        for item in rows:
            name = str((item or {}).get("name") or "")
            user = str(((item or {}).get("user") or {}).get("username") or "")
            if user and name in ("thumbsup", "thumbsdown"):
                out.append((user, "up" if name == "thumbsup" else "down"))
        return out

    def commit_url(self, repo: str, sha: str) -> str | None:
        base = self.api_base.removesuffix("/api/v4")
        return f"{base}/{repo}/-/commit/{sha}"

    @staticmethod
    def _project_path(repo: str) -> str:
        """`group/proj` → `group%2Fproj`, and an already-encoded path unchanged.

        Internal callers hand this method a path they encoded themselves; the
        provider-agnostic entry points hand it a plain slug.
        """
        return repo if "%2F" in repo else quote(repo, safe="")

    def _viewer_username(self) -> str:
        """Who this token posts as, cached for the life of the provider.

        Required before anything is deleted. The marker is an HTML comment in
        the body, and quoting a note copies its RAW markdown — marker included.
        A reviewer who quotes one of our findings to disagree with it would
        otherwise have written a note that a substring test deletes on the
        next run.
        """
        if self._viewer_cache is not None:
            return self._viewer_cache
        try:
            resp = self._http.get(f"{self.api_base}/user")
            if resp.status_code < 400:
                # A 200 whose body is not an object — a captive proxy's page, a
                # list from a mis-routed request — used to reach .get() and
                # raise AttributeError straight past the except below, taking
                # the whole review down. An unreadable answer is not an
                # identity: treat it as no identity and delete nothing.
                body = resp.json()
                username = (
                    str(body.get("username") or "") if isinstance(body, dict) else ""
                )
                if not username:
                    logger.warning("gitlab_viewer_lookup_anonymous")
                self._viewer_cache = username
                return username
            logger.warning("gitlab_viewer_lookup_failed status=%d", resp.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("gitlab_viewer_lookup_error err=%s", exc)
        self._viewer_cache = ""
        return ""

    @classmethod
    def _is_ours(cls, note: dict, marker: str, viewer: str) -> bool:
        """Both conditions, never one: our marker AND our authorship."""
        if not has_marker(note.get("body") or "", marker):
            return False
        return cls._authored_by(note, viewer)

    @staticmethod
    def _authored_by(note: dict, viewer: str) -> bool:
        """Provably written by this token — a malformed author is a no.

        `author` arrives as a plain string on a truncated or proxied payload,
        and `(note.get("author") or {}).get("username")` raised AttributeError
        on it — outside the except around the listing, so it took the whole
        review down. Absence of proof keeps a note; it never crashes the
        review.
        """
        author = note.get("author")
        if not isinstance(author, dict):
            return False
        username = str(author.get("username") or "")
        return bool(username) and username == viewer

    @staticmethod
    def _replace_page_param(url: str, page: str) -> str:
        """Set/replace standalone `page=N` query param.

        Boundary-aware: `(?<=[?&])` ensures that we do not touch 'per_page=100'.
        If `page=N` is not present — append it as a new param.
        """
        import re
        new_url, count = re.subn(r"(?<=[?&])page=\d+", f"page={page}", url)
        if count > 0:
            return new_url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}"

    # ─── Conversation (comment commands) ─────────────────────────

    def viewer_ids(self) -> frozenset[str]:
        username = self._viewer_username()
        return frozenset({username}) if username else frozenset()

    def post_reply(self, ev, body: str) -> str | None:
        """Into the discussion the comment belongs to; a plain note when the
        discussion cannot be written to (resolved threads refuse replies)."""
        project = self._project_path(ev.repo)
        base = f"{self.api_base}/projects/{project}/merge_requests/{ev.pr_number}"
        text = markers.with_chat_marker(body)
        resp = None
        if ev.thread_id:
            resp = self._http.post(
                f"{base}/discussions/{ev.thread_id}/notes", json={"body": text},
            )
        if resp is None or resp.status_code >= 400:
            resp = self._http.post(f"{base}/notes", json={"body": text})
        resp = self._expect_ok(resp, "gitlab reply")
        try:
            data = resp.json()
        except ValueError:
            return None
        nid = data.get("id") if isinstance(data, dict) else None
        return str(nid) if nid is not None else None

    def update_comment(
        self, repo: str, pr_number: int, comment_id: str, body: str, *, kind: str = "issue",
    ) -> bool:
        project = self._project_path(repo)
        resp = self._http.put(
            f"{self.api_base}/projects/{project}/merge_requests/{pr_number}"
            f"/notes/{comment_id}",
            json={"body": markers.with_chat_marker(body)},
        )
        return 200 <= resp.status_code < 300

    def get_thread(self, ev, limit: int = 30) -> list[ThreadMessage]:
        """The discussion the note belongs to, system notes left out."""
        if not ev.thread_id:
            return []
        project = self._project_path(ev.repo)
        try:
            resp = self._http.get(
                f"{self.api_base}/projects/{project}/merge_requests/{ev.pr_number}"
                f"/discussions/{quote(str(ev.thread_id), safe='')}")
            if resp.status_code >= 400:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        notes = data.get("notes") if isinstance(data, dict) else None
        viewer = self._viewer_username()
        found: list[ThreadMessage] = []
        for note in notes if isinstance(notes, list) else []:
            if not isinstance(note, dict) or note.get("system"):
                continue
            author = note.get("author")
            position = note.get("position")
            position = position if isinstance(position, dict) else {}
            line = position.get("new_line")
            if not isinstance(line, int):
                line = position.get("old_line")
            path = position.get("new_path") or position.get("old_path")
            found.append(ThreadMessage(
                comment_id=str(note.get("id")),
                author=str(author.get("username") or "") if isinstance(author, dict) else "",
                text=str(note.get("body") or ""),
                ours=bool(viewer) and self._authored_by(note, viewer),
                path=path if isinstance(path, str) else None,
                line=line if isinstance(line, int) else None,
                created_at=note.get("created_at") if isinstance(note.get("created_at"), str) else None,
            ))
        return trim_thread(found, limit)

    def acknowledge(self, ev) -> bool:
        """An eyes award on the note."""
        project = self._project_path(ev.repo)
        resp = self._http.post(
            f"{self.api_base}/projects/{project}/merge_requests/{ev.pr_number}"
            f"/notes/{ev.comment_id}/award_emoji",
            json={"name": "eyes"},
        )
        return 200 <= resp.status_code < 300

    def actor_permission(
        self, repo: str, *, actor_id: str = "", actor_name: str = "",
    ) -> str:
        """Access level in the project, inherited from its groups too: 30
        (Developer) and up is `write`."""
        if not str(actor_id).isdigit():
            return "unknown"
        project = self._project_path(repo)
        try:
            resp = self._http.get(
                f"{self.api_base}/projects/{project}/members/all/{actor_id}",
            )
            if resp.status_code == 404:
                return "none"
            if resp.status_code >= 400:
                return "unknown"
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return "unknown"
        level = data.get("access_level") if isinstance(data, dict) else None
        if not isinstance(level, int) or isinstance(level, bool):
            return "unknown"
        if level >= 30:
            return "write"
        return "read" if level > 0 else "none"

    def pr_participants(self, repo: str, pr_number: int) -> frozenset[str]:
        project = self._project_path(repo)
        try:
            resp = self._http.get(
                f"{self.api_base}/projects/{project}/merge_requests/{pr_number}",
            )
            if resp.status_code >= 400:
                return frozenset()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return frozenset()
        if not isinstance(data, dict):
            return frozenset()
        people = [data.get("author")]
        people.extend(data.get("reviewers") or [])
        people.extend(data.get("assignees") or [])
        ids: set[str] = set()
        for person in people:
            if isinstance(person, dict):
                ids.update(str(v) for v in (person.get("id"), person.get("username")) if v)
        return frozenset(ids)
