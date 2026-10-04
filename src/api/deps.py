"""FastAPI dependencies — auth, current user, etc."""

from __future__ import annotations

import logging

import jwt
from fastapi import Depends, Header, HTTPException, status
from starlette.requests import Request

from src.api.jwt_auth import decode_token
from src.users import User, UserStore, get_user_store

logger = logging.getLogger(__name__)


def get_users() -> UserStore:
    return get_user_store()


def sync_database_url() -> str:
    """The DATABASE_URL for a blocking engine (the sync role lookups below).

    The async URL names an async driver; the blocking lookups need its sync
    sibling. SQLite is mapped too, so the role checks can be exercised over a
    real database file in tests instead of being replaced wholesale.
    """
    from src.db.session import get_database_url

    url = get_database_url()
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url[len("sqlite+aiosqlite://"):]
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def get_current_user(
    authorization: str | None = Header(default=None),
    users: UserStore = Depends(get_users),
) -> User:
    """Decode bearer token + load user. Raises 401 on failure."""
    token = _bearer_token(authorization)
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims",
        )

    user = users.get_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin scope required",
        )
    return user


def require_superadmin(user: User = Depends(get_current_user)) -> User:
    """The env master account only (src/users/roles.py `is_superadmin`).

    NOT every global admin: an OIDC_ADMIN_ROLE admin keeps the platform pages
    (`require_admin`) but cannot hand out owner (nor admin/editor outside a
    workspace they own) or create a shared workspace — those are the master
    account's.
    """
    from src.users.roles import is_superadmin

    if not is_superadmin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin required",
        )
    return user


# ─── Ops read-only access (debugging without a browser session) ──────
#
# Diagnosing production over HTTP needs a credential that is NOT a user
# session: SSH may be unreachable and minting a JWT requires shell access on
# the box. `CELMIS_OPS_TOKEN` unlocks exactly the read-only ops endpoints
# (logs, diag, metrics) via the `X-Ops-Token` header — nothing else.
#
# Deliberate limits: fail-closed when the env var is unset, minimum length so
# a weak token cannot be brute-forced, constant-time comparison, and every
# accepted call is logged (the tail is worth an audit trail of its own).


def require_ops_access(
    request: Request,
    authorization: str | None = Header(default=None),
    users: UserStore = Depends(get_users),
) -> str:
    """Allow either a global-admin session OR a valid ops token.

    Returns a short label of how access was granted (for logging).
    """
    import hmac
    import os

    token = (request.headers.get("x-ops-token") or "").strip()
    expected = os.environ.get("CELMIS_OPS_TOKEN", "").strip()
    if token and expected and len(expected) >= 16 and \
            hmac.compare_digest(token, expected):
        # `client_ip`, not `request.client.host`. The IP fix reached the audit
        # rows and missed the one line that records PRIVILEGED access: every
        # ops-token line read `ip=172.18.0.7`, the Docker bridge address of
        # the reverse proxy. A read of every tenant's credential metadata was
        # attributed to the proxy.
        logger.info("ops_token_access path=%s ip=%s", request.url.path,
                    client_ip(request) or "?")
        return "ops-token"

    # Fall back to the normal admin session.
    try:
        user = get_current_user(authorization=authorization, users=users)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ops access requires an admin session or a valid X-Ops-Token",
        ) from None
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin scope required",
        )
    return f"admin:{user.email}"


# ─── Repo-scoped RBAC (Stage 14) ────────────────────────────────────
#
# Permission ranking:
#     'read'   → can view repo state (list PRs, past reviews)
#     'review' → can trigger a review, edit policy
#     'admin'  → can add/remove repo, delete review history
#
# `is_admin=True` on the user bypasses all checks. If no team grants
# access AND the user is not admin, we DENY (default-closed) — but only
# when at least one grant exists for the repo; otherwise fall open, so a
# fresh workspace with no team assignments still works.
#
# That last clause is a single-tenant convenience and a multi-tenant hole:
# "no grant exists" is every repository until somebody writes one. It is
# therefore gated on the deployment mode (src/deployment.py) rather than
# being true everywhere — single_tenant keeps it, multi_tenant refuses.

_PERM_RANK = {"read": 1, "review": 2, "admin": 3}


def slug_from_pr_ref(pr_ref: str) -> str | None:
    """Extract ``owner/repo`` slug from either a shorthand
    ``provider:owner/repo#42`` or a full ``https://…/PR/42`` URL.

    Returns None if the ref is unparseable — the caller must decide
    whether to reject or fall open. Kept pure-string so unit tests
    don't need any DB.
    """
    if not pr_ref:
        return None
    ref = pr_ref.strip()
    # Shorthand path (fast path): "gitlab:owner/repo#42" or "owner/repo#42"
    if "#" in ref and "://" not in ref:
        head = ref.split("#", 1)[0]
        if ":" in head:
            head = head.split(":", 1)[1]
        return head.strip("/") or None
    # URL path — best-effort: strip domain, cut common vendor suffixes.
    try:
        from urllib.parse import urlparse
        path = urlparse(ref).path.strip("/")
    except Exception:  # noqa: BLE001
        return None
    if not path:
        return None
    for marker in ("/-/merge_requests/", "/pull/", "/pull-requests/", "/merge_requests/"):
        if marker in path:
            path = path.split(marker, 1)[0]
            break
    # THE WHOLE PROJECT PATH, not the first two segments. The marker split
    # above already left exactly the project — for a GitLab subgroup that is
    # "acme/backend/payments", and cutting it to "acme/backend" named a
    # DIFFERENT repository, which the permission check then answered about.
    # The shorthand branch above never truncated, so one repository produced
    # two different keys depending on which form of the ref was used.
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts)
    return None


def repo_grant_candidates(value: str, workspace_id: str | None) -> list[str]:
    """Every spelling this repository's grants may be stored under.

    The table is written by hand: PUT /teams/{id}/repos/{repo_slug:path} stores
    whatever it is given, and nothing normalises. The UI's own input placeholder
    says "owner/repo" while every path-param route carries the indexed slug
    "{provider}_{owner}-{name}" — so one enforcement family or the other was
    always blind to a grant, and which one depended on how the admin typed it.
    Both spellings exist in production side by side; that was confirmed against
    the deployed instance, two rows for one repository.

    Which is correct cannot be decided from the string. A bare "owner/name"
    does not say which provider it is — `parse_repo_url` defaults it to
    Bitbucket — and "owner-name" cannot be un-flattened at all. So the mapping
    comes from the workspace's own registered repositories, which hold both
    spellings, and the lookup accepts either. Rows written before this keep
    working; nothing has to be migrated under a running install.
    """
    seen = [value]
    if not workspace_id:
        return seen
    try:
        from src.api.auto_review import get_auto_review_store

        for cfg in get_auto_review_store().list_for_workspace(workspace_id):
            if value in (cfg.repo_slug, cfg.full_name):
                for other in (cfg.repo_slug, cfg.full_name):
                    if other and other not in seen:
                        seen.append(other)
                break
    except Exception:  # noqa: BLE001 — an unreadable registry narrows the
        # lookup to the literal spelling; it must not deny the request outright.
        logger.debug("repo_grant_candidates_unavailable repo=%r", value)
    return seen


async def _effective_repo_permission(
    repo_slug: str, user: User, workspace_id: str | None = None,
) -> tuple[str | None, bool]:
    """Returns (permission, any_grants_exist). `permission` is None if the
    user has no grant. `any_grants_exist` = True if at least one team is
    granted on this repo (used to decide fall-open vs default-deny).
    """
    if user.is_admin:
        return "admin", True
    from sqlalchemy import select

    from src.db.models import RepoTeamAccess, Team, TeamMember
    from src.db.session import get_async_session

    async for s in get_async_session():
        candidates = repo_grant_candidates(repo_slug, workspace_id)
        stmt = select(RepoTeamAccess).where(RepoTeamAccess.repo_slug.in_(candidates))
        if workspace_id:
            # Only THIS workspace's teams. `repo_team_access` is keyed by slug
            # alone, and two tenants can register the same repository: without
            # this a grant written in workspace B both counted as "grants
            # exist" in A (denying A's members) and, through a team of B the
            # caller sits in, granted them A's copy of the repo.
            stmt = stmt.join(Team, Team.id == RepoTeamAccess.team_id).where(
                Team.workspace_id == workspace_id)
        all_grants = (await s.scalars(stmt)).all()
        if not all_grants:
            return None, False
        team_ids = {g.team_id for g in all_grants}
        my_teams = (await s.scalars(
            select(TeamMember).where(
                TeamMember.user_id == user.id,
                TeamMember.team_id.in_(team_ids),
            )
        )).all()
        my_team_ids = {m.team_id for m in my_teams}
        best: str | None = None
        best_rank = 0
        for g in all_grants:
            if g.team_id in my_team_ids:
                r = _PERM_RANK.get(g.permission, 0)
                if r > best_rank:
                    best_rank = r
                    best = g.permission
        return best, True
    return None, False


async def enforce_repo_permission(
    repo_slug: str,
    user: User,
    min_perm: str = "read",
    workspace_id: str | None = None,
) -> None:
    """Runtime version of the dep — for handlers that don't get the slug
    from URL path (e.g. it's in body or derived from `pr_ref`).
    Same fall-open + rank rules as `require_repo_permission()`.
    """
    if min_perm not in _PERM_RANK:
        raise ValueError(f"unknown perm {min_perm!r}")
    perm, any_grants = await _effective_repo_permission(
        repo_slug, user, workspace_id)
    if not any_grants:
        from src.deployment import fall_open_allowed
        if fall_open_allowed("api.deps.repo_permission",
                             detail=f"repo={repo_slug} user={user.id}"):
            return  # fall-open (single_tenant)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"No team is granted access to {repo_slug}",
        )
    if perm is None or _PERM_RANK.get(perm, 0) < _PERM_RANK[min_perm]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{min_perm}' on {repo_slug}",
        )


async def current_workspace_id(
    request: Request,
    user: User = Depends(get_current_user),
) -> str:
    """Resolve caller's active workspace_id.

    Priority: `X-Workspace: <slug>` header → any workspace membership
    (best-rank) → 'default'. Global admins may pick any workspace via
    header even without membership; regular users are silently pinned to
    the resolved id.
    """
    header_slug = (request.headers.get("x-workspace") or "").strip().lower()
    try:
        # Cookie fallback so browser flows without SSE header injection work.
        cookie = request.cookies.get("x-workspace")
        if not header_slug and cookie:
            header_slug = cookie.strip().lower()
    except Exception:  # noqa: BLE001
        pass

    from sqlalchemy import create_engine
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _Session

    from src.db.models import Workspace, WorkspaceMember
    eng = create_engine(sync_database_url(), pool_pre_ping=True)
    try:
        with _Session(eng) as s:
            if header_slug:
                ws = s.execute(
                    _select(Workspace).where(Workspace.slug == header_slug)
                ).scalar_one_or_none()
                if ws:
                    m = s.get(WorkspaceMember, (ws.id, user.id))
                    if m or user.is_admin:
                        return ws.id
            memberships = s.execute(
                _select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
            ).scalars().all()
            if memberships:
                from src.users.roles import role_rank
                memberships.sort(key=lambda m: -role_rank(m.role))
                return memberships[0].workspace_id
            # No membership yet — provision this user's personal workspace so
            # their keys/repos land in an isolated tenant (safety net for users
            # created before signup/login provisioning existed). Falls back to
            # the shared "default" tenant only if provisioning fails.
            try:
                from src.api.workspace_provision import ensure_personal_workspace
                return ensure_personal_workspace(s, user.id, user.email, user.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("workspace_autoprovision_failed user=%s err=%s", user.id, exc)
                from src.deployment import fall_open_allowed
                if fall_open_allowed("api.deps.workspace_provision",
                                     detail=f"user={user.id}"):
                    return "default"
                # multi_tenant: "default" is not an attribution. Landing a
                # user there would put their keys and repos in whichever
                # tenant happens to own the seeded workspace.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not resolve your workspace — try again, or "
                           "ask an admin to add you to one.",
                ) from exc
    finally:
        eng.dispose()


def is_workspace_admin(user: User, workspace_id: str) -> bool:
    """Owner/admin of `workspace_id`, or a global admin. The one copy of the rule
    behind :func:`require_workspace_admin`, for handlers that only need to
    decide what to SHOW (e.g. the LiteLLM proxy host on GET /api/llm/config).
    """
    if user.is_admin:
        return True

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session

    from src.db.models import WorkspaceMember
    eng = create_engine(sync_database_url(), pool_pre_ping=True)
    try:
        with _Session(eng) as s:
            from src.users.roles import WORKSPACE_ADMIN_ROLES

            m = s.get(WorkspaceMember, (workspace_id, user.id))
            return m is not None and m.role in WORKSPACE_ADMIN_ROLES
    finally:
        eng.dispose()


async def require_workspace_admin(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> User:
    """Caller must be owner/admin of their ACTIVE workspace (or a global admin).

    This replaces the global `require_admin` on workspace-scoped mutations
    (LLM keys, git connections): every user is the admin of their OWN workspace,
    so they can configure it without a platform admin — but they can never
    mutate a workspace they don't own/administer.
    """
    if is_workspace_admin(user, workspace_id):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Requires owner/admin on this workspace",
    )


#: Workspace roles that may read review analytics: the people who answer for
#: the code and its cost. "editor" is spelled out by name — the role ladder is
#: viewer < member < editor < admin < owner.
ANALYTICS_ROLES = frozenset({"owner", "admin", "editor"})

#: Roles that may change a review issue's status (member and above).
ISSUE_WRITE_ROLES = frozenset({"member", "editor", "admin", "owner"})


def workspace_role(user_id: str, workspace_id: str) -> str | None:
    """The caller's role in `workspace_id`, or None when not a member.

    Blocking — call it from a thread or a sync dependency.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session

    from src.db.models import WorkspaceMember
    eng = create_engine(sync_database_url(), pool_pre_ping=True)
    try:
        with _Session(eng) as s:
            m = s.get(WorkspaceMember, (workspace_id, user_id))
            return m.role if m is not None else None
    finally:
        eng.dispose()


async def require_analytics_access(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> User:
    """Global admin, or owner/admin/editor of the ACTIVE workspace.

    Members and viewers get 403: the analytics page reports cost and how
    often a team merged what the reviewer flagged, which is a lead's view.
    """
    if user.is_admin:
        return user
    import asyncio

    role = await asyncio.to_thread(workspace_role, user.id, workspace_id)
    if role in ANALYTICS_ROLES:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Analytics requires owner, admin or editor on this workspace",
    )


async def require_prompt_editor(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> User:
    """Editor, admin or owner of the ACTIVE workspace (or a global admin).

    The gate for what an editor is FOR: agent system prompts and per-repo
    review policies. It is deliberately not `require_workspace_admin` — an
    editor holds none of the admin powers (members, invites, teams, keys,
    connections) — and not open to members and viewers either.
    """
    if user.is_admin:
        return user
    import asyncio

    from src.users.roles import PROMPT_EDITOR_ROLES

    role = await asyncio.to_thread(workspace_role, user.id, workspace_id)
    if role in PROMPT_EDITOR_ROLES:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Editing prompts requires editor, admin or owner on this workspace",
    )


def _require_repo_in_workspace(slug: str, workspace_id: str | None) -> None:
    """404 unless ``slug`` is a repository registered to ``workspace_id``.

    multi_tenant only. Team grants are looked up by slug across every
    workspace, and a global admin skips them entirely — neither says whose
    repository it is. Ownership snapshots, architecture summaries and the
    vault reverse index are all keyed by slug alone, so without this a
    `/api/intel/*/{repo_slug}` call answered for any tenant's repository the
    caller could spell. Unknown and foreign read the same: 404.

    Accepts either spelling a grant may be stored under (indexed slug or
    owner/name, see :func:`repo_grant_candidates`).
    """
    from src.deployment import is_multi_tenant

    if not is_multi_tenant():
        return
    owned = False
    if workspace_id:
        try:
            from src.api.auto_review import get_auto_review_store

            owned = any(
                slug in (cfg.repo_slug, cfg.full_name)
                for cfg in get_auto_review_store().list_for_workspace(workspace_id)
            )
        except Exception as exc:  # noqa: BLE001 — unreadable registry: refuse
            logger.warning("repo_workspace_binding_unreadable repo=%r err=%s",
                           slug, exc)
    if not owned:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Repo not registered")


def require_repo_permission(min_perm: str = "read"):
    """FastAPI dependency factory. Reads path parameter `repo_slug` (or
    `slug`) from the request and enforces the caller has at least
    ``min_perm``. Default-open when no grants configured for the repo.
    """
    if min_perm not in _PERM_RANK:
        raise ValueError(f"unknown perm {min_perm!r}")
    needed = _PERM_RANK[min_perm]

    async def _dep(
        request: Request,
        user: User = Depends(get_current_user),
        workspace_id: str = Depends(current_workspace_id),
    ) -> User:
        slug = (
            request.path_params.get("repo_slug")
            or request.path_params.get("slug")
        )
        if not slug:
            # No slug in path — nothing to guard, just require auth.
            return user
        _require_repo_in_workspace(slug, workspace_id)
        perm, any_grants = await _effective_repo_permission(
            slug, user, workspace_id)
        if not any_grants:
            from src.deployment import fall_open_allowed
            if fall_open_allowed("api.deps.repo_permission",
                                 detail=f"repo={slug} user={user.id}"):
                return user  # fall-open (single_tenant)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No team is granted access to {slug}",
            )
        if perm is None or _PERM_RANK.get(perm, 0) < needed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires '{min_perm}' on {slug}",
            )
        return user

    return _dep


def client_ip(request) -> str | None:
    """The caller's address, through the reverse proxy.

    The only IP recorded anywhere on this deployment was `172.18.0.7` — the
    Docker bridge address of the proxy — because nothing consulted
    `X-Forwarded-For`. Every "who, from where" question was therefore
    unanswerable, and an audit trail that records the proxy is an audit trail
    that records nothing.

    The LEFTMOST entry is the client. It is also the one a client can forge,
    which is why this is evidence and not authentication: it is written beside
    an already-authenticated actor id, never used to decide anything.
    """
    if request is None:
        return None
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            first = fwd.split(",")[0].strip()
            if first:
                return first[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
        client = getattr(request, "client", None)
        return getattr(client, "host", None)
    except Exception:  # noqa: BLE001
        return None
