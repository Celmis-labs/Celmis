"""Who is asking for knowledge packs, and which repositories they may be pointed at.

The `skills` and `brief` tools read the packs of ONE workspace, the one the
token names. This module turns the MCP caller into the principal the pack
visibility rule (`src.knowledge.visibility`) evaluates, and it is strict about
who is a person at all:

  * a refused token, an unauthenticated call (whichever deployment mode), a
    project token and an internal token see NO pack — every answer is the one
    for "nothing visible", which is also the one for a workspace with no packs;
  * a token whose workspace was only a fallback (`workspace_resolved` False)
    sees nothing either: a pack is never read from a workspace nobody named;
  * everybody else is a principal built from live rows (the superadmin through
    the user store, the role and the teams of THIS workspace), so a person who
    left the workspace loses their packs on the next call, whatever the token
    says.

Repository pointers in a pack (`slug`, `slug:path`, `slug:path:line`) are only
ever shown for a repository the caller may read as code, and for a path no rule
of theirs hides; any other pointer is dropped without a trace.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from src.knowledge.visibility import NOBODY, PackPrincipal, principal_for_sync
from src.mcp_server.dev_profile import access

logger = logging.getLogger(__name__)

#: The one sentence for a skill that is missing, hidden, or in another workspace.
NOT_FOUND = "skill not found or not accessible"
#: Credentials that are not a person with a place in the workspace.
NO_PACK_KINDS = frozenset({"project", "internal"})
#: Most distinct repositories checked for one answer.
MAX_POINTER_REPOS = 200


def caller_principal() -> tuple[PackPrincipal, str]:
    """``(principal, workspace id)`` of the current call; ``(NOBODY, "")`` for any
    caller that may not read packs. Never raises."""
    try:
        from src.mcp_server import identity

        caller = identity.resolve_caller()
        if (getattr(caller, "refused", "") or not getattr(caller, "authenticated", False)
                or getattr(caller, "kind", "") in NO_PACK_KINDS
                or not getattr(caller, "workspace_resolved", False)
                or not getattr(caller, "user_id", "") or not getattr(caller, "workspace_id", "")):
            return NOBODY, ""
        return principal_for_sync(caller.user_id, caller.workspace_id), caller.workspace_id
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_caller_failed err=%s", type(exc).__name__)
        return NOBODY, ""


def pointer_slug(pointer: object) -> str:
    """The repository part of a ``slug[:path[:line]]`` pointer."""
    return str(pointer).split(":", 1)[0].strip()


class RepoGate:
    """Which repository pointers the current caller may be shown.

    Built once per answer from the pointers that could appear in it: one access
    resolution for all of them, through the same scope every other dev tool
    uses (`access.mcp_scope`), so a token's repository list and the deny
    globs of the caller's rules apply. Anything that goes wrong shows no
    pointer at all.
    """

    def __init__(self, pointers: Iterable[object], only: Collection[str] = ()) -> None:
        self._only = frozenset(only)
        self._scope = None
        self._readable: frozenset[str] = frozenset()
        slugs = sorted({pointer_slug(p) for p in pointers} - {""})[:MAX_POINTER_REPOS]
        if not slugs:
            return
        try:
            scope = access.mcp_scope(slugs)
            self._readable = frozenset(scope.slugs(need="code")) & frozenset(slugs)
            self._scope = scope
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("pack_repo_gate_failed err=%s", type(exc).__name__)

    def repo_ok(self, slug: str) -> bool:
        """May ``slug`` be named to this caller (readable as code, and inside the
        ``repos=`` narrowing when the caller gave one)?"""
        return slug in self._readable and (not self._only or slug in self._only)

    def allows(self, pointer: object) -> bool:
        parts = str(pointer).split(":", 2)
        slug = parts[0].strip()
        if not slug or not self.repo_ok(slug):
            return False
        if len(parts) > 1 and parts[1].strip() and self._scope is not None:
            return bool(self._scope.listable(slug, parts[1].strip()))
        return True

    def filter(self, pointers: Iterable[object], limit: int = 6) -> list[str]:
        return [str(p)[:200] for p in pointers if self.allows(p)][:limit]
