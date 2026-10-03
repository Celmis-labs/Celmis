# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Where a licence entered in the UI is kept.

The encrypted credential store (``src/credentials``), under a slot of its
own that no workspace and no user can name:

    user_id  "__installation__"    provider  "__celmis_license__"

So it is installation-wide (not per workspace), encrypted at rest with the
same master key as the git and LLM tokens, and it lives in the workspace
volume — it survives a restart and a container rebuild exactly as those do.

A licence is not a password: holding one grants nothing a signature check
did not. It is still kept out of logs and API responses, because it names a
customer. Only :func:`read_token` ever returns it, and only to the verifier.

The environment outranks this slot (``src/ee/license.py``,
:func:`~src.ee.license.resolve_token`): an operator who sets
``CELMIS_LICENSE_KEY`` or ``CELMIS_LICENSE_FILE`` manages the licence, and
the UI refuses to save over it rather than store something that would be
silently shadowed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER = "__celmis_license__"
SLOT = "__installation__"
LABEL = "default"


def get_store() -> Any:
    """The credential store. A function, so tests point it at a temp store."""
    from src.credentials import get_credential_store

    return get_credential_store()


def read_token() -> str | None:
    """The stored token, or None. Never raises: an unreadable store (a
    changed master key, a locked file) is the community edition, logged."""
    try:
        stored = get_store().load(PROVIDER, user_id=SLOT, account_label=LABEL,
                                  update_last_used=False)
    except Exception as exc:  # noqa: BLE001 — a licence problem never stops the process
        logger.warning("license_store_unreadable err=%s — ignoring the stored "
                       "licence", type(exc).__name__)
        return None
    if stored is None:
        return None
    return (stored.secret or "").strip() or None


def save_token(token: str, *, metadata: dict[str, object]) -> None:
    """Store `token`. `metadata` is the summary only — never the token."""
    get_store().save(PROVIDER, token.strip(), metadata=metadata,
                     user_id=SLOT, account_label=LABEL)


def delete_token() -> bool:
    """Remove the stored token. True when there was one."""
    return bool(get_store().delete(PROVIDER, user_id=SLOT, account_label=LABEL))


__all__ = ["LABEL", "PROVIDER", "SLOT", "delete_token", "get_store", "read_token",
           "save_token"]
