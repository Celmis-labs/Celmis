"""One adapter interface for the three providers, and the plumbing they share.

The adapters return NORMALISED dataclasses and never touch the database: the
engine (`src/productivity/sync.py`) decides what to keep. They take an
`httpx.Client` — built through `src.http.build_client` in production, a mock
transport in tests — and a `RateGate`; every request goes through `_get`,
which spends a token first, refuses a redirect (or follows a few, same-API only, where the adapter allows it), and turns a 429 (or GitHub's
secondary limit) into `RateLimited` instead of an exception the caller has to
parse.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from src.productivity.ratelimit import DEFAULT_MAX_WAIT, RateGate, RateLimited, parse_retry_after
from src.productivity.settings import ProductivitySettings

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """The provider refused or answered nonsense. The message has no secrets."""


@dataclass(frozen=True)
class ActivityRecord:
    """A comment, an approval or a review on a PR. `raw` is read once, for the bot check, and never stored."""

    kind: str  # comment | approval | review | changes_requested
    external_id: str
    actor_key: str
    actor_name: str
    at: datetime
    raw: str = ""
    #: The provider itself says this account is an app/bot (GitHub `[bot]`, GitLab bot users).
    bot_account: bool = False


@dataclass
class PRDetail:
    first_commit_at: datetime | None = None
    commits_count: int | None = None
    additions: int | None = None
    deletions: int | None = None
    files_changed: int | None = None
    #: The exact merge/close time when the detail knows it (Bitbucket's activity feed).
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    activity: list[ActivityRecord] = field(default_factory=list)


@dataclass
class PRRecord:
    number: int
    title: str
    state: str  # open | merged | declined
    created_at: datetime | None
    updated_on: datetime | None
    author_key: str = ""
    author_name: str = ""
    description: str = ""
    url: str = ""
    is_draft: bool = False
    source_branch: str | None = None
    target_branch: str | None = None
    merged_at: datetime | None = None
    merged_at_approx: bool = False
    closed_at: datetime | None = None
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    #: Present when the LIST already carried everything (GitHub's GraphQL): no detail phase.
    detail: PRDetail | None = None


@dataclass(frozen=True)
class ProviderDeployment:
    external_id: str
    deployed_at: datetime
    sha: str | None = None
    environment: str = "production"
    branch: str | None = None
    status: str = "success"
    source: str = "provider"  # provider | tag


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_ts(value: object) -> datetime | None:
    """An ISO-8601 timestamp (Z or offset, any fraction) as an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", value.strip()))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ProductivityProvider(ABC):
    """What the engine needs from a provider. `full_name` is `owner/repo`."""

    name: str = ""
    #: A rough count of API calls one PR costs, for the backfill estimate.
    requests_per_pr: int = 4
    page_size: int = 50

    def __init__(self, full_name: str, *, client: httpx.Client, gate: RateGate) -> None:
        self.full_name = full_name
        self.client = client
        self.gate = gate
        #: The engine lowers this to the time left in the job, so a wait that
        #: would outlast the job ends it (RateLimited) instead of sleeping.
        self.max_wait: float = DEFAULT_MAX_WAIT

    # ── the interface ───────────────────────────────────────────────
    @abstractmethod
    def list_pull_requests(self, since: datetime) -> Iterator[list[PRRecord]]:
        """PRs updated at or after `since`, OLDEST update first, in pages.

        Ascending order is what lets the engine move its watermark page by
        page: a crash or a rate limit resumes after the last saved page.
        """

    @abstractmethod
    def get_pull_request(self, number: int) -> PRRecord | None: ...

    @abstractmethod
    def pr_detail(self, pr: PRRecord) -> PRDetail: ...

    @abstractmethod
    def pr_commit_shas(self, number: int) -> list[str]: ...

    @abstractmethod
    def deployments(self, since: datetime, settings: ProductivitySettings) -> list[ProviderDeployment]:
        """Provider deployments (`deploy_source=provider`) or tags (`tags`) since `since`."""

    @abstractmethod
    def count_pull_requests(self, since: datetime) -> int | None:
        """How many PRs `list_pull_requests(since)` will return, when the provider can say cheaply."""

    # ── plumbing ────────────────────────────────────────────────────
    def _request_kwargs(self) -> dict[str, Any]:
        return {}

    def _limited(self, response: httpx.Response) -> float | None:
        """Seconds to back off when this response is a rate limit, else None."""
        if response.status_code == 429:
            return parse_retry_after(response.headers.get("Retry-After"))
        return None

    def _get(self, url: str, params: Mapping[str, Any] | None = None, **kw: Any) -> httpx.Response:
        return self._send("GET", url, params=params, **kw)

    #: How many redirect hops `_send` takes by itself. 0 = a redirect is an error.
    #: A provider that raises it must also say where a hop may go (`_may_follow`).
    max_redirects: int = 0

    def _may_follow(self, target: str) -> bool:
        """Whether a redirect may be followed to `target` (and carry the credentials there)."""
        return False

    def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """One request, and, when the provider allows it, the same-API redirect hops after it.

        The client never follows redirects by itself (it could be walked off
        the allowlist with the token in hand). A hop is taken here, one at a
        time, only to a URL `_may_follow` accepts, and goes through the rate
        gate like any request. A redirect that is still one after the last
        hop is an error, not a response.
        """
        for hop in range(self.max_redirects + 1):
            response = self._send_once(method, url, **kw)
            if not 300 <= response.status_code < 400:
                return response
            location = response.headers.get("location") or ""
            target = urljoin(url, location) if location else ""
            if hop >= self.max_redirects or not target or not self._may_follow(target):
                raise ProviderError(f"{self.name}: unexpected redirect ({response.status_code})")
            url = target
            kw.pop("params", None)  # the Location carries its own query
        raise AssertionError("unreachable")  # pragma: no cover

    def _send_once(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.gate.acquire(self.max_wait)
        extra = self._request_kwargs()
        headers = {**extra.pop("headers", {}), **(kw.pop("headers", None) or {})}
        try:
            response = self.client.request(method, url, headers=headers, **extra, **kw)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: request timed out ({type(exc).__name__})") from None
        back_off = self._limited(response)
        if back_off is not None:
            until = self.gate.penalise(back_off)
            raise RateLimited(until, f"{self.name} rate limit")
        if response.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {response.status_code} for {_path(url)}")
        return response


def _path(url: str) -> str:
    """The URL without its query: logs and errors never carry a token or a search term."""
    return url.split("?", 1)[0]
