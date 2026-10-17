"""Seats: how many people one Celmis may hold, and who counts.

THE RULE
--------
Without a licence (the community edition) an installation holds at most
:data:`FREE_SEATS` people. A licence lifts the limit with an optional claim,
``seats`` (src/ee/license.py): a licence without it is unlimited, so licences
issued before seats existed keep working unchanged, and a licence never gives
FEWER than the free allowance (the limit in force is the larger of the two).

The same installation may also use single sign-on up to a second allowance.
That allowance is an enterprise matter and lives with the single sign-on code
(``src/ee/sso/allowance.py``); this module only offers it the places to hook
in (:func:`register_extension`) and never learns what it counts.

NOBODY IS EVER LOCKED OUT
-------------------------
The limits refuse ADDING a person, and nothing else. An installation that is
already above a limit — it had seven people when the limit became five, or a
licence ran out — keeps every existing account working: they sign in, accept
invites, join workspaces and teams as before. Only growth stops.

WHO COUNTS
----------
A SEAT is an active account that is not platform-owned: not the master
(break-glass) account, and not the seeded local ``default`` user. Deactivating
an account — or erasing it, which deactivates it — frees its seat. Counting is
one SQL ``COUNT`` (:meth:`UserStore.count_people`), not a scan of the table.

WHERE IT IS ENFORCED
--------------------
In the flows that create or switch on an account, not in ``UserStore.create``:
tests and the seeding of the platform accounts build users directly and must
keep doing so. A flow calls :func:`create_person` instead of ``users.create``
and :func:`reactivate_person` to switch a deactivated account on again.
``tests/users/test_every_way_to_add_a_person_asks_the_seats.py`` reads the
source and fails when a new place builds a ``User`` or flips ``is_active``
without going through here.

THE LICENCE BOUNDARY
--------------------
This module is AGPL and must not import ``src.ee``. The licence code (EE)
imports THIS module and tells it what the licence in force allows:
:func:`set_licence_limits` / :func:`clear_licence_limits`. With nothing
registered, :func:`current_limits` is the community default.

CONCURRENCY
-----------
The check and the creation happen under one process-level lock (:data:`guard`),
and the people are counted again after writing: a creation that took the count
over the limit is undone and refused. That closes the race between threads of
one process. Several API processes sharing one users database could still both
pass the check before either writes; the re-count after the write makes the
overshoot at most the number of racing requests and undoes the later ones when
it sees it, but it is not a distributed lock. The shipped compose runs one API
process. Writes that change an existing account are single-column and
conditional on the row as it is now (``UserStore.set_active`` and friends), so
a stale copy of an account is never written back.

Nothing here puts an email or a name in a message, and a message sent to a
caller who is not signed in carries no head-count either: the exact numbers
are on ``/api/admin/seats`` and in the audit row.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from src.users.models import User
from src.users.roles import MASTER_ADMIN_ID, is_superadmin, master_email
from src.users.store import DEFAULT_USER_ID, UserNotFoundError, UserStore, get_user_store

#: People an installation may hold without a licence.
FREE_SEATS = 5

#: Accounts the platform owns rather than a person (see
#: ``src.api.routers.users.PLATFORM_USER_IDS``): the seeded local user and the
#: master-key recovery account. They never take a seat and are never refused.
PLATFORM_ACCOUNT_IDS = frozenset({DEFAULT_USER_ID, MASTER_ADMIN_ID})

#: The two codes a refusal carries, in `detail.code`.
CODE_SEATS = "seat_limit_reached"
CODE_SSO_SEATS = "sso_seat_limit_reached"


@dataclass(frozen=True)
class SeatLimits:
    """What the installation may hold. ``None`` means unlimited."""

    seats: int | None
    #: A licence is in force.
    licensed: bool


#: Nothing registered: the community edition.
COMMUNITY_LIMITS = SeatLimits(seats=FREE_SEATS, licensed=False)


class SeatLimitReached(Exception):
    """Adding one more person would pass a limit.

    Carries the counts for the operator (the audit row, ``/api/admin/seats``)
    and a message for the person turned away, which names the LIMIT and never
    how many accounts there are — a caller who is not signed in can trigger it.
    """

    def __init__(self, kind: Literal["seats", "sso_seats"], used: int, limit: int,
                 *, licensed: bool = False) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        self.licensed = licensed
        super().__init__(self.message)

    @property
    def code(self) -> str:
        return CODE_SEATS if self.kind == "seats" else CODE_SSO_SEATS

    @property
    def message(self) -> str:
        if self.kind == "seats":
            return (f"This Celmis has reached its limit of {self.limit} people. "
                    "An administrator can add a licence or deactivate an account.")
        if self.licensed:
            return (f"Single sign-on is limited to {self.limit} people by this "
                    "installation's licence.")
        return f"Single sign-on is limited to {self.limit} people without a licence."

    @property
    def detail(self) -> dict[str, str]:
        """The body of the 403: ``{"code": ..., "message": ...}``."""
        return {"code": self.code, "message": self.message}


# ─── what the licence allows ────────────────────────────────────────

_registry_lock = threading.Lock()
_registered: SeatLimits | None = None
_registered_until: datetime | None = None


def set_licence_limits(
    seats: int | None,
    licensed: bool,
    *,
    expires_at: datetime | None = None,
) -> None:
    """Called by the licence code when it applies a licence. ``None`` is
    unlimited. ``expires_at`` makes the registration lapse by itself: a
    process outlives its licence, and after expiry the limit is the community
    one again (nobody is disabled — see the module docstring)."""
    global _registered, _registered_until
    if seats is not None and (isinstance(seats, bool) or not isinstance(seats, int) or seats <= 0):
        raise ValueError("seats must be a positive integer or None")
    with _registry_lock:
        _registered = SeatLimits(seats=seats, licensed=bool(licensed))
        _registered_until = expires_at


def clear_licence_limits() -> None:
    """Called when the licence is removed or refused: back to the community limit."""
    global _registered, _registered_until
    with _registry_lock:
        _registered, _registered_until = None, None


def current_limits(now: datetime | None = None) -> SeatLimits:
    """The limit in force: the registered licence's, or the community default."""
    with _registry_lock:
        limits, until = _registered, _registered_until
    if limits is None:
        return COMMUNITY_LIMITS
    if until is not None and (now or datetime.now(UTC)) >= until:
        return COMMUNITY_LIMITS
    return limits


# ─── counting ───────────────────────────────────────────────────────


def is_platform_account(user: Any) -> bool:
    """The master account or the seeded local user: never a seat, never refused."""
    return getattr(user, "id", None) in PLATFORM_ACCOUNT_IDS or is_superadmin(user)


def count_people(store: UserStore | None = None, *, oidc_only: bool = False) -> int:
    """Active accounts that are not the platform's — one ``COUNT`` query. With
    ``oidc_only``, those bound to single sign-on. The same people as
    :func:`is_platform_account` leaves in, expressed in SQL."""
    store = store if store is not None else get_user_store()
    return store.count_people(
        exclude_ids=PLATFORM_ACCOUNT_IDS, exclude_email=master_email(), oidc_only=oidc_only,
    )


def used_seats(store: UserStore | None = None) -> int:
    return count_people(store)


# ─── extensions: limits that live in other (enterprise) code ────────


@dataclass(frozen=True)
class Extension:
    """A limit this module does not own, hooked in by the code that does.

    ``admit(store, user, written)`` raises :class:`SeatLimitReached` when
    ``user`` may not be added (or switched on). Before the write
    (``written=False``) the person is not counted yet; after it
    (``written=True``) they are, and "over" means strictly more than the limit.
    ``report(store)`` is what the admin card shows for it.
    """

    name: str
    admit: Callable[[UserStore, User, bool], None]
    report: Callable[[UserStore], dict[str, Any]]


_extensions: dict[str, Extension] = {}


def register_extension(extension: Extension) -> None:
    """Hook a further limit into every flow that adds or switches on a person.
    Registering a name again replaces it."""
    _extensions[extension.name] = extension


def seat_state(store: UserStore | None = None) -> dict[str, Any]:
    """What the admin card shows: used and limit for seats (limit ``None`` =
    unlimited), whether a licence is in force, and one block per registered
    extension (single sign-on, in a build that has it)."""
    store = store if store is not None else get_user_store()
    limits = current_limits()
    state: dict[str, Any] = {
        "seats": {"used": count_people(store), "limit": limits.seats},
        "licensed": limits.licensed,
    }
    for name, extension in tuple(_extensions.items()):
        state[name] = extension.report(store)
    return state


# ─── checking ───────────────────────────────────────────────────────


def _check(store: UserStore, user: User, *, written: bool) -> None:
    """Would ``user`` pass a limit? ``written``: they are already counted."""
    limits = current_limits()
    if limits.seats is not None:
        used = count_people(store)
        if (used > limits.seats) if written else (used >= limits.seats):
            raise SeatLimitReached("seats", used - 1 if written else used, limits.seats,
                                   licensed=limits.licensed)
    for extension in tuple(_extensions.values()):
        extension.admit(store, user, written)


# ─── doing, under one lock ──────────────────────────────────────────

#: Serialises check + write inside this process. Reentrant: a flow may nest.
guard = threading.RLock()


def create_person(store: UserStore, user: User) -> User:
    """``store.create(user)`` for a person: refused when there is no free seat
    (or a registered extension refuses). Platform accounts and accounts
    created already inactive take no seat and are never refused.

    Raises :class:`SeatLimitReached` (nothing is left written) or whatever
    ``store.create`` raises."""
    if is_platform_account(user) or not user.is_active:
        return store.create(user)
    with guard:
        _check(store, user, written=False)
        created = store.create(user)
        try:
            _check(store, user, written=True)
        except SeatLimitReached:
            store.delete(user.id)
            raise
        return created


def reactivate_person(store: UserStore, user: User) -> User:
    """Switch a deactivated account on again. It needs a free seat (and whatever
    a registered extension asks of it — a single-sign-on person needs a free
    single-sign-on place); an account that is already active, or a platform
    account, passes straight through.

    The row is read again under the lock and only ``is_active`` is written, so
    an account erased or changed since the caller read it is not put back as it
    was. Raises :class:`UserNotFoundError` when it is gone."""
    with guard:
        fresh = store.get_by_id(user.id)
        if fresh is None:
            raise UserNotFoundError(f"user id={user.id!r} not found")
        if fresh.is_active:
            return fresh
        if is_platform_account(fresh):
            store.set_active(fresh.id, True, expect=False)
            fresh.is_active = True
            return fresh
        _check(store, fresh, written=False)
        if not store.set_active(fresh.id, True, expect=False):
            # Switched on by somebody else between the read and the write.
            return store.get_by_id(fresh.id) or fresh
        fresh.is_active = True
        try:
            _check(store, fresh, written=True)
        except SeatLimitReached:
            store.set_active(fresh.id, False, expect=True)
            raise
        return fresh


def deactivate_person(store: UserStore, user_id: str) -> bool:
    """Switch an account off — the seat is free again. Nothing else changes and
    nothing is deleted. True when the account was active. Platform accounts
    refuse (``ValueError``): the break-glass account must stay usable."""
    with guard:
        fresh = store.get_by_id(user_id)
        if fresh is None:
            raise UserNotFoundError(f"user id={user_id!r} not found")
        if is_platform_account(fresh):
            raise ValueError("platform accounts cannot be deactivated")
        return store.set_active(user_id, False, expect=True)


__all__ = [
    "CODE_SEATS",
    "CODE_SSO_SEATS",
    "COMMUNITY_LIMITS",
    "FREE_SEATS",
    "PLATFORM_ACCOUNT_IDS",
    "Extension",
    "SeatLimitReached",
    "SeatLimits",
    "clear_licence_limits",
    "count_people",
    "create_person",
    "current_limits",
    "deactivate_person",
    "guard",
    "is_platform_account",
    "reactivate_person",
    "register_extension",
    "seat_state",
    "set_licence_limits",
    "used_seats",
]
