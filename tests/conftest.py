"""Suite-wide fixtures.

Seat limits (src/users/seats.py) are process state: the licence code registers
what the licence in force allows, and the registration outlives the app that
made it. A test that applied a licence granting two seats would otherwise hand
two seats to every test that runs after it. So each test starts and ends on the
community limits (five seats, and the single-sign-on allowance of five people,
src/ee/sso/allowance.py) — a test that wants a licensed installation applies
one itself, inside the test.
"""

from __future__ import annotations

import pytest


def _community_limits() -> None:
    from src.users import seats

    seats.clear_licence_limits()
    try:
        from src.ee.sso import allowance
    except ImportError:  # a build without the enterprise package
        return
    allowance.clear()


@pytest.fixture(autouse=True)
def _community_seat_limits():
    _community_limits()
    yield
    _community_limits()
