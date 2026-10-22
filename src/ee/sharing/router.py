# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Making a share and giving more of one: the licensed routes of repository sharing.

    POST /api/sharing-grants                    share a repository of this workspace
    POST /api/sharing-grants/{share_id}/extend  give more of a running share
    GET  /api/sharing-grants/workspaces         the workspaces a share could be made to

Seeing a share, narrowing it, blocking a person, ending it and reading who used it
are free and live in ``src/api/routers/sharing.py`` (``/api/sharing``); this module
is only the two acts that widen what another workspace may read. The prefix is its
own on purpose: ``/api/sharing`` does not cover ``/api/sharing-grants``, so the
capabilities document can say that sharing is on while granting is not.

An enterprise feature: mounted by ``src.ee.mount_enterprise`` only when the licence
grants ``sharing``, and asked about the licence again on every request
(``src.ee._still_licensed``). The routes answer with the shapes of the free router
(``ShareDetailOut``), so a client reads one kind of share.

Who may: creating is for the administrators of the ACTIVE workspace (the owner of
the repository) and answers 403 to anybody else, as every workspace-admin route
does. Extending names a share and is gated on the share's OWNER
(``require_share_admin``): for the receiving workspace's people, a stranger, or a
mistyped id it answers 404, the answer a share that does not exist gets.

The list of workspaces is the picker of the create dialog and is for the same people
as creating. It names workspaces the caller is usually not a member of, which is fine
only where all of an installation's workspaces are one organisation: under
``multi_tenant`` the route answers 404, the bytes an unknown path gets, to everybody and
before it asks who they are, and the dialog keeps its text field.

Each success is written to the OWNER's audit trail (``sharing.created``,
``sharing.extended`` with before and after); the receiving workspace's trail has no
row for it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from src.access import shares as repo_shares
from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_users,
    require_share_admin,
    require_workspace_admin,
)
from src.api.routers.sharing import ShareDetailOut, _iso, _jsonable, _session, share_detail
from src.deployment import is_single_tenant
from src.ee.sharing import service
from src.users import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sharing-grants", tags=["sharing"])


class ShareCreateIn(BaseModel):
    """What the owner agrees to when they share a repository."""

    model_config = ConfigDict(extra="forbid")

    #: The repository's slug, as the owner's repositories list shows it.
    repo_slug: str = Field(min_length=1, max_length=300)
    #: The receiving workspace: its slug or its id.
    workspace: str = Field(min_length=1, max_length=200)
    #: How long it lasts, from now. At most the token ceiling.
    days: StrictInt = repo_shares.DEFAULT_DAYS
    #: Paths the receiving workspace never sees, on top of the owner's own rules for
    #: the repository. Secrets and key files are hidden whatever is written here.
    deny_globs: list[str] = Field(default_factory=list, max_length=repo_shares.MAX_DENY_GLOBS)
    #: Most people who may hold a token for it.
    max_people: StrictInt = repo_shares.DEFAULT_PEOPLE
    #: Off unless the owner turns it on: a question sends excerpts of the repository
    #: to the receiving workspace's language-model provider, on its budget.
    allow_ask: StrictBool = False


class ShareExtendIn(BaseModel):
    """What a running share can be changed to. Every field is optional; at least one
    is needed. Unknown fields are refused so that a client does not believe it
    changed something."""

    model_config = ConfigDict(extra="forbid")

    #: A new end, counted from now. It must be later than the current end.
    days: StrictInt | None = None
    #: The whole list of the share's own hidden paths (not an addition to it).
    deny_globs: list[str] | None = Field(default=None, max_length=repo_shares.MAX_DENY_GLOBS)
    allow_ask: StrictBool | None = None
    max_people: StrictInt | None = None
    #: People (user ids) to switch back on.
    unblock: list[str] = Field(default_factory=list, max_length=repo_shares.MAX_BLOCKED)


class ReceivingWorkspaceOut(BaseModel):
    """A workspace a share can be made to: what a person picks it by."""

    id: str
    slug: str
    name: str


def _where_workspaces_are_one_organisation() -> None:
    """No route at all unless the installation's workspaces are one organisation.

    A dependency of its own, listed before any other on the route, so that under
    ``multi_tenant`` nobody is asked who they are first: the answer is the one a path
    that is not there gets."""
    if not is_single_tenant():
        raise HTTPException(status_code=404, detail="Not Found")


def _refuse(exc: repo_shares.ShareError) -> HTTPException:
    """A refusal as HTTP: the error's own status (404 and 409 are subclasses), else 422."""
    return HTTPException(status_code=getattr(exc, "status_code", 422), detail=str(exc))


@router.get("/workspaces", response_model=list[ReceivingWorkspaceOut])
def list_receiving_workspaces(
    _mode: None = Depends(_where_workspaces_are_one_organisation),
    workspace_id: str = Depends(current_workspace_id),
    _user: User = Depends(require_workspace_admin),
) -> list[ReceivingWorkspaceOut]:
    """The other workspaces of the installation that could receive a share from the
    active one, by name: not this workspace itself and not the built-in ``default``
    one. For the people who may make a share from it (its owners and admins, and the
    global administrators), and only where workspaces are one organisation."""
    with _session() as s:
        rows = service.receiving_workspaces(s, workspace_id)
        return [ReceivingWorkspaceOut(id=w.id, slug=w.slug, name=w.name) for w in rows]


@router.post("", response_model=ShareDetailOut, status_code=201)
def create_share(
    payload: ShareCreateIn,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
    user: User = Depends(require_workspace_admin),
    users: UserStore = Depends(get_users),
) -> ShareDetailOut:
    """Share one repository of the active workspace with another workspace.

    Nobody in the receiving workspace can read it yet: the superadmin names the
    share on a person's developer token, and the owner's consent (this row) holds on
    every call after that."""
    with _session() as s:
        try:
            made = service.create_share(
                s, owner_workspace_id=workspace_id, creator=user,
                repo_slug=payload.repo_slug, workspace=payload.workspace,
                days=payload.days, deny_globs=payload.deny_globs,
                max_people=payload.max_people, allow_ask=payload.allow_ask)
            s.commit()
        except repo_shares.ShareError as exc:
            s.rollback()
            raise _refuse(exc) from exc
        row = made.share
        detail = share_detail(s, row.id, users)
        audit = {
            "repo": row.repo_slug, "grantee_workspace_id": row.grantee_workspace_id,
            "expires_at": _iso(row.expires_at), "allow_ask": bool(row.allow_ask),
            "max_people": int(row.max_people), "deny_globs": list(row.deny_globs or []),
            "replaced": made.replaced,
        }
        share_id = row.id
    repo_shares.audit_owner(
        "sharing.created", owner_workspace_id=workspace_id, share_id=share_id,
        actor=user.email, actor_id=user.id, ip=client_ip(request), detail=audit)
    return detail


@router.post("/{share_id}/extend", response_model=ShareDetailOut)
def extend_share(
    share_id: str,
    payload: ShareExtendIn,
    request: Request,
    user: User = Depends(require_share_admin),
    users: UserStore = Depends(get_users),
) -> ShareDetailOut:
    """Give more of a running share: a later end, a different list of hidden paths,
    questions on, more people, a person switched back on. The receivers' next call
    sees it; no token is reissued."""
    from src.db.models import RepoShare

    with _session() as s:
        try:
            changes = service.extend_share(
                s, share_id, days=payload.days, deny_globs=payload.deny_globs,
                allow_ask=payload.allow_ask, max_people=payload.max_people,
                unblock=payload.unblock)
            s.commit()
        except repo_shares.ShareError as exc:
            s.rollback()
            raise _refuse(exc) from exc
        owner = s.get(RepoShare, share_id).owner_workspace_id
        detail = share_detail(s, share_id, users)
    if changes:
        repo_shares.audit_owner(
            "sharing.extended", owner_workspace_id=owner, share_id=share_id,
            actor=user.email, actor_id=user.id, ip=client_ip(request),
            detail={"changes": _jsonable(changes)})
    return detail


__all__ = ["ReceivingWorkspaceOut", "ShareCreateIn", "ShareExtendIn", "router"]
