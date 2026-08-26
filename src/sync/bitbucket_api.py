"""What a Bitbucket integration needs to be stored.

THIS FILE USED TO BE THE BITBUCKET REST CLIENT. `BitbucketClient`, its three
exception types, `RepoInfo`, `ScopeCheckResult`, `authenticate` and
`is_token_expired` were 340 of its 380 lines, and a repo-wide word-boundary
search found ZERO uses of any of them outside this file — the module arrived
with its client half already orphaned and no caller was ever deleted. Three
independent implementations of the same HTTP calls live elsewhere and are
reached: `src/review/providers/bitbucket.py` for pull requests,
`src/sync/clone.py` for git, `src/api/routers/ops_metrics.py` for the health
probe.

What remains is the one thing that crosses the file boundary: the credential
shape, imported by both credential stores and through them by the API app and
the CLI.

Auth notes, kept because they are what a person needs when the stored token
stops working (as of April 2026, after app passwords were deprecated):
  · REST API — HTTP Basic with email + API token, and the token must carry
    Bitbucket scopes
  · git operations — a different form; see clone.py
  · create the token at id.atlassian.com → Security → "Create API token with
    scopes" → product: Bitbucket → scopes: read:user, read:workspace,
    read:repository (+pullrequest if you want reviews)
  · app passwords stopped working on 9 June 2026
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BitbucketCredentials:
    """What is needed for a full Bitbucket integration.

    `bitbucket_username` is derived automatically from GET /user — the person
    connecting does not have to enter it.
    """

    atlassian_email: str
    api_token: str
    bitbucket_username: str = ""  # auto-derived
    expires_at: str | None = None  # ISO date, optional, drives a warning in the UI


__all__ = ["BitbucketCredentials"]
