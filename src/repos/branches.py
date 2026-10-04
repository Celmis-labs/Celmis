"""Every branch of a repository, read from its provider, searchable.

WHY THIS EXISTS. Each branch dropdown used to ask the provider for ONE page —
`per_page=100` on GitHub and GitLab, `pagelen=100` on Bitbucket — and showed
whatever came back as if it were the whole list. On a repository with a few
hundred branches the branch you wanted was simply not there, and nothing on
screen said the list had been cut. The review-policy picker was worse: it read
the local clone, which is `--single-branch`, so it offered one branch.

WHAT IT DOES.
  * Follows each provider's own "next page" signal to the end — GitHub's
    `Link: rel="next"`, GitLab's `X-Next-Page` (then `Link`), Bitbucket's
    `next` URL — up to `BRANCH_CAP` names, and says when the cap cut it.
  * A "next" URL is followed only when it points back at the same API host:
    the request carries the workspace's token, and a page link is data from
    the network, not an instruction to send that token somewhere else.
  * Caches a listing for `CACHE_TTL` seconds per (provider, repository,
    credential fingerprint), so typing into a search box re-filters a list in
    memory instead of re-walking thirty pages. The fingerprint is a hash, the
    token itself never becomes a dictionary key, and two workspaces with
    different tokens never share an entry.
  * Searches case-insensitively by substring. When the full listing was cut by
    the cap, GitLab (`search=`) and Bitbucket (BBQL `name ~ "…"`) are asked to
    search server-side too, so a branch past the cap is still findable. GitHub
    has no branch-search API; there the cached list is filtered and the answer
    keeps saying `truncated`.
  * Orders: the default branch first, then most recently updated where the
    listing carries a commit date (GitLab, Bitbucket — free in the same
    response), otherwise alphabetically (GitHub's branch list has no dates and
    one extra call per branch is not "cheap").
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from src.http import build_client

logger = logging.getLogger(__name__)

#: Hard ceiling on names read per listing. 50 pages of 100: enough for any
#: repository a person picks branches from by eye, bounded for one that has
#: tens of thousands of stale CI branches.
BRANCH_CAP = 5000
PAGE_SIZE = 100
#: Seconds a listing is reused. Short enough that a branch pushed a minute ago
#: appears on the next open; long enough that every keystroke of a search is
#: served from memory.
CACHE_TTL = 90.0
_CACHE_MAX = 256
#: Longest search term accepted. A branch name is rarely past 100 characters;
#: this only stops an absurd query string from reaching the provider.
MAX_QUERY = 200

GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"
BITBUCKET_API = "https://api.bitbucket.org/2.0"


@dataclass(frozen=True)
class BranchListing:
    """Branch names in display order (default first)."""

    names: tuple[str, ...]
    default_branch: str | None
    #: True when `BRANCH_CAP` stopped the walk with pages still to read.
    truncated: bool


@dataclass(frozen=True)
class BranchPage:
    """One answer to the UI: up to `limit` names and what was left out."""

    branches: list[str]
    default_branch: str | None
    #: Matches in the listing — may exceed len(branches).
    total: int
    truncated: bool


# ─── cache ───────────────────────────────────────────────────────────

_cache: dict[tuple[str, ...], tuple[float, BranchListing]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def clear_branch_cache() -> None:
    with _cache_lock:
        _cache.clear()


def credential_fingerprint(secret: str, email: str = "") -> str:
    """A stable, non-reversible tag for a credential — the cache key part."""
    return hashlib.sha256(f"{email}\0{secret}".encode()).hexdigest()[:24]


def _cache_get(key: tuple[str, ...]) -> BranchListing | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        expires, listing = hit
        if expires <= _now():
            _cache.pop(key, None)
            return None
        return listing


def _cache_put(key: tuple[str, ...], listing: BranchListing) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Drop the entry closest to expiry; the cache is tiny and this
            # runs at most once per miss.
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (_now() + CACHE_TTL, listing)


# ─── helpers ─────────────────────────────────────────────────────────


def normalize_query(q: str | None) -> str:
    return (q or "").strip()[:MAX_QUERY]


def _same_host(url: str, base: str) -> bool:
    """Is ``url`` on the very API host ``base`` names, over https?"""
    try:
        got, want = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    except ValueError:
        return False
    return got.scheme == "https" and got.netloc.lower() == want.netloc.lower()


def _order(
    items: list[tuple[str, str | None]], default: str | None,
) -> tuple[str, ...]:
    """Default first; then newest commit first when dates exist, else A→Z."""
    seen: set[str] = set()
    unique: list[tuple[str, str | None]] = []
    for name, date in items:
        if name and name not in seen:
            seen.add(name)
            unique.append((name, date))
    if any(d for _, d in unique):
        # ISO-8601 strings with offsets do not sort as text across zones;
        # parse what we can and put undated names last, alphabetically.
        def key(item: tuple[str, str | None]) -> tuple[int, float, str]:
            name, date = item
            ts = _ts(date)
            return (0 if ts is not None else 1, -(ts or 0.0), name.lower())
        unique.sort(key=key)
    else:
        unique.sort(key=lambda it: it[0].lower())
    names = [n for n, _ in unique]
    if default and default in seen:
        names.remove(default)
        names.insert(0, default)
    return tuple(names)


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bbql_term(q: str) -> str:
    """The longest quote- and backslash-free piece of ``q``.

    BBQL is a quoted mini-language: an unescaped " or \\ would 400 the call,
    and deleting them would search for a string no branch contains. The
    provider is only asked to NARROW; `search_names` then applies the full
    term to what comes back.
    """
    parts = [p for p in re.split(r'["\\]', q) if p]
    return max(parts, key=len) if parts else ""


# ─── providers ───────────────────────────────────────────────────────


def _github(client: httpx.Client, full_name: str, token: str,
            cap: int) -> BranchListing:
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    default: str | None = None
    try:
        meta = client.get(f"{GITHUB_API}/repos/{full_name}", headers=headers)
        if meta.status_code == 200:
            default = str(meta.json().get("default_branch") or "") or None
    except (httpx.HTTPError, ValueError):
        pass

    items: list[tuple[str, str | None]] = []
    url: str | None = f"{GITHUB_API}/repos/{full_name}/branches"
    params: dict[str, Any] | None = {"per_page": PAGE_SIZE}
    truncated = False
    while url:
        r = client.get(url, headers=headers, params=params)
        r.raise_for_status()
        for b in r.json():
            name = str((b or {}).get("name") or "")
            if name:
                items.append((name, None))
        nxt = r.links.get("next", {}).get("url")
        if nxt and len(items) >= cap:
            truncated = True
            break
        url = nxt if nxt and _same_host(nxt, GITHUB_API) else None
        params = None  # the next link already carries them
    truncated = truncated or len(items) > cap
    return BranchListing(_order(items[:cap], default), default, truncated)


def _gitlab(client: httpx.Client, full_name: str, token: str, cap: int,
            search: str = "") -> BranchListing:
    headers = {"PRIVATE-TOKEN": token}
    pid = urllib.parse.quote(full_name, safe="")
    base = f"{GITLAB_API}/projects/{pid}/repository/branches"
    params: dict[str, Any] = {"per_page": PAGE_SIZE, "page": 1,
                              "sort": "updated_desc"}
    if search:
        params["search"] = search
    items: list[tuple[str, str | None]] = []
    default: str | None = None
    truncated = False
    url: str | None = base
    while url:
        r = client.get(url, headers=headers,
                       params=params if url == base else None)
        r.raise_for_status()
        for b in r.json():
            name = str((b or {}).get("name") or "")
            if not name:
                continue
            if b.get("default"):
                default = name
            items.append((name, ((b.get("commit") or {}).get("committed_date"))))
        next_page = (r.headers.get("X-Next-Page") or "").strip()
        link_next = r.links.get("next", {}).get("url")
        more = bool(next_page) or bool(link_next)
        if more and len(items) >= cap:
            truncated = True
            break
        if next_page.isdigit():
            params = {**params, "page": int(next_page)}
            url = base
        elif link_next and _same_host(link_next, GITLAB_API):
            url = link_next
        else:
            url = None
    truncated = truncated or len(items) > cap
    return BranchListing(_order(items[:cap], default), default, truncated)


def _bitbucket(client: httpx.Client, full_name: str, email: str, token: str,
               cap: int, search: str = "") -> BranchListing:
    auth = (email, token)
    default: str | None = None
    try:
        meta = client.get(f"{BITBUCKET_API}/repositories/{full_name}",
                          auth=auth, params={"fields": "mainbranch.name"})
        if meta.status_code == 200:
            default = str((meta.json().get("mainbranch") or {}).get("name") or "") or None
    except (httpx.HTTPError, ValueError):
        pass

    params: dict[str, Any] | None = {"pagelen": PAGE_SIZE, "sort": "-target.date"}
    term = _bbql_term(search)
    if term and params is not None:
        params["q"] = f'name ~ "{term}"'
    items: list[tuple[str, str | None]] = []
    truncated = False
    url: str | None = f"{BITBUCKET_API}/repositories/{full_name}/refs/branches"
    while url:
        r = client.get(url, auth=auth, params=params)
        r.raise_for_status()
        payload = r.json()
        for b in payload.get("values", []):
            name = str((b or {}).get("name") or "")
            if name:
                items.append((name, ((b.get("target") or {}).get("date"))))
        nxt = payload.get("next")
        if nxt and len(items) >= cap:
            truncated = True
            break
        url = str(nxt) if nxt and _same_host(str(nxt), BITBUCKET_API) else None
        params = None
    truncated = truncated or len(items) > cap
    return BranchListing(_order(items[:cap], default), default, truncated)


def fetch_branches(provider: str, full_name: str, secret: str, email: str = "",
                   *, search: str = "", cap: int | None = None) -> BranchListing:
    """Walk every page (up to ``cap``). Raises ``httpx.HTTPError`` on failure.

    ``search`` narrows server-side where the provider can (GitLab, Bitbucket);
    GitHub ignores it — the caller filters.
    """
    cap = cap or BRANCH_CAP
    with build_client(timeout=15.0) as client:
        if provider == "github":
            return _github(client, full_name, secret, cap)
        if provider == "gitlab":
            return _gitlab(client, full_name, secret, cap, search)
        if provider == "bitbucket":
            return _bitbucket(client, full_name, email, secret, cap, search)
    return BranchListing((), None, False)


def cached_branches(provider: str, full_name: str, secret: str, email: str = "",
                    *, search: str = "") -> BranchListing:
    """`fetch_branches` behind the short per-credential cache. Failures are
    never cached: the next open retries."""
    key = (provider, full_name.lower(), credential_fingerprint(secret, email),
           search.lower())
    hit = _cache_get(key)
    if hit is not None:
        return hit
    listing = fetch_branches(provider, full_name, secret, email, search=search)
    _cache_put(key, listing)
    return listing


# ─── search ──────────────────────────────────────────────────────────


def search_names(names: list[str] | tuple[str, ...], q: str,
                 default: str | None = None) -> list[str]:
    """Case-insensitive substring match. Order: the default branch (if it
    matches), exact name, prefix matches, then the rest — each group in the
    listing's own order."""
    needle = q.lower()
    if not needle:
        return list(names)
    exact: list[str] = []
    prefix: list[str] = []
    rest: list[str] = []
    head: list[str] = []
    for n in names:
        low = n.lower()
        if needle not in low:
            continue
        if n == default:
            head.append(n)
        elif low == needle:
            exact.append(n)
        elif low.startswith(needle):
            prefix.append(n)
        else:
            rest.append(n)
    return head + exact + prefix + rest


def page_of(listing: BranchListing, q: str, limit: int) -> BranchPage:
    matched = search_names(listing.names, q, listing.default_branch)
    return BranchPage(
        branches=matched[:limit], default_branch=listing.default_branch,
        total=len(matched), truncated=listing.truncated,
    )


def branch_page(provider: str, full_name: str, secret: str, email: str = "",
                *, q: str = "", limit: int = 100) -> BranchPage:
    """The answer a dropdown needs: matches for ``q``, at most ``limit``."""
    q = normalize_query(q)
    full = cached_branches(provider, full_name, secret, email)
    if not q or not full.truncated or provider not in ("gitlab", "bitbucket"):
        return page_of(full, q, limit)
    # The cap cut the full list: ask the provider to search the whole repo.
    narrowed = cached_branches(provider, full_name, secret, email, search=q)
    merged = BranchListing(
        names=tuple(dict.fromkeys((*narrowed.names, *full.names))),
        default_branch=full.default_branch or narrowed.default_branch,
        truncated=narrowed.truncated,
    )
    return page_of(merged, q, limit)
