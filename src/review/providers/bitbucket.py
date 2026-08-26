"""Bitbucket Cloud PR provider.

API specifics:
    GET /user                                               — who the token posts as
    GET /repositories/{ws}/{r}/pullrequests/{id}            — PR metadata
    GET .../pullrequests/{id}/diff                          — raw unified diff
    GET .../pullrequests/{id}/comments                      — ONE list: inline, summary, replies
    POST .../pullrequests/{id}/comments                     — inline OR top-level
    PUT  .../pullrequests/{id}/comments/{cid}               — update comment
    DELETE .../pullrequests/{id}/comments/{cid}             — delete comment

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
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.credentials import resolve_git_credential
from src.http import build_client
from src.review.diff import parse_unified_diff
from src.review.models import (
    HunkSide,
    PullRequest,
    ReviewBatch,
)
from src.review.providers.base import (
    PullRequestProvider,
    PullRequestProviderError,
    _anchorable_ranges,
    _format_finding_body,
    _format_summary,
    _snap_to_span,
)
from src.review.settings import get_review_settings

logger = logging.getLogger(__name__)


BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"

# One listing serves both kinds on Bitbucket, but the two are not handled
# alike on a re-run: an inline comment is deleted and re-posted, the summary
# is the one comment PUT in place so its id (and the thread under it) survive.
# The kind travels with the id so the delete pass can tell them apart.
_INLINE_COMMENT = "inline"
_SUMMARY_COMMENT = "summary"


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
    """Bitbucket Cloud PR operations."""

    name = "bitbucket"

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
        if meta_resp.status_code >= 400:
            raise PullRequestProviderError(
                f"Bitbucket API error {meta_resp.status_code}: {meta_resp.text[:200]}"
            )
        meta = meta_resp.json()

        # 2. Raw diff
        diff_resp = self._http.get(
            f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}/pullrequests/{pr_number}/diff"
        )
        if diff_resp.status_code >= 400:
            raise PullRequestProviderError(
                f"Bitbucket diff error {diff_resp.status_code}: "
                f"{diff_resp.text[:200]}"
            )
        raw_diff = diff_resp.text

        settings = get_review_settings()
        hunks, skipped_files = parse_unified_diff(raw_diff, settings=settings)

        source = meta.get("source") or {}
        destination = meta.get("destination") or {}
        author_user = (meta.get("author") or {}).get("nickname") or \
                      (meta.get("author") or {}).get("display_name") or ""

        return PullRequest(
            provider="bitbucket",
            repo=f"{ws}/{name}",
            number=pr_number,
            title=str(meta.get("title") or ""),
            description=str(meta.get("description") or ""),
            author=str(author_user),
            base_ref=str((destination.get("branch") or {}).get("name") or ""),
            base_sha=str((destination.get("commit") or {}).get("hash") or ""),
            head_ref=str((source.get("branch") or {}).get("name") or ""),
            head_sha=str((source.get("commit") or {}).get("hash") or ""),
            state=str(meta.get("state") or "OPEN").lower(),
            is_draft=bool(meta.get("draft", False)),
            url=str((meta.get("links") or {}).get("html", {}).get("href") or ""),
            hunks=hunks,
            raw_diff=raw_diff,
            skipped_files=skipped_files,
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
        if settings.replace_on_synchronize:
            stale, protected, listing_complete = self._marked_comments(
                ws, name, pr.number, settings.comment_marker,
            )

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

        # 2. Inline comments (capped)
        #
        # Same anchor snapping as the other two providers. Bitbucket posts per
        # finding, so an unanchorable line costs one comment rather than the
        # batch — quiet enough that it was never noticed here.
        ranges = _anchorable_ranges(pr)
        posted = 0
        failed = 0
        snapped = 0
        for finding in batch.findings[: settings.max_inline_comments]:
            side = "RIGHT" if finding.side == HunkSide.RIGHT else "LEFT"
            line = _snap_to_span(
                finding.line, ranges.get((finding.file_path, side), []))
            if line != finding.line:
                snapped += 1
                logger.info(
                    "bitbucket_anchor_snapped repo=%s pr=%d path=%s from=%d to=%d",
                    pr.repo, pr.number, finding.file_path, finding.line, line,
                )
            payload: dict[str, Any] = {
                "content": {"raw": _format_finding_body(finding, settings.comment_marker)},
                "inline": {
                    "path": finding.file_path,
                    "to": line,
                },
            }
            # `from` for deleted lines (LEFT side)
            if finding.side == HunkSide.LEFT:
                del payload["inline"]["to"]
                payload["inline"]["from"] = line

            resp = self._http.post(
                f"{BITBUCKET_API_BASE}/repositories/{ws}/{name}"
                f"/pullrequests/{pr.number}/comments",
                json=payload,
            )
            if resp.status_code in (200, 201):
                posted += 1
            else:
                failed += 1
                logger.warning(
                    "bitbucket_inline_comment_failed status=%d body=%s",
                    resp.status_code, resp.text[:200],
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

        return {
            "summary_comment_id": summary_id,
            "inline_posted": posted,
            "inline_failed": failed,
            # `complete: False` means duplicates may still be on the PR — a
            # caller that reports "review posted" can say so rather than let a
            # half-done cleanup look like a finished one.
            "cleanup": cleanup,
        }

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
            if isinstance(root, int) and not self._authored_by_viewer(comment, viewer):
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
        payload = {"content": {"raw": body}}
        if existing_id is not None:
            resp = self._http.put(f"{url_base}/{existing_id}", json=payload)
            if resp.status_code in (200, 201):
                return existing_id
            logger.warning(
                "bitbucket_summary_put_failed id=%d status=%d — posting a new one",
                existing_id, resp.status_code,
            )
        resp = self._http.post(url_base, json=payload)
        if resp.status_code in (200, 201):
            cid = resp.json().get("id")
            return cid if isinstance(cid, int) else None
        logger.warning("bitbucket_summary_post_failed status=%d", resp.status_code)
        return None

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
        if marker not in (raw or ""):
            return False
        return cls._authored_by_viewer(comment, viewer)

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.strip().split("/", 1)
        if len(parts) != 2:
            raise PullRequestProviderError(
                f"Invalid Bitbucket repo format '{repo}'. Expected 'workspace/name'."
            )
        return parts[0], parts[1]
