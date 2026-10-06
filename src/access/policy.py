"""What a repository nobody has granted anything on means.

Until 2.3.7 the answer was "everybody in the workspace may read it": a repo
with no team rule fell open in single_tenant (``resolver.py``) and a repo with
no team grant fell open to REST (``deps.py``). A fresh install worked, and a
repository nobody had thought about was visible to every member and, through
MCP, to every token.

The default is now closed. A repository with no rule and no grant is visible
to owners, admins of the workspace and the superadmin only, in both deployment
modes, until somebody grants a team (Admin > Access / Admin > Teams, or
``analyzer access bootstrap``).

``CELMIS_UNRULED_REPO_ACCESS=open`` restores the old behaviour, for a
single_tenant install that wants a deliberate upgrade path. Under multi_tenant
it is ignored — the setting would otherwise make "no rule yet", which
describes every repository of a new tenant, mean "readable by the whole box".
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The site id this predicate registers in ``src.deployment.FALL_OPEN_SITES``.
SITE = "access.policy.unruled_repo"

DENY = "deny"
OPEN = "open"


def configured_policy() -> str:
    """``deny`` or ``open`` as written; anything unrecognised is ``deny``."""
    raw = ""
    try:
        from src.config import get_settings

        raw = str(get_settings().celmis_unruled_repo_access or "")
    except Exception:  # noqa: BLE001 — an unreadable setting is the closed one
        import os

        raw = os.environ.get("CELMIS_UNRULED_REPO_ACCESS", "")
    value = raw.strip().lower()
    return OPEN if value == OPEN else DENY


def unruled_repo_open(*, detail: str = "") -> bool:
    """May a repository with no rule and no grant be read by a plain member?

    ``True`` only when the operator asked for ``open`` AND the deployment is
    single_tenant. The answer is the same for every repository, so the three
    places that used to fall open (the research resolver, the team permission
    check and the repo listings) cannot disagree.
    """
    from src.deployment import fall_open_allowed

    if configured_policy() != OPEN:
        return False
    if not fall_open_allowed("access.policy.unruled_repo", detail=detail):
        logger.warning(
            "unruled_repo_open_ignored mode=multi_tenant — "
            "CELMIS_UNRULED_REPO_ACCESS=open has no effect here")
        return False
    return True


__all__ = ["DENY", "OPEN", "SITE", "configured_policy", "unruled_repo_open"]
