"""Auto-reviewer assignment via ownership graph.

After a review completes we look at every changed file, resolve owners
via the ownership snapshot, and request them as PR reviewers on the
underlying provider (GitHub for MVP; GitLab/Bitbucket follow with the
same shape).

Design constraints:
    * Idempotent — safe to call multiple times per PR (provider APIs
      dedupe by username).
    * Non-blocking — every error is logged; a failed assignment must
      never fail the review pipeline.
    * Bounded — cap at 3 reviewers so we don't spam.
    * Skip the PR author (provider errors on self-review) and anyone
      already listed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from src.http import build_client

logger = logging.getLogger(__name__)

_MAX_REVIEWERS = 3


def assign_reviewers_by_ownership(
    *,
    provider: str,
    repo: str,
    pr_number: int,
    changed_files: Iterable[str],
    author: str | None,
    user_id: str,
    workspace_id: str = "default",
    repo_slug: str | None = None,
) -> dict:
    """Return: {status, requested:[...], skipped_reason?}. Non-raising.

    TWO ADDRESSES, NOT ONE. `repo` is the provider's path — "owner/name", what
    goes into the REST URL. `repo_slug` is the local indexed slug, which is
    what the ownership snapshot is keyed on (`compute_ownership` writes the
    slug the indexer used).

    One argument used to serve both, and the provider half was the correct
    one — so `lookup_owner("acme/api", ...)` searched for a snapshot stored
    under "github_acme-api", found nothing for every file, and the function
    returned `{"status": "noop", "reason": "no ownership snapshot"}` on every
    review. Automatic reviewer assignment has never assigned anybody. The
    reason string even named the missing thing, and it was not missing.
    """
    files = list(changed_files or [])
    if not files:
        return {"status": "noop", "reason": "no changed files"}

    from src.ownership.builder import lookup_owner
    snapshot_key = repo_slug or repo
    candidates: dict[str, int] = {}
    for f in files[:50]:
        info = lookup_owner(snapshot_key, f)
        if not info:
            continue
        primary = info.get("primary_owner")
        if primary:
            candidates[primary] = candidates.get(primary, 0) + 1
    # Sort by ownership weight (files touched).
    ranked = sorted(candidates.items(), key=lambda x: -x[1])

    if not ranked:
        return {"status": "noop", "reason": "no ownership snapshot"}

    if provider == "github":
        return _assign_github(
            repo=repo, pr_number=pr_number, candidates=ranked,
            author=author, user_id=user_id, workspace_id=workspace_id,
        )
    if provider == "gitlab":
        return _assign_gitlab(
            repo=repo, mr_iid=pr_number, candidates=ranked,
            author=author, user_id=user_id, workspace_id=workspace_id,
        )
    if provider == "bitbucket":
        return _assign_bitbucket(
            repo=repo, pr_id=pr_number, candidates=ranked,
            author=author, user_id=user_id, workspace_id=workspace_id,
        )
    return {"status": "skipped", "reason": f"provider {provider!r} not yet supported"}


def _assign_github(
    *,
    repo: str, pr_number: int, candidates: list[tuple[str, int]],
    author: str | None, user_id: str, workspace_id: str = "default",
) -> dict:
    from src.api.routers.apply_fix import _load_provider_token
    token = _load_provider_token("github", user_id, workspace_id)
    if not token:
        return {"status": "skipped", "reason": "no github token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo}"

    with build_client(timeout=10.0) as http:
        # Ownership identities are emails — GitHub needs usernames. Best
        # effort: search users by email.
        picked: list[str] = []
        for ident, _weight in candidates:
            if len(picked) >= _MAX_REVIEWERS:
                break
            login = _resolve_github_login(http, headers, ident)
            if not login:
                continue
            if author and login.lower() == author.lower():
                continue
            if login in picked:
                continue
            picked.append(login)
        if not picked:
            return {"status": "noop", "reason": "no resolvable github logins"}

        r = http.post(
            f"{api}/pulls/{pr_number}/requested_reviewers",
            headers=headers, json={"reviewers": picked},
        )
        if r.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"github {r.status_code}: {r.text[:150]}"}
    logger.info(
        "auto_reviewer_assigned provider=github repo=%s pr=%d requested=%s",
        repo, pr_number, ",".join(picked),
    )
    return {"status": "ok", "requested": picked}


def _resolve_github_login(http, headers, identity: str) -> str | None:
    """Identity may be name, email, or GitHub login. Try email first, then
    fall back to search-by-name. Cached in-process to avoid repeat lookups
    on the same review batch."""
    if identity in _LOGIN_CACHE:
        return _LOGIN_CACHE[identity]
    login = None
    if "@" in identity:
        # search users API — costs 1 point/request
        r = http.get(
            "https://api.github.com/search/users",
            headers=headers, params={"q": f"{identity} in:email"},
        )
        if r.status_code == 200:
            items = r.json().get("items") or []
            if items:
                login = items[0].get("login")
    if not login and " " not in identity and "@" not in identity:
        # Identity looks like a login already — probe /users.
        r = http.get(f"https://api.github.com/users/{identity}", headers=headers)
        if r.status_code == 200:
            login = r.json().get("login")
    _LOGIN_CACHE[identity] = login
    return login


_LOGIN_CACHE: dict[str, str | None] = {}


def _assign_gitlab(
    *,
    repo: str, mr_iid: int, candidates: list[tuple[str, int]],
    author: str | None, user_id: str, workspace_id: str = "default",
) -> dict:
    """GitLab MRs need numeric user ids. Search /users?search=email
    then PUT /projects/:id/merge_requests/:iid with reviewer_ids."""
    import urllib.parse

    from src.credentials import resolve_git_credential
    from src.credentials.store import CredentialStoreError
    try:
        cred = resolve_git_credential("gitlab", user_id=user_id, workspace_id=workspace_id)
    except CredentialStoreError:
        cred = None
    if cred is None:
        return {"status": "skipped", "reason": "no gitlab token"}
    headers = {"PRIVATE-TOKEN": cred.secret}
    proj = urllib.parse.quote_plus(repo)
    api = "https://gitlab.com/api/v4"

    with build_client(timeout=10.0) as http:
        picked_ids: list[int] = []
        picked_names: list[str] = []
        for ident, _w in candidates:
            if len(picked_ids) >= _MAX_REVIEWERS:
                break
            r = http.get(f"{api}/users", headers=headers,
                         params={"search": ident})
            if r.status_code != 200:
                continue
            users = r.json() or []
            if not users:
                continue
            uid = users[0].get("id")
            username = users[0].get("username", "")
            if not uid:
                continue
            if author and username.lower() == author.lower():
                continue
            if uid in picked_ids:
                continue
            picked_ids.append(int(uid))
            picked_names.append(username)
        if not picked_ids:
            return {"status": "noop", "reason": "no gitlab users resolved"}
        upd = http.put(
            f"{api}/projects/{proj}/merge_requests/{mr_iid}",
            headers=headers, json={"reviewer_ids": picked_ids},
        )
        if upd.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"gitlab {upd.status_code}: {upd.text[:150]}"}
    logger.info(
        "auto_reviewer_assigned provider=gitlab repo=%s mr=%d requested=%s",
        repo, mr_iid, ",".join(picked_names),
    )
    return {"status": "ok", "requested": picked_names}


def _assign_bitbucket(
    *,
    repo: str, pr_id: int, candidates: list[tuple[str, int]],
    author: str | None, user_id: str, workspace_id: str = "default",
) -> dict:
    """Bitbucket reviewers list is UUIDs. We resolve via workspace member
    listing (cheap — cached in-process), then PUT the pullrequest."""
    from src.credentials import resolve_git_credential
    from src.credentials.store import CredentialStoreError
    try:
        cred = resolve_git_credential("bitbucket", user_id=user_id, workspace_id=workspace_id)
    except CredentialStoreError:
        cred = None
    if cred is None:
        return {"status": "skipped", "reason": "no bitbucket credentials"}

    meta = cred.metadata or {}
    if meta.get("auth_scheme") == "basic":
        import base64 as _b64
        blob = _b64.b64encode(f"{meta.get('username','')}:{cred.secret}".encode()).decode()
        headers = {"Authorization": f"Basic {blob}"}
    else:
        headers = {"Authorization": f"Bearer {cred.secret}"}
    if "/" not in repo:
        return {"status": "skipped", "reason": "expected workspace/repo"}
    workspace = repo.split("/", 1)[0]
    api = f"https://api.bitbucket.org/2.0/repositories/{repo}/pullrequests/{pr_id}"

    with build_client(timeout=10.0) as http:
        # Fetch workspace members once — 100/page is plenty for most orgs.
        members_by_email: dict[str, str] = {}
        members_by_name: dict[str, str] = {}
        m = http.get(
            f"https://api.bitbucket.org/2.0/workspaces/{workspace}/members",
            headers=headers, params={"pagelen": 100},
        )
        if m.status_code == 200:
            for entry in (m.json().get("values") or []):
                u = entry.get("user") or {}
                uuid = u.get("uuid")
                if not uuid:
                    continue
                if u.get("email_address"):
                    members_by_email[u["email_address"].lower()] = uuid
                if u.get("display_name"):
                    members_by_name[u["display_name"].lower()] = uuid

        picked: list[str] = []
        picked_names: list[str] = []
        for ident, _w in candidates:
            if len(picked) >= _MAX_REVIEWERS:
                break
            uuid = members_by_email.get(ident.lower())
            if not uuid:
                uuid = members_by_name.get(ident.lower())
            if not uuid or uuid in picked:
                continue
            if author and ident.lower() == author.lower():
                continue
            picked.append(uuid)
            picked_names.append(ident)
        if not picked:
            return {"status": "noop", "reason": "no bitbucket UUIDs resolved"}

        upd = http.put(api, headers=headers, json={
            "reviewers": [{"uuid": u} for u in picked],
        })
        if upd.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"bitbucket {upd.status_code}: {upd.text[:150]}"}
    logger.info(
        "auto_reviewer_assigned provider=bitbucket repo=%s pr=%d requested=%s",
        repo, pr_id, ",".join(picked_names),
    )
    return {"status": "ok", "requested": picked_names}


__all__ = ["assign_reviewers_by_ownership"]
