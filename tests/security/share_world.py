"""Shares for the access tests: workspace B gives workspace A one repository.

Not a test module (no ``test_`` prefix). It rides on `mcp_world`: A is the
receiving workspace (G in the spec), B the owner (O), ``SHARED`` is B's repository
and ``FOREIGN`` is B's other one, which nothing shares.

Every helper writes rows the way the code under test reads them, and nothing
else: a share is a ``repo_shares`` row, a holder is an ``mcp_tokens`` row whose
``shared_repos`` names it. The routes that would create these rows belong to a
later slice, so the tests make them here, and tamper with them here (a row edited
behind the service's back is exactly the case the checks on every call exist for).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from tests.security.mcp_world import SHARED, WS_A, WS_B, McpWorld, _commit

#: A person of workspace A who is in no team and has no rule: whatever they read
#: through a token is what the token (and a share on it) gives, nothing else.
HOLDER = "mn"


def _full_name(slug: str) -> tuple[str, str]:
    """(provider, full name) the registry holds for a slug of the fixture."""
    from src.api.auto_review import get_auto_review_store

    for ws in (WS_B, WS_A):
        cfg = get_auto_review_store().get_in_workspace(ws, slug)
        if cfg is not None:
            return cfg.provider, cfg.full_name
    return "github", slug.split("_", 1)[1].replace("-", "/", 1)


def make_share(
    world: McpWorld,
    *,
    slug: str = SHARED,
    owner: str = WS_B,
    grantee: str = WS_A,
    deny_globs: Iterable[str] = (),
    allow_ask: bool = False,
    max_people: int = 5,
    blocked: Iterable[str] = (),
    days: float | None = 30,
    expires_at: datetime | None = None,
    provider: str | None = None,
    full_name: str | None = None,
) -> str:
    """Insert a live share and return its id. ``expires_at`` wins over ``days``;
    ``provider`` and ``full_name`` default to what the owner's registry holds."""
    from src.db.models import RepoShare

    reg_provider, reg_name = _full_name(slug)
    share_id = str(uuid.uuid4())
    end = expires_at or datetime.now(UTC) + timedelta(days=days if days is not None else 30)
    with world.session() as s:
        s.add(RepoShare(
            id=share_id, owner_workspace_id=owner, grantee_workspace_id=grantee,
            repo_slug=slug, repo_provider=provider or reg_provider,
            repo_full_name=full_name or reg_name, deny_globs=list(deny_globs),
            allow_ask=allow_ask, max_people=max_people, blocked_user_ids=list(blocked),
            created_by=world.users["su"], expires_at=end,
        ))
        s.commit()
    return share_id


def change_share(world: McpWorld, share_id: str, **fields) -> None:
    """Edit the share row directly, as a database operator (or a bug) could."""
    from src.db.models import RepoShare

    with world.session() as s:
        row = s.get(RepoShare, share_id)
        for key, value in fields.items():
            setattr(row, key, value)
        s.commit()


def end_share(world: McpWorld, share_id: str, *, reason: str = "owner") -> None:
    """Revoke the share the way the owner would: row only. The cache of the token
    that carries it is deliberately NOT cleared; that is what the tests probe."""
    change_share(world, share_id, revoked_at=datetime.now(UTC), revoked_by=world.users["su"],
                 revoke_reason=reason)


def share_row(world: McpWorld, share_id: str):  # noqa: ANN201
    from src.db.models import RepoShare

    with world.session() as s:
        return s.get(RepoShare, share_id)


def holder_token(
    world: McpWorld,
    shared: Iterable[str],
    *,
    own: Iterable[str] = (),
    who: str = HOLDER,
    kind: str = "pat",
    profile: str = "dev",
    write: bool = False,
    workspace: str = WS_A,
    days: int | None = 30,
):
    """A real token of ``who`` that carries ``shared`` beside its own repo list.
    Returns ``(bearer, view)``; made through the same ``create_row`` the issuing
    code will use, so a combination the service refuses cannot be built here."""
    from src.mcp_server import token_store

    return token_store.mint(
        kind=kind, workspace_id=workspace, user_id=world.users[who],
        issued_by=world.users["su"], label="test", patterns=list(own),
        allow_write=write, profile=profile, expires_in_days=days, shared=list(shared),
    )


def tamper_token(world: McpWorld, token_id: str, **fields) -> None:
    """Write columns of a token row directly, past ``create_row``'s validation, and
    drop the cached copy. Used to build a token no code of ours would issue: a
    ``cli`` token with a share on it, a ``full`` one, one with write."""
    from src.db.models import McpToken
    from src.mcp_server import token_store

    with world.session() as s:
        row = s.get(McpToken, token_id)
        for key, value in fields.items():
            setattr(row, key, value)
        s.commit()
    token_store.invalidate(token_id)


def register_in(world: McpWorld, workspace: str, slug: str, *, full_name: str | None = None,
                provider: str = "github") -> None:
    """Register ``slug`` to ``workspace`` in the repository registry (a second
    registration of one slug is what the tenant binding exists to refuse)."""
    from src.api.auto_review import RepoConfig, get_auto_review_store

    name = full_name or slug.split("_", 1)[1].replace("-", "/", 1)
    get_auto_review_store().upsert(RepoConfig(
        user_id=f"{workspace}-registrar", repo_slug=slug, provider=provider,
        full_name=name, url=f"https://github.com/{name}", workspace_id=workspace))


def unregister(workspace: str, slug: str) -> None:
    from src.api.auto_review import get_auto_review_store

    get_auto_review_store().delete_in_workspace(workspace, slug)


def real_clones(root, slugs: Iterable[str], files: dict[str, str]) -> None:
    """Turn the marker directories of ``slugs`` under ``root`` (the module's
    ``mcp_graphs``) into real git clones holding ``files``: the dev tools that read
    text (``grep``, ``refs``) run ``git grep`` and a marker has nothing to search."""
    from src.config import Settings

    repos = Settings(workspace_dir=root).repos_dir
    for slug in slugs:
        (repos / slug / ".git").mkdir(parents=True, exist_ok=True)
        _commit(repos / slug, files)
