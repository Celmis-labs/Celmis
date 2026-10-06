"""Bitbucket Cloud PR provider.

API specifics:
    GET /user                                               — who the token posts as
    GET /repositories/{ws}/{r}/pullrequests/{id}            — PR metadata
    GET .../pullrequests/{id}/diff                          — raw unified diff (answers 302!)
    GET .../pullrequests/{id}/diffstat                      — files in the PR (answers 302 too)
    GET .../pullrequests/{id}/comments                      — ONE list: inline, summary, replies
    POST .../pullrequests/{id}/comments                     — inline OR top-level
    PUT  .../pullrequests/{id}/comments/{cid}               — update comment
    DELETE .../pullrequests/{id}/comments/{cid}             — delete comment
    POST/DELETE .../pullrequests/{id}/approve               — approve / take it back
    POST/DELETE .../pullrequests/{id}/request-changes       — block / lift the block
    PUT  .../pullrequests/{id}                              — the summary in the description

Suggestions: Bitbucket Cloud's "Suggest code" (2025) is an editor feature —
`/suggest` in the comment box, single line — and the raw markdown an API
comment would need for an "Apply suggestion" button is not documented. So a
suggested change is rendered as a plain ```diff block here, never as a
committable one, whatever `committable_suggestions` says.

Inline coords:
    {"inline": {"path": "src/x.py", "to": 42}}   — new file line
    {"inline": {"path": "src/x.py", "from": 41}} — old file line
    No `inline` field → top-level comment

A reply carries `parent: {"id": …}` in the same listing — that is how the
cleanup tells a threaded comment from a lone one without a second endpoint.

⚠️ App passwords stop working 9 June 2026 — use Workspace Access Token.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from src.credentials import resolve_git_credential
from src.http import build_client
from src.review import markers
from src.review.diff import parse_unified_diff
from src.review.models import (
    Finding,
    HunkSide,
    PullRequest,
    ReviewBatch,
)
from src.review.pr_actions import APPROVE, REQUEST_CHANGES, review_decision
from src.review.providers.base import (
    MAX_FILE_BYTES,
    CommitInfo,
    EmptyDiffError,
    FileChange,
    OurThread,
    PathCommit,
    PostedComment,
    PullRequestProvider,
    PullRequestProviderError,
    ThreadMessage,
    _anchorable_ranges,
    _count,
    _format_finding_body,
    _format_summary,
    _format_unanchored,
    _new_side_text,
    _old_line_for,
    _original_lines,
    _snap_to_span,
    _with_marker,
    begin_incremental_post,
    committed_since,
    finding_fingerprint,
    finish_incremental_post,
    incremental_skip,
    trim_thread,
)
from src.review.scope import MAX_LISTED_COMMITS
from src.review.settings import get_review_settings

logger = logging.getLogger(__name__)


BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"

# One listing serves both kinds on Bitbucket, but the two are not handled
# alike on a re-run: an inline comment is deleted and re-posted, the summary
# is the one comment PUT in place so its id (and the thread under it) survive.
# The kind travels with the id so the delete pass can tell them apart.
_INLINE_COMMENT = "inline"
_SUMMARY_COMMENT = "summary"

#: Bitbucket answers `/pullrequests/{id}/diff` (and `/diffstat`) with a 302 to
#: `/repositories/{ws}/{r}/diff/{head}..{base}?topic=true`. Never more than a
#: hop or two in practice; three is the most this provider will follow.
_MAX_REDIRECTS = 3
_REDIRECTS = (301, 302, 303, 307, 308)

#: Waits before the plain diff is asked again when it came back empty for a PR
#: whose diffstat lists files — Bitbucket can be lazy right after
#: `pullrequest:created`. Two retries, then it is an error, not a skip.
_EMPTY_DIFF_BACKOFF = (2.0, 5.0)

#: A 429 on a comment POST: the wait when Bitbucket names none, and the longest
#: wait this provider accepts (a review is already minutes long).
_DEFAULT_RETRY_AFTER = 5.0
_MAX_RETRY_AFTER = 30.0


def _api_host() -> tuple[str, ...]:
    """BITBUCKET_API_BASE's own host, as a one-item exception to the allowlist.

    Every request this provider makes goes to exactly one place — the base URL
    above — so that host is named at the call site, the same way the LiteLLM
    gateway names its configured proxy (see src/llm/gateway.py:_proxy_host).
    Derived from the constant rather than spelled twice, so the exception can
    never point anywhere the requests do not.
    """
    host = urlsplit(BITBUCKET_API_BASE).hostname or ""
    return (host,) if host else ()


class BitbucketPRProvider(PullRequestProvider):
    """Bitbucket Cloud PR operations.

    Every text that goes out passes `_outbound` (raw HTML tags become markdown,
    markers become invisible lines, the length is capped without cutting a
    marker) and every text that comes back passes `markers.reveal`, so the rest
    of the review only ever sees, and only ever writes, the HTML-comment form.
    """

    name = "bitbucket"

    #: Best-known limits of Bitbucket Cloud's comment and description fields.
    #: Not documented; 30k is what a real PR body has been seen to hold. Tune
    #: when the probe (`scripts/probe_bitbucket_markdown.py`) says otherwise.
    comment_max_chars = 30_000
    description_max_chars = 30_000

    def __init__(
        self,
        token: str | None = None,
        *,
        email: str | None = None,
        account_label: str = "default",
        user_id: str = "default",
        workspace_id: str = "default",
        timeout: float = 30.0,
    ) -> None:
        if token is None:
            stored = resolve_git_credential(
                "bitbucket", user_id=user_id, account_label=account_label,
                workspace_id=workspace_id,
            )
            if stored is None:
                raise PullRequestProviderError(
                    f"No Bitbucket credentials saved for user '{user_id}'. "
                    f"Connect Bitbucket via the Connections page first."
                )
            token = stored.secret
            # Atlassian API tokens (ATATT…) need Basic auth with email; Workspace
            # Access Tokens (ATCTT…) use Bearer. Email saved in metadata.
            if email is None and isinstance(stored.metadata, dict):
                email = stored.metadata.get("atlassian_email")  # type: ignore[assignment]
        self.token = token
        self.email = email
        #: The stable ids this token posts as (uuid + account_id). Looked up
        #: once, on the first cleanup that needs it; an empty set means the
        #: lookup failed and nothing may be deleted.
        self._viewer_cache: frozenset[str] | None = None

        # Guarded egress (src/http.py), not raw httpx.Clients: both auth
        # branches sat on deployment.UNGUARDED_HTTP_SITES from the day the
        # factory shipped. Same defaults as httpx's own — follow_redirects
        # stays False.
        common_headers = {
            "Accept": "application/json",
            "User-Agent": "code-analyzer/0.1",
        }
        if email:
            # Atlassian API token: Basic auth (email:token)
            self._http = build_client(
                timeout=timeout, headers=common_headers, auth=(str(email), token),
                extra_allowed_hosts=_api_host(),
            )
        else:
            # Workspace Access Token: Bearer auth
            self._http = build_client(
                timeout=timeout,
                headers={**common_headers, "Authorization": f"Bearer {token}"},
                extra_allowed_hosts=_api_host(),
            )

    def __enter__(self) -> BitbucketPRProvider:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ─── Fetch ───────────────────────────────────────────────────

    def fetch_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        ws, name = self._split_repo(repo)

        # 1. Metadata
        meta_resp = self._http.get(
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}"
        )
        if meta_resp.status_code == 404:
            raise PullRequestProviderError(
                f"PR #{pr_number} not found in {repo}"
            )
        self._expect_ok(meta_resp, "Bitbucket API")
        meta = meta_resp.json()

        source = meta.get("source") or {}
        destination = meta.get("destination") or {}
        head_sha = str((source.get("commit") or {}).get("hash") or "")
        base_sha = str((destination.get("commit") or {}).get("hash") or "")

        def build(raw_diff: str, reported: int | None) -> PullRequest:
            hunks, skipped_files = parse_unified_diff(
                raw_diff, settings=get_review_settings(),
            )
            author_user = (meta.get("author") or {}).get("nickname") or \
                          (meta.get("author") or {}).get("display_name") or ""
            return PullRequest(
                provider="bitbucket",
                repo=f"{ws}/{name}",
                number=pr_number,
                title=str(meta.get("title") or ""),
                description=markers.reveal(str(meta.get("description") or "")),
                author=str(author_user),
                base_ref=str((destination.get("branch") or {}).get("name") or ""),
                base_sha=base_sha,
                head_ref=str((source.get("branch") or {}).get("name") or ""),
                head_sha=head_sha,
                state=str(meta.get("state") or "OPEN").lower(),
                is_draft=bool(meta.get("draft", False)),
                url=str((meta.get("links") or {}).get("html", {}).get("href") or ""),
                hunks=hunks,
                raw_diff=raw_diff,
                skipped_files=skipped_files,
                reported_files=reported,
            )

        # 2. Raw diff — the endpoint redirects, see _get_diff
        try:
            raw_diff, reported_files = self._fetch_raw_diff(
                ws, name, pr_number, head_sha=head_sha, base_sha=base_sha,
            )
        except EmptyDiffError as exc:
            exc.pr = build("", exc.files)
            raise
        return build(raw_diff, reported_files)

    # ─── The diff, which Bitbucket answers with a redirect ───────

    def _get_follow(
        self, url: str, *, headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET `url`, following at most three redirects — to this API only.

        The client is built with follow_redirects=False on purpose (a client
        that follows by itself can be walked off the allowlist), and
        `/pullrequests/{id}/diff` answers with a 302 and an EMPTY body. So the
        hops are taken here, one at a time, each checked: same scheme, same
        host as BITBUCKET_API_BASE, and a Location that is present. The token
        therefore never goes anywhere the base URL does not. A redirect that
        is still a redirect after the last hop is an error, not a response.
        """
        base = urlsplit(BITBUCKET_API_BASE)
        for hop in range(_MAX_REDIRECTS + 1):
            try:
                resp = self._http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                # A timeout or a reset is the provider failing, not a bug of
                # ours: callers catch one error type.
                raise PullRequestProviderError(
                    f"Bitbucket request failed ({type(exc).__name__})") from exc
            if resp.status_code not in _REDIRECTS:
                return resp
            location = resp.headers.get("location") or ""
            if not location:
                raise PullRequestProviderError(
                    f"Bitbucket answered {resp.status_code} with no Location"
                )
            target = urljoin(url, location)
            parts = urlsplit(target)
            if (parts.scheme, parts.hostname) != (base.scheme, base.hostname):
                raise PullRequestProviderError(
                    f"Bitbucket redirected to another host ({parts.hostname}); "
                    f"refusing to follow it"
                )
            if hop == _MAX_REDIRECTS:
                raise PullRequestProviderError(
                    f"Bitbucket redirected more than {_MAX_REDIRECTS} times"
                )
            url = target
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_diff(self, ws: str, name: str, pr_number: int) -> str:
        """The PR's raw unified diff ("" when Bitbucket sends none)."""
        resp = self._get_follow(
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}/diff",
            headers={"Accept": "text/plain, */*"},
        )
        self._expect_ok(resp, "Bitbucket diff")
        # bytes decoded here, not `.text`: a text/plain answer without a
        # charset would be guessed as latin-1 and turn Cyrillic into mojibake
        return resp.content.decode("utf-8", "replace")

    def _get_spec_diff(
        self, ws: str, name: str, head_sha: str, base_sha: str,
    ) -> str:
        """The same diff asked for by its commits: `diff/{head}..{base}`.

        What the 302 points at (newest hash first, `topic=true` = the merge-base
        diff a PR shows). A fallback for an empty answer, never the first try:
        the redirect also covers merged and declined PRs, whose hashes move.
        """
        resp = self._get_follow(
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/diff/"
            f"{head_sha}..{base_sha}?topic=true",
            headers={"Accept": "text/plain, */*"},
        )
        self._expect_ok(resp, "Bitbucket diff")
        return resp.content.decode("utf-8", "replace")

    def _get_diffstat_files(self, ws: str, name: str, pr_number: int) -> int | None:
        """How many files Bitbucket says the PR changes; None = could not ask.

        An answer, even 0, is a fact; a failure to get one is not, and must not
        turn into "0 files" — that would put the quiet skip back.
        """
        try:
            resp = self._get_follow(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/"
                f"{pr_number}/diffstat?pagelen=100",
            )
            self._expect_ok(resp, "Bitbucket diffstat")
            body = resp.json()
        except (PullRequestProviderError, httpx.HTTPError, ValueError) as exc:
            logger.warning("bitbucket_diffstat_failed pr=%s error=%s", pr_number, exc)
            return None
        if not isinstance(body, dict):
            return None
        size = _count(body.get("size"))
        if size is not None:
            return size
        values = body.get("values")
        return len(values) if isinstance(values, list) else None

    def _fetch_raw_diff(
        self, ws: str, name: str, pr_number: int, *, head_sha: str, base_sha: str,
    ) -> tuple[str, int | None]:
        """(raw diff, files the PR is reported to change).

        A non-empty diff returns at once with the count unknown (None): the
        extra request would only buy a number nobody reads. An EMPTY diff is
        the case that matters. Bitbucket's diffstat then says whether that is
        true (0 files: a quiet skip, as before) or a lost diff (N > 0 files, or
        a diffstat that could not be read: retry — by commits first, then the
        plain endpoint after 2 s and 5 s — and finally an error, so the run
        fails where someone can see it instead of posting "no diff" on a PR
        with eight files in it).
        """
        raw_diff = self._get_diff(ws, name, pr_number)
        if raw_diff.strip():
            return raw_diff, None
        reported = self._get_diffstat_files(ws, name, pr_number)
        if reported == 0:
            return raw_diff, reported
        logger.warning(
            "bitbucket_empty_diff pr=%s diffstat_files=%s", pr_number, reported,
        )
        if head_sha and base_sha:
            try:
                raw_diff = self._get_spec_diff(ws, name, head_sha, base_sha)
            except PullRequestProviderError as exc:
                logger.warning("bitbucket_spec_diff_failed pr=%s error=%s",
                               pr_number, type(exc).__name__)
            if raw_diff.strip():
                return raw_diff, reported
        # `reported` is None when the diffstat could not be read: nothing then
        # shows the empty diff to be true, so it is not taken for an empty PR.
        # It gets the same retries, and an error where someone can see it.
        for delay in _EMPTY_DIFF_BACKOFF:
            time.sleep(delay)
            try:
                raw_diff = self._get_diff(ws, name, pr_number)
            except PullRequestProviderError as exc:
                logger.warning("bitbucket_diff_retry_failed pr=%s error=%s",
                               pr_number, type(exc).__name__)
                continue
            if raw_diff.strip():
                return raw_diff, reported
        if reported is None:
            raise EmptyDiffError(
                "Bitbucket returned an empty diff and its diffstat could not be read",
                files=None,
            )
        raise EmptyDiffError(
            f"Bitbucket returned an empty diff for a PR whose diffstat lists "
            f"{reported} file{'' if reported == 1 else 's'}",
            files=reported,
        )

    # ─── Post review ─────────────────────────────────────────────

    def post_review(
        self, batch: ReviewBatch, *, dry_run: bool = False,
    ) -> dict:
        pr = batch.pull_request
        if pr.provider != "bitbucket":
            raise PullRequestProviderError(
                f"Bitbucket provider received non-bitbucket PR: {pr.provider}"
            )
        ws, name = self._split_repo(pr.repo)
        settings = get_review_settings()

        if dry_run:
            return {
                "dry_run": True,
                "findings": len(batch.findings),
                "verdict": batch.verdict.value,
            }

        # 1. Read what the previous review left — summary AND inline.
        #
        # Only the summary was deleted at first, so each push added a fresh set
        # of inline comments on top of the last: pullrequest:updated fires on
        # every push, and the queue dedup key only blocks jobs still pending,
        # so five pushes meant five full sets. Listed HERE, before the new set
        # goes up, so the ids cannot include the comments we are about to post;
        # deleted at step 3, after they are up — the deletes used to run first,
        # so a POST that then failed left the PR stripped bare instead of
        # holding the previous review.
        stale: list[tuple[str, int]] = []
        protected: set[int] = set()
        listing_complete = True
        # An incremental review (only the new commits were read) keeps every
        # earlier inline comment: they are still about code the PR carries,
        # and the summary below is rewritten in place as before.
        incremental = begin_incremental_post(self, batch, settings.comment_marker)
        if settings.replace_on_synchronize:
            stale, protected, listing_complete = self._marked_comments(
                ws, name, pr.number, settings.comment_marker,
            )
            if incremental is not None:
                stale = [x for x in stale if x[0] == _SUMMARY_COMMENT]

        # The oldest marked summary is the one comment worth keeping: it is
        # PUT in place (step 4), like GitHub's PATCH and GitLab's PUT, so its
        # id survives re-runs. Chosen here, before the protected set is
        # consulted, on the GitLab decision: a summary a human replied to is
        # still the upsert target, because a PUT preserves the thread — only
        # a DELETE orphans it. Until this existed, every run re-POSTed the
        # summary (new id each push), and a replied-to summary was kept AND
        # joined by a fresh one — two summaries visible on the PR.
        keep_summary_id = next(
            (cid for kind, cid in stale if kind == _SUMMARY_COMMENT), None,
        )
        # The lifecycle comment this run already posted ("🔄 reviewing…") is
        # the summary — rewritten in place, never joined by a second one.
        if self._status_comment_id is not None:
            keep_summary_id = self._status_comment_id

        # 2. Inline comments (capped)
        #
        # Same anchor snapping as the other two providers. Bitbucket posts per
        # finding, so an unanchorable line costs one comment rather than the
        # batch — quiet enough that it was never noticed here.
        ranges = _anchorable_ranges(pr)
        new_side = _new_side_text(pr)
        posted = 0
        failed = 0
        snapped = 0
        refused: list[Finding] = []
        inline_comments: list[PostedComment] = []
        comments_url = (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{pr.number}/comments"
        )
        for finding in batch.inline_findings(
            settings.max_inline_comments, skip=incremental_skip(incremental),
        ):
            side = "RIGHT" if finding.side == HunkSide.RIGHT else "LEFT"
            line = _snap_to_span(
                finding.line, ranges.get((finding.file_path, side), []))
            if line != finding.line:
                snapped += 1
                logger.info(
                    "bitbucket_anchor_snapped repo=%s pr=%d path=%s from=%d to=%d",
                    pr.repo, pr.number, finding.file_path, finding.line, line,
                )
            raw_body = _format_finding_body(
                finding, settings.comment_marker,
                original=_original_lines(new_side, finding),
                head_sha=pr.head_sha,
            )
            inline: dict[str, Any] = {"path": finding.file_path}
            if finding.side == HunkSide.LEFT:
                # A deleted line has an old side only.
                inline["from"] = line
            else:
                inline["to"] = line
                # An unchanged line has both sides, and Bitbucket anchors it
                # reliably only when both are named; an added line has `to` alone.
                old_line = _old_line_for(pr, finding.file_path, line)
                if old_line is not None:
                    inline["from"] = old_line
            payload: dict[str, Any] = {
                "content": {"raw": self._outbound(raw_body)},
                "inline": inline,
            }

            resp = self._post_comment(comments_url, payload)
            if resp.status_code in (200, 201):
                posted += 1
                if self._marker_shown(resp, raw_body):
                    self._rewrite_hidden(comments_url, resp, raw_body)
                cid = self._created_id(resp)
                if cid is not None:
                    short, full = finding_fingerprint(finding)
                    inline_comments.append(PostedComment(
                        comment_id=cid, path=finding.file_path, line=line,
                        fingerprint=short, finding_key=full))
            else:
                failed += 1
                refused.append(finding)
                logger.warning(
                    "bitbucket_inline_comment_failed status=%d body=%s",
                    resp.status_code, resp.text[:200],
                )
        # A comment Bitbucket would not place is not a finding lost: it joins
        # the summary, which is composed below, after this loop. An
        # incremental run also resolves the threads its commits made outdated
        # and puts its banner in the summary.
        incremental_response = finish_incremental_post(
            self, batch, incremental, posted=posted, refused=refused)
        if refused and incremental is None:
            batch.add_section(
                "unanchored", _format_unanchored(refused, pr, batch.review_language),
                order=900, targets={"comment"},
            )

        # 3. Drop the previous run's comments, now that this run's are up
        cleanup = self._delete_stale(
            ws, name, pr.number, stale, keep_summary_id=keep_summary_id,
            protected=protected, listing_complete=listing_complete,
        )

        # 4. Top-level summary — rewritten in place, never re-posted
        summary_id = self._upsert_summary(
            ws, name, pr.number,
            _format_summary(batch, marker=settings.comment_marker),
            keep_summary_id,
        )
        self._status_comment_id = None

        # 5. Approve / request changes — and take back whichever an earlier
        #    run gave that this one does not. Only for a repository that lets
        #    reviews approve or block at all.
        review_state: dict[str, Any] = {}
        if batch.pr_actions.manages_review_state:
            review_state = self._apply_review_state(
                ws, name, pr.number, review_decision(batch),
            )

        response: dict[str, Any] = {
            "summary_comment_id": summary_id,
            "inline_posted": posted,
            "inline_failed": failed,
            # `complete: False` means duplicates may still be on the PR — a
            # caller that reports "review posted" can say so rather than let a
            # half-done cleanup look like a finished one.
            "cleanup": cleanup,
            "review_state": review_state,
            # What this run created, for the learning loop to follow.
            "inline_comments": inline_comments,
            **incremental_response,
        }
        # Every write above is per comment and only LOGGED on failure, so a
        # run could come back "posted" with nothing on the pull request. The
        # summary is the one comment that carries the verdict here (Bitbucket
        # has no review object), so its loss is a delivery failure: `error`
        # is what the run record's `post_error` reads.
        if summary_id is None:
            why = (
                f"Bitbucket refused the summary comment (HTTP {self._last_summary_status})"
                if self._last_summary_status else
                "the Bitbucket summary comment could not be written"
            )
            logger.error(
                "bitbucket_summary_not_delivered repo=%s pr=%d inline_posted=%d "
                "inline_failed=%d status=%s",
                pr.repo, pr.number, posted, failed, self._last_summary_status or "-",
            )
            response["summary_error"] = why
            response["error"] = why
        if failed:
            response["inline_error"] = (
                f"{failed} of {posted + failed} inline comment(s) were refused"
            )
            logger.warning(
                "bitbucket_inline_partial repo=%s pr=%d posted=%d failed=%d",
                pr.repo, pr.number, posted, failed,
            )
        return response

    @staticmethod
    def _created_id(resp: httpx.Response) -> int | None:
        """The id of the comment a POST just created, None when unreadable."""
        try:
            body = resp.json()
        except ValueError:
            return None
        cid = body.get("id") if isinstance(body, dict) else None
        return cid if isinstance(cid, int) else None

    # ─── Incremental review: commits, two-commit diff, threads ───

    def list_pr_commits(self, repo: str, pr_number: int) -> list[CommitInfo] | None:
        """The PR's commits with their parents (newest first, as Bitbucket
        lists them). None when a page cannot be read or the list is longer
        than `MAX_LISTED_COMMITS`: a cut-short list proves nothing."""
        ws, name = self._split_repo(repo)
        url: str | None = (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{pr_number}/commits?pagelen=50"
        )
        out: list[CommitInfo] = []
        seen: set[str] = set()
        try:
            while url:
                if url in seen:
                    return None
                seen.add(url)
                resp = self._get_follow(url)
                self._expect_ok(resp, "Bitbucket commits")
                body = resp.json()
                if not isinstance(body, dict):
                    return None
                for item in body.get("values") or []:
                    if not isinstance(item, dict) or not item.get("hash"):
                        return None
                    parents = tuple(
                        str(p.get("hash")) for p in item.get("parents") or []
                        if isinstance(p, dict) and p.get("hash"))
                    out.append(CommitInfo(
                        sha=str(item["hash"]), parents=parents,
                        message=str(item.get("message") or ""),
                        committed_at=item.get("date")))
                if len(out) > MAX_LISTED_COMMITS:
                    return None
                nxt = body.get("next")
                url = nxt if isinstance(nxt, str) and nxt else None
        except (PullRequestProviderError, httpx.HTTPError, ValueError) as exc:
            logger.warning("bitbucket_commits_failed pr=%s error=%s", pr_number, exc)
            return None
        return out

    def fetch_incremental_diff(
        self, repo: str, pr_number: int, base_sha: str, head_sha: str,
    ) -> str | None:
        """The straight diff from `base_sha` to `head_sha` (two commits, no
        merge base: `merge=false`). Any failure, a redirect that is not
        followed included, is None, and the caller reviews the whole PR."""
        ws, name = self._split_repo(repo)
        try:
            resp = self._get_follow(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/diff/"
                f"{head_sha}..{base_sha}?merge=false",
                headers={"Accept": "text/plain, */*"},
            )
            self._expect_ok(resp, "Bitbucket diff")
        except (PullRequestProviderError, httpx.HTTPError) as exc:
            logger.warning("bitbucket_incremental_diff_failed pr=%s error=%s", pr_number, exc)
            return None
        return resp.content.decode("utf-8", "replace")

    def our_inline_threads(self, pr: PullRequest, marker: str) -> list[OurThread] | None:
        """Our root inline comments, with their fingerprint and resolution.
        Marker AND author, like every cleanup here; None when the listing is
        not complete (deduping against half a list would post duplicates)."""
        ws, name = self._split_repo(pr.repo)
        comments, complete = self._list_comments(ws, name, pr.number)
        viewer = self._viewer_ids()
        if not complete or not viewer:
            return None
        replied: set[int] = set()
        for comment in comments:
            parent = comment.get("parent")
            root = parent.get("id") if isinstance(parent, dict) else None
            if isinstance(root, int) and not self._authored_by_viewer(comment, viewer):
                replied.add(root)
        threads: list[OurThread] = []
        for comment in comments:
            inline = comment.get("inline")
            cid = comment.get("id")
            if (not isinstance(inline, dict) or not isinstance(cid, int)
                    or comment.get("parent") or not self._is_ours(comment, marker, viewer)):
                continue
            content = comment.get("content")
            raw = content.get("raw") if isinstance(content, dict) else ""
            found = markers.parse_finding_marker(raw or "")
            line = inline.get("to") if isinstance(inline.get("to"), int) else inline.get("from")
            threads.append(OurThread(
                comment_id=cid, path=str(inline.get("path") or ""),
                line=line if isinstance(line, int) else None,
                side="RIGHT" if isinstance(inline.get("to"), int) else "LEFT",
                resolved=bool(comment.get("resolution")),
                fingerprint=found[0] if found else None,
                sha=found[1] if found else None,
                replied=cid in replied,
            ))
        return threads

    def resolve_threads(self, pr: PullRequest, threads: list[OurThread]) -> dict[str, int]:
        """Resolve each thread (`POST .../comments/{id}/resolve`). A 404 / 405 /
        501 means this Bitbucket cannot: the rest are left alone, counted as
        unsupported."""
        ws, name = self._split_repo(pr.repo)
        out = {"resolved": 0, "failed": 0, "unsupported": 0}
        unsupported = False
        for th in threads:
            if unsupported:
                out["unsupported"] += 1
                continue
            try:
                resp = self._http.post(
                    f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/"
                    f"{pr.number}/comments/{th.comment_id}/resolve")
            except httpx.HTTPError as exc:
                out["failed"] += 1
                logger.warning("bitbucket_resolve_error id=%s err=%s", th.comment_id, exc)
                continue
            if resp.status_code in (200, 201, 204, 409):
                out["resolved"] += 1  # 409: somebody resolved it first
            elif resp.status_code in (404, 405, 501):
                unsupported = True
                out["unsupported"] += 1
            else:
                out["failed"] += 1
                logger.warning("bitbucket_resolve_failed id=%s status=%d",
                               th.comment_id, resp.status_code)
        return out

    # ─── Lifecycle comment ("🔄 reviewing…" → summary) ──────────

    def upsert_status_comment(
        self, pr: PullRequest, body: str, *, create: bool = True,
        only_if_in_progress: bool = False,
    ) -> int | None:
        """See `PullRequestProvider.upsert_status_comment`."""
        if pr.provider != "bitbucket":
            raise PullRequestProviderError(
                f"Bitbucket provider received non-bitbucket PR: {pr.provider}"
            )
        settings = get_review_settings()
        ws, name = self._split_repo(pr.repo)
        write, existing = self._status_target(
            pr, settings.comment_marker,
            replace=settings.replace_on_synchronize, create=create,
            only_if_in_progress=only_if_in_progress,
        )
        if not write:
            return None
        cid = self._upsert_summary(
            ws, name, pr.number,
            _with_marker(body, settings.comment_marker), existing,
        )
        if cid is not None:
            self._status_comment_id = cid
        return cid

    def _comment_marker(self) -> str:
        return get_review_settings().comment_marker

    def _write_top_level_comment(
        self, pr: PullRequest, body: str, existing_id: int | None,
    ) -> int | None:
        ws, name = self._split_repo(pr.repo)
        return self._upsert_summary(ws, name, pr.number, body, existing_id)

    # ─── Approve / request changes ──────────────────────────────

    def _my_participant_state(self, ws: str, name: str, pr_number: int) -> str | None:
        """'approved' | 'changes_requested' | '' (neither) | None (unknown).

        Read off the PR's `participants`, matched on the immutable uuid /
        account_id this token posts as — the same proof of identity the
        comment cleanup uses.
        """
        viewer = self._viewer_ids()
        if not viewer:
            return None
        try:
            resp = self._http.get(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}"
            )
            if resp.status_code >= 400:
                return None
            meta = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        for part in meta.get("participants") or []:
            if not isinstance(part, dict) or not self._authored_by_viewer(part, viewer):
                continue
            state = str(part.get("state") or "")
            if state in ("approved", "changes_requested"):
                return state
            return "approved" if part.get("approved") is True else ""
        return ""

    def _apply_review_state(
        self, ws: str, name: str, pr_number: int, decision: str | None,
    ) -> dict[str, Any]:
        """Bring our participant state in line with `decision`. Never raises.

        approve → POST /approve (lifting a block of ours first);
        request_changes → POST /request-changes (withdrawing our approval
        first); neither → DELETE whichever of the two we hold. The current
        state is read first so a re-run sends nothing that is already true;
        when it cannot be read every needed call is sent and a 404 on a
        DELETE — nothing of ours to take back — counts as done.
        """
        state = self._my_participant_state(ws, name, pr_number)
        base = f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}"
        want = {APPROVE: "approved", REQUEST_CHANGES: "changes_requested"}.get(
            decision or "", "")
        calls: list[tuple[str, str, str]] = []
        for held, path in (("approved", "approve"), ("changes_requested", "request-changes")):
            if held != want and state in (held, None):
                calls.append(("DELETE", path, f"withdrew_{path}"))
        if want and state != want:
            path = "approve" if want == "approved" else "request-changes"
            calls.append(("POST", path, path))
        done: list[str] = []
        failed: list[str] = []
        for method, path, label in calls:
            try:
                resp = self._http.request(method, f"{base}/{path}")
            except httpx.HTTPError as exc:
                failed.append(label)
                logger.warning("bitbucket_review_state_error call=%s err=%s", label, exc)
                continue
            if resp.status_code < 400:
                done.append(label)
            elif method == "DELETE" and resp.status_code == 404:
                pass  # nothing of ours to take back — the state we wanted
            else:
                failed.append(label)
                logger.warning("bitbucket_review_state_failed call=%s status=%d body=%s",
                               label, resp.status_code, resp.text[:200])
        return {"state": want or "none", "done": done, "failed": failed}

    # ─── Commit messages (where teams also write the task key) ───

    def fetch_commit_messages(self, pr: PullRequest, limit: int = 50) -> list[str]:
        """GET /pullrequests/{id}/commits — newest first, at most `limit`.
        Never raises; [] when Bitbucket cannot say."""
        ws, name = self._split_repo(pr.repo)
        try:
            resp = self._get_follow(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/"
                f"{pr.number}/commits?pagelen={max(1, min(int(limit), 100))}",
            )
            self._expect_ok(resp, "Bitbucket commits")
            body = resp.json()
        except (PullRequestProviderError, httpx.HTTPError, ValueError) as exc:
            logger.info("bitbucket_commits_failed pr=%s error=%s", pr.number,
                        type(exc).__name__)
            return []
        values = body.get("values") if isinstance(body, dict) else None
        return [str(c.get("message") or "") for c in values or []
                if isinstance(c, dict) and c.get("message")][:limit]

    # ─── The summary in the pull request description ────────────

    def update_description(self, pr: PullRequest, transform) -> dict:
        """GET the PR, PUT its description with `transform(description)`.

        The PUT carries the title, the reviewers, the draft flag and the
        close-source-branch flag back as read:
        Bitbucket's PR update treats the body as the new state, and a PUT
        without `reviewers` has been known to drop them.
        """
        ws, name = self._split_repo(pr.repo)
        url = f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr.number}"
        try:
            resp = self._http.get(url)
            if resp.status_code >= 400:
                return {"written": False, "error": f"Bitbucket refused the read (HTTP {resp.status_code})"}
            meta = resp.json()
            if not isinstance(meta, dict):
                return {"written": False, "error": "unreadable pull request"}
            stored = str(meta.get("description") or "")
            # The block is read the way it is written in code: HTML markers.
            current = markers.reveal(stored)
            new = transform(current)
            if new is None:
                return {"written": False, "error": None, "unchanged": True}
            outbound = self._outbound(new, self.description_max_chars)
            if outbound == stored:
                return {"written": False, "error": None, "unchanged": True}
            payload: dict[str, Any] = {"description": outbound}
            if meta.get("title"):
                payload["title"] = meta["title"]
            # The body of a PR update is the new state: whatever it leaves out
            # may be reset, so the settings the PR already had go back as read.
            for key in ("close_source_branch", "draft"):
                if isinstance(meta.get(key), bool):
                    payload[key] = meta[key]
            reviewers = [
                {"uuid": r["uuid"]} for r in meta.get("reviewers") or []
                if isinstance(r, dict) and r.get("uuid")
            ]
            if reviewers:
                payload["reviewers"] = reviewers
            resp = self._http.put(url, json=payload)
            if resp.status_code < 400 and self._marker_shown(resp, new):
                payload["description"] = self._outbound(new, self.description_max_chars)
                resp = self._http.put(url, json=payload)
        except (httpx.HTTPError, ValueError) as exc:
            return {"written": False, "error": type(exc).__name__}
        if resp.status_code >= 400:
            logger.warning("bitbucket_description_put_failed status=%d", resp.status_code)
            return {"written": False, "error": f"Bitbucket refused the description (HTTP {resp.status_code})"}
        return {"written": True, "error": None}

    def _our_summary_comments(
        self, pr: PullRequest, marker: str,
    ) -> list[tuple[int, str]]:
        ws, name = self._split_repo(pr.repo)
        comments, _ = self._list_comments(ws, name, pr.number)
        viewer = self._viewer_ids()
        if not viewer:
            return []  # fail closed — see `_viewer_ids`
        out: list[tuple[int, str]] = []
        for comment in comments:
            cid = comment.get("id")
            if (comment.get("inline") or not isinstance(cid, int)
                    or not self._is_ours(comment, marker, viewer)):
                continue
            content = comment.get("content")
            raw = content.get("raw") if isinstance(content, dict) else ""
            out.append((cid, markers.reveal(str(raw or ""))))
        return out

    # ─── Idempotency: what a previous run left behind ────────────

    def find_existing_review_comment(
        self, repo: str, pr_number: int, marker: str,
    ) -> int | None:
        """The summary comment to update in place — oldest marked one of OURS.

        Authorship is part of the predicate for the same reason it is in the
        delete path: "Quote reply" copies the quoted comment's raw markdown,
        marker included, so a human rebuttal carries the marker too — and the
        id this returns is one a caller may PUT a new body into. Inline
        comments are not eligible either: writing a summary over a comment
        anchored to line 42 of some file is not an update, it is vandalism
        (the same rule GitLab states on its own lookup).
        """
        ws, name = self._split_repo(repo)
        ours, _, _ = self._marked_comments(ws, name, pr_number, marker)
        for kind, cid in ours:
            if kind == _SUMMARY_COMMENT:
                return cid
        return None

    def find_marked_comment_ids(
        self, repo: str, pr_number: int, marker: str,
    ) -> list[int]:
        """Every comment of ours on this PR — summary and inline alike."""
        ws, name = self._split_repo(repo)
        ours, _, _ = self._marked_comments(ws, name, pr_number, marker)
        return [cid for _, cid in ours]

    def _comments_url(self, ws: str, name: str, pr_number: int) -> str:
        return (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{pr_number}/comments?pagelen=100&q=deleted=false"
        )

    def _list_comments(
        self, ws: str, name: str, pr_number: int,
    ) -> tuple[list[dict], bool]:
        """Every comment on the PR, and whether the listing finished.

        The bool is False when a page could not be read. This listing used to
        swallow any >=400 with a bare `break` and hand back whatever it had;
        the caller deleted that partial set and reported nothing, so a
        half-read page one looked exactly like a finished cleanup — and a busy
        PR runs past one page of 100 comments routinely.
        """
        found: list[dict] = []
        seen: set[str] = set()
        url: str | None = self._comments_url(ws, name, pr_number)
        while url:
            if url in seen:
                # A proxy that echoes back the same `next` link would spin here
                # forever. Stop, and admit the listing is partial.
                logger.warning("bitbucket_list_comments_loop url=%s", url)
                return found, False
            seen.add(url)
            try:
                resp = self._http.get(url)
                if resp.status_code >= 400:
                    logger.warning(
                        "bitbucket_list_comments_failed status=%d", resp.status_code,
                    )
                    return found, False
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("bitbucket_list_comments_error err=%s", exc)
                return found, False
            if not isinstance(data, dict):
                logger.warning("bitbucket_list_comments_not_an_object")
                return found, False
            found.extend(c for c in data.get("values") or [] if isinstance(c, dict))
            nxt = data.get("next")
            url = nxt if isinstance(nxt, str) and nxt else None
        return found, True

    def _marked_comments(
        self, ws: str, name: str, pr_number: int, marker: str,
    ) -> tuple[list[tuple[str, int]], set[int], bool]:
        """Our comments as (kind, id), the protected roots, listing health.

        Matching is `marker` AND authorship, exactly as in the GitHub/GitLab
        providers — this one shipped with the marker alone, and "Quote reply"
        copies the quoted comment's RAW markdown, marker included, so a
        reviewer who quoted a finding to argue with it held our marker in
        their own comment and would have lost it on the next push.

        `protected` holds every id that appears as the `parent` of a comment
        not provably ours: deleting such a root tears the human's reply out of
        its context. Proof of ours is what LIFTS protection — a reply whose
        author cannot be read protects its root too.
        """
        comments, complete = self._list_comments(ws, name, pr_number)
        viewer = self._viewer_ids()
        if not viewer:
            # Fail closed. Without knowing who we are, the marker alone is not
            # proof of authorship — see `_viewer_ids`.
            return [], set(), False
        protected: set[int] = set()
        for comment in comments:
            parent = comment.get("parent")
            root = parent.get("id") if isinstance(parent, dict) else None
            content = comment.get("content")
            raw = content.get("raw") if isinstance(content, dict) else ""
            # A comment from our own account with none of our markers is a
            # person speaking through the token (a single-token install): it
            # protects its root like any other human's.
            if isinstance(root, int) and (
                not self._authored_by_viewer(comment, viewer)
                or not markers.is_bot_text(raw or "")
            ):
                protected.add(root)
        ours: list[tuple[str, int]] = []
        for comment in comments:
            if not self._is_ours(comment, marker, viewer):
                continue
            cid = comment.get("id")
            if isinstance(cid, int):
                kind = _INLINE_COMMENT if comment.get("inline") else _SUMMARY_COMMENT
                ours.append((kind, cid))
        return ours, protected, complete

    def _delete_stale(
        self,
        ws: str,
        name: str,
        pr_number: int,
        stale: list[tuple[str, int]],
        *,
        keep_summary_id: int | None,
        protected: set[int],
        listing_complete: bool,
    ) -> dict[str, Any]:
        """Delete the previous run's comments, minus the summary; never raise.

        `keep_summary_id` — the oldest marked summary — is NOT deleted: the
        caller PUTs the new summary into it, which keeps its id (and any
        thread hanging off it) across a re-run. Checked before `protected`,
        deliberately: a replied-to summary is still the upsert target, not a
        kept_threaded casualty — a PUT preserves the thread, only a DELETE
        orphans it. Surplus summaries, the ones the old post-every-run left
        behind, go the way of the inline comments.

        A review with duplicate comments beats no review, so a refused delete
        is logged, counted, and surfaced in the returned stats. Two kinds of
        id are not deleted at all: a root some other voice replied to (counted
        in `kept_threaded` — a decision, not a failure, so it does not turn
        `complete` off), and anything found by a listing that did not finish,
        because the unread pages may hold exactly the reply that would have
        protected it.
        """
        deleted = 0
        failed = 0
        kept_threaded = 0
        for kind, cid in stale:
            if kind == _SUMMARY_COMMENT and cid == keep_summary_id:
                continue
            if cid in protected:
                kept_threaded += 1
                continue
            if not listing_complete:
                continue
            try:
                resp = self._http.delete(
                    f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
                    f"/pullrequests/{pr_number}/comments/{cid}"
                )
            except httpx.HTTPError as exc:
                failed += 1
                logger.warning(
                    "bitbucket_delete_comment_error id=%d err=%s", cid, exc,
                )
                continue
            # 404 — somebody deleted it first, which is the state we wanted.
            if resp.status_code in (200, 204, 404):
                deleted += 1
            else:
                failed += 1
                logger.warning(
                    "bitbucket_delete_comment_failed id=%d status=%d body=%s",
                    cid, resp.status_code, resp.text[:100],
                )
        return {
            "deleted": deleted,
            "failed": failed,
            "kept_threaded": kept_threaded,
            "complete": listing_complete and failed == 0,
        }

    def _outbound(self, text: str, limit: int | None = None) -> str:
        """`text` as Bitbucket must receive it: markdown only, markers hidden.

        Raw HTML (`<sub>`, `<details>`) is shown as text by Bitbucket, and an
        HTML-comment marker with it. The cap keeps every marker whole.
        """
        return markers.fit(
            markers.bitbucket_flavour(text), limit or self.comment_max_chars,
        )

    @staticmethod
    def _rendered_html(data: object) -> str:
        """The HTML Bitbucket rendered for what it just stored (comment or description)."""
        if not isinstance(data, dict):
            return ""
        content = data.get("content")
        if isinstance(content, dict) and content.get("html"):
            return str(content["html"])
        rendered = data.get("rendered")
        if isinstance(rendered, dict):
            desc = rendered.get("description")
            if isinstance(desc, dict) and desc.get("html"):
                return str(desc["html"])
        return ""

    def _marker_shown(self, resp: httpx.Response, sent: str) -> bool:
        """Did Bitbucket render a marker we sent as visible text?

        Checked on the write's own answer, which carries the rendered HTML. The
        first time it is true, the process falls back to zero-width markers
        and the caller writes the text once more.
        """
        if markers.marker_style() in ("html", "zwsp"):
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        if not markers.leaks_marker(self._rendered_html(data), sent):
            return False
        logger.warning("bitbucket_marker_visible style=%s", markers.marker_style())
        markers.fall_back_to_zwsp()
        return True

    def _rewrite_hidden(self, url: str, resp: httpx.Response, raw: str) -> None:
        """Put `raw` again, markers hidden the fallback way, into the comment `resp` created."""
        try:
            cid = resp.json().get("id")
        except ValueError:
            return
        if isinstance(cid, int):
            self._http.put(f"{url}/{cid}", json={"content": {"raw": self._outbound(raw)}})

    def _post_comment(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """POST a comment; on 429 wait the time Bitbucket names (at most
        `_MAX_RETRY_AFTER` seconds) and try once more.

        A review posts one request per finding, which is exactly the shape that
        trips the rate limit; without the wait every comment after the limit
        was lost for the whole review.
        """
        resp = self._http.post(url, json=payload)
        if resp.status_code != 429:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = _DEFAULT_RETRY_AFTER
        if not math.isfinite(wait):
            wait = _DEFAULT_RETRY_AFTER
        wait = min(max(wait, 0.0), _MAX_RETRY_AFTER)
        logger.warning("bitbucket_rate_limited url=%s wait=%.1f", url, wait)
        time.sleep(wait)
        return self._http.post(url, json=payload)

    def _upsert_summary(
        self, ws: str, name: str, pr_number: int, body: str,
        existing_id: int | None,
    ) -> int | None:
        """PUT the summary we already own, or POST the first one.

        Qodo's persistent-comment pattern, as GitHub's PATCH and GitLab's PUT
        already do it. Bitbucket used to re-POST the summary every run: a link
        to it from a ticket stopped resolving after the next push, and a
        summary a human had replied to was kept (protected) while a fresh one
        was posted anyway — two summaries visible at once.
        """
        url_base = (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{pr_number}/comments"
        )
        payload = {"content": {"raw": self._outbound(body)}}
        self._last_summary_status = None
        if existing_id is not None:
            resp = self._http.put(f"{url_base}/{existing_id}", json=payload)
            if resp.status_code in (200, 201):
                if self._marker_shown(resp, body):
                    self._http.put(
                        f"{url_base}/{existing_id}",
                        json={"content": {"raw": self._outbound(body)}},
                    )
                return existing_id
            logger.warning(
                "bitbucket_summary_put_failed id=%d status=%d — posting a new one",
                existing_id, resp.status_code,
            )
        resp = self._http.post(url_base, json=payload)
        if resp.status_code in (200, 201):
            if self._marker_shown(resp, body):
                self._rewrite_hidden(url_base, resp, body)
            cid = resp.json().get("id")
            return cid if isinstance(cid, int) else None
        self._last_summary_status = resp.status_code
        logger.warning("bitbucket_summary_post_failed status=%d", resp.status_code)
        return None

    #: HTTP status of the last summary write that failed, for the run record.
    _last_summary_status: int | None = None

    def _viewer_ids(self) -> frozenset[str]:
        """The stable ids this token posts as, cached for the provider's life.

        Bitbucket's /2.0/user and the `user` object on every comment share
        `uuid` and `account_id`, and both are immutable. The `nickname` they
        also share is NOT — an account rename would silently turn all of our
        own comments into someone else's — so it takes no part in the match.
        An empty set means the lookup failed, and nothing may be deleted.
        """
        if self._viewer_cache is not None:
            return self._viewer_cache
        ids: frozenset[str] = frozenset()
        try:
            resp = self._http.get(f"{BITBUCKET_API_BASE}/user")
            if resp.status_code < 400:
                # A 200 whose body is not an object — a captive proxy's page —
                # is not an identity. Neither is one with no stable id in it.
                body = resp.json()
                if isinstance(body, dict):
                    ids = frozenset(
                        str(v) for v in (body.get("uuid"), body.get("account_id")) if v
                    )
                if not ids:
                    logger.warning("bitbucket_viewer_lookup_anonymous")
            else:
                logger.warning(
                    "bitbucket_viewer_lookup_failed status=%d", resp.status_code,
                )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("bitbucket_viewer_lookup_error err=%s", exc)
        self._viewer_cache = ids
        return ids

    @staticmethod
    def _authored_by_viewer(comment: dict, viewer: frozenset[str]) -> bool:
        """Provably written by this token — a malformed author is a no.

        `user` arrives as a plain string on a truncated or proxied payload;
        the GitHub twin of this check crashed the whole review on exactly that
        shape, so the guard is an isinstance, not an `or {}`.
        """
        user = comment.get("user")
        if not isinstance(user, dict):
            return False
        author_ids = {str(v) for v in (user.get("uuid"), user.get("account_id")) if v}
        return bool(author_ids & viewer)

    @classmethod
    def _is_ours(cls, comment: dict, marker: str, viewer: frozenset[str]) -> bool:
        """Both conditions, never one: our marker AND our authorship.

        The marker alone identifies the SHAPE of the comment; the author
        identifies who wrote it. Only the pair identifies a comment this bot
        is entitled to delete.
        """
        content = comment.get("content")
        raw = content.get("raw") if isinstance(content, dict) else ""
        # `has_marker` reveals a hidden marker first and matches only a line of
        # its own, so a quote reply that copied the marker is not ours.
        if not markers.has_marker(raw or "", marker):
            return False
        return cls._authored_by_viewer(comment, viewer)

    # ─── Reading the target branch (the issues backlog) ──────────

    def _repo_api(self, repo: str) -> str:
        ws, name = self._split_repo(repo)
        return f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"

    def branch_head_sha(self, repo: str, branch: str) -> str:
        resp = self._get_follow(
            f"{self._repo_api(repo)}/refs/branches/{quote(branch, safe='/')}")
        self._expect_ok(resp, "Bitbucket branch")
        sha = str(((resp.json() or {}).get("target") or {}).get("hash") or "")
        if not sha:
            raise PullRequestProviderError(
                f"Bitbucket named no commit for branch {branch!r}")
        return sha

    def read_file_at(self, repo: str, ref: str, path: str) -> str | None:
        resp = self._get_follow(
            f"{self._repo_api(repo)}/src/{quote(ref, safe='')}/{quote(path, safe='/')}",
            headers={"Accept": "text/plain, */*"},
        )
        if resp.status_code == 404:
            return None
        self._expect_ok(resp, "Bitbucket file")
        if len(resp.content) > MAX_FILE_BYTES:
            raise PullRequestProviderError(
                f"{path} is larger than {MAX_FILE_BYTES} bytes; not read")
        return resp.content.decode("utf-8", "replace")

    def commits_touching(
        self, repo: str, ref: str, path: str, *,
        since: datetime | None = None, limit: int = 10,
    ) -> list[PathCommit]:
        resp = self._get_follow(
            f"{self._repo_api(repo)}/commits/{quote(ref, safe='')}"
            f"?path={quote(path, safe='')}&pagelen={max(1, min(int(limit), 100))}")
        self._expect_ok(resp, "Bitbucket commits")
        out: list[PathCommit] = []
        for c in (resp.json() or {}).get("values") or []:
            date = c.get("date")
            if not committed_since(date, since):
                continue
            out.append(PathCommit(
                sha=str(c.get("hash") or ""),
                subject=str(c.get("message") or "").strip().splitlines()[0][:300]
                if str(c.get("message") or "").strip() else "",
                date=date,
                url=((c.get("links") or {}).get("html") or {}).get("href"),
            ))
        return [c for c in out if c.sha][:limit]

    def file_change_in_commit(
        self, repo: str, sha: str, path: str,
    ) -> FileChange | None:
        url: str | None = f"{self._repo_api(repo)}/diffstat/{quote(sha, safe='')}?pagelen=100"
        for _ in range(5):
            if not url:
                break
            resp = self._get_follow(url)
            self._expect_ok(resp, "Bitbucket diffstat")
            body = resp.json() or {}
            for e in body.get("values") or []:
                old = ((e.get("old") or {}).get("path")) or None
                new = ((e.get("new") or {}).get("path")) or None
                if path not in (old, new):
                    continue
                status = str(e.get("status") or "modified")
                if status == "removed":
                    return FileChange("deleted", old or path)
                if status == "renamed" and old == path and new:
                    return FileChange("renamed", new, previous_path=old)
                return FileChange("added" if status == "added" else "modified",
                                  new or path)
            url = body.get("next")
        return None

    def commit_url(self, repo: str, sha: str) -> str | None:
        ws, name = self._split_repo(repo)
        return f"https://bitbucket.org/{ws}/{name}/commits/{sha}"

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.strip().split("/", 1)
        if len(parts) != 2:
            raise PullRequestProviderError(
                f"Invalid Bitbucket repo format '{repo}'. Expected 'workspace/name'."
            )
        return parts[0], parts[1]

    # ─── Conversation (comment commands) ─────────────────────────

    def viewer_ids(self) -> frozenset[str]:
        return self._viewer_ids()

    def post_reply(self, ev, body: str) -> str | None:
        """A reply under the comment that asked, as its child."""
        ws, name = self._split_repo(ev.repo)
        payload: dict[str, Any] = {
            "content": {"raw": self._outbound(markers.with_chat_marker(body))},
        }
        if str(ev.comment_id).isdigit():
            payload["parent"] = {"id": int(ev.comment_id)}
        url = (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{ev.pr_number}/comments"
        )
        resp = self._expect_ok(self._post_comment(url, payload), "bitbucket reply")
        cid = _comment_id(resp)
        return str(cid) if cid is not None else None

    def update_comment(
        self, repo: str, pr_number: int, comment_id: str, body: str, *, kind: str = "issue",
    ) -> bool:
        ws, name = self._split_repo(repo)
        url = (
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
            f"/pullrequests/{pr_number}/comments/{comment_id}"
        )
        resp = self._http.put(
            url, json={"content": {"raw": self._outbound(markers.with_chat_marker(body))}},
        )
        return 200 <= resp.status_code < 300

    def get_thread(self, ev, limit: int = 30) -> list[ThreadMessage]:
        """The comment's root and everything under it, oldest first.

        Bitbucket nests replies by `parent.id`; the thread is found by walking
        up from the comment that asked, then taking the root's descendants.
        """
        ws, name = self._split_repo(ev.repo)
        rows, _complete = self._list_comments(ws, name, ev.pr_number)
        by_id: dict[int, dict] = {c["id"]: c for c in rows if isinstance(c.get("id"), int)}
        if not str(ev.comment_id).isdigit() or int(ev.comment_id) not in by_id:
            return []

        def parent_of(c: dict) -> int | None:
            parent = c.get("parent")
            pid = parent.get("id") if isinstance(parent, dict) else None
            return pid if isinstance(pid, int) else None

        root = int(ev.comment_id)
        for _hop in range(len(by_id)):
            up = parent_of(by_id[root])
            if up is None or up not in by_id:
                break
            root = up
        members = {root}
        grew = True
        while grew:
            grew = False
            for cid, c in by_id.items():
                if cid not in members and parent_of(c) in members:
                    members.add(cid)
                    grew = True
        viewer = self._viewer_ids()
        found: list[ThreadMessage] = []
        for cid in sorted(members):
            c = by_id[cid]
            content = c.get("content")
            user = c.get("user")
            inline = c.get("inline")
            inline = inline if isinstance(inline, dict) else {}
            line = inline.get("to") if isinstance(inline.get("to"), int) else inline.get("from")
            found.append(ThreadMessage(
                comment_id=str(cid),
                author=str(user.get("nickname") or user.get("display_name") or "")
                if isinstance(user, dict) else "",
                text=str(content.get("raw") or "") if isinstance(content, dict) else "",
                ours=bool(viewer) and self._authored_by_viewer(c, viewer),
                path=inline.get("path") if isinstance(inline.get("path"), str) else None,
                line=line if isinstance(line, int) else None,
                created_at=c.get("created_on") if isinstance(c.get("created_on"), str) else None,
            ))
        return trim_thread(found, limit)

    def acknowledge(self, ev) -> bool:
        # Bitbucket Cloud has no reactions on comments: the caller posts a
        # short reply instead.
        return False

    def actor_permission(
        self, repo: str, *, actor_id: str = "", actor_name: str = "",
    ) -> str:
        # Repository permissions need an admin-scoped token; the participants
        # of the pull request are what this provider can prove.
        return "unknown"

    def pr_participants(self, repo: str, pr_number: int) -> frozenset[str]:
        ws, name = self._split_repo(repo)
        try:
            resp = self._http.get(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}",
            )
            if resp.status_code >= 400:
                return frozenset()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return frozenset()
        if not isinstance(data, dict):
            return frozenset()
        people: list[object] = [data.get("author")]
        people.extend(
            p for p in (data.get("reviewers") or []) if isinstance(p, (dict, str))
        )
        # `participants` lists everyone who commented or approved, a stranger
        # on a public repository included (the comment that fired the webhook
        # already exists), so only an entry with the REVIEWER role counts.
        people.extend(
            p.get("user") for p in (data.get("participants") or [])
            if isinstance(p, dict) and str(p.get("role") or "").upper() == "REVIEWER"
        )
        ids: set[str] = set()
        for person in people:
            if isinstance(person, dict):
                ids.update(
                    str(v) for v in (person.get("uuid"), person.get("account_id")) if v
                )
        return frozenset(ids)


def _comment_id(resp: httpx.Response) -> int | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    cid = body.get("id") if isinstance(body, dict) else None
    return cid if isinstance(cid, int) else None
