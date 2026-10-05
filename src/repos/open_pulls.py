"""Every open pull request of a repository, any target branch, searchable.

WHY THIS EXISTS. The manual-review list asked each provider for ONE page —
50 PRs on GitHub, GitLab and Bitbucket alike — and showed it as the whole
list: on a repository with more open PRs than that, the one you wanted to
review was simply not there, and nothing said the list had been cut. On
Bitbucket it was worse: the listing always sent Basic auth with the saved
Atlassian e-mail, so with a workspace or repository access token (no e-mail,
Bearer auth — what the review provider itself supports) the list failed
outright while a review triggered by hand worked.

WHAT IT DOES.
  * Follows each provider's own "next page" signal to the end — GitHub's
    `Link: rel="next"`, GitLab's `X-Next-Page` (then `Link`), Bitbucket's
    `next` URL — up to `PULL_CAP` PRs, and says when the cap cut it. A "next"
    URL is followed only when it points back at the same API host: the
    request carries the workspace's token.
  * Lists PRs targeting ANY branch. The target branch is an optional filter,
    applied server-side where the provider supports it (to read fewer pages)
    and again in memory.
  * Caches a listing for `CACHE_TTL` seconds per (provider, repository,
    target filter, credential fingerprint), so typing into the search box
    filters in memory instead of re-walking the pages.
  * Searches title, number, author and both branch names, case-insensitively.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from src.http import build_client

logger = logging.getLogger(__name__)

#: Hard ceiling on PRs read per listing (pages of 100, or 50 on Bitbucket).
PULL_CAP = 1000
CACHE_TTL = 30.0
_CACHE_MAX = 128
MAX_QUERY = 200

GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"
BITBUCKET_API = "https://api.bitbucket.org/2.0"

SORTS = ("newest", "recently_updated", "oldest")


@dataclass(frozen=True)
class OpenPull:
    number: int
    title: str
    author: str
    url: str
    source_branch: str | None
    target_branch: str | None
    created_at: str | None
    updated_at: str | None
    draft: bool = False


@dataclass(frozen=True)
class OpenPullListing:
    items: tuple[OpenPull, ...]
    #: True when `PULL_CAP` stopped the walk with pages still to read.
    truncated: bool


_cache: dict[tuple[str, ...], tuple[float, OpenPullListing]] = {}
_cache_lock = threading.Lock()


def clear_pull_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _fingerprint(secret: str, email: str = "") -> str:
    return hashlib.sha256(f"{email}\0{secret}".encode()).hexdigest()[:16]


def _same_host(url: str, base: str) -> bool:
    try:
        got, want = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    except ValueError:
        return False
    # Same scheme as the API base (https, or http only for an instance the
    # operator allowed over http), same host:port, and under the base path —
    # a self-hosted GitLab's next link must not leave its /sub-path/api/v4.
    prefix = want.path.rstrip("/")
    return (got.scheme == want.scheme and got.scheme in ("https", "http")
            and got.netloc.lower() == want.netloc.lower()
            and (not prefix or got.path == prefix or got.path.startswith(prefix + "/")))


def _bbql_value(value: str) -> str:
    """A string BBQL can carry inside double quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ─── providers ───────────────────────────────────────────────────────


def _github(client: httpx.Client, full_name: str, token: str, cap: int,
            target: str | None) -> OpenPullListing:
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    params: dict[str, Any] | None = {"state": "open", "per_page": 100,
                                     "sort": "created", "direction": "desc"}
    if target:
        params["base"] = target
    url: str | None = f"{GITHUB_API}/repos/{full_name}/pulls"
    out: list[OpenPull] = []
    truncated = False
    while url:
        r = client.get(url, headers=headers, params=params)
        r.raise_for_status()
        for p in r.json() or []:
            out.append(OpenPull(
                number=int(p.get("number") or 0),
                title=str(p.get("title") or ""),
                author=str((p.get("user") or {}).get("login") or ""),
                url=str(p.get("html_url") or ""),
                source_branch=(p.get("head") or {}).get("ref") or None,
                target_branch=(p.get("base") or {}).get("ref") or None,
                created_at=p.get("created_at"), updated_at=p.get("updated_at"),
                draft=bool(p.get("draft")),
            ))
        nxt = r.links.get("next", {}).get("url")
        if nxt and len(out) >= cap:
            truncated = True
            break
        url = nxt if nxt and _same_host(nxt, GITHUB_API) else None
        params = None  # the next link carries them
    return OpenPullListing(tuple(out[:cap]), truncated or len(out) > cap)


def _gitlab(client: httpx.Client, full_name: str, token: str, cap: int,
            target: str | None, *, api: str = GITLAB_API) -> OpenPullListing:
    headers = {"PRIVATE-TOKEN": token}
    pid = urllib.parse.quote(full_name, safe="")
    base = f"{api}/projects/{pid}/merge_requests"
    params: dict[str, Any] = {"state": "opened", "per_page": 100, "page": 1,
                              "order_by": "created_at", "sort": "desc"}
    if target:
        params["target_branch"] = target
    out: list[OpenPull] = []
    truncated = False
    url: str | None = base
    while url:
        r = client.get(url, headers=headers, params=params if url == base else None)
        r.raise_for_status()
        for m in r.json() or []:
            out.append(OpenPull(
                number=int(m.get("iid") or 0),
                title=str(m.get("title") or ""),
                author=str((m.get("author") or {}).get("username") or ""),
                url=str(m.get("web_url") or ""),
                source_branch=m.get("source_branch") or None,
                target_branch=m.get("target_branch") or None,
                created_at=m.get("created_at"), updated_at=m.get("updated_at"),
                draft=bool(m.get("draft") or m.get("work_in_progress")),
            ))
        next_page = (r.headers.get("X-Next-Page") or "").strip()
        link_next = r.links.get("next", {}).get("url")
        if (next_page or link_next) and len(out) >= cap:
            truncated = True
            break
        if next_page.isdigit():
            params = {**params, "page": int(next_page)}
            url = base
        elif link_next and _same_host(link_next, api):
            url = link_next
        else:
            url = None
    return OpenPullListing(tuple(out[:cap]), truncated or len(out) > cap)


def bitbucket_auth(email: str, token: str) -> dict[str, Any]:
    """Basic auth with the Atlassian e-mail for an API token; Bearer for a
    workspace or repository access token, which has no e-mail — the same
    split `BitbucketPRProvider` makes. Always sending (email, token) made a
    Bearer token's listing a 401."""
    if email:
        return {"auth": (email, token)}
    return {"headers": {"Authorization": f"Bearer {token}"}}


def _bitbucket(client: httpx.Client, full_name: str, email: str, token: str,
               cap: int, target: str | None) -> OpenPullListing:
    auth = bitbucket_auth(email, token)
    params: dict[str, Any] | None = {"state": "OPEN", "pagelen": 50,
                                     "sort": "-created_on"}
    if target:
        params["q"] = (f'state="OPEN" AND destination.branch.name='
                       f'"{_bbql_value(target)}"')
    url: str | None = f"{BITBUCKET_API}/repositories/{full_name}/pullrequests"
    out: list[OpenPull] = []
    truncated = False
    while url:
        r = client.get(url, params=params, **auth)
        r.raise_for_status()
        payload = r.json() or {}
        for p in payload.get("values", []) or []:
            author = p.get("author") or {}
            out.append(OpenPull(
                number=int(p.get("id") or 0),
                title=str(p.get("title") or ""),
                author=str(author.get("display_name") or author.get("nickname") or ""),
                url=str(((p.get("links") or {}).get("html") or {}).get("href") or ""),
                source_branch=((p.get("source") or {}).get("branch") or {}).get("name")
                or None,
                target_branch=((p.get("destination") or {}).get("branch") or {})
                .get("name") or None,
                created_at=p.get("created_on"), updated_at=p.get("updated_on"),
                draft=bool(p.get("draft")),
            ))
        nxt = payload.get("next")
        if nxt and len(out) >= cap:
            truncated = True
            break
        url = str(nxt) if nxt and _same_host(str(nxt), BITBUCKET_API) else None
        params = None  # the next URL carries them
    return OpenPullListing(tuple(out[:cap]), truncated or len(out) > cap)


def fetch_open_pulls(provider: str, full_name: str, secret: str, email: str = "",
                     *, target: str | None = None, cap: int | None = None,
                     gitlab=None) -> OpenPullListing:
    """Walk every page (up to `cap`). Raises `httpx.HTTPError` on failure.

    `gitlab` — the workspace's GitLabInstance (None → gitlab.com)."""
    from src.sync.gitlab_instance import DEFAULT_INSTANCE

    cap = cap or PULL_CAP
    inst = gitlab or DEFAULT_INSTANCE
    extra = inst.http_kwargs() if provider == "gitlab" else {}
    with build_client(timeout=15.0, **extra) as client:
        if provider == "github":
            return _github(client, full_name, secret, cap, target)
        if provider == "gitlab":
            return _gitlab(client, full_name, secret, cap, target, api=inst.api_base)
        if provider == "bitbucket":
            return _bitbucket(client, full_name, email, secret, cap, target)
    return OpenPullListing((), False)


def cached_open_pulls(provider: str, full_name: str, secret: str, email: str = "",
                      *, target: str | None = None, refresh: bool = False,
                      gitlab=None) -> OpenPullListing:
    """`fetch_open_pulls`, reused for `CACHE_TTL` seconds. Failures are
    never cached."""
    instance_key = gitlab.base_url if gitlab is not None else ""
    key = (provider, full_name, target or "", _fingerprint(secret, email), instance_key)
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    listing = fetch_open_pulls(provider, full_name, secret, email, target=target,
                               gitlab=gitlab)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, listing)
    return listing


def matches(pr: OpenPull, q: str) -> bool:
    """Title, #number, author or either branch, case-insensitive."""
    term = (q or "").strip()[:MAX_QUERY]
    if not term:
        return True
    bare = term.lstrip("#").strip()
    if bare.isdigit() and int(bare) == pr.number:
        return True
    low = term.lower()
    return any(low in (v or "").lower() for v in (
        pr.title, pr.author, pr.source_branch, pr.target_branch))


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def select(items, *, q: str = "", target: str | None = None,
           sort: str = "newest") -> list[OpenPull]:
    """Filter by search and target branch, then order."""
    out = [p for p in items
           if matches(p, q) and (not target or p.target_branch == target)]
    if sort == "oldest":
        out.sort(key=lambda p: (_ts(p.created_at), p.number))
    elif sort == "recently_updated":
        out.sort(key=lambda p: (-_ts(p.updated_at or p.created_at), -p.number))
    else:
        out.sort(key=lambda p: (-_ts(p.created_at), -p.number))
    return out
