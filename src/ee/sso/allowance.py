# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The single sign-on allowance: how many people may sign in through it.

LICENSE_EE lets an installation use single sign-on without a commercial
licence while no more than :data:`FREE_SSO_SEATS` people sign in through it. A
licence that grants ``sso`` lifts that to its ``sso_seats`` claim, or to no
limit when the claim is absent (and never to fewer than the free allowance).

THIS FILE is the check. It counts the accounts bound to single sign-on that
are active at the moment a new person is added, and refuses the one that would
pass the limit. It is a licence check within the meaning of LICENSE_EE section
2, and so is the hook below that makes every flow ask it — not only the
sign-in route but also reactivating a deactivated account that is bound to the
identity provider, which would otherwise be a second door in.

What it never does is switch anybody off. A person already bound to the
provider signs in however many there are, and a licence that lapses returns
the limit to the free allowance without disabling an account: only the next
NEW person is refused.

The generic seat count and the five-person limit of the community edition are
not here but in ``src/users/seats.py`` (AGPL); this file plugs into it through
:func:`src.users.seats.register_extension`, so the AGPL code never imports
``src.ee``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.users import User, UserStore, seats

#: People who may sign in through single sign-on without a licence.
FREE_SSO_SEATS = 5

EXTENSION_NAME = "sso_seats"


class AccountUnavailable(Exception):
    """The account to bind is gone or switched off (also: since it was read)."""


class AlreadyBound(Exception):
    """The account is already bound to a single sign-on identity."""


@dataclass(frozen=True)
class _Published:
    limit: int | None
    #: The limit is the licence's own, not the free allowance (only the wording
    #: of a refusal depends on it).
    from_licence: bool


_lock = threading.Lock()
_published: _Published | None = None
_until: datetime | None = None


def publish(limit: int | None, *, from_licence: bool, expires_at: datetime | None = None) -> None:
    """Called by ``src.ee`` when it applies a licence. ``limit`` ``None`` is
    unlimited. ``expires_at`` makes it lapse by itself back to the free
    allowance — a process outlives its licence."""
    global _published, _until
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError("the single sign-on limit must be a positive integer or None")
    with _lock:
        _published, _until = _Published(limit, bool(from_licence)), expires_at


def clear() -> None:
    """No licence in force: the free allowance."""
    global _published, _until
    with _lock:
        _published, _until = None, None


def effective(now: datetime | None = None) -> tuple[int | None, bool]:
    """``(limit, from_licence)`` in force; limit ``None`` is unlimited."""
    with _lock:
        published, until = _published, _until
    if published is None or (until is not None and (now or datetime.now(UTC)) >= until):
        return FREE_SSO_SEATS, False
    return published.limit, published.from_licence


def used(store: UserStore | None = None) -> int:
    """Active accounts bound to single sign-on (the platform's own excluded)."""
    return seats.count_people(store, oidc_only=True)


def admit(store: UserStore, user: User, written: bool) -> None:
    """The check: may ``user`` be a single sign-on person? Raises
    :class:`src.users.seats.SeatLimitReached` (kind ``sso_seats``) when not.
    Applies only to an account bound to the provider; ``written`` says the
    account is already counted, so "over" means strictly more than the limit."""
    if not user.has_oidc or seats.is_platform_account(user):
        return
    limit, from_licence = effective()
    if limit is None:
        return
    count = used(store)
    if (count > limit) if written else (count >= limit):
        raise seats.SeatLimitReached("sso_seats", count - 1 if written else count, limit,
                                     licensed=from_licence)


def report(store: UserStore) -> dict[str, Any]:
    """The admin card's block: used and limit (``None`` = unlimited), and the
    free allowance for the sentence "up to N people without a licence"."""
    return {"used": used(store), "limit": effective()[0], "free": FREE_SSO_SEATS}


def create_person(store: UserStore, user: User) -> User:
    """A NEW person signing in through single sign-on: needs a free seat and a
    free place in the allowance. Nothing is written when either refuses."""
    if not user.has_oidc:
        raise ValueError("a single sign-on person carries the provider's identity")
    return seats.create_person(store, user)


def bind(store: UserStore, user_id: str, *, issuer: str, subject: str) -> User:
    """Bind an EXISTING local account to single sign-on. It keeps its seat and
    takes a place in the allowance; refused (nothing written) when none is free.

    The row is read again under the lock — so an account erased, switched off
    or bound meanwhile is not put back as it was read — and only the two
    identity columns are written, conditionally on the row being active and
    unbound now. Raises :class:`AccountUnavailable`, :class:`AlreadyBound`,
    ``UserExistsError`` (another account holds the identity) or
    :class:`src.users.seats.SeatLimitReached`."""
    with seats.guard:
        fresh = store.get_by_id(user_id)
        if fresh is None or not fresh.is_active:
            raise AccountUnavailable
        if seats.is_platform_account(fresh):
            raise ValueError("platform accounts cannot be bound to single sign-on")
        if fresh.has_oidc:
            raise AlreadyBound
        admit(store, fresh, False)
        if not store.bind_oidc(fresh.id, issuer, subject):
            # Changed between the read and the write — by another process.
            again = store.get_by_id(fresh.id)
            if again is None or not again.is_active:
                raise AccountUnavailable
            raise AlreadyBound
        fresh.oidc_iss, fresh.oidc_sub = issuer, subject
        try:
            admit(store, fresh, True)
        except seats.SeatLimitReached:
            store.unbind_oidc(fresh.id, issuer, subject)
            raise
        return fresh


seats.register_extension(seats.Extension(EXTENSION_NAME, admit=admit, report=report))

__all__ = [
    "EXTENSION_NAME",
    "FREE_SSO_SEATS",
    "AccountUnavailable",
    "AlreadyBound",
    "admit",
    "bind",
    "clear",
    "create_person",
    "effective",
    "publish",
    "report",
    "used",
]
