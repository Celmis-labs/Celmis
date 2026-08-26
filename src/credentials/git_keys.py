"""One place that answers "which git token do I use for this repo?".

Git tokens used to be stored per admin identity: whoever pressed Save on the
Connections page owned the row, and every background worker looked the token up
under that exact `user_id`. That made offboarding a silent outage — when a
person leaves the org their PAT keeps its shape but starts returning 403, and
the poller simply skipped their repos with a debug-level log. Nobody was told.

So git tokens now resolve the same way LLM provider keys do (see
``src.llm.keys`` — this is deliberately the same shape, because having two
different answers to "whose credential is this?" is what caused the bug):

    tier 0  workspace   — the shared, admin-managed slot. New writes land here.
    tier 1  <user_id>   — read-only back-compat for rows saved before this.
    tier 2  default     — the legacy CLI slot.

Nothing here mints or validates tokens; it only decides *which stored row* a
caller gets. Provider-specific work (GitHub App installation tokens, GitLab
token rotation) plugs in behind this function later without touching callers.
"""

from __future__ import annotations

import logging
from typing import Final

from src.credentials.store import (
    CredentialStore,
    CredentialStoreError,
    StoredCredentials,
    get_credential_store,
)

logger = logging.getLogger(__name__)

# Legacy credentials-store `user_id` under which single-tenant git tokens lived
# before per-workspace isolation. Still readable, but ONLY for the "default"
# transition tenant. Mirrors `src.llm.keys.WORKSPACE_KEY_USER` on purpose.
WORKSPACE_GIT_USER: Final[str] = "workspace"

# Per-workspace git tokens live under `ws:{workspace_id}` (mirrors
# `src.llm.keys.WORKSPACE_SLOT_PREFIX`). This slot is AUTHORITATIVE: a
# non-default workspace resolves only its own token, never a sibling's — a
# leaked git PAT would let one tenant clone another's private repos.
WORKSPACE_SLOT_PREFIX: Final[str] = "ws:"

GIT_PROVIDERS: Final[tuple[str, ...]] = ("github", "gitlab", "bitbucket")


def git_workspace_slot(workspace_id: str) -> str:
    """The credentials-store `user_id` for a workspace's own git token."""
    return f"{WORKSPACE_SLOT_PREFIX}{workspace_id}"


def _git_slot_chain(workspace_id: str, user_id: str) -> list[str]:
    """Ordered git-token slots to try. Only the default/transition tenant reads
    the legacy shared/personal/default rows; every other workspace is strictly
    isolated to its own `ws:{id}` slot (no cross-tenant fallback)."""
    primary = git_workspace_slot(workspace_id)
    if workspace_id == "default":
        return list(dict.fromkeys((primary, WORKSPACE_GIT_USER, user_id, "default")))
    return [primary]


def resolve_git_credential(
    provider: str,
    *,
    user_id: str = "default",
    account_label: str = "default",
    workspace_id: str = "default",
    store: CredentialStore | None = None,
) -> StoredCredentials | None:
    """Return the git credential to use for `workspace_id`, or None if no slot
    has one.

    ``StoredCredentials.user_id`` tells you which slot answered — worth logging
    when a token misbehaves, since "the workspace token 403s" and "a departed
    user's personal token 403s" need different fixes.
    """
    store = store or get_credential_store()
    primary_slot = git_workspace_slot(workspace_id)
    for slot in _git_slot_chain(workspace_id, user_id):
        try:
            stored = store.load(
                provider=provider, user_id=slot, account_label=account_label,
            )
        except CredentialStoreError as exc:
            # Undecryptable row — the master key changed or the DB is damaged.
            # Don't fall through silently to a different identity; that would
            # swap the acting account without anyone noticing.
            logger.warning(
                "git_credential_decrypt_failed provider=%s slot=%s err=%s",
                provider, slot, exc,
            )
            raise
        if stored is not None and stored.secret:
            if slot != primary_slot:
                logger.warning(
                    "git_credential_legacy_slot provider=%s workspace=%s slot=%s — "
                    "resolved via a legacy slot; re-save it on the Connections "
                    "page to isolate this token to the workspace",
                    provider, workspace_id, slot,
                )
            return stored
    return None
