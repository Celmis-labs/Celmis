"""GitHub: one GraphQL query per 50 PRs, and no detail phase.

The query returns everything a PR row needs — creation, merge time, lines,
files, the first commit, reviews and comments — so a repository of 1000 PRs
costs about 20 requests. GraphQL's `pullRequests` connection cannot filter by
update time, so the walk goes newest-update first, stops at `since`, and the
page list is handed over oldest-first once it is complete.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.productivity.providers.base import (
    ActivityRecord,
    PRDetail,
    ProductivityProvider,
    ProviderDeployment,
    ProviderError,
    PRRecord,
    parse_ts,
)
from src.productivity.ratelimit import RateLimited, parse_retry_after
from src.productivity.settings import ProductivitySettings
from src.repos.open_pulls import GITHUB_API, _same_host

_GRAPHQL = f"{GITHUB_API}/graphql"
_PAGE = 50
_MAX_PAGES = 200

_PR_FRAGMENT = """
fragment PR on PullRequest {
  number title body url state isDraft createdAt updatedAt mergedAt closedAt
  additions deletions changedFiles baseRefName headRefName headRefOid
  mergeCommit { oid }
  author { login __typename }
  commits(first: 1) { totalCount nodes { commit { committedDate authoredDate } } }
  reviews(first: 50) { nodes {
    databaseId state submittedAt body author { login __typename }
    comments(first: 5) { nodes { body } }
  } }
  comments(first: 50) { nodes { databaseId createdAt body author { login __typename } } }
}
"""
_LIST_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: __PAGE__, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...PR }
    }
  }
}
""".replace("__PAGE__", str(_PAGE)) + _PR_FRAGMENT
_ONE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { ...PR } }
}
""" + _PR_FRAGMENT
_COUNT_QUERY = """
query($q: String!) { search(query: $q, type: ISSUE, first: 1) { issueCount } }
"""
_TAGS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/tags/", first: 100, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      nodes { name target {
        __typename
        ... on Commit { oid committedDate }
        ... on Tag { target { ... on Commit { oid committedDate } } }
      } }
    }
  }
}
"""
_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "declined"}
_REVIEW_KINDS = {"APPROVED": "approval", "CHANGES_REQUESTED": "changes_requested"}


def _actor(node: dict[str, Any] | None) -> tuple[str, str, bool]:
    node = node or {}
    login = str(node.get("login") or "")
    return login, login, node.get("__typename") == "Bot" or login.endswith("[bot]")


class GitHubProductivity(ProductivityProvider):
    name = "github"
    requests_per_pr = 1
    page_size = _PAGE

    def __init__(self, full_name, *, client, gate, token: str = "") -> None:
        super().__init__(full_name, client=client, gate=gate)
        self._token = token
        owner, _, name = full_name.partition("/")
        self._owner, self._repo = owner, name

    def _request_kwargs(self) -> dict[str, Any]:
        return {"headers": {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }}

    def _limited(self, response: httpx.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        if response.headers.get("Retry-After"):
            return parse_retry_after(response.headers["Retry-After"])
        if response.headers.get("x-ratelimit-remaining") == "0":
            try:
                return max(1.0, float(response.headers.get("x-ratelimit-reset", "0")) - time.time())
            except ValueError:
                return 60.0
        if response.status_code == 429 or "rate limit" in response.text[:500].lower():
            return 60.0
        return None

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = self._send("POST", _GRAPHQL, json={"query": query, "variables": variables}).json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            if any(e.get("type") == "RATE_LIMITED" for e in errors if isinstance(e, dict)):
                raise RateLimited(self.gate.penalise(60.0), "github rate limit")
            first = errors[0] if isinstance(errors[0], dict) else {}
            raise ProviderError(f"github: GraphQL error: {str(first.get('message'))[:200]}")
        return (payload or {}).get("data") or {}

    # ── records ─────────────────────────────────────────────────────
    @staticmethod
    def _record(n: dict[str, Any]) -> PRRecord:
        state = _STATES.get(str(n.get("state") or "").upper(), "open")
        key, name, _ = _actor(n.get("author"))
        merged_at, closed_at = parse_ts(n.get("mergedAt")), parse_ts(n.get("closedAt"))
        commits = n.get("commits") or {}
        first = next(iter(commits.get("nodes") or []), None)
        commit = (first or {}).get("commit") or {}
        detail = PRDetail(
            first_commit_at=parse_ts(commit.get("authoredDate") or commit.get("committedDate")),
            commits_count=commits.get("totalCount"),
            additions=n.get("additions"), deletions=n.get("deletions"),
            files_changed=n.get("changedFiles"),
            merged_at=merged_at, closed_at=closed_at,
        )
        for review in (n.get("reviews") or {}).get("nodes") or []:
            at = parse_ts(review.get("submittedAt"))
            rkey, rname, rbot = _actor(review.get("author"))
            if not at or not rkey:
                continue
            body = "\n".join([str(review.get("body") or "")] + [
                str(c.get("body") or "") for c in (review.get("comments") or {}).get("nodes") or []])
            kind = _REVIEW_KINDS.get(str(review.get("state") or "").upper(), "review")
            if str(review.get("state") or "").upper() == "PENDING":
                continue
            detail.activity.append(ActivityRecord(
                kind, f"{kind}:{review.get('databaseId')}", rkey, rname, at, raw=body, bot_account=rbot))
        for comment in (n.get("comments") or {}).get("nodes") or []:
            at = parse_ts(comment.get("createdAt"))
            ckey, cname, cbot = _actor(comment.get("author"))
            if at and ckey:
                detail.activity.append(ActivityRecord(
                    "comment", f"comment:{comment.get('databaseId')}", ckey, cname, at,
                    raw=str(comment.get("body") or ""), bot_account=cbot))
        return PRRecord(
            number=int(n.get("number") or 0), title=str(n.get("title") or ""),
            description=str(n.get("body") or ""), url=str(n.get("url") or ""),
            state=state, is_draft=bool(n.get("isDraft")),
            author_key=key, author_name=name,
            source_branch=n.get("headRefName"), target_branch=n.get("baseRefName"),
            created_at=parse_ts(n.get("createdAt")), updated_on=parse_ts(n.get("updatedAt")),
            merged_at=merged_at, closed_at=closed_at if state == "declined" else None,
            merge_commit_sha=(n.get("mergeCommit") or {}).get("oid"),
            head_sha=n.get("headRefOid"), detail=detail,
        )

    def list_pull_requests(self, since: datetime) -> Iterator[list[PRRecord]]:
        found: list[PRRecord] = []
        after: str | None = None
        complete = False
        for _ in range(_MAX_PAGES):
            data = self._graphql(_LIST_QUERY, {"owner": self._owner, "name": self._repo, "after": after})
            conn = ((data.get("repository") or {}).get("pullRequests")) or {}
            stop = False
            for node in conn.get("nodes") or []:
                rec = self._record(node)
                if rec.updated_on and rec.updated_on < since:
                    stop = True
                    break
                found.append(rec)
            info = conn.get("pageInfo") or {}
            if stop or not info.get("hasNextPage"):
                complete = True
                break
            after = info.get("endCursor")
        if not complete:
            # The walk is newest first, so what was cut off is the OLDEST: yielding
            # the rest would move the watermark past those PRs for good.
            raise ProviderError(
                f"github: more than {_MAX_PAGES * _PAGE} pull requests changed since "
                f"{since:%Y-%m-%d}; shorten the backfill window")
        found.sort(key=lambda r: (r.updated_on or datetime.min.replace(tzinfo=UTC), r.number))
        for i in range(0, len(found), _PAGE):
            yield found[i:i + _PAGE]

    def get_pull_request(self, number: int) -> PRRecord | None:
        data = self._graphql(_ONE_QUERY, {"owner": self._owner, "name": self._repo, "number": int(number)})
        node = (data.get("repository") or {}).get("pullRequest")
        return self._record(node) if node else None

    def pr_detail(self, pr: PRRecord) -> PRDetail:
        return pr.detail or PRDetail()

    def count_pull_requests(self, since: datetime) -> int | None:
        q = f"repo:{self.full_name} is:pr updated:>={since.strftime('%Y-%m-%d')}"
        data = self._graphql(_COUNT_QUERY, {"q": q})
        count = (data.get("search") or {}).get("issueCount")
        return int(count) if isinstance(count, int) else None

    def pr_commit_shas(self, number: int) -> list[str]:
        out: list[str] = []
        url: str | None = f"{GITHUB_API}/repos/{self.full_name}/pulls/{int(number)}/commits"
        params: dict[str, Any] | None = {"per_page": 100}
        for _ in range(5):
            if not url or not _same_host(url, GITHUB_API):
                break
            response = self._get(url, params)
            out.extend(str(c["sha"]) for c in response.json() or [] if c.get("sha"))
            url, params = response.links.get("next", {}).get("url"), None
        return out

    # ── deployments ─────────────────────────────────────────────────
    def deployments(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        if settings.deploy_source == "tags":
            return self._tags(since, settings)
        out: list[ProviderDeployment] = []
        url: str | None = f"{GITHUB_API}/repos/{self.full_name}/deployments"
        params: dict[str, Any] | None = {"environment": "production", "per_page": 100}
        for _ in range(5):
            if not url or not _same_host(url, GITHUB_API):
                break
            response = self._get(url, params)
            for d in response.json() or []:
                created = parse_ts(d.get("created_at"))
                if not created or created < since - timedelta(days=1):
                    continue
                status = self._get(f"{GITHUB_API}/repos/{self.full_name}/deployments/{d['id']}/statuses",
                                   {"per_page": 1}).json() or []
                latest = status[0] if status else {}
                if latest.get("state") != "success":
                    continue
                done = parse_ts(latest.get("created_at")) or created
                if done >= since:
                    out.append(ProviderDeployment(
                        external_id=str(d["id"]), deployed_at=done, sha=d.get("sha"),
                        branch=d.get("ref"), environment=str(d.get("environment") or "production")))
            url, params = response.links.get("next", {}).get("url"), None
        return out

    def _tags(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        import re

        try:
            wanted = re.compile(settings.tag_pattern)
        except re.error:
            return []
        data = self._graphql(_TAGS_QUERY, {"owner": self._owner, "name": self._repo})
        out: list[ProviderDeployment] = []
        for node in ((data.get("repository") or {}).get("refs") or {}).get("nodes") or []:
            target = node.get("target") or {}
            if target.get("__typename") == "Tag":
                target = target.get("target") or {}
            when = parse_ts(target.get("committedDate"))
            name = str(node.get("name") or "")
            if when and when >= since and wanted.search(name):
                out.append(ProviderDeployment(
                    external_id=name, deployed_at=when, sha=target.get("oid"), source="tag"))
        return out
