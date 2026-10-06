"""A small, guarded Jira Cloud REST v3 client — read-only, five calls.

Everything that can go wrong with the tracker is turned into a `JiraError`
whose `sentence` is safe to print on a pull request: it names the host and the
status code, never the token, never a URL carrying credentials, never the
exception's own text. A review treats a `JiraError` as "no task context, and
here is why" — it must never be the reason a review fails.

Egress: the client is built by `src.http.build_client` with the connection's
own host as the one extra allowed destination and the address the instance
validated pinned (src/sync/jira_instance.py); redirects are not followed (a
redirect on a REST call means the site URL is wrong, and following it is how
a token would leave the host it was saved for).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from src.sync.jira_instance import JiraInstance, UnsafeJiraURL

logger = logging.getLogger(__name__)

#: What a Jira issue key looks like — checked before one is put in a URL.
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")

#: The issue fields one read asks for (plus the acceptance-criteria field).
ISSUE_FIELDS = (
    "summary", "description", "issuetype", "status", "priority", "parent",
    "subtasks", "labels", "updated",
)

_PROJECT_PAGE = 100
_PROJECT_PAGES = 10


class JiraError(Exception):
    """Jira could not be read. `kind` says why in a word, `sentence` in a line."""

    def __init__(self, kind: str, sentence: str) -> None:
        super().__init__(sentence)
        self.kind = kind
        self.sentence = sentence

    def __str__(self) -> str:
        return self.sentence


class JiraClient:
    """Read-only access to one Jira site with one token."""

    def __init__(
        self, instance: JiraInstance, email: str, token: str, *,
        timeout: float = 8.0, client: httpx.Client | None = None,
    ) -> None:
        self.instance = instance
        self.timeout = timeout
        # time.monotonic() after which no further request is made (set by the
        # caller that owns an overall budget).
        self.deadline: float | None = None
        self._owns_client = client is None
        if client is None:
            from src.http import build_client

            try:
                client = build_client(
                    timeout=timeout,
                    headers={"Accept": "application/json",
                             "User-Agent": "celmis-task-context/1"},
                    auth=(str(email), token),
                    **instance.http_kwargs(),
                )
            except UnsafeJiraURL as exc:
                raise JiraError("blocked", f"Jira: {exc}") from None
        self._http = client

    def __repr__(self) -> str:    # never the token, never the email
        return f"JiraClient(host={self.instance.host!r})"

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # ── transport ──────────────────────────────────────────────

    def _get(
        self, path: str, params: dict[str, Any] | None = None, *,
        root: str | None = None, noun: str = "task",
    ) -> Any:
        """GET `path` under `root` (default: the site's REST v3 base)."""
        from src.security.egress import EgressBlockedError

        host = self.instance.host
        request_timeout: httpx.Timeout | None = None
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise JiraError("timeout", f"Jira at {host}: the time allowed for reading it ran out")
            if remaining < self.timeout:
                request_timeout = httpx.Timeout(remaining)
        try:
            url = f"{root or self.instance.api_base}{path}"
            if request_timeout is not None:
                resp = self._http.get(url, params=params, timeout=request_timeout)
            else:
                resp = self._http.get(url, params=params)
        except EgressBlockedError:
            raise JiraError(
                "blocked",
                f"Jira at {host}: outbound connections to this host are blocked by "
                "the server's egress policy") from None
        except httpx.TimeoutException:
            raise JiraError(
                "timeout",
                f"Jira at {host} did not answer within {self.timeout:g} seconds") from None
        except httpx.HTTPError as exc:
            raise JiraError(
                "unreachable",
                f"Jira at {host}: could not connect ({type(exc).__name__})") from None
        status = resp.status_code
        if 300 <= status < 400:
            raise JiraError(
                "error",
                f"Jira at {host} answered a redirect ({status}); redirects are not "
                "followed — check the site URL on the Connections page")
        if status == 401:
            raise JiraError(
                "auth",
                "Jira rejected the token (401) — it may have expired; reconnect Jira "
                "on the Connections page")
        if status == 403:
            raise JiraError(
                "forbidden", "Jira returned 403: this token cannot read that")
        if status == 404:
            raise JiraError(
                "not_found",
                f"Jira returned 404: the {noun} is missing, or this token cannot see it")
        if status == 429:
            raise JiraError("error", "Jira is rate limiting requests (429)")
        if status >= 400:
            raise JiraError("error", f"Jira returned {status}")
        try:
            return resp.json()
        except ValueError:
            raise JiraError(
                "error", f"Jira at {host} did not answer like Jira — check the site URL"
            ) from None

    # ── calls ──────────────────────────────────────────────────

    def myself(self) -> dict[str, Any]:
        """GET /myself — who the token is. The connection's test."""
        data = self._get("/myself")
        if not isinstance(data, dict) or not (data.get("accountId") or data.get("name")):
            raise JiraError(
                "error",
                f"Jira at {self.instance.host} did not answer like Jira — check the "
                "site URL")
        return data

    def get_issue(self, key: str, *, extra_fields: tuple[str, ...] = ()) -> dict[str, Any]:
        """GET /issue/{key} with the fields a review reads."""
        _check_key(key)
        data = self._get(
            f"/issue/{key}", {"fields": ",".join((*ISSUE_FIELDS, *extra_fields))})
        if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
            raise JiraError("error", f"Jira returned something unexpected for {key}")
        return data

    def get_page(self, page_id: str) -> dict[str, Any]:
        """GET a Confluence page of the same site (REST v2, ADF body), for the
        on-demand check when a page link is given instead of a task key. Same
        host, same credentials, same guards as every other call."""
        if not re.fullmatch(r"\d{1,15}", str(page_id)):
            raise JiraError("error", "That is not a Confluence page id")
        data = self._get(
            f"/pages/{page_id}", {"body-format": "atlas_doc_format"},
            root=f"{self.instance.base_url}/wiki/api/v2", noun="page")
        if not isinstance(data, dict):
            raise JiraError("error", f"Confluence returned something unexpected for page {page_id}")
        return data

    def get_updated(self, key: str) -> str | None:
        """GET /issue/{key}?fields=updated — the cheap "has it changed" probe."""
        _check_key(key)
        data = self._get(f"/issue/{key}", {"fields": "updated"})
        fields = data.get("fields") if isinstance(data, dict) else None
        value = fields.get("updated") if isinstance(fields, dict) else None
        return str(value) if value else None

    def get_comments(self, key: str, limit: int) -> list[dict[str, Any]]:
        """The latest `limit` comments, newest first."""
        _check_key(key)
        if limit <= 0:
            return []
        data = self._get(
            f"/issue/{key}/comment",
            {"orderBy": "-created", "maxResults": min(int(limit), 10)})
        comments = data.get("comments") if isinstance(data, dict) else None
        return [c for c in comments or [] if isinstance(c, dict)][:limit]

    def list_fields(self) -> list[dict[str, Any]]:
        """GET /field — for the acceptance-criteria field picker."""
        data = self._get("/field")
        return [f for f in data if isinstance(f, dict)] if isinstance(data, list) else []

    def list_projects(self) -> list[dict[str, str]]:
        """Every project the token can browse: [{key, name}] (first 1000)."""
        out: list[dict[str, str]] = []
        for page in range(_PROJECT_PAGES):
            data = self._get(
                "/project/search",
                {"startAt": page * _PROJECT_PAGE, "maxResults": _PROJECT_PAGE})
            values = data.get("values") if isinstance(data, dict) else None
            for p in values or []:
                if isinstance(p, dict) and p.get("key"):
                    out.append({"key": str(p["key"]).upper(),
                                "name": str(p.get("name") or "")})
            if not isinstance(data, dict) or data.get("isLast", True) or not values:
                break
        return out


def _check_key(key: str) -> None:
    if not KEY_PATTERN.match(str(key)):
        raise JiraError("not_found", f"{str(key)[:20]!r} is not a Jira issue key")
