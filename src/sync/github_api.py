"""GitHub REST API client (Stage 1.4, May 2026).

Endpoints used (GitHub REST API v3, current as of May 2026):
    GET /user                       — verify token + auto-derive login
    GET /repos/{owner}/{repo}       — repo metadata
    GET /user/repos                 — current user's repos (with pagination)
    GET /orgs/{org}/repos           — organization's repos
    GET /search/repositories        — search by query

Authentication:
    Bearer header with a Personal Access Token (classic or fine-grained).
    Fine-grained tokens (recommended 2026): scoped to specific repos +
    explicit permissions. Classic PAT — legacy, but works.

Required scopes:
    Public access: no scopes (read-only public repos)
    Private repos: 'repo' scope (classic) or 'Contents: Read' (fine-grained)

Rate limits:
    Authenticated: 5000 req/hour
    Unauthenticated: 60 req/hour (for public repo detection via is_repo_public)

Pagination:
    Via the Link header (rel="next"). Implemented in `_paginate_get()`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.http import build_client

logger = logging.getLogger(__name__)


GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "code-analyzer/0.1"


class GitHubAuthError(Exception):
    """401 — token invalid/expired."""


class GitHubScopeError(Exception):
    """403 — token is valid, but the scope is insufficient."""


class GitHubAPIError(Exception):
    """4xx/5xx other than 401/403."""


@dataclass
class GitHubCredentials:
    """What gets stored in the CredentialStore (provider='github')."""

    token: str
    login: str = ""  # auto-derived from GET /user
    expires_at: str | None = None  # for fine-grained tokens


@dataclass
class GitHubRepoInfo:
    """The subset of GitHub repo metadata that we need."""

    full_name: str  # 'owner/name'
    owner: str
    name: str
    description: str = ""
    language: str = ""
    private: bool = False
    archived: bool = False
    fork: bool = False
    default_branch: str = "main"
    size_kb: int = 0  # GitHub repo size in KB
    updated_at: str = ""
    clone_url: str = ""  # https clone URL
    ssh_url: str = ""
    topics: list[str] = field(default_factory=list)


# ─── Client ─────────────────────────────────────────────────────────


class GitHubClient:
    """Read-only GitHub REST API client."""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self._http = build_client(
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",  # stable API version
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ─── auth verification ─────────────────────────────────────

    def fetch_current_user(self) -> dict[str, Any]:
        """GET /user — verify token + extract login."""
        r = self._http.get(f"{GITHUB_API_BASE}/user")
        if r.status_code == 401:
            raise GitHubAuthError(
                "Token invalid or expired. Check that the PAT has not expired "
                "+ has the right scopes (for private repos — the 'repo' scope)."
            )
        if r.status_code == 403:
            # Could be rate-limited or a scope issue
            ratelimit_remaining = r.headers.get("X-RateLimit-Remaining")
            if ratelimit_remaining == "0":
                reset = r.headers.get("X-RateLimit-Reset", "?")
                raise GitHubAPIError(
                    f"Rate limit exceeded. Resets at {reset}. "
                    f"Use authenticated requests (5000/hour) instead of "
                    f"unauthenticated (60/hour)."
                )
            raise GitHubScopeError(f"403 forbidden: {r.text[:200]}")
        if r.status_code >= 400:
            raise GitHubAPIError(f"unexpected status {r.status_code}: {r.text[:200]}")
        return r.json()

    # ─── repo metadata ─────────────────────────────────────────

    def get_repo(self, owner: str, name: str) -> GitHubRepoInfo | None:
        """GET /repos/{owner}/{name}. None if 404."""
        r = self._http.get(f"{GITHUB_API_BASE}/repos/{owner}/{name}")
        if r.status_code == 404:
            return None
        if r.status_code == 401:
            raise GitHubAuthError("Token invalid")
        if r.status_code == 403:
            raise GitHubScopeError("Insufficient scope")
        if r.status_code >= 400:
            raise GitHubAPIError(f"status {r.status_code}: {r.text[:200]}")
        return _parse_repo(r.json())

    # ─── list repos ───────────────────────────────────────────

    def list_user_repos(
        self,
        *,
        affiliation: str = "owner,collaborator,organization_member",
        sort: str = "updated",
        limit: int = 100,
    ) -> list[GitHubRepoInfo]:
        """GET /user/repos — repos accessible to authenticated user.

        Args:
            affiliation: a combination of 'owner', 'collaborator', 'organization_member'
            sort: 'created' | 'updated' | 'pushed' | 'full_name'
            limit: max repos to return (auto-paginates up to 100/page)
        """
        url = (
            f"{GITHUB_API_BASE}/user/repos"
            f"?per_page=100&sort={sort}&affiliation={affiliation}"
        )
        return self._paginate_get(url, limit)

    def list_org_repos(
        self,
        org: str,
        *,
        repo_type: str = "all",
        limit: int = 200,
    ) -> list[GitHubRepoInfo]:
        """GET /orgs/{org}/repos.

        Args:
            repo_type: 'all' | 'public' | 'private' | 'forks' | 'sources' | 'member'
        """
        url = (
            f"{GITHUB_API_BASE}/orgs/{org}/repos"
            f"?per_page=100&type={repo_type}"
        )
        return self._paginate_get(url, limit)

    def search_repos(
        self,
        query: str,
        *,
        limit: int = 30,
    ) -> list[GitHubRepoInfo]:
        """GET /search/repositories?q={query} — public + accessible private repos.

        Query syntax (GitHub):
            'org:acme python'           — repos in the org with python
            'user:konstantin language:go' — user's Go repos
        """
        url = f"{GITHUB_API_BASE}/search/repositories?q={query}&per_page=100"
        r = self._http.get(url)
        if r.status_code == 401:
            raise GitHubAuthError("Token invalid")
        if r.status_code == 403:
            raise GitHubScopeError("Search rate limited or scope insufficient")
        if r.status_code >= 400:
            raise GitHubAPIError(f"status {r.status_code}: {r.text[:200]}")
        data = r.json()
        items = data.get("items", [])[:limit]
        return [_parse_repo(item) for item in items]

    # ─── helpers ───────────────────────────────────────────────

    def _paginate_get(
        self, url: str, limit: int,
    ) -> list[GitHubRepoInfo]:
        """Walk Link header rel="next" pages until the limit is reached."""
        out: list[GitHubRepoInfo] = []
        next_url: str | None = url
        while next_url and len(out) < limit:
            r = self._http.get(next_url)
            if r.status_code == 401:
                raise GitHubAuthError("Token invalid")
            if r.status_code == 403:
                raise GitHubScopeError("Insufficient scope or rate limited")
            if r.status_code >= 400:
                raise GitHubAPIError(f"status {r.status_code}: {r.text[:200]}")
            for item in r.json():
                if len(out) >= limit:
                    break
                out.append(_parse_repo(item))
            next_url = _parse_link_next(r.headers.get("Link"))
        return out


# ─── parsing helpers ────────────────────────────────────────────────


def _parse_repo(data: dict[str, Any]) -> GitHubRepoInfo:
    owner = (data.get("owner") or {}).get("login", "")
    return GitHubRepoInfo(
        full_name=str(data.get("full_name", "")),
        owner=str(owner),
        name=str(data.get("name", "")),
        description=str(data.get("description") or ""),
        language=str(data.get("language") or ""),
        private=bool(data.get("private", False)),
        archived=bool(data.get("archived", False)),
        fork=bool(data.get("fork", False)),
        default_branch=str(data.get("default_branch", "main")),
        size_kb=int(data.get("size") or 0),
        updated_at=str(data.get("updated_at") or ""),
        clone_url=str(data.get("clone_url") or ""),
        ssh_url=str(data.get("ssh_url") or ""),
        topics=list(data.get("topics") or []),
    )


_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_link_next(link_header: str | None) -> str | None:
    """RFC5988 Link header → next URL (or None)."""
    if not link_header:
        return None
    m = _LINK_RE.search(link_header)
    return m.group(1) if m else None


# ─── module-level convenience ───────────────────────────────────────


def authenticate(token: str) -> GitHubCredentials:
    """Verify token + auto-derive login. Raises GitHubAuthError on bad token."""
    creds = GitHubCredentials(token=token.strip())
    with GitHubClient(creds.token) as client:
        user = client.fetch_current_user()
        creds.login = str(user.get("login") or "")
        if not creds.login:
            raise GitHubAPIError(
                "API returned user info without a login — cannot identify the token owner"
            )
    logger.info("github_authenticated login=%s", creds.login)
    return creds
