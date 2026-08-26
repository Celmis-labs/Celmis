"""Universal git provider detection + URL normalization.

Instead of per-provider hardcoding in clone.py, this module encapsulates all
provider-specific logic: detection, URL parsing, authenticated URL building,
public-repo detection.

Supported providers: Bitbucket, GitHub, GitLab. For unknown hosts — a
'generic' fallback with minimal handling (clone URL as-is).

Slug format: '{provider}_{owner}-{name}' — avoids collisions between providers
(pallets/click on GitHub vs a same-named one on another host). GitLab subgroups
are flattened with '-' (group/subgroup/repo → group-subgroup-repo).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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


# Hostname → provider; we do not do subdomain matching — gitlab.com self-hosted
# instances are out of scope (decision recorded May 2026).
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


# ─── detection ──────────────────────────────────────────────────────


def detect_provider(url_or_slug: str) -> GitProvider:
    """Determine the provider by the host in the URL or an explicit prefix in
    the slug.

    Examples:
        'https://github.com/foo/bar'    → GITHUB
        'git@gitlab.com:foo/bar.git'    → GITLAB
        'github:foo/bar'                → GITHUB (explicit prefix)
        'acme/frontend'             → BITBUCKET (legacy default — backward compat)
    """
    s = url_or_slug.strip()

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


def parse_repo_url(url_or_slug: str) -> ParsedRepo:
    """Parse any form of URL/slug → ParsedRepo.

    Supports:
        HTTPS browser URLs with branch in path (Bitbucket /src/, GitHub /tree/, GitLab /-/tree/)
        HTTPS clone URLs (with .git suffix)
        SSH URLs (git@host:owner/repo.git, ssh://git@host/...)
        Slug form 'owner/repo' (Bitbucket default)
        Explicit-prefixed 'github:owner/repo' / 'gitlab:group/subgroup/repo'
    """
    s = url_or_slug.strip()
    provider = detect_provider(s)

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


def build_clone_url(repo: ParsedRepo) -> str:
    """Without auth — anonymous/public clone form."""
    if repo.provider == GitProvider.BITBUCKET:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}.git"
    if repo.provider == GitProvider.GITHUB:
        return f"https://github.com/{repo.owner}/{repo.name}.git"
    if repo.provider == GitProvider.GITLAB:
        return f"https://gitlab.com/{repo.owner}/{repo.name}.git"
    raise ValueError(f"cannot build clone URL for generic provider: {repo}")


def build_authenticated_url(
    repo: ParsedRepo,
    *,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
) -> str:
    """Authenticated clone URL — for private repos.

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
        if token:
            t = quote(token, safe="")
            return f"https://oauth2:{t}@gitlab.com/{repo.owner}/{repo.name}.git"
        if username and password:
            u, p = quote(username, safe=""), quote(password, safe="")
            return f"https://{u}:{p}@gitlab.com/{repo.owner}/{repo.name}.git"
        return build_clone_url(repo)

    # Generic — no auth
    raise ValueError(f"cannot build authenticated URL for generic provider: {repo}")


# ─── public detection ───────────────────────────────────────────────


_PUBLIC_API_ENDPOINTS: dict[GitProvider, str] = {
    GitProvider.BITBUCKET: "https://api.bitbucket.org/2.0/repositories/{owner}/{name}",
    GitProvider.GITHUB: "https://api.github.com/repos/{owner}/{name}",
    # GitLab — slug form url-encoded
    GitProvider.GITLAB: "https://gitlab.com/api/v4/projects/{slug}",
}


def is_repo_public(repo: ParsedRepo, *, timeout: float = 10.0) -> bool | None:
    """Fast public check via an unauthenticated API call.

    Returns:
        True  — repo is public (HTTP 200 without auth)
        False — repo is private or does not exist (401/403/404)
        None  — provider does not support detection (generic) or network error
    """
    endpoint_template = _PUBLIC_API_ENDPOINTS.get(repo.provider)
    if endpoint_template is None:
        return None

    if repo.provider == GitProvider.GITLAB:
        slug = quote(f"{repo.owner}/{repo.name}", safe="")
        url = endpoint_template.format(slug=slug)
    else:
        url = endpoint_template.format(owner=repo.owner, name=repo.name)

    try:
        with build_client(timeout=timeout, follow_redirects=True) as client:
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
