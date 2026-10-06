"""GitLab (gitlab.com or a self-hosted instance): REST, two calls per MR.

The list takes `updated_after` and sorts ascending, so the watermark moves
page by page. Detail is the commit list (first commit, count) and the notes
(comments, and the system note "approved this merge request"). Line counts are
NOT read: GitLab's list does not carry them and the per-MR `changes` call is
the heaviest one in the API. `additions`/`deletions` stay NULL on GitLab and
the size metrics say so rather than showing zero.
"""

from __future__ import annotations

import urllib.parse
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
from src.repos.open_pulls import _same_host

_STATES = {"opened": "open", "merged": "merged", "closed": "declined", "locked": "declined"}
_PER_PAGE = 100
_MAX_PAGES = 10


def _is_bot(username: str) -> bool:
    return (username.startswith("project_") and username.endswith("_bot")) or username.endswith("[bot]")


class GitLabProductivity(ProductivityProvider):
    name = "gitlab"
    requests_per_pr = 3
    page_size = _PER_PAGE

    def __init__(self, full_name, *, client, gate, token: str = "",
                 api_base: str = "https://gitlab.com/api/v4") -> None:
        super().__init__(full_name, client=client, gate=gate)
        self._token = token
        self._api = api_base.rstrip("/")
        self._project = f"{self._api}/projects/{urllib.parse.quote(full_name, safe='')}"

    def _request_kwargs(self) -> dict[str, Any]:
        return {"headers": {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}}

    def _paged(self, url: str, params: dict[str, Any], limit: int = _MAX_PAGES) -> Iterator[tuple[Any, Any]]:
        """(json, response) per page; `X-Next-Page` first, then a same-host `Link: next`."""
        current: str | None = url
        query: dict[str, Any] | None = {**params, "per_page": params.get("per_page", _PER_PAGE)}
        for _ in range(limit):
            if not current or not _same_host(current, self._api):
                return
            response = self._get(current, query)
            yield response.json(), response
            next_page = (response.headers.get("X-Next-Page") or "").strip()
            link = response.links.get("next", {}).get("url")
            if next_page.isdigit():
                current, query = url, {**(query or params), "page": int(next_page)}
            elif link:
                current, query = link, None
            else:
                return

    @staticmethod
    def _record(m: dict[str, Any]) -> PRRecord:
        state = _STATES.get(str(m.get("state") or ""), "open")
        author = m.get("author") or {}
        return PRRecord(
            number=int(m.get("iid") or 0), title=str(m.get("title") or ""),
            description=str(m.get("description") or ""), url=str(m.get("web_url") or ""),
            state=state, is_draft=bool(m.get("draft") or m.get("work_in_progress")),
            author_key=str(author.get("id") or ""), author_name=str(author.get("username") or ""),
            source_branch=m.get("source_branch"), target_branch=m.get("target_branch"),
            created_at=parse_ts(m.get("created_at")), updated_on=parse_ts(m.get("updated_at")),
            merged_at=parse_ts(m.get("merged_at")),
            closed_at=parse_ts(m.get("closed_at")) if state == "declined" else None,
            merge_commit_sha=m.get("merge_commit_sha") or m.get("squash_commit_sha"),
            head_sha=m.get("sha"),
        )

    def list_pull_requests(self, since: datetime) -> Iterator[list[PRRecord]]:
        params = {"state": "all", "scope": "all", "order_by": "updated_at", "sort": "asc",
                  "updated_after": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for data, _ in self._paged(f"{self._project}/merge_requests", params, limit=10_000):
            page = [self._record(m) for m in data or [] if m.get("iid")]
            if page:
                yield page

    def get_pull_request(self, number: int) -> PRRecord | None:
        try:
            data = self._get(f"{self._project}/merge_requests/{int(number)}").json()
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return self._record(data) if isinstance(data, dict) and data.get("iid") else None

    def count_pull_requests(self, since: datetime) -> int | None:
        response = self._get(f"{self._project}/merge_requests", {
            "state": "all", "scope": "all", "per_page": 1,
            "updated_after": since.strftime("%Y-%m-%dT%H:%M:%SZ")})
        total = response.headers.get("X-Total", "")
        return int(total) if total.isdigit() else None

    def pr_detail(self, pr: PRRecord) -> PRDetail:
        detail = PRDetail(merged_at=pr.merged_at, closed_at=pr.closed_at)
        base = f"{self._project}/merge_requests/{pr.number}"
        first = self._get(f"{base}/commits", {"per_page": _PER_PAGE})
        commits = first.json() or []
        total = first.headers.get("X-Total", "")
        pages = first.headers.get("X-Total-Pages", "")
        if pages.isdigit() and int(pages) > 1:
            # Newest first: the oldest commit is on the last page.
            commits = self._get(f"{base}/commits", {"per_page": _PER_PAGE, "page": int(pages)}).json() or commits
        dates = [d for d in (parse_ts(c.get("authored_date") or c.get("created_at")) for c in commits) if d]
        detail.first_commit_at = min(dates) if dates else None
        detail.commits_count = int(total) if total.isdigit() else len(commits)
        for notes, _ in self._paged(f"{base}/notes", {"sort": "asc", "order_by": "created_at"}, limit=5):
            for note in notes or []:
                self._take_note(note, detail)
        return detail

    @staticmethod
    def _take_note(note: dict[str, Any], detail: PRDetail) -> None:
        at = parse_ts(note.get("created_at"))
        author = note.get("author") or {}
        key, name = str(author.get("id") or ""), str(author.get("username") or "")
        if not at or not key:
            return
        body = str(note.get("body") or "")
        if note.get("system"):
            if body.lower().startswith("approved this merge request"):
                detail.activity.append(ActivityRecord(
                    "approval", f"approval:{note.get('id')}", key, name, at))
            return
        detail.activity.append(ActivityRecord(
            "comment", f"comment:{note.get('id')}", key, name, at, raw=body, bot_account=_is_bot(name)))

    def pr_commit_shas(self, number: int) -> list[str]:
        out: list[str] = []
        url = f"{self._project}/merge_requests/{int(number)}/commits"
        for data, _ in self._paged(url, {"per_page": _PER_PAGE}, limit=5):
            out.extend(str(c["id"]) for c in data or [] if c.get("id"))
        return out

    def deployments(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        if settings.deploy_source == "tags":
            return self._tags(since, settings)
        out: list[ProviderDeployment] = []
        params = {"environment": "production", "status": "success", "order_by": "finished_at",
                  "sort": "desc", "per_page": _PER_PAGE,
                  "updated_after": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for data, _ in self._paged(f"{self._project}/deployments", params, limit=5):
            for d in data or []:
                done = parse_ts((d.get("deployable") or {}).get("finished_at") or d.get("updated_at"))
                sha = d.get("sha") or (((d.get("deployable") or {}).get("commit") or {}).get("id"))
                if done and done >= since:
                    out.append(ProviderDeployment(
                        external_id=str(d.get("id")), deployed_at=done, sha=sha, branch=d.get("ref")))
        return out

    def _tags(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        import re

        try:
            wanted = re.compile(settings.tag_pattern)
        except re.error:
            return []
        out: list[ProviderDeployment] = []
        params = {"order_by": "updated", "sort": "desc", "per_page": _PER_PAGE}
        for data, _ in self._paged(f"{self._project}/repository/tags", params, limit=3):
            for t in data or []:
                commit = t.get("commit") or {}
                when = parse_ts(commit.get("created_at") or commit.get("committed_date"))
                name = str(t.get("name") or "")
                if when and when >= since and wanted.search(name):
                    out.append(ProviderDeployment(
                        external_id=name, deployed_at=when, sha=commit.get("id"), source="tag"))
        return out


__all__ = ["GitLabProductivity"]
