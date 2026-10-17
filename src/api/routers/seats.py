"""Seats: what the installation holds, against what it may hold.

    GET /api/admin/seats    used and limit for seats and single sign-on  (global admin)
    GET /api/invites/seats  is there a free seat? — a yes or no          (inviter, team workspace)

The rule lives in ``src/users/seats.py``: five people without a licence, and a
licence lifts the limit; single sign-on has an allowance of its own
(``src/ee/sso/allowance.py``) that appears here only in a build that has it.
The first route is the card on ``/admin/health``. The second is what the
invite dialog needs and nothing more: whether a person WITHOUT an account could
still be added. It answers a bare boolean, because a workspace admin has no
business learning how many accounts the rest of the installation holds — and
only to somebody who may create an invite in a TEAM workspace, which is the
only place that dialog exists. Every signed-in person owns a personal
workspace, so the plain "workspace admin" gate alone would have let anybody
ask whether the installation is full.

Neither route changes anything, and neither names a person.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.api.deps import current_workspace_id, get_users, require_admin, require_workspace_admin
from src.users import User, UserStore, seats

router = APIRouter(tags=["seats"])


class SeatCountOut(BaseModel):
    used: int
    #: None = unlimited.
    limit: int | None


class SeatsOut(BaseModel):
    seats: SeatCountOut
    #: Single sign-on's allowance; absent in a build without the enterprise
    #: package, where there is no single sign-on to count.
    sso_seats: SeatCountOut | None = None
    #: A licence is in force (it may or may not lift the limits).
    licensed: bool
    #: The free allowances, for the sentence "up to N people without a licence".
    free_seats: int
    free_sso_seats: int | None = None


class FreeSeatOut(BaseModel):
    #: Could one more person without an account sign up right now?
    has_free_seat: bool


@router.get("/api/admin/seats", response_model=SeatsOut)
def get_seats(
    _: User = Depends(require_admin),
    users: UserStore = Depends(get_users),
) -> SeatsOut:
    state = seats.seat_state(users)
    sso = state.get("sso_seats")
    return SeatsOut(
        seats=SeatCountOut(**state["seats"]),
        sso_seats=SeatCountOut(used=sso["used"], limit=sso["limit"]) if sso else None,
        licensed=state["licensed"],
        free_seats=seats.FREE_SEATS,
        free_sso_seats=sso.get("free") if sso else None,
    )


def is_personal_workspace(slug: str | None, users: UserStore) -> bool:
    """Is this the personal workspace of some account (slug ``u-<user id>``,
    see ``workspace_provision.personal_slug``)? A team workspace whose name
    merely starts with ``u-`` is not: the rest must be an account's id."""
    from src.api.workspace_provision import personal_slug

    if not slug or not slug.startswith(personal_slug("")):
        return False
    owner = slug[len(personal_slug("")):]
    return bool(owner) and users.get_by_id(owner) is not None


def _slug_of(workspace_id: str) -> str | None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.api.deps import sync_database_url
    from src.db.models import Workspace

    eng = create_engine(sync_database_url(), pool_pre_ping=True)
    try:
        with Session(eng) as s:
            ws = s.get(Workspace, workspace_id)
            return ws.slug if ws is not None else None
    finally:
        eng.dispose()


async def require_team_workspace_inviter(
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
    users: UserStore = Depends(get_users),
) -> User:
    """The people who may create an invite (`require_workspace_admin`, the
    gate of ``POST /api/invites``) — in a team workspace. In somebody's personal
    workspace nobody is invited, so the question is not theirs to ask."""
    import asyncio

    slug = await asyncio.to_thread(_slug_of, workspace_id)
    if slug is None or await asyncio.to_thread(is_personal_workspace, slug, users):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires owner/admin on a team workspace",
        )
    return user


@router.get("/api/invites/seats", response_model=FreeSeatOut)
async def has_free_seat(
    _: User = Depends(require_team_workspace_inviter),
    users: UserStore = Depends(get_users),
) -> FreeSeatOut:
    import asyncio

    state = await asyncio.to_thread(seats.seat_state, users)
    limit = state["seats"]["limit"]
    return FreeSeatOut(has_free_seat=limit is None or state["seats"]["used"] < limit)
