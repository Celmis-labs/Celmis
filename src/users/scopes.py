"""What scopes an account actually holds.

Two places asked this question and disagreed. Token issuance computed the
answer — an account carrying the standard set picks up new members of that set
as they are added, so a feature does not become unreachable for everyone who
signed up before it existed. Client registration read the stored list straight
off the record, which is the same account's *older* answer, and refused to
grant a scope the caller's own token was already carrying.

One function now, used by both. Anything else that needs to know is expected
to call it rather than read `user.scopes`.
"""

from __future__ import annotations

#: The set a normal account is issued. `write:repos` lets an automated caller —
#: an external Claude Code over MCP, or a ticket connector — register a
#: repository and start an audit: the same authority the person already has by
#: clicking, reachable from another surface.
STANDARD_SCOPES: tuple[str, ...] = (
    "read:graph", "read:groups", "write:groups", "review:pr", "write:repos",
)


def held_scopes(user) -> list[str]:
    """Scopes this account holds, upgrading ones that predate a new member.

    An account whose list contains `write:groups` was issued the standard set,
    so it gains later additions to that set. A list somebody deliberately
    narrowed is returned exactly as stored — a deploy must not widen a
    restriction back open.
    """
    stored = list(getattr(user, "scopes", None) or [])
    if "write:groups" in stored:
        return sorted(set(stored) | set(STANDARD_SCOPES))
    return stored


__all__ = ["STANDARD_SCOPES", "held_scopes"]
