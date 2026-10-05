"""Universal git provider detection + URL normalization.

Instead of per-provider hardcoding in clone.py, this module encapsulates all
provider-specific logic: detection, URL parsing, authenticated URL building,
public-repo detection.

Supported providers: Bitbucket, GitHub, GitLab. For unknown hosts — a
'generic' fallback with minimal handling (clone URL as-is).

Self-hosted GitLab: a workspace's GitLab connection may name its own instance
(src/sync/gitlab_instance.py). Parsing and URL building take that instance's
base URL as ``gitlab_base_url`` — a URL on that host (sub-path included) is
GitLab, and clone/API URLs are built against it. Without it, gitlab.com.

Slug format: '{provider}_{owner}-{name}' — avoids collisions between providers
(pallets/click on GitHub vs a same-named one on another host). GitLab subgroups
are flattened with '-' (group/subgroup/repo → group-subgroup-repo).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote, urlparse

import httpx

from src.http import build_client

logger = logging.getLogger(__name__)


class GitProvider(StrEnum):
    BITBUCKET = "bitbucket"
    GITHUB = "github"
    GITLAB = "gitlab"
    GENERIC = "generic"


# Hostname → provider; no subdomain matching. A self-hosted GitLab is not in
# this table: it is recognised only through the `gitlab_base_url` a caller
# passes in, taken from the calling workspace's own GitLab connection.
_PROVIDER_HOSTS: dict[str, GitProvider] = {
    "bitbucket.org": GitProvider.BITBUCKET,
    "github.com": GitProvider.GITHUB,
    "www.github.com": GitProvider.GITHUB,
    "gitlab.com": GitProvider.GITLAB,
}


@dataclass(frozen=True)
class ParsedRepo:
    """Normalized repo identification."""

    provider: GitProvider
    owner: str  # 'acme' | 'pallets' | for GitLab a subgroup-path 'group/subgroup'
    name: str  # 'frontend' | 'click'
    branch_hint: str | None = None  # from browser URL (.../tree/main/...) if any
    #: Self-hosted GitLab instance root the URL was parsed against (None =
    #: gitlab.com). Not part of the identity — the slug is the same either way.
    base_url: str | None = field(default=None, compare=False)

    @property
    def slug(self) -> str:
        """Local slug — directory name + DB key.

        Bitbucket: '{owner}-{name}' — legacy format without a provider prefix.
            Backward compat: the existing cloned repos in ~/code-analysis/repos/
            use this format, plus the derived data (graph.fdblite, vault).

        GitHub/GitLab: '{provider}_{owner}-{name}' — collision-safe with Bitbucket.
            GitLab subgroups flattened with '-': 'gitlab_group-subgroup-repo'.

        Generic: '{owner}-{name}' (rare, no real use case).
        """
        owner_part = self.owner.replace("/", "-")
        if self.provider in (GitProvider.BITBUCKET, GitProvider.GENERIC):
            return f"{owner_part}-{self.name}"
        return f"{self.provider.value}_{owner_part}-{self.name}"

    @property
    def full_path(self) -> str:
        """{owner}/{name} — for API endpoints (with GitLab subgroups support)."""
        return f"{self.owner}/{self.name}"


# ─── self-hosted GitLab ─────────────────────────────────────────────


def _gitlab_instance(gitlab_base_url: str | None):
    """The non-default instance named by ``gitlab_base_url``, else None."""
    if not gitlab_base_url:
        return None
    from src.sync.gitlab_instance import instance_of

    inst = instance_of(gitlab_base_url)
    return None if inst.is_default else inst


def _ssh_host(s: str) -> str:
    if s.startswith("git@"):
        m = re.match(r"^git@([^:]+):", s)
        return m.group(1).lower() if m else ""
    if s.startswith("ssh://"):
        return (urlparse(s).hostname or "").lower()
    return ""


# ─── detection ──────────────────────────────────────────────────────


def detect_provider(url_or_slug: str, *, gitlab_base_url: str | None = None) -> GitProvider:
    """Determine the provider by the host in the URL or an explicit prefix in
    the slug.

    Examples:
        'https://github.com/foo/bar'    → GITHUB
        'git@gitlab.com:foo/bar.git'    → GITLAB
        'github:foo/bar'                → GITHUB (explicit prefix)
        'acme/frontend'             → BITBUCKET (legacy default — backward compat)

    `gitlab_base_url` — the workspace's self-hosted GitLab: a URL on that
    instance (host, port and sub-path) is GITLAB.
    """
    s = url_or_slug.strip()
    inst = _gitlab_instance(gitlab_base_url)
    if inst is not None:
        if s.startswith(("http://", "https://")) and inst.owns_url(s):
            return GitProvider.GITLAB
        if s.startswith(("git@", "ssh://")) and _ssh_host(s) == inst.host:
            return GitProvider.GITLAB

    # Explicit provider prefix in slug form: 'github:owner/repo'
    if ":" in s and not s.startswith(("http://", "https://", "git@", "ssh://")):
        prefix, _ = s.split(":", 1)
        prefix = prefix.lower()
        valid_prefixes = {p.value for p in GitProvider}
        if prefix in valid_prefixes:
            return GitProvider(prefix)

    # SSH form: 'git@host:owner/repo.git' or 'ssh://git@host/owner/repo.git'
    if s.startswith("git@"):
        m = re.match(r"^git@([^:]+):", s)
        if m:
            return _PROVIDER_HOSTS.get(m.group(1).lower(), GitProvider.GENERIC)
        return GitProvider.GENERIC

    if s.startswith("ssh://"):
        host = (urlparse(s).hostname or "").lower()
        return _PROVIDER_HOSTS.get(host, GitProvider.GENERIC)

    # HTTPS
    if s.startswith(("http://", "https://")):
        host = (urlparse(s).hostname or "").lower()
        return _PROVIDER_HOSTS.get(host, GitProvider.GENERIC)

    # Slug form 'owner/repo' (no scheme) — legacy default Bitbucket
    return GitProvider.BITBUCKET


# ─── parse ──────────────────────────────────────────────────────────


def parse_repo_url(url_or_slug: str, *, gitlab_base_url: str | None = None) -> ParsedRepo:
    """Parse any form of URL/slug → ParsedRepo.

    Supports:
        HTTPS browser URLs with branch in path (Bitbucket /src/, GitHub /tree/, GitLab /-/tree/)
        HTTPS clone URLs (with .git suffix)
        SSH URLs (git@host:owner/repo.git, ssh://git@host/...)
        Slug form 'owner/repo' (Bitbucket default)
        Explicit-prefixed 'github:owner/repo' / 'gitlab:group/subgroup/repo'
        Self-hosted GitLab URLs, when `gitlab_base_url` names that instance
        (https://host[/sub-path]/group/sub/project[/-/merge_requests/N])
    """
    s = url_or_slug.strip()
    provider = detect_provider(s, gitlab_base_url=gitlab_base_url)
    inst = _gitlab_instance(gitlab_base_url)
    if inst is not None and provider == GitProvider.GITLAB:
        parsed = _parse_on_instance(s, inst)
        if parsed is not None:
            return parsed

    # Explicit provider prefix
    if ":" in s and not s.startswith(("http://", "https://", "git@", "ssh://")):
        prefix, rest = s.split(":", 1)
        if prefix.lower() in {p.value for p in GitProvider}:
            return _parse_slug(rest, provider)

    # SSH form
    if s.startswith("git@"):
        m = re.match(r"^git@[^:]+:(.+?)(?:\.git)?/?$", s)
        if m:
            return _parse_slug(m.group(1), provider)
        raise ValueError(f"cannot parse SSH URL: {s}")

    if s.startswith("ssh://"):
        parsed = urlparse(s)
        path = parsed.path.lstrip("/").removesuffix(".git").rstrip("/")
        return _parse_slug(path, provider)

    # HTTPS
    if s.startswith(("http://", "https://")):
        return _parse_https(s, provider)

    # Slug form
    return _parse_slug(s, provider)


def _parse_on_instance(s: str, inst) -> ParsedRepo | None:
    """A URL on a self-hosted instance → ParsedRepo carrying its base URL."""
    if s.startswith(("http://", "https://")):
        rel = inst.relative_path(s)
        if rel is None:
            return None
        # Re-rooted on a bare host so the sub-path never becomes a group.
        parsed = _parse_https(f"https://{inst.host}/{rel}", GitProvider.GITLAB)
    elif s.startswith("git@"):
        m = re.match(r"^git@[^:]+:(.+?)(?:\.git)?/?$", s)
        if not m:
            raise ValueError(f"cannot parse SSH URL: {s}")
        parsed = _parse_slug(m.group(1), GitProvider.GITLAB)
    elif s.startswith("ssh://"):
        path = urlparse(s).path.lstrip("/").removesuffix(".git").rstrip("/")
        parsed = _parse_slug(path, GitProvider.GITLAB)
    else:
        return None
    return ParsedRepo(provider=parsed.provider, owner=parsed.owner,
                      name=parsed.name, branch_hint=parsed.branch_hint,
                      base_url=inst.base_url)


def _parse_slug(slug: str, provider: GitProvider) -> ParsedRepo:
    """slug 'owner/name' or 'group/subgroup/name' (GitLab)."""
    parts = [p for p in slug.strip("/").split("/") if p]
    parts = [p.removesuffix(".git") for p in parts]
    if len(parts) < 2:
        raise ValueError(
            f"slug must have at least owner/name (got {len(parts)} segments): {slug!r}"
        )

    if provider == GitProvider.GITLAB and len(parts) > 2:
        # GitLab subgroups: group/subgroup1/.../repo — everything but the last → owner
        return ParsedRepo(provider=provider, owner="/".join(parts[:-1]), name=parts[-1])

    return ParsedRepo(provider=provider, owner=parts[0], name=parts[1])


def _parse_https(url: str, provider: GitProvider) -> ParsedRepo:
    """HTTPS URL → ParsedRepo with branch_hint (if it is a browser URL)."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        raise ValueError(f"cannot parse path from URL: {url!r}")

    parts = [p.removesuffix(".git") for p in parts]
    branch_hint: str | None = None

    if provider == GitProvider.BITBUCKET:
        # /{owner}/{name}/src/{branch}/...
        if len(parts) < 2:
            raise ValueError(f"Bitbucket URL must have at least owner/name: {url!r}")
        owner, name = parts[0], parts[1]
        if len(parts) >= 4 and parts[2] in ("src", "branch", "browse", "commits"):
            branch_hint = parts[3]
        return ParsedRepo(provider=provider, owner=owner, name=name, branch_hint=branch_hint)

    if provider == GitProvider.GITHUB:
        # /{owner}/{name}/{tree|blob|commit}/{branch}/...
        if len(parts) < 2:
            raise ValueError(f"GitHub URL must have at least owner/name: {url!r}")
        owner, name = parts[0], parts[1]
        if len(parts) >= 4 and parts[2] in ("tree", "blob", "commit", "commits"):
            branch_hint = parts[3]
        return ParsedRepo(provider=provider, owner=owner, name=name, branch_hint=branch_hint)

    if provider == GitProvider.GITLAB:
        # /{group}/[subgroup/]*{name}/-/{tree|blob|commit}/{branch}/...
        # the '-' separator splits the repo path from the ref part (GitLab convention)
        if "-" in parts:
            dash_idx = parts.index("-")
            repo_parts = parts[:dash_idx]
            after_dash = parts[dash_idx + 1 :]
            if len(after_dash) >= 2 and after_dash[0] in ("tree", "blob", "commit", "commits"):
                branch_hint = after_dash[1]
        else:
            repo_parts = parts

        if len(repo_parts) < 2:
            raise ValueError(f"GitLab URL must have at least group/repo: {url!r}")

        return ParsedRepo(
            provider=provider,
            owner="/".join(repo_parts[:-1]),  # subgroups join
            name=repo_parts[-1],
            branch_hint=branch_hint,
        )

    # Generic fallback — we take the first 2 segments
    if len(parts) < 2:
        return ParsedRepo(provider=provider, owner=parts[0], name=parts[0])
    return ParsedRepo(provider=provider, owner=parts[0], name=parts[1])


# ─── URL building ───────────────────────────────────────────────────


def _gitlab_base(repo: ParsedRepo, gitlab_base_url: str | None) -> str:
    from src.sync.gitlab_instance import DEFAULT_BASE_URL, instance_of

    return instance_of(gitlab_base_url or repo.base_url or DEFAULT_BASE_URL).base_url


def build_clone_url(repo: ParsedRepo, *, gitlab_base_url: str | None = None) -> str:
    """Without auth — anonymous/public clone form.

    GitLab: against `gitlab_base_url` (or the instance the repo was parsed
    against), gitlab.com otherwise.
    """
    if repo.provider == GitProvider.BITBUCKET:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}.git"
    if repo.provider == GitProvider.GITHUB:
        return f"https://github.com/{repo.owner}/{repo.name}.git"
    if repo.provider == GitProvider.GITLAB:
        return f"{_gitlab_base(repo, gitlab_base_url)}/{repo.owner}/{repo.name}.git"
    raise ValueError(f"cannot build clone URL for generic provider: {repo}")


def build_authenticated_url(
    repo: ParsedRepo,
    *,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    gitlab_base_url: str | None = None,
) -> str:
    """Authenticated clone URL — for private repos.

    Prefer NOT to hand this URL to git: it puts the credential in argv and, for
    a clone, in .git/config. `src/sync/clone.py` clones GitLab with the plain
    URL and a credential helper instead.

    Per-provider auth pattern (May 2026):
        Bitbucket: x-bitbucket-api-token-auth:TOKEN@... (new API token, recommended)
                   OR username:app_password@... (legacy, deprecated 2026-06-09)
        GitHub:    x-access-token:PAT@... (Personal Access Token or GitHub App)
                   OR username:PAT@... (legacy basic auth, also works)
        GitLab:    oauth2:PAT@... (Personal Access Token)
                   OR username:password@... (basic auth, for self-hosted in V2)

    If no credentials are supplied — fall back to the public URL (anonymous clone).
    """
    if repo.provider == GitProvider.BITBUCKET:
        if token:
            t = quote(token, safe="")
            return (
                f"https://x-bitbucket-api-token-auth:{t}@bitbucket.org/"
                f"{repo.owner}/{repo.name}.git"
            )
        if username and password:
            u, p = quote(username, safe=""), quote(password, safe="")
            return f"https://{u}:{p}@bitbucket.org/{repo.owner}/{repo.name}.git"
        return build_clone_url(repo)

    if repo.provider == GitProvider.GITHUB:
        if token:
            t = quote(token, safe="")
            return f"https://x-access-token:{t}@github.com/{repo.owner}/{repo.name}.git"
        if username and password:
            u, p = quote(username, safe=""), quote(password, safe="")
            return f"https://{u}:{p}@github.com/{repo.owner}/{repo.name}.git"
        return build_clone_url(repo)

    if repo.provider == GitProvider.GITLAB:
        base = _gitlab_base(repo, gitlab_base_url)
        scheme, _, rest = base.partition("://")
        if token:
            t = quote(token, safe="")
            return f"{scheme}://oauth2:{t}@{rest}/{repo.owner}/{repo.name}.git"
        if username and password:
            u, p = quote(username, safe=""), quote(password, safe="")
            return f"{scheme}://{u}:{p}@{rest}/{repo.owner}/{repo.name}.git"
        return build_clone_url(repo, gitlab_base_url=gitlab_base_url)

    # Generic — no auth
    raise ValueError(f"cannot build authenticated URL for generic provider: {repo}")


# ─── public detection ───────────────────────────────────────────────


_PUBLIC_API_ENDPOINTS: dict[GitProvider, str] = {
    GitProvider.BITBUCKET: "https://api.bitbucket.org/2.0/repositories/{owner}/{name}",
    GitProvider.GITHUB: "https://api.github.com/repos/{owner}/{name}",
    # GitLab — slug form url-encoded
    GitProvider.GITLAB: "https://gitlab.com/api/v4/projects/{slug}",
}


def is_repo_public(repo: ParsedRepo, *, timeout: float = 10.0,
                   gitlab_base_url: str | None = None) -> bool | None:
    """Fast public check via an unauthenticated API call.

    Returns:
        True  — repo is public (HTTP 200 without auth)
        False — repo is private or does not exist (401/403/404)
        None  — provider does not support detection (generic) or network error
    """
    endpoint_template = _PUBLIC_API_ENDPOINTS.get(repo.provider)
    if endpoint_template is None:
        return None

    client_kwargs: dict = {}
    follow = True
    if repo.provider == GitProvider.GITLAB:
        slug = quote(f"{repo.owner}/{repo.name}", safe="")
        from src.sync.gitlab_instance import UnsafeGitLabURL, instance_of

        try:
            inst = instance_of(gitlab_base_url or repo.base_url)
            url = f"{inst.api_base}/projects/{slug}"
            client_kwargs = inst.http_kwargs()
        except UnsafeGitLabURL as exc:
            logger.warning("is_repo_public gitlab_url_refused err=%s", exc)
            return None
        # A self-hosted instance is pinned to its validated address; a
        # redirect would leave that pin, so it is not followed there.
        follow = inst.is_default
    else:
        url = endpoint_template.format(owner=repo.owner, name=repo.name)

    try:
        with build_client(timeout=timeout, follow_redirects=follow,
                          **client_kwargs) as client:
            r = client.get(url, headers={"User-Agent": "code-analyzer/0.1"})
    except httpx.HTTPError as e:
        logger.warning("is_repo_public http_error url=%s err=%s", url, e)
        return None

    if r.status_code == 200:
        return True
    if r.status_code in (401, 403, 404):
        return False
    logger.debug("is_repo_public unexpected status=%s url=%s", r.status_code, url)
    return None


# ─── credential redaction (for logs) ────────────────────────────────


def strip_credentials(text: str) -> str:
    """Removes creds from a URL for safe logging.

    Examples:
        'https://user:pass@host/...' → 'https://[REDACTED]@host/...'
        'https://x-access-token:ghp_abc@github.com/...' → 'https://[REDACTED]@github.com/...'
    """
    return re.sub(r"(https?://)[^:@/]+:[^@]+@", r"\1[REDACTED]@", text)
