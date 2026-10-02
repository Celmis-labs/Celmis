# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Enterprise features, mounted only under a valid licence.

The AGPL application knows exactly one thing about this package: the guarded
call in ``src/api/main.py``::

    try:
        from src.ee import mount_enterprise
    except ImportError:
        ...  # a build without src/ee is the community edition
    else:
        mount_enterprise(app)

Everything else follows from the route table. ``/api/capabilities`` derives
"available" from what is mounted, so a feature this function does not mount
is reported off and the frontend hides it — no second list to keep in step.

What it leaves on the app for others to read is ``app.state.celmis_license``:
the licence SUMMARY (customer, expiry, features) or None. Never the token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from src.ee.license import License, load_license

logger = logging.getLogger(__name__)

#: Where the licence summary is left on `app.state`. Read by
#: src/api/routers/capabilities.py by name, so that file imports nothing here.
STATE_ATTR = "celmis_license"


def _router_for(feature: str) -> APIRouter:
    """Imported per feature, so a licence for one feature never imports the
    other's dependencies."""
    if feature == "sso":
        from src.ee.sso.router import router
        return router
    if feature == "analytics":
        from src.ee.analytics.router import router
        return router
    raise KeyError(feature)


def _still_licensed(lic: License, feature: str) -> Callable[[], None]:
    """Per-request check that the licence has not expired since start-up.

    The licence is verified when the process starts, and a process can easily
    outlive its licence. Without this a router mounted on day 364 would keep
    serving on day 400 until the next restart.
    """

    def dependency() -> None:
        if lic.expired(datetime.now(UTC)):
            logger.warning("license_expired_at_runtime feature=%s", feature)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The Celmis Enterprise licence for this feature has expired",
            )

    return dependency


def mount_enterprise(app: Any, *, env: dict[str, str] | None = None) -> License | None:
    """Mount every enterprise feature the configured licence grants.

    Returns the licence (or None) for the caller's log line. Never raises for
    a licence problem: an installation without a valid licence is the
    community edition, not a broken process.
    """
    lic = load_license(env)
    setattr(app.state, STATE_ATTR, lic.summary() if lic else None)
    if lic is None:
        return None
    for feature in sorted(lic.features):
        try:
            router = _router_for(feature)
        except KeyError:  # pragma: no cover — KNOWN_FEATURES and this agree
            continue
        app.include_router(router, dependencies=[Depends(_still_licensed(lic, feature))])
        logger.info("enterprise_feature_mounted feature=%s", feature)
    return lic


__all__ = ["STATE_ATTR", "mount_enterprise"]
