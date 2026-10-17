"""One way to write a moment in the Codebase API: ISO-8601, always UTC, always with its offset.

PostgreSQL hands back timezone-aware values and SQLite (tests, small installs)
hands back naive ones. A naive string such as ``2026-10-17T09:30:00`` has no
offset, so a browser reads it as LOCAL time and a pack uploaded "just now" shows
hours in the past or future. Every timestamp the Codebase routes answer with goes
through :func:`iso_utc`, so the text is the same on both: ``2026-10-17T09:30:00+00:00``.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(moment: datetime | None) -> str | None:
    """``moment`` as ISO-8601 in UTC with an explicit offset; None stays None.

    A naive value is taken as UTC, which is what the application stores; an
    aware one in another zone is converted."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


__all__ = ["iso_utc"]
