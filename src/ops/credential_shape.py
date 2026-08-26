"""What an operator can be told about a credential without being told the
credential.

The ops diagnostics returned `token_prefix` — the first six characters of a
live token — and `token_len`, its exact length, for every git connection in
every workspace on the installation. The stated reason was sound: an operator
staring at "clone failed 403" needs to know whether the stored secret is a
`ghp_` or a `glpat-` or an Atlassian `ATATT`, because the wrong SHAPE and a
token that merely lacks access are different problems with different fixes.

But six characters plus an exact length is not the shape, it is a piece of the
secret plus a strong narrowing hint, handed out over HTTP. Everything the
diagnosis actually needs can be said without disclosing any of it:

    is it there          →  present
    is it the right kind →  format, matched against a table of known prefixes
    is it truncated      →  length (a number is not a secret; a prefix is)
    is it the SAME one   →  fingerprint, so two slots can be compared without
                            either being revealed

The fingerprint is the one that could not be done before at all: comparing
`token_prefix` across slots told you two tokens shared six characters, which
for `ghp_` tokens is nearly meaningless. sha256 answers it exactly.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

#: Prefix → the credential family it identifies. Ordered longest-first at
#: match time, so `github_pat_` is not swallowed by a shorter neighbour.
KNOWN_PREFIXES: Final[dict[str, str]] = {
    "ghp_": "github-pat-classic",
    "gho_": "github-oauth",
    "ghu_": "github-user-to-server",
    "ghs_": "github-app-installation",
    "ghr_": "github-refresh",
    "github_pat_": "github-pat-fine-grained",
    "glpat-": "gitlab-pat",
    "gloas-": "gitlab-oauth",
    "glrt-": "gitlab-runner",
    "ATATT": "atlassian-api-token",
    "BBDC-": "bitbucket-app-password",
    "sk-ant-": "anthropic-key",
    "sk-": "openai-key",
    "AIza": "google-key",
    "xox": "slack-token",
}

#: Metadata keys an operator may see. Everything else is withheld, because the
#: metadata dict is written by several different code paths and nothing stops
#: a future one from putting something sensitive in it. `check-repo` returned
#: `dict(meta)` wholesale while the diagnostics endpoint next to it already
#: filtered — one allow-list now, so they cannot drift apart again.
SAFE_METADATA_KEYS: Final[frozenset[str]] = frozenset({
    "atlassian_email",
    "bitbucket_workspace",
    "saved_via",
    "last_verified",
    "account_label",
    "scopes",
    "expires_at",
})


def token_format(secret: str) -> str:
    """The credential family, from the prefix table. Never the prefix."""
    if not secret:
        return "empty"
    for prefix in sorted(KNOWN_PREFIXES, key=len, reverse=True):
        if secret.startswith(prefix):
            return KNOWN_PREFIXES[prefix]
    return "unrecognised"


def token_fingerprint(secret: str) -> str:
    """Stable, comparable, non-reversible.

    Twelve hex characters of sha256 — enough that a collision across the few
    hundred credentials one installation holds is not a thing that happens,
    and short enough to read off a screen and compare by eye.
    """
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def describe_token(secret: str | None) -> dict[str, Any]:
    """Everything an operator gets to know about a stored secret."""
    secret = secret or ""
    return {
        "present": bool(secret),
        "format": token_format(secret),
        "length": len(secret),
        "fingerprint": token_fingerprint(secret),
    }


def safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """The metadata keys an operator may see, and only those."""
    return {k: v for k, v in (metadata or {}).items() if k in SAFE_METADATA_KEYS}


__all__ = [
    "KNOWN_PREFIXES",
    "SAFE_METADATA_KEYS",
    "describe_token",
    "safe_metadata",
    "token_fingerprint",
    "token_format",
]
