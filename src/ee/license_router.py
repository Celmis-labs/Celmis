# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Entering the licence from the UI — for a global admin.

    GET    /api/license   what is in force, where it came from, why not
    PUT    /api/license   {"token": "..."} — verify, store, apply now
    DELETE /api/license   remove the stored licence, apply now

Mounted by ``src.ee.mount_enterprise`` ALWAYS, licensed or not: it is how a
community installation becomes an enterprise one without a restart.

Global admins only (``require_admin``): a licence is installation-wide, and
a workspace owner does not decide the edition of everybody else's
installation.

PRECEDENCE
----------
``CELMIS_LICENSE_KEY`` > ``CELMIS_LICENSE_FILE`` > the stored token. While an
environment variable is set, PUT answers 409: storing a token the variable
would shadow is a save that silently does nothing, which is worse than a
refusal that says where the licence is managed.

WHAT TAKES EFFECT, AND WHEN
---------------------------
At once, in this process. PUT mounts the features the new licence grants;
PUT of a licence that drops a feature, and DELETE, unmount them — and every
enterprise route also re-checks the licence in force per request, so nothing
is served on a licence that is no longer there. The stored token is read at
start-up too, so the same state survives a restart.

PUT and DELETE are serialised (one write lock per event loop): the store
write and the apply that follows it happen as one step, so two admins acting
at the same moment cannot leave one licence in force while another — or
none — is stored.

The token is in no response and no log line, and the audit rows carry the
customer, the features and the expiry — never the token.

A note on scale: this applies to the process that answered the request. A
deployment running several API replicas applies a UI change on the others at
their next restart; the shipped compose runs one.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.api.deps import client_ip, require_admin
from src.ee import license_store
from src.ee.license import (
    ENV_FILE,
    ENV_KEY,
    SOURCE_ENV_FILE,
    SOURCE_ENV_KEY,
    SOURCE_UI,
    License,
    LicenseError,
    Loaded,
    env_source,
    verify,
)
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["license", "enterprise"])

#: Far above any real licence (a few hundred bytes). Checked in the handler,
#: not as a pydantic `max_length`: a model error would answer 422 with a list
#: of error objects as `detail`, which the UI cannot show as a sentence.
#: (`verify` refuses above 16 KiB with the same reason.)
_MAX_BODY_CHARS = 64 * 1024

#: One write lock per event loop. asyncio.Lock binds to the loop it is first
#: contended on, and a test process runs several loops (one per TestClient).
_WRITE_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _WRITE_LOCKS.get(loop)
    if lock is None:
        lock = _WRITE_LOCKS[loop] = asyncio.Lock()
    return lock

_ENV_NAME = {SOURCE_ENV_KEY: ENV_KEY, SOURCE_ENV_FILE: ENV_FILE}

#: LicenseError reasons → what an administrator should read. Anything not
#: listed is shown as "Not a valid Celmis licence: <reason>".
_MESSAGES = {
    "empty": "Paste a licence key first.",
    "expired": "This licence has expired. Ask for a renewed one.",
    "not valid yet": "This licence is not valid yet — its start date is in the future.",
    "wrong issuer": "This is not a Celmis licence (wrong issuer).",
    "signature does not verify":
        "This licence was not signed by Celmis — the signature does not verify. "
        "Check that it was copied whole.",
    "not a JWT": "This is not a licence key — check that it was copied whole.",
    "too large to be a licence": "This is far too large to be a licence key.",
    "grants no feature this build has":
        "This licence grants no feature this version of Celmis has.",
}


class LicenseIn(BaseModel):
    token: str


class LicenseSummaryOut(BaseModel):
    customer: str
    features: list[str]
    issued_at: str
    expires_at: str
    expired: bool


class LicenseStateOut(BaseModel):
    #: "enterprise" | "community"
    edition: str
    #: Where the licence in force (or refused) came from: "env_key",
    #: "env_file", "ui", or None when none was given.
    source: str | None
    #: True while CELMIS_LICENSE_KEY or CELMIS_LICENSE_FILE is set: the UI
    #: shows the licence read-only and PUT answers 409.
    managed_by_env: bool
    #: The variable that manages it, for the message — a name, not a value.
    env_variable: str | None = None
    #: Why a licence that was given is not in force (e.g. "expired").
    problem: str | None = None
    license: LicenseSummaryOut | None = None


def _summary(lic: License) -> LicenseSummaryOut:
    return LicenseSummaryOut(
        customer=lic.customer,
        features=sorted(lic.features),
        issued_at=lic.issued_at.isoformat(),
        expires_at=lic.expires_at.isoformat(),
        expired=lic.expired(datetime.now(UTC)),
    )


def _state(app: Any) -> LicenseStateOut:
    from src.ee import runtime

    rt = runtime(app)
    managed = env_source(rt.env)
    return LicenseStateOut(
        edition="enterprise" if rt.license is not None else "community",
        source=rt.source,
        managed_by_env=managed is not None,
        env_variable=_ENV_NAME.get(managed or ""),
        problem=rt.problem,
        license=_summary(rt.license) if rt.license is not None else None,
    )


def _refuse_if_env_managed(app: Any) -> None:
    from src.ee import runtime

    managed = env_source(runtime(app).env)
    if managed is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"The licence is managed by the server environment "
                    f"({_ENV_NAME[managed]}). Change it there and restart the API."),
        )


def _audit(action: str, request: Request, user: User, *,
           lic: License | None, previous: License | None) -> None:
    from src.security.audit import record_action

    detail: dict[str, Any] = {}
    if lic is not None:
        detail.update(customer=lic.customer, features=sorted(lic.features),
                      expires_at=lic.expires_at.isoformat())
    if previous is not None:
        detail.update(previous_customer=previous.customer,
                      previous_features=sorted(previous.features),
                      previous_expires_at=previous.expires_at.isoformat())
    record_action(action=action, actor=user.email, actor_id=user.id,
                  target="license", ip=client_ip(request), detail=detail)


@router.get("", response_model=LicenseStateOut)
async def get_license(request: Request,
                      _: User = Depends(require_admin)) -> LicenseStateOut:
    return _state(request.app)


@router.put("", response_model=LicenseStateOut)
async def put_license(body: LicenseIn, request: Request,
                      user: User = Depends(require_admin)) -> LicenseStateOut:
    from src.ee import apply_license, runtime

    app = request.app
    _refuse_if_env_managed(app)
    try:
        if len(body.token) > _MAX_BODY_CHARS:
            raise LicenseError("too large to be a licence")
        lic = verify(body.token)
    except LicenseError as exc:
        reason = str(exc)
        logger.warning("license_ui_refused reason=%s actor=%s", reason, user.id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_MESSAGES.get(reason, f"Not a valid Celmis licence: {reason}."),
        ) from None

    summary = lic.summary()
    summary.pop("edition", None)
    async with _write_lock():
        # Read under the lock, so the audit row's "previous" is what this
        # write actually replaced.
        previous = runtime(app).license
        await asyncio.to_thread(license_store.save_token, body.token, metadata=summary)
        apply_license(app, Loaded(lic, SOURCE_UI))
    logger.info("license_ui_saved customer=%s features=%s expires_at=%s actor=%s",
                lic.customer, ",".join(sorted(lic.features)),
                lic.expires_at.isoformat(), user.id)
    _audit("license.replaced" if previous is not None else "license.saved",
           request, user, lic=lic, previous=previous)
    return _state(app)


@router.delete("", response_model=LicenseStateOut)
async def delete_license(request: Request,
                         user: User = Depends(require_admin)) -> LicenseStateOut:
    from src.ee import reload_license, runtime

    app = request.app
    _refuse_if_env_managed(app)
    async with _write_lock():
        previous = runtime(app).license
        removed = await asyncio.to_thread(license_store.delete_token)
        if not removed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="No licence was entered in the UI.")
        reload_license(app)
    logger.info("license_ui_removed actor=%s", user.id)
    _audit("license.removed", request, user, lic=None, previous=previous)
    return _state(app)


__all__ = ["router"]
