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
from urllib.parse import quote, urlsplit

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
        api_base: str = GITLAB_API_BASE,
        timeout: float = 30.0,
    ) -> None:
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
        self.api_base = api_base.rstrip("/")
        #: Who this token posts as. Looked up once, on the first cleanup that
        #: needs it; "" means the lookup failed and nothing may be deleted.
        self._viewer_cache: str | None = None
        # Guarded egress (src/http.py), not a raw httpx.Client. gitlab.com is
        # already on the shipped public allowlist; the host exception matters
        # only for a self-hosted `api_base`, and it is derived from that
        # configured value — never from request data — the same way
        # GitHubPRProvider derives _api_host() from its constant.
        api_host = urlsplit(self.api_base).hostname or ""
        self._http = build_client(
            timeout=timeout,
            extra_allowed_hosts=(api_host,) if api_host else (),
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
        if meta_resp.status_code >= 400:
            raise PullRequestProviderError(
                f"GitLab API error {meta_resp.status_code}: {meta_resp.text[:200]}"
            )
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
        elif diff_resp.status_code >= 400:
            raise PullRequestProviderError(
                f"GitLab raw_diffs error {diff_resp.status_code}: "
                f"{diff_resp.text[:200]}"
            )
        else:
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
        if settings.replace_on_synchronize:
            stale, listing_complete = self._marked_notes(
                project_path, pr.number, settings.comment_marker,
            )
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
                    "gitlab_anchor_snapped project=%s mr=%d path=%s from=%d to=%d",
                    project_path, pr.number, finding.file_path,
                    finding.line, line,
                )
            payload = {
                "body": _format_finding_body(finding, settings.comment_marker),
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
            else:
                failed += 1
                logger.warning(
                    "gitlab_discussion_failed status=%d body=%s",
                    resp.status_code, resp.text[:200],
                )

        # 4. Drop the previous run's notes, now that this run's are up
        keep_summary_id, cleanup = self._delete_stale_notes(
            project_path, pr.number, stale,
            protected=protected, listing_complete=listing_complete,
        )

        # 5. Top-level summary note
        summary_id = self._upsert_summary(
            project_path, pr.number,
            _format_summary(batch, marker=settings.comment_marker),
            keep_summary_id,
        )

        return {
            "summary_note_id": summary_id,
            "discussions_posted": posted,
            "discussions_failed": failed,
            # `complete: False` means duplicates may still be on the MR — a
            # caller reporting "review posted" can say so rather than let a
            # half-done cleanup look like a finished one.
            "cleanup": cleanup,
        }

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
                foreign = any(
                    not n.get("system") and not self._authored_by(n, viewer)
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
        keep_summary_id: int | None = None
        deleted = 0
        failed = 0
        kept_threaded = 0
        for kind, nid in stale:
            if kind == _SUMMARY_NOTE and keep_summary_id is None:
                keep_summary_id = nid
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
        logger.warning("gitlab_summary_post_failed status=%d", resp.status_code)
        return None

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
        if marker not in (note.get("body") or ""):
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
