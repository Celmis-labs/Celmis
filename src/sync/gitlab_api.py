"""GitLab REST API v4 client (Stage 1.5, May 2026).

Endpoints (GitLab REST API v4, current as of May 2026):
    GET /api/v4/user                                    — verify token
    GET /api/v4/projects/{id_or_url_encoded_path}       — project metadata
    GET /api/v4/projects?membership=true                — user's projects
    GET /api/v4/groups/{id}/projects                    — group's projects
    GET /api/v4/projects?search={query}                 — search

Authentication:
    PRIVATE-TOKEN header (Personal Access Token) — recommended
    OR Authorization: Bearer (for OAuth tokens)

Required scopes (PAT):
    Public projects: no scopes (read_api)
    Private projects: 'read_api' + 'read_repository'

Pagination:
    X-Next-Page header (present/empty)
    X-Total + X-Total-Pages — for UI progress
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from src.http import build_client

logger = logging.getLogger(__name__)


GITLAB_API_BASE = "https://gitlab.com/api/v4"
USER_AGENT = "code-analyzer/0.1"


class GitLabAuthError(Exception):
    """401 — token invalid/expired."""


class GitLabScopeError(Exception):
    """403 — insufficient scope."""


class GitLabAPIError(Exception):
    """4xx/5xx other than 401/403."""


@dataclass
class GitLabCredentials:
    """Stored in the CredentialStore (provider='gitlab')."""

    token: str
    username: str = ""  # auto-derived
    user_id: int | None = None
    expires_at: str | None = None


@dataclass
class GitLabProjectInfo:
    """Subset GitLab project metadata."""

    id: int
    full_path: str  # 'group/subgroup/project'
    name: str  # plain project name (without groups)
    description: str = ""
    default_branch: str = "main"
    visibility: str = "private"  # 'public' | 'internal' | 'private'
    archived: bool = False
    fork: bool = False
    last_activity_at: str = ""
    http_url_to_repo: str = ""
    ssh_url_to_repo: str = ""
    topics: list[str] = field(default_factory=list)


# ─── Client ─────────────────────────────────────────────────────────


class GitLabClient:
    """Read-only GitLab v4 API client."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = GITLAB_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        # gitlab.com is already on the shipped public allowlist; the host
        # exception matters only for a self-hosted `api_base`, and it is
        # derived from that configured value — never from request data — as
        # src/http.py's rule demands.
        api_host = urlsplit(self.api_base).hostname or ""
        self._http = build_client(
            timeout=timeout,
            extra_allowed_hosts=(api_host,) if api_host else (),
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    def __enter__(self) -> GitLabClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ─── auth ─────────────────────────────────────────────────

    def fetch_current_user(self) -> dict[str, Any]:
        r = self._http.get(f"{self.api_base}/user")
        if r.status_code == 401:
            raise GitLabAuthError(
                "Token invalid or expired. Check the PAT scopes — "
                "'read_api' is the minimum."
            )
        if r.status_code == 403:
            raise GitLabScopeError(
                f"Insufficient scope. Need 'read_api'. Response: {r.text[:200]}"
            )
        if r.status_code >= 400:
            raise GitLabAPIError(f"status {r.status_code}: {r.text[:200]}")
        return r.json()

    # ─── project metadata ─────────────────────────────────────

    def get_project(
        self, full_path_or_id: str | int,
    ) -> GitLabProjectInfo | None:
        """GET /projects/{id_or_url_encoded_path}.

        Args:
            full_path_or_id: 'group/subgroup/project' string OR numeric ID.
                String automatically url-encoded (per GitLab API spec).
        """
        if isinstance(full_path_or_id, str):
            ident = quote(full_path_or_id, safe="")
        else:
            ident = str(full_path_or_id)
        r = self._http.get(f"{self.api_base}/projects/{ident}")
        if r.status_code == 404:
            return None
        if r.status_code == 401:
            raise GitLabAuthError("Token invalid")
        if r.status_code == 403:
            raise GitLabScopeError("Insufficient scope")
        if r.status_code >= 400:
            raise GitLabAPIError(f"status {r.status_code}: {r.text[:200]}")
        return _parse_project(r.json())

    # ─── list projects ────────────────────────────────────────

    def list_user_projects(
        self,
        *,
        membership: bool = True,
        owned: bool = False,
        order_by: str = "last_activity_at",
        limit: int = 200,
    ) -> list[GitLabProjectInfo]:
        """GET /projects with a membership/owned filter.

        Args:
            membership: True → projects user is member of (default — broader)
            owned: True → user-owned only
            order_by: 'id' | 'name' | 'path' | 'created_at' | 'updated_at' | 'last_activity_at'
        """
        params = []
        if membership:
            params.append("membership=true")
        if owned:
            params.append("owned=true")
        params.append("per_page=100")
        params.append(f"order_by={order_by}")
        url = f"{self.api_base}/projects?{'&'.join(params)}"
        return self._paginate_get(url, limit)

    def list_group_projects(
        self,
        group: str | int,
        *,
        include_subgroups: bool = True,
        limit: int = 200,
    ) -> list[GitLabProjectInfo]:
        """GET /groups/{id}/projects."""
        ident = quote(group, safe="") if isinstance(group, str) else str(group)
        params = ["per_page=100"]
        if include_subgroups:
            params.append("include_subgroups=true")
        url = f"{self.api_base}/groups/{ident}/projects?{'&'.join(params)}"
        return self._paginate_get(url, limit)

    def search_projects(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> list[GitLabProjectInfo]:
        """GET /projects?search={query} — search projects accessible to user."""
        url = f"{self.api_base}/projects?search={quote(query, safe='')}&per_page=100"
        return self._paginate_get(url, limit)

    # ─── helpers ───────────────────────────────────────────────

    def _paginate_get(
        self, url: str, limit: int,
    ) -> list[GitLabProjectInfo]:
        """Walk X-Next-Page header pages."""
        out: list[GitLabProjectInfo] = []
        current_url: str | None = url
        while current_url and len(out) < limit:
            r = self._http.get(current_url)
            if r.status_code == 401:
                raise GitLabAuthError("Token invalid")
            if r.status_code == 403:
                raise GitLabScopeError("Insufficient scope")
            if r.status_code >= 400:
                raise GitLabAPIError(f"status {r.status_code}: {r.text[:200]}")

            for item in r.json():
                if len(out) >= limit:
                    break
                out.append(_parse_project(item))

            # GitLab pagination via X-Next-Page (number)
            next_page = r.headers.get("X-Next-Page", "").strip()
            if not next_page:
                break
            # Replace the `page=` query — keep all the other params
            current_url = _replace_page_param(url, next_page)
        return out


# ─── parsing helpers ────────────────────────────────────────────────


def _parse_project(data: dict[str, Any]) -> GitLabProjectInfo:
    return GitLabProjectInfo(
        id=int(data.get("id") or 0),
        full_path=str(
            data.get("path_with_namespace")
            or data.get("full_path")
            or ""
        ),
        name=str(data.get("name", "")),
        description=str(data.get("description") or ""),
        default_branch=str(data.get("default_branch") or "main"),
        visibility=str(data.get("visibility") or "private"),
        archived=bool(data.get("archived", False)),
        fork=bool(data.get("forked_from_project") is not None),
        last_activity_at=str(data.get("last_activity_at") or ""),
        http_url_to_repo=str(data.get("http_url_to_repo") or ""),
        ssh_url_to_repo=str(data.get("ssh_url_to_repo") or ""),
        topics=list(data.get("topics") or []),
    )


def _replace_page_param(url: str, page: str) -> str:
    """Set/replace the `page=N` query parameter (keeps the other params).

    Carefully matches only a standalone `page` (with a '?' or '&' boundary),
    so as not to false-positive on `per_page` or other similar params.
    """
    import re

    # Match `?page=N` or `&page=N` (boundary-aware)
    pattern = re.compile(r"(?P<sep>[?&])page=\d+")
    if pattern.search(url):
        return pattern.sub(rf"\g<sep>page={page}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


# ─── module-level convenience ───────────────────────────────────────


def authenticate(token: str) -> GitLabCredentials:
    """Verify + auto-derive username/id."""
    creds = GitLabCredentials(token=token.strip())
    with GitLabClient(creds.token) as client:
        user = client.fetch_current_user()
        creds.username = str(user.get("username") or "")
        creds.user_id = int(user.get("id") or 0) or None
        if not creds.username:
            raise GitLabAPIError(
                "API returned user info without a username — cannot identify the token owner"
            )
    logger.info(
        "gitlab_authenticated username=%s user_id=%s",
        creds.username, creds.user_id,
    )
    return creds
