"""Which secret verifies this webhook delivery.

Auto-review is driven by webhooks, and a webhook endpoint is public: anyone on
the internet can POST to `/webhook/github`. The secret is the only thing that
separates a real delivery from a forged one, and a forged one costs a
workspace's LLM budget and posts comments on somebody's pull request.

Until now there was ONE secret for the whole instance — `settings.webhook_secret`,
read from the environment. That is fine for a single-tenant box and wrong for
this one: every workspace would have to configure its GitHub with the same
string, and any tenant holding it could sign a delivery naming another
tenant's repository.

So a secret belongs to a workspace, and lives in the credential store beside
that workspace's git token, under the same `ws:{id}` slot convention. No
migration: `credentials_v2` is keyed by (user_id, provider, account_label) and
this adds rows, not columns.

Two rules keep it honest:

  * `workspace_id is None` is the LEGACY route — the un-suffixed path a
    deployment with REVIEW_WEBHOOK_SECRET in its .env already has registered
    with GitHub. It resolves to the environment value, exactly as before.
  * Any other workspace reads its own row and nothing else. The one exception
    is `default`, which falls back to the environment — the same transition
    rule `_git_slot_chain` already applies to git tokens, so an existing
    single-tenant install keeps working while it migrates.

Never raises. A store that cannot be opened — a lost master key, a corrupt
file — returns None, and the caller refuses the delivery. Failing closed on an
unreadable secret is right; taking the API process down with it is not.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The `provider` column value. Short on purpose — the column is String(32) in
#: the SQLAlchemy store, and this has to fit beside "github"/"gitlab" rows.
PROVIDER = "review_webhook"

#: How long a resolved secret is reused before the store is read again. Each
#: delivery would otherwise mean a decrypt on an unauthenticated path, and
#: GitHub can deliver in bursts. Short enough that a rotation from the UI takes
#: effect on its own; `invalidate()` makes it immediate.
CACHE_TTL_SECONDS = 30

_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_cache_lock = threading.Lock()


def _env_secret(provider: str, settings: Any) -> str | None:
    """The instance-wide secret for a provider, or None.

    The three are separate fields with separate names because the providers
    verify differently — GitHub and Bitbucket sign the body, GitLab compares a
    token in plaintext — and mixing them up produces a 401 that looks like a
    wrong secret rather than a wrong scheme.
    """
    field = {
        "github": "webhook_secret",
        "gitlab": "gitlab_token",
        "bitbucket": "bitbucket_secret",
    }.get(provider)
    if field is None:
        return None
    value = getattr(settings, field, None)
    if value is None:
        return None
    # SecretStr in this codebase, but tolerate a plain string.
    get = getattr(value, "get_secret_value", None)
    return get() if callable(get) else str(value)


def _stored_secret(provider: str, workspace_id: str) -> str | None:
    """This workspace's own secret, from the credential store."""
    now = time.monotonic()
    key = (provider, workspace_id)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    value: str | None = None
    try:
        from src.credentials import git_workspace_slot
        from src.credentials.store import get_credential_store

        stored = get_credential_store().load(
            PROVIDER,
            user_id=git_workspace_slot(workspace_id),
            account_label=provider,
            # This runs before the request is authenticated — every unsigned
            # POST from the internet would otherwise be a write to the store.
            update_last_used=False,
        )
        value = stored.secret if stored else None
    except Exception as exc:  # noqa: BLE001 — an unreadable store is a refusal
        logger.warning(
            "webhook_secret_unreadable provider=%s workspace=%s err=%s",
            provider, workspace_id, exc,
        )
        value = None

    with _cache_lock:
        _cache[key] = (now, value)
    return value


def resolve_webhook_secret(
    provider: str, workspace_id: str | None, settings: Any,
) -> str | None:
    """The secret to verify a delivery with, or None to refuse it."""
    if workspace_id is None:
        # The legacy un-suffixed route. Byte-identical to the old behaviour,
        # so a deployment that already registered that URL keeps working.
        return _env_secret(provider, settings)

    stored = _stored_secret(provider, workspace_id)
    if stored:
        return stored
    if workspace_id == "default":
        # Same transition rule as git tokens: the default tenant may still be
        # configured entirely through the environment.
        return _env_secret(provider, settings)
    # Strict isolation. A workspace with no secret of its own does not inherit
    # the instance's — inheriting is precisely how one tenant would end up
    # able to sign for another.
    return None


def save_webhook_secret(provider: str, workspace_id: str, secret: str) -> None:
    """Store (or rotate) a workspace's secret for one provider."""
    from src.credentials import git_workspace_slot
    from src.credentials.store import get_credential_store

    get_credential_store().save(
        PROVIDER,
        secret,
        user_id=git_workspace_slot(workspace_id),
        account_label=provider,
        metadata={"kind": "webhook_secret", "provider": provider},
    )
    invalidate(provider, workspace_id)


def invalidate(provider: str, workspace_id: str) -> None:
    """Drop the cached value, so a rotation takes effect on the next delivery."""
    with _cache_lock:
        _cache.pop((provider, workspace_id), None)


__all__ = [
    "PROVIDER",
    "invalidate",
    "resolve_webhook_secret",
    "save_webhook_secret",
]
