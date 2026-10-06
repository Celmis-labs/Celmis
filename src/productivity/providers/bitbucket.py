"""Bitbucket Cloud: the list, then three calls per PR.

Cost per PR: commits (first commit date and count), activity (comments,
approvals and the exact MERGED/DECLINED time, which the list does not carry)
and diffstat (lines). About four calls with the last-page lookup.

NOT VERIFIED AGAINST A LIVE WORKSPACE: the BBQL form of the `updated_on`
filter (unquoted ISO timestamp), and whether `fields=` keeps the union members
of `/activity`. Check both against a real repository before trusting a backfill.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from src.productivity.providers.base import (
    ActivityRecord,
    PRDetail,
    ProductivityProvider,
    ProviderDeployment,
    ProviderError,
    PRRecord,
    parse_ts,
)
from src.productivity.settings import ProductivitySettings
from src.repos.open_pulls import BITBUCKET_API, _same_host, bitbucket_auth

logger = logging.getLogger(__name__)

_STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "declined", "SUPERSEDED": "declined"}
_LIST_FIELDS = ",".join((
    "next", "values.id", "values.title", "values.description", "values.state", "values.draft",
    "values.created_on", "values.updated_on", "values.author.account_id", "values.author.uuid",
    "values.author.nickname", "values.author.display_name", "values.source.branch.name",
    "values.source.commit.hash", "values.destination.branch.name", "values.merge_commit.hash",
    "values.links.html.href",
))
_PR_FIELDS = ",".join(f[len("values."):] for f in _LIST_FIELDS.split(",") if f.startswith("values."))
_ACTIVITY_FIELDS = ",".join((
    "next", "values.update.state", "values.update.date", "values.approval.date",
    "values.approval.user.account_id", "values.approval.user.uuid",
    "values.approval.user.nickname", "values.approval.user.display_name",
    "values.comment.id", "values.comment.created_on", "values.comment.deleted",
    "values.comment.content.raw", "values.comment.user.account_id", "values.comment.user.uuid",
    "values.comment.user.nickname", "values.comment.user.display_name",
))
_COMMIT_PAGE = 50
_MAX_PAGES = 10
#: The activity feed is NEWEST first and the first review sits at its far end, so a busy PR
#: gets a longer walk than the other lists: 40 pages x 50 = 2000 items.
_ACTIVITY_PAGES = 40


def _who(user: dict[str, Any] | None) -> tuple[str, str]:
    """(stable key, display name). The account id beats the uuid; a nickname is mutable and never a key."""
    user = user or {}
    key = str(user.get("account_id") or user.get("uuid") or "")
    return key, str(user.get("display_name") or user.get("nickname") or "")


class BitbucketProductivity(ProductivityProvider):
    name = "bitbucket"
    requests_per_pr = 4
    page_size = 50
    #: `/pullrequests/{id}/diff` answers 302 (S1), and `/diffstat` is a sibling: a hop
    #: or three to the same API are normal here, a hop anywhere else is not.
    max_redirects = 3

    def __init__(self, full_name, *, client, gate, email: str = "", token: str = "") -> None:
        super().__init__(full_name, client=client, gate=gate)
        self._auth = bitbucket_auth(email, token)

    # ── plumbing ────────────────────────────────────────────────────
    @property
    def _base(self) -> str:
        return f"{BITBUCKET_API}/repositories/{self.full_name}"

    def _request_kwargs(self) -> dict[str, Any]:
        out = dict(self._auth)
        out["headers"] = {"Accept": "application/json", **(out.get("headers") or {})}
        return out

    def _may_follow(self, target: str) -> bool:
        return _same_host(target, BITBUCKET_API)

    def _json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _same_host(url, BITBUCKET_API):
            raise ProviderError("bitbucket: a link led away from the API host")
        data = self._get(url, params).json()
        return data if isinstance(data, dict) else {}

    def _pages(self, url: str, params: dict[str, Any], limit: int = _MAX_PAGES) -> Iterator[dict[str, Any]]:
        """Follow `next` (same host only), at most `limit` pages."""
        nxt: str | None = url
        for _ in range(limit):
            if not nxt:
                return
            payload = self._json(nxt, params if nxt == url else None)
            yield payload
            nxt = payload.get("next") if isinstance(payload.get("next"), str) else None

    # ── list ────────────────────────────────────────────────────────
    @staticmethod
    def _record(p: dict[str, Any]) -> PRRecord:
        state = _STATES.get(str(p.get("state") or "").upper(), "open")
        key, name = _who(p.get("author"))
        updated = parse_ts(p.get("updated_on"))
        source = p.get("source") or {}
        return PRRecord(
            number=int(p.get("id") or 0),
            title=str(p.get("title") or ""),
            description=str(p.get("description") or ""),
            url=str(((p.get("links") or {}).get("html") or {}).get("href") or ""),
            state=state,
            is_draft=bool(p.get("draft")),
            author_key=key, author_name=name,
            source_branch=(source.get("branch") or {}).get("name"),
            target_branch=((p.get("destination") or {}).get("branch") or {}).get("name"),
            created_at=parse_ts(p.get("created_on")), updated_on=updated,
            # The list has no merge time. `updated_on` stands in until the
            # activity feed gives the real one, and says so.
            merged_at=updated if state == "merged" else None,
            merged_at_approx=state == "merged",
            closed_at=updated if state == "declined" else None,
            merge_commit_sha=(p.get("merge_commit") or {}).get("hash"),
            head_sha=(source.get("commit") or {}).get("hash"),
        )

    def list_pull_requests(self, since: datetime) -> Iterator[list[PRRecord]]:
        params = {
            "state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"],
            "q": f"updated_on >= {since.strftime('%Y-%m-%dT%H:%M:%S')}+00:00",
            "sort": "updated_on", "pagelen": self.page_size, "fields": _LIST_FIELDS,
        }
        for payload in self._pages(f"{self._base}/pullrequests", params, limit=10_000):
            page = [self._record(p) for p in payload.get("values") or [] if p.get("id")]
            if page:
                yield page

    def get_pull_request(self, number: int) -> PRRecord | None:
        try:
            payload = self._json(f"{self._base}/pullrequests/{int(number)}",
                                 {"fields": _PR_FIELDS})
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return self._record(payload) if payload.get("id") else None

    def count_pull_requests(self, since: datetime) -> int | None:
        payload = self._json(f"{self._base}/pullrequests", {
            "state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"],
            "q": f"updated_on >= {since.strftime('%Y-%m-%dT%H:%M:%S')}+00:00",
            "pagelen": 1, "fields": "size",
        })
        size = payload.get("size")
        return int(size) if isinstance(size, int) else None

    # ── detail ──────────────────────────────────────────────────────
    def pr_detail(self, pr: PRRecord) -> PRDetail:
        detail = PRDetail()
        self._commits(pr.number, detail)
        self._activity(pr, detail)
        self._diffstat(pr.number, detail)
        return detail

    def _commits(self, number: int, detail: PRDetail) -> None:
        url = f"{self._base}/pullrequests/{number}/commits"
        params = {"pagelen": _COMMIT_PAGE, "fields": "next,size,values.date,values.hash"}
        first = self._json(url, params)
        size = first.get("size") if isinstance(first.get("size"), int) else None
        values = first.get("values") or []
        count = size if size is not None else len(values)
        if first.get("next") and size:
            # Newest first: the oldest commit is on the last page. Ask for it
            # by number instead of walking every page between.
            last = self._json(url, {**params, "page": math.ceil(size / _COMMIT_PAGE)})
            values = last.get("values") or values
        elif first.get("next"):
            # Bitbucket marks `size` optional and often leaves it out of commit
            # lists. Without it the last page cannot be addressed, so walk the
            # pages (bounded) and take the oldest date and the count from them.
            values, count = list(values), len(values)
            nxt = first["next"] if isinstance(first.get("next"), str) else None
            for _ in range(_MAX_PAGES - 1):
                if not nxt:
                    break
                page = self._json(nxt)
                more = page.get("values") or []
                values.extend(more)
                count += len(more)
                nxt = page.get("next") if isinstance(page.get("next"), str) else None
        dates = [d for d in (parse_ts(v.get("date")) for v in values) if d]
        detail.first_commit_at = min(dates) if dates else None
        detail.commits_count = count

    def _activity(self, pr: PRRecord, detail: PRDetail) -> None:
        url = f"{self._base}/pullrequests/{pr.number}/activity"
        last: dict[str, Any] = {}
        for last in self._pages(url, {"pagelen": 50, "fields": _ACTIVITY_FIELDS}, limit=_ACTIVITY_PAGES):
            for item in last.get("values") or []:
                self._take_activity(item, detail)
        if last.get("next"):
            # The oldest items (the first review) are the ones that fell off: say so in the log,
            # the pickup/review time of this PR may come out too late.
            logger.warning("bitbucket_activity_truncated pr=%s pages=%s", pr.number, _ACTIVITY_PAGES)

    @staticmethod
    def _take_activity(item: dict[str, Any], detail: PRDetail) -> None:
        update = item.get("update")
        if isinstance(update, dict):
            at, state = parse_ts(update.get("date")), str(update.get("state") or "").upper()
            if at and state == "MERGED":
                detail.merged_at = max(at, detail.merged_at) if detail.merged_at else at
            elif at and state in ("DECLINED", "SUPERSEDED"):
                detail.closed_at = max(at, detail.closed_at) if detail.closed_at else at
            return
        approval = item.get("approval")
        if isinstance(approval, dict):
            at = parse_ts(approval.get("date"))
            key, name = _who(approval.get("user"))
            if at and key:
                detail.activity.append(ActivityRecord(
                    "approval", f"approval:{key}:{at.isoformat()}", key, name, at))
            return
        comment = item.get("comment")
        if isinstance(comment, dict) and not comment.get("deleted"):
            at = parse_ts(comment.get("created_on"))
            key, name = _who(comment.get("user"))
            if at and key:
                detail.activity.append(ActivityRecord(
                    "comment", f"comment:{comment.get('id')}", key, name, at,
                    raw=str((comment.get("content") or {}).get("raw") or "")))

    def _diffstat(self, number: int, detail: PRDetail) -> None:
        """Lines and files. A diffstat that cannot be read leaves the size UNKNOWN (None):
        the PR keeps its dates and reviews, it just falls out of the size buckets."""
        url = f"{self._base}/pullrequests/{number}/diffstat"
        added = removed = files = 0
        try:
            for payload in self._pages(url, {"pagelen": 500, "fields": "next,values.lines_added,values.lines_removed"}, limit=4):
                for v in payload.get("values") or []:
                    added += int(v.get("lines_added") or 0)
                    removed += int(v.get("lines_removed") or 0)
                    files += 1
        except ProviderError as exc:
            logger.warning("bitbucket_diffstat_failed pr=%s error=%s", number, exc)
            return
        detail.additions, detail.deletions, detail.files_changed = added, removed, files

    def pr_commit_shas(self, number: int) -> list[str]:
        out: list[str] = []
        url = f"{self._base}/pullrequests/{int(number)}/commits"
        for payload in self._pages(url, {"pagelen": 100, "fields": "next,values.hash"}):
            out.extend(str(v["hash"]) for v in payload.get("values") or [] if v.get("hash"))
        return out

    # ── deployments ─────────────────────────────────────────────────
    def deployments(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        if settings.deploy_source == "tags":
            return self._tags(since, settings)
        return self._provider_deployments(since)

    def _provider_deployments(self, since: datetime) -> list[ProviderDeployment]:
        production: set[str] = set()
        for payload in self._pages(f"{self._base}/environments/", {"pagelen": 100}, limit=3):
            for env in payload.get("values") or []:
                kind = str(((env.get("environment_type") or {}).get("name")) or "").lower()
                if kind == "production" or str(env.get("name") or "").lower() == "production":
                    production.add(str(env.get("uuid")))
        out: list[ProviderDeployment] = []
        for payload in self._pages(f"{self._base}/deployments/", {"pagelen": 100}, limit=10):
            for d in payload.get("values") or []:
                state = d.get("state") or {}
                status = str((state.get("status") or {}).get("name") or "").upper()
                done = parse_ts(state.get("completed_on") or state.get("started_on"))
                env_id = str((d.get("environment") or {}).get("uuid"))
                if not done or done < since or env_id not in production or status != "SUCCESSFUL":
                    continue
                out.append(ProviderDeployment(
                    external_id=str(d.get("uuid")), deployed_at=done,
                    sha=((d.get("release") or {}).get("commit") or {}).get("hash"),
                ))
        return out

    def _tags(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        import re

        try:
            wanted = re.compile(settings.tag_pattern)
        except re.error:
            return []
        out: list[ProviderDeployment] = []
        params = {"pagelen": 100, "sort": "-target.date"}
        for payload in self._pages(f"{self._base}/refs/tags", params, limit=5):
            for t in payload.get("values") or []:
                name = str(t.get("name") or "")
                when = parse_ts((t.get("target") or {}).get("date"))
                if when and when >= since and wanted.search(name):
                    out.append(ProviderDeployment(
                        external_id=name, deployed_at=when, sha=(t.get("target") or {}).get("hash"),
                        source="tag"))
        return out

