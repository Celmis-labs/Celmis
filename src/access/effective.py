"""The one decision: what may THIS caller read of THESE repositories.

Three readings used to answer that question on their own — the research rules
(``resolver.py``), the team grants (``deps.py``) and, for MCP, the token — and
each of them fell open in its own way. :func:`effective_access` is the single
read path:

1. **Scope.** Only repositories registered in the caller's workspace exist for
   the caller, in both deployment modes. Anything else is absent — the same
   answer as a repository that was never there.
2. **A person** (no token list): the resolver's reading — owner/admin and
   global admin see everything of the workspace; a team grant of ``read`` or
   higher means ``code``; a ``RepoAccessRule`` of the team narrows it; a repo
   nobody granted anything on is closed (:mod:`src.access.policy`).
3. **A token with a repo list** (``mcp_tokens.repo_patterns``): the list is
   authoritative — the superadmin issued it for a named person — and grants
   ``code`` on the matching repositories of the token's workspace, team rules
   or not. Path-level ``deny_globs`` of any rule on the repo still conceal
   paths (secrets, crypto, connections) — a token never opens those.

:class:`CallerScope` is what MCP tool bodies use: ``scope.get(slug)`` returns a
decision or raises :class:`RepoNotAccessible`, whose message is the same for a
repository that is denied and one that does not exist, so no answer is an
existence oracle.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.access.resolver import RepoAccessDecision, _RuleView

logger = logging.getLogger(__name__)

#: The only sentence a caller hears about a repository it may not read —
#: whether it is denied or does not exist.
NOT_ACCESSIBLE = "repo not found or not accessible"

#: What the pattern list means when it is ``*``: every repository of the workspace.
ANY = "*"


class RepoNotAccessible(LookupError):
    """Raised for a repository the caller may not read. ``str(e)`` is always
    :data:`NOT_ACCESSIBLE`, whether the repo is denied or absent."""

    def __init__(self) -> None:
        super().__init__(NOT_ACCESSIBLE)


@dataclass(frozen=True)
class Principal:
    """Who is asking, as far as access is concerned."""

    user_id: str
    #: Global admin (``User.is_admin``): sees every registered repo.
    is_admin: bool = False
    #: The token's repo list. ``None`` = no token constraint (a person asking
    #: with their own rights, or a self-service token whose ceiling is the
    #: person's own access).
    repo_patterns: tuple[str, ...] | None = None


# ════════════════════════════════════════════════════════════════════
# Patterns
# ════════════════════════════════════════════════════════════════════


def pattern_matches(pattern: str, *names: str) -> bool:
    """Does ``pattern`` (exact slug or glob) match any spelling in ``names``?

    Case-sensitive: slugs are. ``*`` matches across ``/`` and ``_``, so
    ``acme/*`` and ``github_acme-*`` both do what they look like.
    """
    pat = (pattern or "").strip()
    if not pat:
        return False
    return any(n and fnmatch.fnmatchcase(n, pat) for n in names)


def match_any(patterns: Iterable[str], *names: str) -> bool:
    return any(pattern_matches(p, *names) for p in patterns)


# ════════════════════════════════════════════════════════════════════
# Registry (the repositories that exist for a workspace)
# ════════════════════════════════════════════════════════════════════


def registered_repos(workspace_id: str) -> dict[str, tuple[str, ...]]:
    """``{indexed slug: (slug, full_name)}`` of the workspace's own repos.

    Empty on any failure: an unreadable registry means "nothing exists", never
    "everything does".
    """
    if not workspace_id:
        return {}
    try:
        from src.api.auto_review import get_auto_review_store

        out: dict[str, tuple[str, ...]] = {}
        for cfg in get_auto_review_store().list_for_workspace(workspace_id):
            names = tuple(n for n in (cfg.repo_slug, getattr(cfg, "full_name", "")) if n)
            out[cfg.repo_slug] = names
        return out
    except Exception as exc:  # noqa: BLE001 — refuse rather than grant
        logger.warning("repo_registry_unreadable ws=%s err=%s", workspace_id, exc)
        return {}


def matched_repos(patterns: Iterable[str], workspace_id: str) -> list[str]:
    """The workspace's repositories a pattern list covers (for the issue
    preview and for the verifier)."""
    pats = tuple(patterns)
    reg = registered_repos(workspace_id)
    return sorted(slug for slug, names in reg.items() if match_any(pats, *names))


# ════════════════════════════════════════════════════════════════════
# The decision
# ════════════════════════════════════════════════════════════════════


def _token_decisions(session, workspace_id: str, slugs: list[str]
                     ) -> dict[str, RepoAccessDecision]:
    """``code`` on ``slugs``, minus every path a rule of the repo conceals."""
    from sqlalchemy import select

    from src.db.models import RepoAccessRule

    if not slugs:
        return {}
    rules = session.execute(
        select(RepoAccessRule).where(
            RepoAccessRule.workspace_id == workspace_id,
            RepoAccessRule.repo_slug.in_(slugs),
        )
    ).scalars().all()
    deny: dict[str, list[str]] = {}
    for r in rules:
        deny.setdefault(r.repo_slug, []).extend(str(g) for g in (r.deny_globs or []))
    out: dict[str, RepoAccessDecision] = {}
    for slug in slugs:
        globs = tuple(dict.fromkeys(deny.get(slug, [])))
        if not globs:
            out[slug] = RepoAccessDecision.full(slug)
            continue
        out[slug] = RepoAccessDecision(
            repo_slug=slug, visibility="code",
            rules=(_RuleView("code", (), globs, ()),),
            open_default=False, deny_globs=globs,
        )
    return out


def effective_access_sync(
    session,
    principal: Principal,
    workspace_id: str,
    repos: Iterable[str] | None = None,
) -> dict[str, RepoAccessDecision]:
    """``{slug: decision}`` for every slug asked about (or, with ``repos=None``,
    for every repository registered in the workspace). A slug the caller may
    not read — or that does not exist for it — is ``denied``."""
    from src.access.resolver import resolve_access_sync

    registry = registered_repos(workspace_id)
    asked = list(dict.fromkeys(registry if repos is None else repos))
    out: dict[str, RepoAccessDecision] = {r: RepoAccessDecision.denied(r) for r in asked}
    known = [r for r in asked if r in registry]
    if not known:
        return out

    patterns = principal.repo_patterns
    if patterns is not None:
        listed = [r for r in known if match_any(patterns, *registry[r])]
        out.update(_token_decisions(session, workspace_id, listed))
        return out

    out.update(resolve_access_sync(
        session,
        user_id=principal.user_id,
        is_admin=principal.is_admin,
        workspace_id=workspace_id,
        repos=known,
    ))
    return out


def effective_access(
    principal: Principal,
    workspace_id: str,
    repos: Iterable[str] | None = None,
) -> dict[str, RepoAccessDecision]:
    """:func:`effective_access_sync` on its own session. Fails CLOSED: a
    database error denies every repository asked about."""
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine

    asked = None if repos is None else list(dict.fromkeys(repos))
    try:
        with Session(_sync_engine()) as s:
            return effective_access_sync(s, principal, workspace_id, asked)
    except Exception as exc:  # noqa: BLE001
        logger.error("effective_access_failed err=%s — failing CLOSED (deny)", exc)
        return {r: RepoAccessDecision.denied(r) for r in (asked or [])}


# ════════════════════════════════════════════════════════════════════
# What an MCP tool body holds
# ════════════════════════════════════════════════════════════════════

Need = Literal["metadata", "code"]


@dataclass(frozen=True)
class CallerScope:
    user_id: str
    workspace_id: str
    token_id: str | None
    #: pat | cli | self | oauth | legacy | session | stdio
    kind: str
    allow_write: bool
    #: ONLY researchable repos. Absent == denied or does not exist.
    decisions: Mapping[str, RepoAccessDecision] = field(default_factory=dict)

    def slugs(self, *, need: Need = "metadata") -> list[str]:
        """The repos the caller may use at ``need``, sorted. Repos the caller
        may not read are not in the list and not anywhere else."""
        want_code = need == "code"
        return sorted(
            s for s, d in self.decisions.items()
            if (d.code_visible if want_code else d.researchable)
        )

    def get(self, slug: str, *, need: Need = "code") -> RepoAccessDecision:
        """The decision for ``slug``, or :class:`RepoNotAccessible`."""
        dec = self.decisions.get(slug)
        if dec is None or not (dec.code_visible if need == "code" else dec.researchable):
            raise RepoNotAccessible()
        try:
            from src.mcp_server import callctx

            callctx.note_repos(slug)
        except Exception:  # noqa: BLE001 — audit bookkeeping never blocks a read
            pass
        return dec


def _indexed_slugs() -> list[str]:
    try:
        from src.mcp_server import tools as legacy

        # Names only: `list_repos()` opens every graph to count symbols, which
        # made each howto/MCP call cost seconds per repository in the workspace.
        return legacy.list_repo_slugs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("indexed_repos_unreadable err=%s", exc)
        return []


def mcp_scope(candidates: Iterable[str] | None = None) -> CallerScope:
    """The current MCP caller's scope over ``candidates`` (default: every
    indexed repository). The only access entry point new MCP code uses."""
    from src.mcp_server.identity import caller_access

    slugs = list(dict.fromkeys(_indexed_slugs() if candidates is None else candidates))
    caller, access = caller_access(slugs)
    return CallerScope(
        user_id=caller.user_id,
        workspace_id=caller.workspace_id,
        token_id=caller.token_id,
        kind=caller.kind,
        allow_write=caller.allow_write,
        decisions={s: d for s, d in access.items() if d is not None and d.researchable},
    )


__all__ = [
    "ANY", "NOT_ACCESSIBLE", "CallerScope", "Principal", "RepoNotAccessible",
    "effective_access", "effective_access_sync", "match_any", "matched_repos",
    "mcp_scope", "pattern_matches", "registered_repos",
]
