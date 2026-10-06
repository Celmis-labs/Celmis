"""GitHub PR provider — extends Phase 9 GitHubClient with PR endpoints + reviews.

API specifics (per research May 2026):
    GET /repos/{o}/{r}/pulls/{n}                         — PR metadata
    GET .../pulls/{n} (Accept: application/vnd.github.v3.diff) — raw diff
    POST .../pulls/{n}/reviews                           — batched review
    GET .../pulls/{n}/comments                           — inline comments (all reviews)
    DELETE .../pulls/comments/{cid}                      — delete one inline comment
    GET .../issues/{n}/comments                          — top-level comments
    POST .../issues/{n}/comments                         — top-level summary
    PATCH .../issues/comments/{cid}                      — update existing summary
    DELETE .../issues/comments/{cid}                     — delete a surplus summary
    GET .../pulls/{n}/reviews                            — reviews on the PR
    POST /graphql                                        — minimizeComment
    PUT .../pulls/{n}/reviews/{id}/dismissals            — withdraw our approval / block
    PATCH .../pulls/{n}                                  — the summary in the description

The review EVENT is COMMENT unless the repository asks for more (2.3.0): APPROVE
only with `approve_when_clean`, REQUEST_CHANGES only with
`request_changes_on_critical` — see `pr_actions.review_decision`. Until 2.3.0
the event followed the verdict, so every clean review approved and every
blocking one requested changes on its own.

Comment positioning (2026): line + side (RIGHT for new code, LEFT for deleted).
`position` parameter — DEPRECATED in API version 2026-03-10.

A comment must also anchor to a line the diff CARRIES, and the whole review is
validated as one object, so a single anchor outside every hunk 422s the batch.
`_snap_to_span` moves such an anchor onto the nearest covered line BEFORE the
POST rather than recovering from the refusal afterwards.

⚠️ A SUBMITTED review is immutable: GitHub offers no delete for it (the delete
endpoint covers PENDING reviews only, and a dismissal leaves the body on the
page). While the review body carried the same summary text as the issue
comment, every re-run stacked one more full copy onto the timeline — the one
duplication no cleanup here could reach. The review body is therefore a short
pointer now (see `_format_review_pointer`): the verdict, and a link to the
persistent issue comment, which IS updatable and holds the only full summary.

That shrank each entry but not their number: a dozen re-runs still leave a
dozen entries on the timeline. REST cannot remove them, but GraphQL can FOLD
them. Introspecting the schema —

    { __type(name: "Minimizable") { possibleTypes { name } } }
    -> CommitComment, DiscussionComment, GistComment, IssueComment,
       PullRequestReview, PullRequestReviewComment

— `PullRequestReview` is minimizable, so the previous runs' review bodies are
collapsed with `minimizeComment(classifier: OUTDATED)` once the new review is
up (`_minimize_reviews`). Superseded is exactly what they are.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from src.credentials import resolve_git_credential
from src.http import build_client
from src.review import markers
from src.review.diff import parse_unified_diff
from src.review.markers import has_marker, parse_finding_marker
from src.review.models import (
    HunkSide,
    PullRequest,
    ReviewBatch,
)
from src.review.pr_actions import APPROVE, REQUEST_CHANGES, review_decision
from src.review.providers.base import (
    MAX_FILE_BYTES,
    SUGGESTION_GITHUB,
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
    _format_review_pointer,
    _format_summary,
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

logger = logging.getLogger(__name__)


GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"


def _api_host() -> tuple[str, ...]:
    """GITHUB_API_BASE's own host, as a one-item exception to the allowlist.

    Every request this provider makes goes to exactly one place — the base URL
    above — so that host is named at the call site, the same way the LiteLLM
    gateway names its configured proxy (see src/llm/gateway.py:_proxy_host).
    Derived from the constant rather than spelled twice, so the exception can
    never point anywhere the requests do not.
    """
    host = urlsplit(GITHUB_API_BASE).hostname or ""
    return (host,) if host else ()

# GitHub keeps two comment collections on a pull request and they share neither
# the list endpoint nor the delete endpoint: the inline comments a review leaves
# behind are "review comments" under /pulls, the standalone summary is an
# "issue comment" under /issues. Every id we collect therefore has to travel
# with the kind that says which path deletes it — Bitbucket needs no such tag
# because there one endpoint holds both.
_INLINE_COMMENT = "inline"
_ISSUE_COMMENT = "issue"

#: GitHub's GraphQL endpoint, derived from the REST base so the two can never
#: point at different installations. Same HOST as every REST call above, which
#: is what matters here: the guarded client (src/http.py) filters by host, and
#: `_api_host()` already names it, so GraphQL needs no new egress exception and
#: no second client.
GITHUB_GRAPHQL_URL = f"{GITHUB_API_BASE}/graphql"

#: What `minimizeComment` records as the reason. The previous runs' review
#: bodies are superseded by the review being posted right now — that is the
#: definition of OUTDATED, and it is the classifier the collapsed entry shows.
_MINIMIZE_CLASSIFIER = "OUTDATED"


def _json_id(resp: httpx.Response) -> object:
    """The `id` of a JSON object response, or None for anything else."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("id") if isinstance(body, dict) else None


def _findings_as_body(comments: list[dict]) -> str:
    """Inline comments rewritten as a list, for when GitHub refuses the anchors.

    Each keeps its file and line as text, so a reader can still navigate to it
    — the position is information even when GitHub will not render it as a
    marker.
    """
    lines = ["**Findings that could not be anchored to the diff**", ""]
    for c in comments:
        where = c.get("path", "?")
        line = c.get("line") or c.get("position")
        lines.append(f"- `{where}`" + (f" line {line}" if line else ""))
        body = str(c.get("body", "")).strip()
        if body:
            lines.extend("  " + ln for ln in body.splitlines())
        lines.append("")
    return "\n".join(lines)


class GitHubPRProvider(PullRequestProvider):
    """GitHub Pull Request operations via REST API v3."""

    name = "github"

    def __init__(
        self,
        token: str | None = None,
        *,
        account_label: str = "default",
        user_id: str = "default",
        workspace_id: str = "default",
        timeout: float = 30.0,
    ) -> None:
        if token is None:
            stored = resolve_git_credential(
                "github", user_id=user_id, account_label=account_label,
                workspace_id=workspace_id,
            )
            if stored is None:
                raise PullRequestProviderError(
                    f"No GitHub credentials saved for user '{user_id}'. "
                    f"Connect GitHub via the Connections page first."
                )
            token = stored.secret
        self.token = token
        #: Who this token posts as. Looked up once, on the first cleanup that
        #: needs it; "" means the lookup failed and nothing may be deleted.
        self._viewer_cache: str | None = None
        # Guarded egress (src/http.py), not a raw httpx.Client: this module
        # sat on deployment.UNGUARDED_HTTP_SITES from the day the factory
        # shipped. Same defaults as httpx's own — follow_redirects stays False.
        self._http = build_client(
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "Authorization": f"Bearer {token}",
                "User-Agent": "code-analyzer/0.1",
            },
            extra_allowed_hosts=_api_host(),
        )

    def __enter__(self) -> GitHubPRProvider:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ─── Fetch ───────────────────────────────────────────────────

    def fetch_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        owner, name = self._split_repo(repo)
        # 1. Metadata
        meta_resp = self._http.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}",
        )
        if meta_resp.status_code == 404:
            raise PullRequestProviderError(
                f"PR #{pr_number} not found in {repo}"
            )
        # A 301 (renamed or moved repository) used to pass this check and then
        # fail on `.json()` of an empty body; now it says where it pointed.
        self._expect_ok(meta_resp, "GitHub API")
        meta = meta_resp.json()

        # 2. Raw diff (accept v3.diff)
        diff_resp = self._http.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        self._expect_ok(diff_resp, "GitHub diff")
        raw_diff = diff_resp.text

        # 3. Parse hunks via unidiff
        settings = get_review_settings()
        hunks, skipped_files = parse_unified_diff(raw_diff, settings=settings)

        return PullRequest(
            provider="github",
            repo=f"{owner}/{name}",
            number=pr_number,
            title=str(meta.get("title") or ""),
            description=str(meta.get("body") or ""),
            author=str((meta.get("user") or {}).get("login") or ""),
            base_ref=str((meta.get("base") or {}).get("ref") or ""),
            base_sha=str((meta.get("base") or {}).get("sha") or ""),
            head_ref=str((meta.get("head") or {}).get("ref") or ""),
            head_sha=str((meta.get("head") or {}).get("sha") or ""),
            state=str(meta.get("state") or "open"),
            is_draft=bool(meta.get("draft", False)),
            url=str(meta.get("html_url") or ""),
            hunks=hunks,
            raw_diff=raw_diff,
            skipped_files=skipped_files,
            reported_files=_count(meta.get("changed_files")),
        )

    # ─── Post review ─────────────────────────────────────────────

    def post_review(
        self, batch: ReviewBatch, *, dry_run: bool = False,
    ) -> dict:
        pr = batch.pull_request
        if pr.provider != "github":
            raise PullRequestProviderError(
                f"GitHub provider received non-github PR: {pr.provider}"
            )
        owner, name = self._split_repo(pr.repo)
        settings = get_review_settings()

        # ── 1. Build review payload ──
        #
        # The event is a decision the repository opts into, not the verdict:
        # COMMENT unless `approve_when_clean` / `request_changes_on_critical`
        # say otherwise (see `review_decision`).
        decision = review_decision(batch)
        event = {APPROVE: "APPROVE", REQUEST_CHANGES: "REQUEST_CHANGES"}.get(
            decision or "", "COMMENT")
        committable = _committable_enabled(batch)
        new_side = _new_side_text(pr)

        # Every anchor is made postable BEFORE the POST, because GitHub
        # validates the review as one object and one refused anchor takes the
        # whole batch with it (see `_snap_to_span` for the measurement).
        ranges = _anchorable_ranges(pr)
        comments_payload: list[dict[str, Any]] = []
        snapped = 0
        # An incremental review (only the new commits were read) lists our
        # earlier threads first: what is already on the PR is not posted again.
        incremental = begin_incremental_post(self, batch, settings.comment_marker)
        selected = batch.inline_findings(
            settings.max_inline_comments, skip=incremental_skip(incremental))
        for finding in selected:
            side = "RIGHT" if finding.side == HunkSide.RIGHT else "LEFT"
            line = _snap_to_span(finding.line, ranges.get((finding.file_path, side), []))
            if line != finding.line:
                snapped += 1
                logger.info(
                    "github_anchor_snapped repo=%s pr=%d path=%s from=%d to=%d rule=%s",
                    pr.repo, pr.number, finding.file_path, finding.line, line,
                    finding.rule_id,
                )
            span = _committable_span(finding, line, ranges) if committable else None
            comment: dict[str, Any] = {
                "path": finding.file_path,
                "line": line,
                "side": side,
                "body": _format_finding_body(
                    finding, settings.comment_marker,
                    committable=SUGGESTION_GITHUB if span else None,
                    original=_original_lines(new_side, finding),
                    head_sha=pr.head_sha,
                ),
            }
            if span and span[1] > span[0]:
                # A multi-line suggestion replaces the commented RANGE, so the
                # comment spans it: start_line..line, both on the new side.
                comment["start_line"] = span[0]
                comment["start_side"] = "RIGHT"
                comment["line"] = span[1]
            comments_payload.append(comment)
        if snapped:
            logger.info(
                "github_anchors_snapped repo=%s pr=%d moved=%d of=%d",
                pr.repo, pr.number, snapped, len(comments_payload),
            )

        # The review body is a POINTER, never the summary: a submitted review
        # is immutable, so a full summary here re-appears once per re-run and
        # no cleanup can reach it (see `_format_review_pointer` for the whole
        # story). Linkless for now — the anchor needs the persistent comment's
        # id, which the listing below may know and a dry run never does.
        review_payload = {
            "commit_id": pr.head_sha,
            "event": event,
            "body": self._review_body(batch, settings.comment_marker),
            "comments": comments_payload,
        }

        if dry_run:
            logger.info(
                "github_review_dry_run repo=%s pr=%d findings=%d verdict=%s",
                pr.repo, pr.number, len(batch.findings), batch.verdict.value,
            )
            return {
                "dry_run": True,
                "would_post": review_payload,
            }

        # ── 2. Replace-on-synchronize: read what the previous run left ──
        #
        # Re-running a review posted a SECOND full set of inline comments: only
        # the summary was ever deleted, and the inline comments — which carry
        # the same marker — stayed where they were. Three pushes, three sets.
        #
        # The listing happens HERE, before the new review is submitted, so the
        # ids can never include the comments we are about to post; the deletes
        # happen after (step 4), so a review POST that fails leaves the previous
        # review standing instead of wiping it and replacing it with nothing.
        stale: list[tuple[str, int]] = []
        protected: set[int] = set()
        listing_complete = True
        stale_reviews: list[str] = []
        if settings.replace_on_synchronize:
            stale, protected, listing_complete = self._marked_comments(
                owner, name, pr.number, settings.comment_marker,
            )
            if incremental is not None:
                # Earlier inline comments stay: they are still about code the
                # PR carries (outdated ones are resolved below).
                stale = [x for x in stale if x[0] == _ISSUE_COMMENT]
            # The review BODIES of earlier runs, listed here for the same
            # reason and with the same ordering: a review that fails to post
            # must not leave the pull request with its previous review already
            # folded away. Minimized after the POST succeeds, in step 4.
            stale_reviews = self._our_review_node_ids(
                owner, name, pr.number, settings.comment_marker,
            )

        # The oldest marked issue comment is the persistent summary — the one
        # the upsert (step 5) rewrites in place, so its id survives re-runs.
        # Known here, before the review POST, it gives the immutable review
        # body a link that keeps resolving; on the first run there is nothing
        # to link to yet and the pointer stays linkless.
        keep_summary_id = next(
            (cid for kind, cid in stale if kind == _ISSUE_COMMENT), None,
        )
        # The lifecycle comment this run already posted ("🔄 reviewing…") IS
        # the summary: the final body goes into it, whatever its age. With
        # re-runs replacing it is the oldest marked comment anyway (the
        # placeholder was upserted into that one); with history kept it is the
        # only way the placeholder is replaced instead of joined.
        if self._status_comment_id is not None:
            keep_summary_id = self._status_comment_id
        if keep_summary_id is not None and pr.url:
            review_payload["body"] = self._review_body(
                batch, settings.comment_marker,
                summary_url=f"{pr.url}#issuecomment-{keep_summary_id}",
            )

        # ── 3. Submit review (batched) ──
        review_url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr.number}/reviews"
        review_resp = self._http.post(review_url, json=review_payload)

        # ── 3b. Recover from a 422 — each cause once, in whatever order ──
        #
        # GitHub can refuse this POST for two unrelated reasons and reports ONE
        # of them at a time: the token approving its own pull request, and an
        # anchor it will not place. These used to be two sequential one-shot
        # `if`s, so whichever reason came back FIRST was the only one that could
        # ever fire — the second `if` looked at the already-retried response and
        # saw a status that no longer matched. A pull request that was both ours
        # AND carried a refused anchor therefore lost the whole review.
        #
        # The loop lets each recovery fire once, in either order. It is bounded
        # by `pending`, which loses exactly one entry per iteration that
        # continues, so at most two extra POSTs leave here.
        unanchored = ""
        pending = ["own_pr", "anchors"]
        while review_resp.status_code == 422 and pending:
            folded = self._recover_from_422(pending, review_payload, review_resp, pr)
            if folded is None:
                break  # a 422 neither recovery addresses — let it raise below
            if folded:
                # "" is a recovery that changed the payload without producing
                # text, which is why None and "" have to stay distinguishable.
                unanchored = folded
            review_resp = self._http.post(review_url, json=review_payload)

        if review_resp.status_code >= 400:
            raise PullRequestProviderError(
                f"GitHub review POST failed {review_resp.status_code}: "
                f"{review_resp.text[:300]}"
            )

        # ── 4. Drop the previous run's comments, now that this run's are up ──
        cleanup = self._delete_stale_comments(
            owner, name, stale, keep_summary_id=keep_summary_id,
            protected=protected, listing_complete=listing_complete,
        )
        # …and fold away the previous runs' review bodies, which no REST call
        # can delete. Never raises: a long timeline beats no review.
        cleanup.update(self._minimize_reviews(stale_reviews))

        # ── 4b. Withdraw an approval / a block an earlier run left, when this
        # run is neither. Only for a repository that lets reviews approve or
        # block at all — otherwise this review never gave either.
        review_state: dict[str, Any] = {"event": review_payload["event"]}
        if batch.pr_actions.manages_review_state and review_payload["event"] == "COMMENT":
            review_state.update(self._dismiss_our_verdicts(
                owner, name, pr.number, settings.comment_marker,
                keep_review_id=_json_id(review_resp),
            ))

        # ── 4c. An incremental run: resolve what its commits made outdated, put
        # the banner in the summary, and say which comments this run created.
        review_comments = review_payload.get("comments") or []
        incremental_response = finish_incremental_post(
            self, batch, incremental, posted=len(review_comments))
        inline_comments = self._created_comments(
            owner, name, pr.number, _json_id(review_resp), selected)

        # ── 5. Top-level summary comment (separate, with an idempotency marker) ──
        summary_body = _format_summary(batch, marker=settings.comment_marker)
        if unanchored:
            summary_body += "\n\n---\n\n" + unanchored
        summary_id = self._upsert_summary(
            owner, name, pr.number, summary_body, keep_summary_id,
        )
        # Consumed: the next review on this instance starts its own lifecycle.
        self._status_comment_id = None

        result = review_resp.json()
        response: dict[str, Any] = {
            "review_id": result.get("id"),
            "summary_comment_id": summary_id,
            "html_url": result.get("html_url"),
            "comments_posted": len(comments_payload),
            # How many anchors had to move to become postable. One that cannot
            # be placed costs the whole batch, so this is the number that says
            # how close the run came to losing every finding on the PR.
            "anchors_snapped": snapped,
            # `complete: False` means duplicates may still be on the PR — a
            # caller that reports "review posted" can say so rather than let a
            # half-done cleanup look like a finished one.
            "cleanup": cleanup,
            "review_state": review_state,
            # What this run created, for the learning loop to follow.
            "inline_comments": inline_comments,
            **incremental_response,
        }
        if summary_id is None:
            # The review itself is up (it carries the verdict), but the full
            # summary — findings, scope, and the placeholder it was meant to
            # replace — is not. Same contract as Bitbucket and GitLab: `error`
            # becomes the run's `post_error`, so the run reads PARTIAL instead
            # of passing as delivered.
            why = (
                f"GitHub refused the summary comment (HTTP {self._last_summary_status})"
                if self._last_summary_status else
                "the GitHub summary comment could not be written"
            )
            logger.error("github_summary_not_delivered repo=%s pr=%d status=%s",
                         pr.repo, pr.number, self._last_summary_status or "-")
            response["summary_error"] = why
            response["error"] = why
        return response

    def _created_comments(
        self, owner: str, name: str, pr_number: int, review_id: object,
        findings: list,
    ) -> list[PostedComment]:
        """The inline comments the review just created, matched back to their
        findings by the finding marker in each body. Best effort: the review
        is already up, so any failure here is an empty list."""
        if not isinstance(review_id, int) or not findings:
            return []
        try:
            comments, _ = self._list_all(
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
                f"/reviews/{review_id}/comments?per_page=100")
        except Exception:  # noqa: BLE001
            return []
        by_short = {}
        for f in findings:
            short, full = finding_fingerprint(f)
            by_short.setdefault(short, full)
        out: list[PostedComment] = []
        for c in comments:
            found = parse_finding_marker(str(c.get("body") or ""))
            cid = c.get("id")
            if not found or found[0] not in by_short or not isinstance(cid, int):
                continue
            line = c.get("line") or c.get("original_line") or 0
            out.append(PostedComment(
                comment_id=cid, path=str(c.get("path") or ""),
                line=int(line) if isinstance(line, int) else 0,
                fingerprint=found[0], finding_key=by_short[found[0]]))
        return out

    # ─── Incremental review: commits, compare diff, review threads ───

    def list_pr_commits(self, repo: str, pr_number: int) -> list[CommitInfo] | None:
        """The PR's commits with their parents. GitHub stops listing at 250:
        a list that long may be cut short, so it is None (review the whole PR)."""
        owner, name = self._split_repo(repo)
        try:
            items, complete = self._list_all(
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
                "/commits?per_page=100")
        except Exception as exc:  # noqa: BLE001
            logger.warning("github_commits_failed pr=%s error=%s", pr_number, type(exc).__name__)
            return None
        if not complete or len(items) >= MAX_LISTED_COMMITS:
            return None
        out: list[CommitInfo] = []
        for item in items:
            sha = item.get("sha")
            if not sha:
                return None
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
            out.append(CommitInfo(
                sha=str(sha),
                parents=tuple(str(p.get("sha")) for p in item.get("parents") or []
                              if isinstance(p, dict) and p.get("sha")),
                message=str(commit.get("message") or ""),
                committed_at=committer.get("date")))
        return out

    def fetch_incremental_diff(
        self, repo: str, pr_number: int, base_sha: str, head_sha: str,
    ) -> str | None:
        """`compare/{base}...{head}` as a unified diff. None on any failure."""
        owner, name = self._split_repo(repo)
        try:
            resp = self._http.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/compare/{base_sha}...{head_sha}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
            self._expect_ok(resp, "GitHub compare")
        except (PullRequestProviderError, httpx.HTTPError) as exc:
            logger.warning("github_incremental_diff_failed pr=%s error=%s", pr_number, exc)
            return None
        return resp.text

    _THREADS_QUERY = (
        "query($owner: String!, $name: String!, $number: Int!, $after: String) { "
        "repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
        "reviewThreads(first: 100, after: $after) { "
        "pageInfo { hasNextPage endCursor } "
        "nodes { id isResolved path line originalLine diffSide "
        "comments(first: 50) { nodes { databaseId body author { login } } } } } } } }"
    )

    def our_inline_threads(self, pr: PullRequest, marker: str) -> list[OurThread] | None:
        """Our review threads (first comment: marker AND author), with their
        GraphQL id (what resolving needs). None when a page cannot be read."""
        owner, name = self._split_repo(pr.repo)
        viewer = self._viewer_login()
        if not viewer:
            return None
        threads: list[OurThread] = []
        after: str | None = None
        for _ in range(10):  # 1000 threads is more than a PR holds
            data = self._graphql(self._THREADS_QUERY, {
                "owner": owner, "name": name, "number": int(pr.number), "after": after})
            try:
                block = data["repository"]["pullRequest"]["reviewThreads"]  # type: ignore[index]
                nodes = block["nodes"]
            except (TypeError, KeyError):
                return None
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                comments = ((node.get("comments") or {}).get("nodes")) or []
                if not comments or not isinstance(comments[0], dict):
                    continue
                first = comments[0]
                body = str(first.get("body") or "")
                author = str((first.get("author") or {}).get("login") or "")
                if author != viewer or not has_marker(body, marker):
                    continue
                found = parse_finding_marker(body)
                current = isinstance(node.get("line"), int)
                line = node.get("line") if current else node.get("originalLine")
                threads.append(OurThread(
                    comment_id=first.get("databaseId") or 0,
                    thread_id=str(node.get("id") or "") or None,
                    path=str(node.get("path") or ""),
                    line=line if isinstance(line, int) else None,
                    side="LEFT" if node.get("diffSide") == "LEFT" else "RIGHT",
                    resolved=bool(node.get("isResolved")),
                    line_is_current=current,
                    fingerprint=found[0] if found else None,
                    sha=found[1] if found else None,
                    replied=any(
                        isinstance(c, dict)
                        and str((c.get("author") or {}).get("login") or "") != viewer
                        for c in comments[1:]),
                ))
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return threads
            after = page.get("endCursor")
            if not after:
                return None
        return None

    def resolve_threads(self, pr: PullRequest, threads: list[OurThread]) -> dict[str, int]:
        """`resolveReviewThread`, one aliased mutation for all of them."""
        out = {"resolved": 0, "failed": 0, "unsupported": 0}
        todo = [t for t in threads if t.thread_id]
        out["unsupported"] += len(threads) - len(todo)
        if not todo:
            return out
        fields = " ".join(
            f"r{i}: resolveReviewThread(input: $i{i}) {{ thread {{ isResolved }} }}"
            for i in range(len(todo)))
        signature = ", ".join(f"$i{i}: ResolveReviewThreadInput!" for i in range(len(todo)))
        data = self._graphql(
            f"mutation({signature}) {{ {fields} }}",
            {f"i{i}": {"threadId": t.thread_id} for i, t in enumerate(todo)})
        for i in range(len(todo)):
            result = (data or {}).get(f"r{i}")
            ok = (isinstance(result, dict) and isinstance(result.get("thread"), dict)
                  and result["thread"].get("isResolved") is True)
            out["resolved" if ok else "failed"] += 1
        return out

    # ─── Lifecycle comment ("🔄 reviewing…" → summary) ──────────

    def upsert_status_comment(
        self, pr: PullRequest, body: str, *, create: bool = True,
        only_if_in_progress: bool = False,
    ) -> int | None:
        """See `PullRequestProvider.upsert_status_comment`."""
        if pr.provider != "github":
            raise PullRequestProviderError(
                f"GitHub provider received non-github PR: {pr.provider}"
            )
        settings = get_review_settings()
        owner, name = self._split_repo(pr.repo)
        write, existing = self._status_target(
            pr, settings.comment_marker,
            replace=settings.replace_on_synchronize, create=create,
            only_if_in_progress=only_if_in_progress,
        )
        if not write:
            return None
        cid = self._upsert_summary(
            owner, name, pr.number,
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
        owner, name = self._split_repo(pr.repo)
        return self._upsert_summary(owner, name, pr.number, body, existing_id)

    # ─── Approval / request changes: taking ours back ───────────

    def _dismiss_our_verdicts(
        self, owner: str, name: str, pr_number: int, marker: str,
        *, keep_review_id: object = None,
    ) -> dict[str, int]:
        """Dismiss our APPROVED / CHANGES_REQUESTED reviews. Never raises.

        Called when this run's review is a plain COMMENT: a COMMENT review
        does not replace a reviewer's earlier approval or block on GitHub, so
        without this an approval for a clean commit would outlive the push
        that broke it. `PUT .../reviews/{id}/dismissals` is the only way to
        take a submitted review's verdict back. Our reviews are found by the
        same marker-AND-author proof the cleanup uses; a person's own review
        on a shared token is never touched. Dismissing on a protected branch
        can need extra rights — a refusal is counted, not raised.
        """
        stats = {"dismissed": 0, "dismiss_failed": 0}
        reviews, _ = self._list_all(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
            "/reviews?per_page=100",
        )
        viewer = self._viewer_login() if reviews else ""
        if not viewer:
            return stats
        for review in reviews:
            rid = review.get("id")
            if (not isinstance(rid, int) or rid == keep_review_id
                    or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED")
                    or not self._is_ours(review, marker, viewer)):
                continue
            try:
                resp = self._http.put(
                    f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
                    f"/reviews/{rid}/dismissals",
                    json={
                        "message": "Superseded by a newer Celmis review of this "
                                   "pull request.",
                        "event": "DISMISS",
                    },
                )
            except httpx.HTTPError as exc:
                stats["dismiss_failed"] += 1
                logger.warning("github_dismiss_review_error id=%d err=%s", rid, exc)
                continue
            if resp.status_code < 400:
                stats["dismissed"] += 1
            else:
                stats["dismiss_failed"] += 1
                logger.warning("github_dismiss_review_failed id=%d status=%d body=%s",
                               rid, resp.status_code, resp.text[:200])
        return stats

    # ─── The summary in the pull request description ────────────

    def update_description(self, pr: PullRequest, transform) -> dict:
        """GET the PR body, PATCH it with `transform(body)` when that changes it."""
        owner, name = self._split_repo(pr.repo)
        url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr.number}"
        try:
            resp = self._http.get(url)
            if resp.status_code >= 400:
                return {"written": False, "error": f"GitHub refused the read (HTTP {resp.status_code})"}
            meta = resp.json()
            current = str((meta or {}).get("body") or "") if isinstance(meta, dict) else ""
            new = transform(current)
            if new is None or new == current:
                return {"written": False, "error": None, "unchanged": True}
            resp = self._http.patch(url, json={"body": new})
        except (httpx.HTTPError, ValueError) as exc:
            return {"written": False, "error": type(exc).__name__}
        if resp.status_code >= 400:
            logger.warning("github_description_patch_failed status=%d", resp.status_code)
            return {"written": False, "error": f"GitHub refused the description (HTTP {resp.status_code})"}
        return {"written": True, "error": None}

    def fetch_commit_messages(self, pr: PullRequest, limit: int = 50) -> list[str]:
        """Newest first. GitHub answers oldest first and at most 250 commits
        (three pages of 100), so the whole list is read and then reversed: the
        newest commits, where a key added late sits, are on the last page."""
        owner, name = self._split_repo(pr.repo)
        url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr.number}/commits"
        rows: list[Any] = []
        try:
            for page in range(1, 4):
                resp = self._http.get(url, params={"per_page": 100, "page": page})
                if resp.status_code != 200:
                    return []
                batch = resp.json()
                if not isinstance(batch, list):
                    return []
                rows.extend(batch)
                if len(batch) < 100:
                    break
        except (httpx.HTTPError, ValueError):
            return []
        messages = [str((r.get("commit") or {}).get("message") or "")
                    for r in rows if isinstance(r, dict)]
        return [m for m in reversed(messages) if m][:max(1, int(limit))]

    def _our_summary_comments(
        self, pr: PullRequest, marker: str,
    ) -> list[tuple[int, str]]:
        owner, name = self._split_repo(pr.repo)
        comments, _ = self._list_all(self._issue_comments_url(owner, name, pr.number))
        viewer = self._viewer_login()
        if not viewer:
            return []  # fail closed — see `_viewer_login`
        return [
            (c["id"], str(c.get("body") or "")) for c in comments
            if self._is_ours(c, marker, viewer) and isinstance(c.get("id"), int)
        ]

    # ─── Review body: pointer + proof of authorship ─────────────

    @staticmethod
    def _review_body(
        batch: ReviewBatch, marker: str, summary_url: str | None = None,
    ) -> str:
        """The review pointer, carrying the marker that proves it is ours.

        `_format_review_pointer` is shared with the other providers and takes no
        marker, so it is prepended here — an HTML comment, invisible in the
        rendered body, exactly the way `_format_summary` carries it.

        It has to be there because a review body can otherwise be identified
        only by its author, and the author is whatever account the operator
        connected (`resolve_git_credential`) — frequently a person's own PAT
        rather than a dedicated bot. On authorship alone `_minimize_reviews`
        would fold away that person's hand-written reviews of the pull
        request, the same class of mistake the marker-only comment cleanup
        made in the other direction.

        Consequence, stated rather than hidden: reviews posted BEFORE this
        change carry no marker, so the two-part proof can never be met for them
        and they are never collapsed. Only reviews from here on fold.
        """
        return f"{marker}\n{_format_review_pointer(batch, summary_url)}"

    @staticmethod
    def _recover_from_422(
        pending: list[str],
        payload: dict[str, Any],
        resp: httpx.Response,
        pr: PullRequest,
    ) -> str | None:
        """Apply the first still-pending recovery this 422 calls for.

        Returns the markdown the caller must move into the persistent summary —
        "" when the recovery produced none — and removes its key from
        `pending`, so the caller's loop terminates. None means no pending
        recovery matches this response, which is a 422 nothing here can fix.

        Order inside the list is priority, not sequence: `own_pr` is tried
        first because it is the one with a message to match on, and `anchors`
        is deliberately unguarded — GitHub words an unplaceable-anchor 422
        several ways, so anything left over is treated as one.
        """
        detail = resp.text.lower()
        for key in list(pending):
            if key == "own_pr":
                if payload["event"] not in ("APPROVE", "REQUEST_CHANGES"):
                    continue
                if "own pull request" not in detail:
                    continue
                logger.info(
                    "github_own_pr_fallback repo=%s pr=%d original_event=%s",
                    pr.repo, pr.number, payload["event"],
                )
                pending.remove(key)
                # event=COMMENT so the inline comments and summary still post.
                payload["event"] = "COMMENT"
                return ""
            if key == "anchors":
                if not payload.get("comments"):
                    continue
                # The findings are worth more than their anchors: retry without
                # the inline comments and fold them into the PERSISTENT summary
                # comment — not into the review body, which is immutable, so a
                # PR whose anchors kept being refused collected one full
                # findings list per push. The persistent comment is rewritten in
                # place: however many re-runs it takes, one copy.
                logger.warning(
                    "github_inline_rejected repo=%s pr=%d comments=%d detail=%s "
                    "— retrying without anchors",
                    pr.repo, pr.number, len(payload["comments"]),
                    resp.text[:200],
                )
                pending.remove(key)
                fallback = _findings_as_body(payload["comments"])
                payload["comments"] = []
                return fallback
        return None

    # ─── Idempotency: find existing marker comment ──────────────

    def find_existing_review_comment(
        self, repo: str, pr_number: int, marker: str,
    ) -> int | None:
        """The summary comment to update in place — oldest marked one, if any.

        GitHub lists issue comments oldest first, so this is the comment the
        FIRST review created: keeping that one is what makes a link to the
        summary from a ticket still resolve after the third push.
        """
        owner, name = self._split_repo(repo)
        ids, _ = self._marked_ids_on(
            self._issue_comments_url(owner, name, pr_number), marker,
        )
        return ids[0] if ids else None

    def find_marked_comment_ids(
        self, repo: str, pr_number: int, marker: str,
    ) -> list[int]:
        """Every comment of ours on this PR — inline ones first, then summaries.

        The ids come from two different collections and are only unique within
        their own, which is why the cleanup uses `_marked_comments` and its
        (kind, id) pairs; this flat list exists for the provider-agnostic
        contract in PullRequestProvider.
        """
        owner, name = self._split_repo(repo)
        marked, _, _ = self._marked_comments(owner, name, pr_number, marker)
        return [cid for _, cid in marked]

    def _inline_comments_url(self, owner: str, name: str, pr_number: int) -> str:
        return (
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
            "/comments?per_page=100"
        )

    def _issue_comments_url(self, owner: str, name: str, pr_number: int) -> str:
        return (
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/issues/{pr_number}"
            "/comments?per_page=100"
        )

    def _marked_comments(
        self, owner: str, name: str, pr_number: int, marker: str,
    ) -> tuple[list[tuple[str, int]], set[int], bool]:
        """Our comments as (kind, id), the protected roots, listing health.

        Matching is `marker` AND authorship — see `_is_ours`. It was the marker
        alone, on the reasoning that only this module writes one; "Quote reply"
        is what broke that, because it copies the quoted comment's RAW markdown
        and the marker is an HTML comment inside it. A reviewer arguing with one
        of our findings therefore writes a comment carrying our marker, and the
        marker-only test would have deleted their words on the next run.

        The set holds inline ids that a reply by anyone else points at via
        `in_reply_to_id`. Nothing inline was ever deleted before this cleanup
        existed, so no reply could be orphaned; now that our own inline
        comments go on every push, deleting a root somebody answered would
        tear their reply out of its context. Proof of ours is what LIFTS
        protection — a reply whose author cannot be read protects too.
        """
        inline_comments, inline_ok = self._list_all(
            self._inline_comments_url(owner, name, pr_number),
        )
        issue_comments, issue_ok = self._list_all(
            self._issue_comments_url(owner, name, pr_number),
        )
        viewer = self._viewer_login()
        if not viewer:
            # Fail closed. Without knowing who we are, the marker alone is not
            # proof of authorship — see `_viewer_login`.
            return [], set(), False
        protected: set[int] = set()
        for comment in inline_comments:
            root = comment.get("in_reply_to_id")
            # A comment from our own account with none of our markers is a
            # person speaking through the token: it protects its root too.
            if isinstance(root, int) and (
                not self._authored_by(comment, viewer)
                or not markers.is_bot_text(str(comment.get("body") or ""))
            ):
                protected.add(root)
        marked = [
            (_INLINE_COMMENT, c["id"]) for c in inline_comments
            if self._is_ours(c, marker, viewer) and isinstance(c.get("id"), int)
        ] + [
            (_ISSUE_COMMENT, c["id"]) for c in issue_comments
            if self._is_ours(c, marker, viewer) and isinstance(c.get("id"), int)
        ]
        return marked, protected, inline_ok and issue_ok

    def _marked_ids_on(self, url: str, marker: str) -> tuple[list[int], bool]:
        """Marked comment ids of OURS from one paginated list endpoint."""
        comments, ok = self._list_all(url)
        viewer = self._viewer_login()
        if not viewer:
            # Fail closed — see `_viewer_login`.
            return [], False
        found = [
            c["id"] for c in comments
            if self._is_ours(c, marker, viewer) and isinstance(c.get("id"), int)
        ]
        return found, ok

    def _list_all(self, url: str) -> tuple[list[dict], bool]:
        """Every comment from one paginated list endpoint, unfiltered.

        The bool is False when a page could not be read. A cleanup that saw only
        page one and reported success is worse than no cleanup at all — the
        duplicates it missed stay on the PR and nothing says so — and a busy PR
        runs to hundreds of comments against a page size of 100, so page two is
        the normal case, not the exotic one.
        """
        found: list[dict] = []
        seen: set[str] = set()
        next_url: str | None = url
        while next_url:
            if next_url in seen:
                # A proxy that echoes back the same rel="next" would spin here
                # forever. Stop, and admit the listing is partial.
                logger.warning("github_list_comments_loop url=%s", next_url)
                return found, False
            seen.add(next_url)
            try:
                resp = self._http.get(next_url)
                if resp.status_code >= 400:
                    logger.warning(
                        "github_list_comments_failed status=%d url=%s",
                        resp.status_code, next_url,
                    )
                    return found, False
                page = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "github_list_comments_error url=%s err=%s", next_url, exc,
                )
                return found, False
            found.extend(c for c in page or [] if isinstance(c, dict))
            # Next page (pagination via the Link header)
            next_url = self._parse_link_next(resp.headers.get("Link", ""))
        return found, True

    def _viewer_login(self) -> str:
        """Who this token posts as, cached for the life of the provider.

        Required before anything is deleted. The marker is an HTML comment in
        the body, and GitHub's "Quote reply" copies the quoted comment's RAW
        markdown — marker included — into the new comment. So a reviewer who
        quotes one of our findings to argue with it writes a comment that
        contains our marker, and a substring test would have deleted their
        words on the next run.
        """
        if self._viewer_cache is not None:
            return self._viewer_cache
        try:
            resp = self._http.get(f"{GITHUB_API_BASE}/user")
            if resp.status_code < 400:
                # A 200 whose body is not an object — a captive proxy's page, a
                # list from a mis-routed request — used to reach .get() and
                # raise AttributeError straight past the except below, taking
                # the whole review down. An unreadable answer is not an
                # identity: treat it as no identity and delete nothing.
                body = resp.json()
                login = str(body.get("login") or "") if isinstance(body, dict) else ""
                if not login:
                    logger.warning("github_viewer_lookup_anonymous")
                self._viewer_cache = login
                return login
            logger.warning("github_viewer_lookup_failed status=%d", resp.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("github_viewer_lookup_error err=%s", exc)
        self._viewer_cache = ""
        return ""

    @classmethod
    def _is_ours(cls, comment: dict, marker: str, viewer: str) -> bool:
        """Both conditions, never one: our marker AND our authorship.

        The marker alone identifies the SHAPE of the comment; the author
        identifies who wrote it. Only the pair identifies a comment this bot
        is entitled to delete.
        """
        if not has_marker(comment.get("body") or "", marker):
            return False
        return cls._authored_by(comment, viewer)

    @staticmethod
    def _authored_by(comment: dict, viewer: str) -> bool:
        """Provably written by this token — a malformed author is a no.

        `user` arrives as a plain string on a truncated or proxied payload —
        {"user": "octocat"} — and `(comment.get("user") or {}).get("login")`
        raised AttributeError on it, past the except around the listing and
        straight out of post_review, taking the whole review down. Absence of
        proof keeps a comment; it never crashes the review.
        """
        user = comment.get("user")
        if not isinstance(user, dict):
            return False
        author = str(user.get("login") or "")
        return bool(author) and author == viewer

    def _delete_stale_comments(
        self,
        owner: str,
        name: str,
        stale: list[tuple[str, int]],
        *,
        keep_summary_id: int | None,
        protected: set[int],
        listing_complete: bool,
    ) -> dict[str, Any]:
        """Delete the previous run's comments, minus the one worth keeping.

        `keep_summary_id` — the oldest marked issue comment — is NOT deleted:
        the caller PATCHes the new summary into it (Qodo's persistent-comment
        pattern), which keeps its permalink, its place in the conversation and
        everyone subscribed to it across a re-run. The caller picks it before
        the review POST because the immutable review body links to it by id;
        this method only honours the choice. Surplus summaries, the ones
        earlier runs piled up, are deleted like the inline comments.

        Two more things are never deleted: an inline root somebody else
        replied to — removing it would orphan the human's words, so it is
        counted in `kept_threaded` (a decision, not a failure: it does not
        turn `complete` off) — and anything at all when the listing did not
        finish, because the unread pages may hold exactly such a reply.

        Nothing here raises. A review with duplicate comments beats no review,
        so a failure is logged, counted, and surfaced in the returned stats.
        """
        deleted = 0
        failed = 0
        kept_threaded = 0
        for kind, cid in stale:
            if kind == _ISSUE_COMMENT and cid == keep_summary_id:
                continue
            if kind == _INLINE_COMMENT and cid in protected:
                kept_threaded += 1
                continue
            if not listing_complete:
                continue
            url = (
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/comments/{cid}"
                if kind == _INLINE_COMMENT
                else f"{GITHUB_API_BASE}/repos/{owner}/{name}/issues/comments/{cid}"
            )
            try:
                resp = self._http.delete(url)
            except httpx.HTTPError as exc:
                failed += 1
                logger.warning(
                    "github_delete_comment_error kind=%s id=%d err=%s", kind, cid, exc,
                )
                continue
            # 404 — somebody deleted it first, which is the state we wanted.
            if resp.status_code in (200, 204, 404):
                deleted += 1
            else:
                failed += 1
                logger.warning(
                    "github_delete_comment_failed kind=%s id=%d status=%d body=%s",
                    kind, cid, resp.status_code, resp.text[:100],
                )
        return {
            "deleted": deleted,
            "failed": failed,
            "kept_threaded": kept_threaded,
            "complete": listing_complete and failed == 0,
        }

    # ─── Fold away the previous runs' review bodies ─────────────

    def _our_review_node_ids(
        self, owner: str, name: str, pr_number: int, marker: str,
    ) -> list[str]:
        """GraphQL ids of the reviews on this PR that are provably ours.

        Same two-part proof `_marked_comments` uses — the marker AND the author
        (`_is_ours`) — and for the same reason: the marker identifies the SHAPE
        of a body and anyone can copy a shape, only the author identifies who
        wrote it. The token here is a person's, so their own reviews are on this
        listing too and must survive it untouched.

        The REST review object carries `node_id`, which IS the GraphQL id
        `minimizeComment` wants, so no second lookup is needed to translate.

        A half-read listing is used as far as it got, unlike the delete path
        which refuses to act on one at all. The asymmetry is deliberate: a
        missed delete leaves a duplicate comment nothing will ever clean up,
        while a missed minimize leaves one extra entry on a timeline the next
        run will fold anyway. Nothing is destroyed either way.
        """
        reviews, _ = self._list_all(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
            "/reviews?per_page=100",
        )
        if not reviews:
            return []
        viewer = self._viewer_login()
        if not viewer:
            # Fail closed — see `_viewer_login`.
            return []
        return [
            r["node_id"] for r in reviews
            if self._is_ours(r, marker, viewer) and isinstance(r.get("node_id"), str)
        ]

    def _minimize_reviews(self, node_ids: list[str]) -> dict[str, int]:
        """Collapse earlier runs' review bodies. Never raises.

        A submitted review is immutable and REST has no delete for it, so
        before this every re-run added one more permanent entry to the
        timeline. `minimizeComment` is the only thing that can reach them, and
        `PullRequestReview` is one of the six types that accept it.

        Two GraphQL requests, both bounded regardless of how many reviews:
        one query that reads `isMinimized`, then ONE mutation document whose
        aliased fields minimize everything still open.

        The `isMinimized` query is what stops a re-run folding the same review
        again every time. It has to be a GraphQL question because REST's review
        object has no field for it — the collapsed state exists only on the
        GraphQL type — and a probe that cannot be read means no mutation this
        run: those reviews are counted `minimize_failed` and the next run tries
        again, which is cheaper than a mutation per stale review per push.
        """
        stats = {"minimized": 0, "minimize_failed": 0, "already_minimized": 0}
        if not node_ids:
            return stats

        probe = self._graphql(
            "query($ids: [ID!]!) { nodes(ids: $ids) { "
            "... on PullRequestReview { id isMinimized } } }",
            {"ids": node_ids},
        )
        if probe is None:
            stats["minimize_failed"] = len(node_ids)
            return stats
        nodes = probe.get("nodes")
        state = {
            n["id"]: bool(n.get("isMinimized"))
            for n in (nodes if isinstance(nodes, list) else [])
            if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        pending = []
        for node_id in node_ids:
            if state.get(node_id) is True:
                stats["already_minimized"] += 1
            elif node_id in state:
                pending.append(node_id)
            else:
                # The id came back null or as some other type: it is not a
                # review we can fold, and guessing is how a mutation hits
                # something it was never meant to touch.
                stats["minimize_failed"] += 1
        if not pending:
            return stats

        # One document, one round trip: `minimizeComment` takes a single
        # subject, so N aliases rather than N requests. The ids travel as
        # variables, never interpolated into the query text.
        fields = " ".join(
            f"m{i}: minimizeComment(input: $i{i}) "
            "{ minimizedComment { isMinimized } }"
            for i in range(len(pending))
        )
        signature = ", ".join(f"$i{i}: MinimizeCommentInput!" for i in range(len(pending)))
        data = self._graphql(
            f"mutation({signature}) {{ {fields} }}",
            {
                f"i{i}": {"subjectId": node_id, "classifier": _MINIMIZE_CLASSIFIER}
                for i, node_id in enumerate(pending)
            },
        )
        for i in range(len(pending)):
            result = (data or {}).get(f"m{i}")
            minimized = (
                isinstance(result, dict)
                and isinstance(result.get("minimizedComment"), dict)
                and result["minimizedComment"].get("isMinimized") is True
            )
            if minimized:
                stats["minimized"] += 1
            else:
                stats["minimize_failed"] += 1
        return stats

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        """One GraphQL round trip. None when the answer is unusable.

        Goes through the SAME guarded client as every REST call: GraphQL lives
        on `api.github.com` too, so `_api_host()` already covers it and no new
        egress exception exists to get wrong.

        GraphQL answers a partly-failed request with HTTP 200, `data` holding
        what worked and `errors` describing what did not — so the errors are
        logged and the data is still returned, and the caller decides per field
        whether it got what it asked for.
        """
        try:
            resp = self._http.post(
                GITHUB_GRAPHQL_URL, json={"query": query, "variables": variables},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "github_graphql_failed status=%d body=%s",
                    resp.status_code, resp.text[:200],
                )
                return None
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("github_graphql_error err=%s", exc)
            return None
        if not isinstance(body, dict):
            logger.warning("github_graphql_unreadable")
            return None
        if body.get("errors"):
            logger.warning("github_graphql_errors detail=%s", str(body["errors"])[:200])
        data = body.get("data")
        return data if isinstance(data, dict) else None

    def _upsert_summary(
        self, owner: str, name: str, pr_number: int, body: str,
        existing_id: int | None,
    ) -> int | None:
        """PATCH the summary we already own, or POST the first one."""
        self._last_summary_status = None
        if existing_id is not None:
            resp = self._http.patch(
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/issues/comments/{existing_id}",
                json={"body": body},
            )
            if resp.status_code == 200:
                return existing_id
            logger.warning(
                "github_summary_patch_failed id=%d status=%d — posting a new one",
                existing_id, resp.status_code,
            )
        resp = self._http.post(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/issues/{pr_number}/comments",
            json={"body": body},
        )
        if resp.status_code == 201:
            cid = resp.json().get("id")
            return cid if isinstance(cid, int) else None
        self._last_summary_status = resp.status_code
        logger.warning("github_summary_post_failed status=%d", resp.status_code)
        return None

    #: HTTP status of the last summary write that failed, for the run record.
    _last_summary_status: int | None = None

    @staticmethod
    def _parse_link_next(link: str) -> str | None:
        """RFC 5988 Link rel="next"."""
        import re
        m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
        return m.group(1) if m else None

    # ─── Reading the target branch (the issues backlog) ──────────

    def branch_head_sha(self, repo: str, branch: str) -> str:
        owner, name = self._split_repo(repo)
        resp = self._http.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/branches/{quote(branch, safe='/')}")
        self._expect_ok(resp, "GitHub branch")
        sha = str(((resp.json() or {}).get("commit") or {}).get("sha") or "")
        if not sha:
            raise PullRequestProviderError(f"GitHub named no commit for branch {branch!r}")
        return sha

    def read_file_at(self, repo: str, ref: str, path: str) -> str | None:
        owner, name = self._split_repo(repo)
        resp = self._http.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if resp.status_code == 404:
            return None
        self._expect_ok(resp, "GitHub file")
        if len(resp.content) > MAX_FILE_BYTES:
            raise PullRequestProviderError(
                f"{path} is larger than {MAX_FILE_BYTES} bytes; not read")
        return resp.content.decode("utf-8", "replace")

    def commits_touching(
        self, repo: str, ref: str, path: str, *,
        since: datetime | None = None, limit: int = 10,
    ) -> list[PathCommit]:
        owner, name = self._split_repo(repo)
        params: dict[str, Any] = {
            "sha": ref, "path": path, "per_page": max(1, min(int(limit), 100)),
        }
        if since is not None:
            params["since"] = since.isoformat()
        resp = self._http.get(f"{GITHUB_API_BASE}/repos/{owner}/{name}/commits",
                              params=params)
        self._expect_ok(resp, "GitHub commits")
        out: list[PathCommit] = []
        for c in resp.json() or []:
            commit = c.get("commit") or {}
            message = str(commit.get("message") or "").strip()
            out.append(PathCommit(
                sha=str(c.get("sha") or ""),
                subject=message.splitlines()[0][:300] if message else "",
                date=((commit.get("committer") or {}).get("date")),
                url=c.get("html_url"),
            ))
        return [c for c in out if c.sha][:limit]

    def file_change_in_commit(
        self, repo: str, sha: str, path: str,
    ) -> FileChange | None:
        owner, name = self._split_repo(repo)
        resp = self._http.get(f"{GITHUB_API_BASE}/repos/{owner}/{name}/commits/{sha}")
        self._expect_ok(resp, "GitHub commit")
        for f in (resp.json() or {}).get("files") or []:
            current, previous = f.get("filename"), f.get("previous_filename")
            if path not in (current, previous):
                continue
            status = str(f.get("status") or "modified")
            if status == "removed":
                return FileChange("deleted", current or path)
            if status == "renamed" and previous == path and current:
                return FileChange("renamed", current, previous_path=previous)
            return FileChange("added" if status == "added" else "modified",
                              current or path)
        return None

    def commit_url(self, repo: str, sha: str) -> str | None:
        owner, name = self._split_repo(repo)
        return f"https://github.com/{owner}/{name}/commit/{sha}"

    def list_comment_reactions(
        self, repo: str, pr_number: int, comment_id: str,
    ) -> list[tuple[str, str]]:
        owner, name = self._split_repo(repo)
        url = (f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/comments/"
               f"{quote(str(comment_id), safe='')}/reactions?per_page=100")
        resp = self._http.get(url)
        self._expect_ok(resp, "GitHub reactions")
        out: list[tuple[str, str]] = []
        for item in resp.json() or []:
            content = str((item or {}).get("content") or "")
            user = str(((item or {}).get("user") or {}).get("login") or "")
            if user and content in ("+1", "-1"):
                out.append((user, "up" if content == "+1" else "down"))
        return out

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        """'owner/name' → (owner, name)."""
        parts = repo.strip().split("/", 1)
        if len(parts) != 2:
            raise PullRequestProviderError(
                f"Invalid GitHub repo format '{repo}'. Expected 'owner/name'."
            )
        return parts[0], parts[1]

    # ─── Conversation (comment commands) ─────────────────────────

    def viewer_ids(self) -> frozenset[str]:
        login = self._viewer_login()
        return frozenset({login}) if login else frozenset()

    def post_reply(self, ev, body: str) -> str | None:
        """Into the review thread when the comment is on the diff; otherwise a
        new PR comment that opens with a quote of who asked (an issue comment
        has no thread to reply into)."""
        owner, name = self._split_repo(ev.repo)
        text = markers.with_chat_marker(body)
        if ev.kind == "inline" and ev.thread_id:
            url = (
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{ev.pr_number}"
                f"/comments/{ev.thread_id}/replies"
            )
        else:
            url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/issues/{ev.pr_number}/comments"
            if ev.actor_name:
                text = f"> @{ev.actor_name}\n\n{text}"
        resp = self._expect_ok(self._http.post(url, json={"body": text}), "github reply")
        cid = _json_id(resp)
        return str(cid) if cid is not None else None

    def update_comment(
        self, repo: str, pr_number: int, comment_id: str, body: str, *, kind: str = "issue",
    ) -> bool:
        owner, name = self._split_repo(repo)
        where = "pulls" if kind == "inline" else "issues"
        resp = self._http.patch(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/{where}/comments/{comment_id}",
            json={"body": markers.with_chat_marker(body)},
        )
        return 200 <= resp.status_code < 300

    def get_thread(self, ev, limit: int = 30) -> list[ThreadMessage]:
        """The review thread a diff comment belongs to: the root and its replies
        (GitHub keeps replies flat, all pointing at the root). A plain PR
        comment has no thread."""
        if ev.kind != "inline" or not ev.thread_id:
            return []
        owner, name = self._split_repo(ev.repo)
        items, _complete = self._list_all(self._inline_comments_url(owner, name, ev.pr_number))
        root = str(ev.thread_id)
        viewer = self._viewer_login()
        found: list[tuple[int, ThreadMessage]] = []
        for c in items:
            cid = c.get("id")
            if not isinstance(cid, int) or (str(cid) != root and str(c.get("in_reply_to_id")) != root):
                continue
            user = c.get("user")
            line = c.get("line") if isinstance(c.get("line"), int) else c.get("original_line")
            found.append((cid, ThreadMessage(
                comment_id=str(cid),
                author=str(user.get("login") or "") if isinstance(user, dict) else "",
                text=str(c.get("body") or ""),
                ours=bool(viewer) and self._authored_by(c, viewer),
                path=c.get("path") if isinstance(c.get("path"), str) else None,
                line=line if isinstance(line, int) else None,
                created_at=c.get("created_at") if isinstance(c.get("created_at"), str) else None,
            )))
        found.sort(key=lambda pair: pair[0])
        return trim_thread([m for _i, m in found], limit)

    def acknowledge(self, ev) -> bool:
        """An eyes reaction on the comment."""
        owner, name = self._split_repo(ev.repo)
        where = "pulls" if ev.kind == "inline" else "issues"
        resp = self._http.post(
            f"{GITHUB_API_BASE}/repos/{owner}/{name}/{where}/comments/{ev.comment_id}/reactions",
            json={"content": "eyes"},
        )
        return 200 <= resp.status_code < 300

    def actor_permission(
        self, repo: str, *, actor_id: str = "", actor_name: str = "",
    ) -> str:
        owner, name = self._split_repo(repo)
        login = actor_name or actor_id
        if not login:
            return "unknown"
        try:
            resp = self._http.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{name}/collaborators/{login}/permission",
            )
            if resp.status_code >= 400:
                return "unknown"
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return "unknown"
        level = str(data.get("permission") or "").lower() if isinstance(data, dict) else ""
        if level in ("admin", "maintain", "write"):
            return "write"
        if level in ("read", "triage"):
            return "read"
        return "none" if level == "none" else "unknown"

    def pr_participants(self, repo: str, pr_number: int) -> frozenset[str]:
        owner, name = self._split_repo(repo)
        try:
            resp = self._http.get(f"{GITHUB_API_BASE}/repos/{owner}/{name}/pulls/{pr_number}")
            if resp.status_code >= 400:
                return frozenset()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return frozenset()
        if not isinstance(data, dict):
            return frozenset()
        people = [data.get("user")]
        people.extend(data.get("requested_reviewers") or [])
        people.extend(data.get("assignees") or [])
        ids: set[str] = set()
        for person in people:
            if isinstance(person, dict):
                ids.update(str(v) for v in (person.get("id"), person.get("login")) if v)
        return frozenset(ids)
