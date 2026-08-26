"""One place that turns a stored git credential into clone/push auth.

Bitbucket is the reason this exists — and it wants DIFFERENT usernames for
its REST API and for git over HTTPS with the same Atlassian API token
(``ATATT…``). Both verified against production:

    REST api.bitbucket.org   Basic(email, token)                    → 200
                             Basic(x-bitbucket-api-token-auth, tok) → 401
                             "API token must be used with an atlassian
                              registered email"

    git  bitbucket.org       x-bitbucket-api-token-auth:token       → clone OK
                             email:token                            → "You may
                              not have access to this repository", auth failed

So `purpose` is not a detail: sending the API shape to git (or vice versa)
fails with a message that blames access rights instead of the credential
format, which is exactly how this cost two debugging rounds.

Rules encoded here (May 2026):
    ATATT… + git → Basic(x-bitbucket-api-token-auth, token)
    ATATT… + api → Basic(email, token)
    ATCTT…       → Basic(x-token-auth, token)   — repo/workspace access token
    other        → Basic(username, token)       — legacy app password
    GitHub       → token URL (x-access-token)
    GitLab       → token URL (oauth2)
"""

from __future__ import annotations

from typing import Any

# Prefixes are Atlassian's own, not ours — see the module docstring.
_ATLASSIAN_API_TOKEN = "ATATT"
_ATLASSIAN_ACCESS_TOKEN = "ATCTT"


def git_auth_kwargs(
    provider: str,
    secret: str,
    metadata: dict[str, Any] | None = None,
    *,
    purpose: str = "git",
) -> dict[str, str]:
    """Kwargs for :meth:`RepoSync.clone_or_update` / ``build_authenticated_url``.

    `purpose` selects the Bitbucket username an Atlassian API token needs:
    ``"git"`` (clone/push) or ``"api"`` (REST). See the module docstring —
    the two are not interchangeable.

    Returns either ``{"api_token": …}`` or ``{"username": …, "password": …}``.
    Never raises — an unknown shape degrades to the token form, which is what
    the caller would have done anyway.
    """
    meta = metadata or {}
    if provider != "bitbucket" or not secret:
        return {"api_token": secret}

    if secret.startswith(_ATLASSIAN_API_TOKEN):
        if purpose == "api":
            email = str(meta.get("atlassian_email") or "").strip()
            if email:
                return {"username": email, "password": secret}
            return {"api_token": secret}
        # git: the token-URL form maps to x-bitbucket-api-token-auth:<token>
        # in build_authenticated_url — the only shape git accepts.
        return {"api_token": secret}

    if secret.startswith(_ATLASSIAN_ACCESS_TOKEN):
        return {"username": "x-token-auth", "password": secret}

    # Legacy app password — needs the Bitbucket username (not the email).
    username = str(meta.get("username") or meta.get("bitbucket_username") or "").strip()
    if username:
        return {"username": username, "password": secret}
    return {"api_token": secret}


def describe_auth(provider: str, secret: str, metadata: dict[str, Any] | None = None,
                  *, purpose: str = "git") -> str:
    """Short, secret-free label for logs: 'bitbucket:email+api-token'."""
    kw = git_auth_kwargs(provider, secret, metadata, purpose=purpose)
    if "username" in kw:
        user = kw["username"]
        kind = ("email+api-token" if "@" in user
                else "x-token-auth" if user == "x-token-auth"
                else "app-password")
        return f"{provider}:{kind}"
    if provider == "bitbucket" and secret.startswith(_ATLASSIAN_API_TOKEN):
        return "bitbucket:x-bitbucket-api-token-auth"
    return f"{provider}:token-url"


__all__ = ["git_auth_kwargs", "describe_auth"]
