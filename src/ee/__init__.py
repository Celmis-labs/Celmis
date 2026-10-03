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

APPLIED AT START-UP, AND AGAIN AT RUNTIME
-----------------------------------------
``mount_enterprise`` is the first call of :func:`apply_license`, which is
idempotent and may be called again while the process runs — that is what
``PUT /api/license`` and ``DELETE /api/license`` (``src/ee/license_router.py``)
do. Each call makes the route table match the licence in force:

* a feature the licence grants and that is not mounted is mounted, once;
* a feature that is mounted and no longer granted (the licence was removed,
  or replaced by one that drops it) is UNMOUNTED — its routes are taken out of
  the table, so ``/api/capabilities`` reports it off on the next request;
* ``app.state.celmis_license`` is replaced, so the edition follows at once.

And behind the route table, every enterprise route still asks the licence in
force on each request (:func:`_still_licensed`): a request that was routed a
moment before a removal, or a licence that lapses while the process runs, is
answered 403 rather than served.

The licence router itself is mounted always, licensed or not — it is how a
community installation becomes an enterprise one.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.ee import license_store
from src.ee.license import License, Loaded, load

logger = logging.getLogger(__name__)

#: Where the licence summary is left on `app.state`. Read by
#: src/api/routers/capabilities.py by name, so that file imports nothing here.
STATE_ATTR = "celmis_license"
#: Where the runtime licensing state lives on `app.state`. Private to src/ee.
RUNTIME_ATTR = "celmis_ee"


@dataclass
class Runtime:
    """What this process knows about its licence. Never the token."""

    #: The environment the licence is read from; None means `os.environ`
    #: (always, in a real process — an explicit mapping is for tests). Kept
    #: out of the repr: the mapping may hold the token itself.
    env: dict[str, str] | None = field(default=None, repr=False)
    license: License | None = None
    #: "env_key" | "env_file" | "ui" | None — see src/ee/license.py.
    source: str | None = None
    #: Why a licence that was given is not in force, e.g. "expired".
    problem: str | None = None
    #: feature → the route objects its mount added to `app.router.routes`.
    mounted: dict[str, list[Any]] = field(default_factory=dict)
    license_router_mounted: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def runtime(app: Any) -> Runtime:
    state = getattr(app.state, RUNTIME_ATTR, None)
    if not isinstance(state, Runtime):
        state = Runtime()
        setattr(app.state, RUNTIME_ATTR, state)
    return state


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


def _still_licensed(feature: str) -> Callable[..., None]:
    """Per-request check against the licence IN FORCE NOW.

    The licence is verified when it is applied, and a process can easily
    outlive it — or have it removed or replaced from the UI. Without this a
    router mounted on day 364 would keep serving on day 400, and a request
    routed just before a removal would still be served.
    """

    def dependency(request: Request) -> None:
        lic = runtime(request.app).license
        if lic is None or not lic.grants(feature):
            logger.warning("license_feature_not_in_force feature=%s", feature)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This feature needs a Celmis Enterprise licence that grants it",
            )
        if lic.expired(datetime.now(UTC)):
            logger.warning("license_expired_at_runtime feature=%s", feature)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The Celmis Enterprise licence for this feature has expired",
            )

    return dependency


def _mount_feature(app: Any, feature: str) -> list[Any]:
    before = {id(r) for r in app.router.routes}
    app.include_router(_router_for(feature),
                       dependencies=[Depends(_still_licensed(feature))])
    return [r for r in app.router.routes if id(r) not in before]


def _unmount(app: Any, routes: list[Any]) -> None:
    gone = {id(r) for r in routes}
    # A new list, assigned in one step, rather than removing in place: the
    # router iterates `routes` while matching a request.
    app.router.routes = [r for r in app.router.routes if id(r) not in gone]


def _mount_license_router(app: Any, rt: Runtime) -> None:
    if rt.license_router_mounted:
        return
    from src.ee.license_router import router

    app.include_router(router)
    rt.license_router_mounted = True


def apply_license(app: Any, loaded: Loaded) -> Runtime:
    """Make the app match `loaded`: mount what it grants, unmount what it no
    longer grants, publish the summary. Idempotent — applying the same
    licence twice changes nothing and mounts nothing twice."""
    rt = runtime(app)
    with rt.lock:
        _mount_license_router(app, rt)
        lic = loaded.license
        rt.license, rt.source, rt.problem = lic, loaded.source, loaded.problem
        wanted = set(lic.features) if lic is not None else set()
        changed = False
        for feature in sorted(set(rt.mounted) - wanted):
            _unmount(app, rt.mounted.pop(feature))
            logger.info("enterprise_feature_unmounted feature=%s", feature)
            changed = True
        for feature in sorted(wanted - set(rt.mounted)):
            try:
                rt.mounted[feature] = _mount_feature(app, feature)
            except KeyError:  # pragma: no cover — KNOWN_FEATURES and this agree
                continue
            logger.info("enterprise_feature_mounted feature=%s", feature)
            changed = True
        if changed:
            # FastAPI memoises the schema; capabilities reads it.
            app.openapi_schema = None
        setattr(app.state, STATE_ATTR, lic.summary() if lic else None)
    return rt


def reload_license(app: Any) -> Runtime:
    """Read the licence again from its sources and apply it."""
    rt = runtime(app)
    return apply_license(app, load(rt.env, stored=license_store.read_token))


def mount_enterprise(app: Any, *, env: dict[str, str] | None = None) -> License | None:
    """Mount every enterprise feature the configured licence grants, and the
    licence router (always).

    Returns the licence (or None) for the caller's log line. Never raises for
    a licence problem: an installation without a valid licence is the
    community edition, not a broken process.
    """
    runtime(app).env = env
    return reload_license(app).license


__all__ = ["RUNTIME_ATTR", "STATE_ATTR", "Runtime", "apply_license",
           "mount_enterprise", "reload_license", "runtime"]
