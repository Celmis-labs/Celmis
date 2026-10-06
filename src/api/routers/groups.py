"""Repository groups over HTTP (Stage 24).

WHY THIS FILE EXISTS. Cross-repo drift — the deterministic grep that catches a
constant changed in one repository and left behind in its siblings — only runs
when a repository belongs to a GROUP with at least one other member. Groups
were YAML files written by `analyzer group create` on the server, and an audit
of the deployed product found no HTTP route that could make one: 184 paths in
`openapi.json`, not one containing "group".

So a workspace set up entirely over the web — the normal way to use the
product — had no groups, could never have one, and the whole feature was
unreachable. Worse than absent: the review still ran, saw no group, and the
model rendered that silence as a completed cross-repo search that found
nothing.

TENANCY IS NEW HERE AND IT IS THE POINT. Groups were installation-global,
which was harmless while only a shell could create them. Over HTTP it is not:
drift GREPS every sibling in a group and quotes what it finds into a review
comment, so a group naming another tenant's repository would read their source
and publish it. Every route below is scoped to the caller's workspace, and a
group may only name repositories registered in that same workspace.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import (
    current_workspace_id,
    enforce_repo_permission,
    get_current_user,
    require_prompt_editor,
)
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repos/groups", tags=["groups"])


class GroupOut(BaseModel):
    name: str
    description: str = ""
    repos: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    repos: list[str] = Field(default_factory=list, max_length=100)
    # Every sibling payload in this API forbids extras; this one did not, so
    # `{"repo_slugs": [...]}` — the wrong field name, and an easy one to
    # reach for — returned 201 and an EMPTY group with no complaint.
    model_config = ConfigDict(extra="forbid")


class GroupRepos(BaseModel):
    repos: list[str] = Field(min_length=1, max_length=100)


def _manager():
    from src.groups import get_group_manager
    return get_group_manager()


def _registered(workspace_id: str) -> dict[str, str]:
    """slug → the identifier a group should store for it.

    `provider:owner/name`, not the bare slug and not the bare full name, and
    both alternatives are wrong in a way that fails differently:

      * the bare slug (`github_owner-name`) is what the first version stored,
        and `RepoGroup.add_repo` parses what it is given — `parse_repo_url`
        raises "slug must have at least owner/name (got 1 segments)" on it, so
        every non-empty create returned HTTP 500;
      * the bare full name (`owner/name`) parses, but `parse_repo_url` defaults
        to the BITBUCKET provider, yielding `owner-name` — which never equals
        the `github_owner-name` the review carries, so `_find_group_for_repo`
        would return None and drift would stay dead with no error anywhere.
        Silent is worse than 500.

    The prefixed form round-trips: `parse_repo_url("github:owner/name").slug`
    is exactly the registered slug.
    """
    from src.api.auto_review import get_auto_review_store
    return {
        c.repo_slug: f"{c.provider}:{c.full_name}"
        for c in get_auto_review_store().list_for_workspace(workspace_id)
    }


def _readable_slugs(user: User, workspace_id: str, slugs: list[str]) -> set[str]:
    """Which of `slugs` this person may read the code of (the MCP resolver's
    reading, owners and admins included)."""
    from src.access.effective import Principal, effective_access

    decisions = effective_access(
        Principal(user.id, is_admin=bool(getattr(user, "is_admin", False))), workspace_id,
        slugs)
    return {s for s, d in decisions.items() if d.code_visible}


def _member_slug(ident: str) -> str | None:
    """The registry slug a stored group member (`provider:owner/name`) stands for."""
    from src.sync.git_providers import parse_repo_url

    try:
        return parse_repo_url(ident).slug
    except Exception:  # noqa: BLE001
        return None


def _visible_group(group, user: User, workspace_id: str) -> GroupOut | None:
    """The group as this person may see it: only the members whose code they
    may read, and not at all when they may read none (an empty group too, to
    anybody but those who manage the workspace's repositories).

    A group is a list of repository identifiers; naming one the caller cannot
    read tells them it exists and what it is called."""
    slugs = {m: _member_slug(m) for m in group.repos}
    readable = _readable_slugs(
        user, workspace_id, sorted({s for s in slugs.values() if s}))
    shown = [m for m in group.repos if slugs[m] in readable]
    if not shown and not _is_manager(user, workspace_id):
        return None
    out = _out(group)
    out.repos = shown
    return out


def _is_manager(user: User, workspace_id: str) -> bool:
    from src.api.deps import is_workspace_admin

    return is_workspace_admin(user, workspace_id)


async def _require_review_on(
    idents: list[str], user: User, workspace_id: str, *, unreadable_is_refused: bool,
) -> None:
    """A change to a group is a change to what drift reads and quotes for each
    of its repositories: the caller needs ``review`` on every one of them
    (owners and admins always have it).

    ``unreadable_is_refused`` is for the members a group ALREADY holds: one the
    caller may not read at all is refused here (a group they cannot see is
    answered like one that does not exist). For identifiers the caller is only
    ADDING it is False: those are refused by `_resolve_in_workspace`, with the
    text of an unregistered repository, so the answer is no existence oracle."""
    for ident in idents:
        slug = _member_slug(_canonical(ident, workspace_id))
        if slug is None:
            continue
        try:
            await enforce_repo_permission(slug, user, "review", workspace_id)
        except HTTPException as exc:
            if exc.status_code == 404:  # not visible: it does not exist for them
                if unreadable_is_refused:
                    raise HTTPException(status_code=404, detail="group not found") from None
                continue
            raise HTTPException(
                status_code=403,
                detail="Changing a group needs review access on every repository in it",
            ) from None


async def _guard_new_group(
    payload: GroupCreate,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    await _require_review_on(payload.repos, user, workspace_id, unreadable_is_refused=False)


async def _guard_group(
    name: str,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    group = _load_owned(name, workspace_id)
    await _require_review_on(list(group.repos), user, workspace_id, unreadable_is_refused=True)


async def _guard_group_and_additions(
    name: str,
    payload: GroupRepos,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    await _guard_group(name, user, workspace_id)
    await _require_review_on(payload.repos, user, workspace_id, unreadable_is_refused=False)


def _canonical(ident: str, workspace_id: str) -> str:
    """The stored form (`provider:owner/name`) of a repository the caller named
    by registry slug or by bare `owner/name`; anything else is returned as is."""
    registered = _registered(workspace_id)
    if ident in registered:
        return registered[ident]
    same = [v for v in registered.values() if v.split(":", 1)[-1] == ident]
    return same[0] if len(same) == 1 else ident


def _resolve_in_workspace(
    identifiers: list[str], workspace_id: str, user: User | None = None,
) -> list[str]:
    """Every identifier, checked against this workspace's registry.

    A group is a grep target. Accepting an identifier that resolves to a
    repository this workspace does not own would make drift read a stranger's
    source and quote it into a review comment, which is the one thing this
    router must not allow.

    The same goes for a repository that IS registered here but that the caller
    may not read: a group of someone else's repositories is a way to be quoted
    their code. With ``user`` given, such an identifier is answered exactly like
    an unregistered one (422, same text), so it is no existence oracle either.
    """
    from src.sync.git_providers import parse_repo_url

    known = _registered(workspace_id)
    readable: set[str] | None = None
    if user is not None:
        readable = _readable_slugs(user, workspace_id, list(known))
    out: list[str] = []
    for ident in identifiers:
        slug = None
        if ident in known:
            slug = ident
        else:
            try:
                slug = parse_repo_url(ident).slug
            except Exception:  # noqa: BLE001
                slug = None
        if slug is None or slug not in known or (
                readable is not None and slug not in readable):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{ident!r} is not a repository registered in this "
                    "workspace — register it first, then add it to a group"
                ),
            )
        out.append(known[slug])
    return out


def _out(group) -> GroupOut:
    return GroupOut(
        name=group.name, description=group.description,
        repos=list(group.repos), created_at=group.created_at,
        updated_at=group.updated_at,
    )


def _load_owned(name: str, workspace_id: str):
    """The group, or 404. A group in another workspace is 404, not 403 — the
    caller must not learn that the name is taken elsewhere."""
    from src.groups.manager import GroupNotFoundError

    try:
        group = _manager().load(name, workspace_id)
    except GroupNotFoundError:
        raise HTTPException(status_code=404, detail="group not found") from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="group not found") from exc
    if group.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="group not found")
    return group


@router.get("", response_model=list[GroupOut])
def list_groups(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[GroupOut]:
    mgr = _manager()
    out = []
    for name in mgr.list(workspace_id):
        try:
            shown = _visible_group(mgr.load(name, workspace_id), user, workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("group_unreadable name=%s err=%s", name, exc)
            continue
        if shown is not None:
            out.append(shown)
    return out


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_guard_new_group)])
def create_group(
    payload: GroupCreate,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> GroupOut:
    from src.groups.manager import GroupValidationError

    mgr = _manager()
    identifiers = _resolve_in_workspace(payload.repos, workspace_id, user)
    try:
        group = mgr.create(payload.name, payload.description,
                           workspace_id=workspace_id)
    except GroupValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # From here the file EXISTS, so anything that raises leaves it behind.
    #
    # It did. `mgr.create` writes before `workspace_id` is set, so a failing
    # `add_repo` left a group owned by "default" — invisible to this tenant's
    # list, 404 to its delete, and 422 "already exists" to a retry. The name
    # was permanently squatted and unreachable over HTTP, and since every
    # non-empty create hit the parse bug above, every realistic create leaked
    # one.
    try:
        group.workspace_id = workspace_id
        for ident in identifiers:
            group.add_repo(ident)
        mgr.save(group)
    except Exception:
        with contextlib.suppress(Exception):
            mgr.delete(payload.name, workspace_id)
        raise
    logger.info("group_created_via_api name=%s ws=%s repos=%d by=%s",
                group.name, workspace_id, len(group.repos), user.email)
    return _out(group)


@router.post("/{name}/repos", response_model=GroupOut,
             dependencies=[Depends(_guard_group_and_additions)])
def add_repos(
    name: str,
    payload: GroupRepos,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> GroupOut:
    group = _load_owned(name, workspace_id)
    for ident in _resolve_in_workspace(payload.repos, workspace_id, user):
        group.add_repo(ident)
    _manager().save(group)
    logger.info("group_repos_added name=%s ws=%s total=%d by=%s",
                name, workspace_id, len(group.repos), user.email)
    return _out(group)


@router.delete("/{name}/repos", response_model=GroupOut,
               dependencies=[Depends(_guard_group)])
def remove_repos(
    name: str,
    payload: GroupRepos,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> GroupOut:
    group = _load_owned(name, workspace_id)
    for ident in payload.repos:
        group.remove_repo(_canonical(ident, workspace_id))
    _manager().save(group)
    logger.info("group_repos_removed name=%s ws=%s total=%d by=%s",
                name, workspace_id, len(group.repos), user.email)
    return _out(group)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(_guard_group)])
def delete_group(
    name: str,
    user: User = Depends(require_prompt_editor),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    _load_owned(name, workspace_id)
    _manager().delete(name, workspace_id)
    logger.info("group_deleted name=%s ws=%s by=%s", name, workspace_id, user.email)
