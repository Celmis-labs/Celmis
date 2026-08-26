"""Research-access resolver — the single source of truth for *what a team
may learn about a repo* through Q&A / graph / vector search (Stage 22).

Enforced identically across the three surfaces so UI, API and MCP behave the
same:

  * Q&A retrieval  (``src/qa/multi_repo_retriever.py``)
  * MCP tool bodies (``src/mcp_server/http_app.py``)
  * REST endpoints  (``src/api/routers/access.py`` — read/CRUD of rules)

Semantics (see :class:`src.db.models.RepoAccessRule`):

  * ``visibility`` gate: ``none`` → repo invisible for research; ``metadata``
    → docs / architecture notes only; ``code`` → source code readable.
  * ``deny_globs`` always win, even at ``code`` level (creds, crypto, DB
    connections, secret verification).
  * ``allow_globs`` — if non-empty, an allow-list; deny still subtracts.

Fall-open convention (mirrors ``_effective_repo_permission``): if **no** rule
exists for a repo in the active workspace, every member gets full ``code``
access. Once *any* rule exists for that repo, teams without a matching rule
get ``none``. Global admins always bypass.

That fall-open is the single_tenant reading of "no rule", and it is the only
reading that lets a fresh install work at all. It is not a safe reading for a
box with several tenants on it, where "no rule yet" describes every repository
until somebody writes one — so it is gated on the deployment mode
(:mod:`src.deployment`): single_tenant keeps it, multi_tenant resolves an
unruled repo to ``denied`` instead. The DB-error path below already fails
closed regardless of mode.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

_VALID_VISIBILITY = ("none", "metadata", "code")
_VIS_RANK = {"none": 0, "metadata": 1, "code": 2}


# ════════════════════════════════════════════════════════════════════
# Glob matching (supports ** across directories + bare-dir prefixes)
# ════════════════════════════════════════════════════════════════════


@lru_cache(maxsize=2048)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob into an anchored regex.

    ``**`` matches any characters including ``/``; ``*`` matches within a
    single path segment; ``?`` matches one non-slash char.
    """
    i = 0
    out: list[str] = []
    n = len(pattern)
    while i < n:
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _norm_path(p: str) -> str:
    """Normalise a repo-relative path: backslashes → ``/`` and strip any
    leading ``./`` **prefix** (repeatedly). NOTE: we must not use
    ``str.lstrip("./")`` — that strips a *character set*, so ``.env`` would
    become ``env`` and a deny-glob like ``**/.env*`` would silently miss it."""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def glob_match(path: str, pattern: str) -> bool:
    """True if ``path`` matches ``pattern``.

    A pattern with no wildcard is treated as a *prefix / subtree* match so
    ``src/credentials`` blocks everything under that directory as well as the
    file/dir itself. Patterns with wildcards use full glob semantics.
    """
    if not pattern:
        return False
    path = _norm_path(path)
    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return False
    if not any(ch in pattern for ch in "*?"):
        base = pattern.rstrip("/")
        return path == base or path.startswith(base + "/")
    if _glob_regex(pattern).match(path):
        return True
    # A trailing ``/**`` (e.g. ``**/credentials/**``) is meant to cover the
    # whole subtree — including the directory *node* itself, so a module/vault
    # note that points at that directory is hidden too, not just files under
    # it. Fall back to matching the directory prefix.
    if pattern.endswith("/**"):
        return glob_match(path, pattern[:-3])
    return False


def _any_match(path: str, globs: Iterable[str]) -> bool:
    return any(glob_match(path, g) for g in globs)


# ════════════════════════════════════════════════════════════════════
# Decision object
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _RuleView:
    visibility: str
    allow_globs: tuple[str, ...]
    deny_globs: tuple[str, ...]
    sensitivity_tags: tuple[str, ...]

    def grants_path(self, rel_path: str) -> bool:
        """True if this rule makes the *source file* at ``rel_path`` readable
        (requires ``code`` visibility + passes deny/allow)."""
        if self.visibility != "code":
            return False
        return not self.hides_path(rel_path)

    def hides_path(self, rel_path: str) -> bool:
        """True if this rule explicitly conceals ``rel_path`` — deny-glob match
        or allow-list miss. Independent of the ``code``/``metadata`` gate so it
        can hide a *note* about a sensitive path even at ``metadata`` level."""
        if _any_match(rel_path, self.deny_globs):
            return True
        # An allow-list, when present, is exhaustive: anything outside it hides.
        return bool(self.allow_globs) and not _any_match(rel_path, self.allow_globs)


@dataclass
class RepoAccessDecision:
    """Effective research access for one (user, repo, workspace).

    Combines every rule that applies to the caller's teams. A path is visible
    if *any* applicable rule grants it (teams are additive); ``visibility`` is
    the most permissive across those rules.
    """

    repo_slug: str
    visibility: str = "code"          # merged coarse gate
    rules: tuple[_RuleView, ...] = ()
    open_default: bool = True         # no rule configured → fall open
    allow_globs: tuple[str, ...] = ()  # merged, indicative (for /my + UI)
    deny_globs: tuple[str, ...] = ()   # merged, indicative
    sensitivity_tags: tuple[str, ...] = field(default=())

    # ── coarse gates ────────────────────────────────────────────────
    @property
    def researchable(self) -> bool:
        """Repo may be researched at all (docs and/or code)."""
        return _VIS_RANK.get(self.visibility, 0) >= 1

    @property
    def code_visible(self) -> bool:
        return self.visibility == "code"

    # ── per-path gate ────────────────────────────────────────────────
    def path_visible(self, rel_path: str) -> bool:
        """True if the source file at ``rel_path`` (repo-relative) is
        code-visible to the caller."""
        if self.open_default:
            return True
        if not self.rules:
            return False
        rel = _norm_path(rel_path)
        return any(r.grants_path(rel) for r in self.rules)

    def path_denied(self, rel_path: str) -> bool:
        """True if ``rel_path`` is *explicitly concealed* (deny-glob or
        allow-list miss) under EVERY applicable rule — so a vault note about
        that path is hidden even at ``metadata`` level. Unlike
        ``path_visible`` this does not require ``code`` visibility, so
        ``metadata`` repos still surface their non-sensitive notes."""
        if self.open_default or not self.rules:
            return False
        rel = _norm_path(rel_path)
        return all(r.hides_path(rel) for r in self.rules)

    def to_dict(self) -> dict:
        return {
            "repo_slug": self.repo_slug,
            "visibility": self.visibility,
            "researchable": self.researchable,
            "code_visible": self.code_visible,
            "open_default": self.open_default,
            "allow_globs": list(self.allow_globs),
            "deny_globs": list(self.deny_globs),
            "sensitivity_tags": list(self.sensitivity_tags),
        }

    @classmethod
    def full(cls, repo_slug: str) -> RepoAccessDecision:
        """Unrestricted (admin / fall-open)."""
        return cls(repo_slug=repo_slug, visibility="code", open_default=True)

    @classmethod
    def denied(cls, repo_slug: str) -> RepoAccessDecision:
        """No research access at all."""
        return cls(
            repo_slug=repo_slug, visibility="none", rules=(), open_default=False,
        )


# ════════════════════════════════════════════════════════════════════
# Resolution
# ════════════════════════════════════════════════════════════════════


def _build_decision(repo_slug: str, my_rules: list) -> RepoAccessDecision:
    """Combine a caller's applicable ORM rules into a decision."""
    views: list[_RuleView] = []
    best_vis = "none"
    allow_union: list[str] = []
    deny_union: list[str] = []
    tags_union: list[str] = []
    for r in my_rules:
        vis = r.visibility if r.visibility in _VALID_VISIBILITY else "code"
        if _VIS_RANK[vis] > _VIS_RANK[best_vis]:
            best_vis = vis
        allow = tuple(str(g) for g in (r.allow_globs or []))
        deny = tuple(str(g) for g in (r.deny_globs or []))
        tags = tuple(str(t) for t in (r.sensitivity_tags or []))
        views.append(_RuleView(vis, allow, deny, tags))
        allow_union.extend(allow)
        deny_union.extend(deny)
        tags_union.extend(tags)
    return RepoAccessDecision(
        repo_slug=repo_slug,
        visibility=best_vis,
        rules=tuple(views),
        open_default=False,
        allow_globs=tuple(dict.fromkeys(allow_union)),
        deny_globs=tuple(dict.fromkeys(deny_union)),
        sensitivity_tags=tuple(dict.fromkeys(tags_union)),
    )


def resolve_access_sync(
    session,
    *,
    user_id: str,
    is_admin: bool,
    workspace_id: str,
    repos: Iterable[str],
) -> dict[str, RepoAccessDecision]:
    """Resolve research access for several repos at once, using a **sync**
    SQLAlchemy ``Session``. Returns ``{repo_slug: RepoAccessDecision}``.
    """
    from sqlalchemy import select

    from src.db.models import RepoAccessRule, TeamMember

    repos = list(dict.fromkeys(repos))
    if not repos:
        return {}
    if is_admin:
        return {r: RepoAccessDecision.full(r) for r in repos}

    rules = session.execute(
        select(RepoAccessRule).where(
            RepoAccessRule.workspace_id == workspace_id,
            RepoAccessRule.repo_slug.in_(repos),
        )
    ).scalars().all()

    my_team_ids = {
        tm.team_id
        for tm in session.execute(
            select(TeamMember).where(TeamMember.user_id == user_id)
        ).scalars().all()
    }

    by_repo: dict[str, list] = {}
    for r in rules:
        by_repo.setdefault(r.repo_slug, []).append(r)

    out: dict[str, RepoAccessDecision] = {}
    for repo in repos:
        repo_rules = by_repo.get(repo)
        if not repo_rules:
            from src.deployment import fall_open_allowed
            out[repo] = (
                RepoAccessDecision.full(repo)          # fall-open (single_tenant)
                if fall_open_allowed("access.resolver.no_rule",
                                     detail=f"repo={repo} ws={workspace_id}")
                else RepoAccessDecision.denied(repo)   # multi_tenant
            )
            continue
        mine = [r for r in repo_rules if r.team_id in my_team_ids]
        if not mine:
            out[repo] = RepoAccessDecision.denied(repo)  # default-deny
            continue
        out[repo] = _build_decision(repo, mine)
    return out


# ── sync-engine helper (for callers without a Session: MCP, Q&A) ──────

_ENGINE = None


def _sync_engine():
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine

        from src.db.session import get_database_url

        url = get_database_url().replace(
            "postgresql+asyncpg://", "postgresql+psycopg://"
        )
        _ENGINE = create_engine(url, pool_pre_ping=True, pool_size=5)
    return _ENGINE


def resolve_access(
    *,
    user_id: str,
    is_admin: bool,
    workspace_id: str,
    repos: Iterable[str],
) -> dict[str, RepoAccessDecision]:
    """Convenience wrapper that opens its own sync ``Session`` (for callers
    outside a request scope: MCP tool bodies, Q&A retriever)."""
    from sqlalchemy.orm import Session

    repos = list(repos)
    if not repos:
        return {}
    try:
        with Session(_sync_engine()) as s:
            return resolve_access_sync(
                s,
                user_id=user_id,
                is_admin=is_admin,
                workspace_id=workspace_id,
                repos=repos,
            )
    except Exception as exc:  # noqa: BLE001
        # Fail-CLOSED: this function *is* the access-control boundary, so a
        # transient DB error must not silently grant full research access to
        # every repo. Deny instead — Q&A/MCP degrade to "no access, ask an
        # admin" rather than leaking. (The no-rules fall-open case is handled
        # inside resolve_access_sync without raising, so healthy single-tenant
        # deployments are unaffected.)
        logger.error("resolve_access_failed err=%s — failing CLOSED (deny)", exc)
        return {r: RepoAccessDecision.denied(r) for r in repos}
