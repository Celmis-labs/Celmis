# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Making a share, and giving more of one: the licensed half of repository sharing.

Everything that makes a share SAFER is free and lives in ``src/access/shares.py``
and ``src/api/routers/sharing.py``: enforcing it on every call, narrowing it,
blocking a person, ending it, reading who used it. What needs the licence claim
``sharing`` is only the two acts that give another workspace more of the owner's
code than it had:

* :func:`create_share`, the first grant, and
* :func:`extend_share`, a later one (a longer end, fewer hidden paths, questions
  switched on, more people, a person switched back on).

Without the claim neither is reachable (``src/ee/__init__.py`` mounts the router
only for a licence that grants it), and a licence that lapses leaves every share
that exists running to its own end: nothing here is read by the enforcement.

Both functions STAGE their changes on the caller's session and do not commit, as
the rest of ``shares`` does; the router commits and then writes the audit row.
They raise :class:`ShareError` for a request that cannot be honoured, with a
subclass for the two answers that are not a plain "no" (404 and 409). Nothing the
caller types is trusted: lengths, ranges and the glob list are checked here, not
only by the request model, because a test, a script or another router can call
these directly.

Design rules the code below keeps (see docs/SHARING.md):

* The repository comes from the OWNER's registry only (``get_in_workspace``) and is
  taken whole as it stands: provider and name are copied onto the share, so a slug
  that is later registered again for another repository does not revive it. A
  repository somebody else shared with the owner is not in that registry, so a
  share cannot be passed on (it is not transitive).
* The person making the share must be able to read the repository in full
  themselves. An owner or admin always can; the check is there so a share is never
  the way to hand over what its maker could not open.
* The receiving side is another workspace of the installation, never the owner
  itself and never the built-in ``default`` one (whose records carry no tenant).
* The end is always set: ``days`` from now, at most the token ceiling
  (``CELMIS_MCP_TOKEN_MAX_DAYS``). An extension counts from now as well, so a share
  is never further than the ceiling from the moment somebody last chose its end.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.access import shares as repo_shares
from src.access.shares import ShareError

logger = logging.getLogger(__name__)

#: Most shares a workspace may give that are still running, and most it may receive.
#: A workspace shares a handful; the ceilings are there so a loop cannot fill the table.
MAX_GIVEN = 200
MAX_RECEIVED = 25

NO_SUCH_WORKSPACE = "No such workspace."
NO_SUCH_REPOSITORY = "No such repository."
ALREADY_SHARED = "This repository is already shared with that workspace. Extend it instead."
TOO_MANY = "Too many active shares."
ENDED = "This share has ended. Share the repository again."
CHANGED = "The repository is no longer the one that was shared. Share it again."
NO_DEFAULT = "The built-in default workspace cannot give or receive a share."
OWN_WORKSPACE = "A repository cannot be shared with its own workspace."
NOT_LATER = "That is not later than the current end. Ending a share sooner is narrowing it."
NOTHING = "Nothing to change."


class NotFound(ShareError):
    """The repository or the workspace named is not there (for the person asking)."""

    status_code = 404


class Conflict(ShareError):
    """The request is fine, the state is not: already shared, too many, already ended."""

    status_code = 409


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """A stored time as UTC: SQLite hands back naive datetimes."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Made:
    """A share :func:`create_share` staged, and the ids of the lapsed ones of the
    same repository and pair it ended to make room."""

    share: Any
    replaced: list[str]


def _whole(value: object, name: str, low: int, high: int) -> int:
    """An integer in ``low..high``. A bool is not a number here (``True`` is not 1 day)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShareError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise ShareError(f"{name} is {low} to {high}")
    return value


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ShareError(f"{name} must be true or false")
    return value


# ─── create ──────────────────────────────────────────────────────────


def _registered_repo(session, creator: Any, owner: str, slug: object) -> tuple[str, str, str]:  # noqa: ANN001
    """``(slug, provider, full_name)`` of a repository the OWNER has registered and
    that ``creator`` may read in full; :class:`NotFound` otherwise.

    The same answer for "not there", "belongs to somebody else too" and "you may not
    read it": the person asking is the owner's administrator and learns nothing they
    could not see, and the three are not worth telling apart."""
    text = slug.strip() if isinstance(slug, str) else ""
    if not text or len(text) > 300 or "\x00" in text:
        raise NotFound(NO_SUCH_REPOSITORY)
    from src.access.effective import Principal, effective_access_sync
    from src.api.auto_review import get_auto_review_store

    store = get_auto_review_store()
    cfg = store.get_in_workspace(owner, text)
    # A slug two workspaces registered is nobody's: the graph and the clone on disk
    # are keyed by the slug alone, so the share could not be told from the other's.
    if cfg is None or store.workspace_for_slug(text) != owner:
        raise NotFound(NO_SUCH_REPOSITORY)
    provider = str(cfg.provider or "").strip()
    full_name = str(cfg.full_name or "").strip()
    if not provider or not full_name:
        raise NotFound(NO_SUCH_REPOSITORY)
    decision = effective_access_sync(
        session, Principal(user_id=str(creator.id), is_admin=bool(getattr(creator, "is_admin", False))),
        owner, [text],
    ).get(text)
    if decision is None or not decision.code_visible:
        raise NotFound(NO_SUCH_REPOSITORY)
    return text, provider, full_name


def _receiving_workspace(session, reference: object, owner: str):  # noqa: ANN001, ANN202
    """The workspace named by ``reference`` (its slug or its id, any case), or
    :class:`NotFound`. Ambiguous means unknown: a text that is one workspace's slug
    and another's id picks neither."""
    from sqlalchemy import func, or_, select

    from src.db.models import Workspace

    text = reference.strip().lower() if isinstance(reference, str) else ""
    if not text or len(text) > 200 or "\x00" in text:
        raise NotFound(NO_SUCH_WORKSPACE)
    found = session.execute(
        select(Workspace).where(
            or_(func.lower(Workspace.slug) == text, func.lower(Workspace.id) == text))
    ).scalars().all()
    if len(found) != 1:
        raise NotFound(NO_SUCH_WORKSPACE)
    workspace = found[0]
    if workspace.id == owner:
        raise ShareError(OWN_WORKSPACE)
    if not repo_shares.may_take_part(workspace.id):
        raise ShareError(NO_DEFAULT)
    return workspace


def receiving_workspaces(session, owner_workspace_id: str) -> list[Any]:  # noqa: ANN001
    """The workspaces a share of ``owner_workspace_id`` could be made to: every other
    workspace of the installation except the built-in ``default`` one, by name.

    This tells the caller which workspaces EXIST, so it is only for an installation
    whose workspaces are one organisation (``single_tenant``); the route refuses
    otherwise and the caller types a slug it was told. The same two rules as
    :func:`_receiving_workspace` decide who is left out, so that a workspace this
    lists is never one a share to it would be refused for being the wrong kind."""
    from sqlalchemy import func, select

    from src.db.models import Workspace

    if not repo_shares.may_take_part(owner_workspace_id):
        return []  # the built-in `default` workspace gives no share, so it has no list
    rows = session.execute(
        select(Workspace).order_by(func.lower(Workspace.name), Workspace.id)
    ).scalars().all()
    return [w for w in rows if w.id != owner_workspace_id and repo_shares.may_take_part(w.id)]


def _running(session, column, workspace_id: str, now: datetime) -> int:  # noqa: ANN001
    from sqlalchemy import func, select

    from src.db.models import RepoShare

    return int(session.execute(
        select(func.count()).select_from(RepoShare).where(
            column == workspace_id, RepoShare.revoked_at.is_(None),
            RepoShare.expires_at > now)
    ).scalar() or 0)


def create_share(
    session,  # noqa: ANN001
    *,
    owner_workspace_id: str,
    creator: Any,
    repo_slug: str,
    workspace: str,
    days: int = repo_shares.DEFAULT_DAYS,
    deny_globs: Iterable[object] | None = (),
    max_people: int = repo_shares.DEFAULT_PEOPLE,
    allow_ask: bool = False,
    now: datetime | None = None,
) -> Made:
    """Stage a new share of ``repo_slug`` from ``owner_workspace_id`` to ``workspace``
    and return it (flushed, not committed). ``creator`` is the signed-in user
    (``id`` and ``is_admin`` are read).

    ``allow_ask`` is off unless the owner turns it on: a question over the shared
    repository sends excerpts of the owner's code to the receiving workspace's
    language-model provider and is paid from its budget. The row of a share that
    lapsed without being ended is ended here (reason ``expired``) so a repository
    can be shared again with the same workspace; one that is still running is a
    409 that points at extending it."""
    moment = now or _now()
    if not repo_shares.may_take_part(owner_workspace_id):
        raise ShareError(NO_DEFAULT)
    length = _whole(days, "days", 1, repo_shares.max_share_days())
    people = _whole(max_people, "max_people", repo_shares.MIN_PEOPLE, repo_shares.MAX_PEOPLE)
    asked = _flag(allow_ask, "allow_ask")
    deny = repo_shares.validate_deny_globs(deny_globs)

    slug, provider, full_name = _registered_repo(session, creator, owner_workspace_id, repo_slug)
    receiver = _receiving_workspace(session, workspace, owner_workspace_id)

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from src.db.models import RepoShare

    if _running(session, RepoShare.owner_workspace_id, owner_workspace_id, moment) >= MAX_GIVEN \
            or _running(session, RepoShare.grantee_workspace_id, receiver.id, moment) >= MAX_RECEIVED:
        raise Conflict(TOO_MANY)

    same = session.execute(
        select(RepoShare).where(
            RepoShare.owner_workspace_id == owner_workspace_id,
            RepoShare.grantee_workspace_id == receiver.id,
            RepoShare.repo_slug == slug, RepoShare.revoked_at.is_(None))
    ).scalars().all()
    if any(repo_shares.status_of(r, moment) == "active" for r in same):
        raise Conflict(ALREADY_SHARED)
    # Only lapsed rows are left. Ending them is what makes room for the new row under
    # the "one running share per repository and pair" index.
    replaced = repo_shares.end_rows(same, by="system", reason="expired")
    session.flush()

    row = RepoShare(
        owner_workspace_id=owner_workspace_id, grantee_workspace_id=receiver.id,
        repo_slug=slug, repo_provider=provider, repo_full_name=full_name,
        deny_globs=deny, allow_ask=asked, max_people=people, blocked_user_ids=[],
        created_by=str(creator.id)[:200], created_at=moment,
        expires_at=moment + timedelta(days=length))
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:  # two requests made the same share at once
        raise Conflict(ALREADY_SHARED) from exc
    return Made(row, replaced)


# ─── extend ──────────────────────────────────────────────────────────


def _user_ids(values: Iterable[object] | None) -> list[str]:
    out: list[str] = []
    for raw in values or ():
        if not isinstance(raw, str):
            raise ShareError("not a user id")
        person = raw.strip()
        if not person:
            continue
        if len(person) > 200 or "\x00" in person:
            raise ShareError("not a user id")
        if person not in out:
            out.append(person)
    return out


def extend_share(
    session,  # noqa: ANN001
    share_id: str,
    *,
    days: int | None = None,
    deny_globs: Iterable[object] | None = None,
    allow_ask: bool | None = None,
    max_people: int | None = None,
    unblock: Iterable[object] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Change a running share in place, in the direction the owner chooses, and return
    ``{field: {before, after}}`` of what changed (for the audit); staged, not committed.

    What it takes, each optional and at least one required:

    * ``days``: the new end, counted from now. It must be later than the current end
      (a share is ended sooner by narrowing it, which is free) and at most the token
      ceiling away.
    * ``deny_globs``: the WHOLE list of hidden paths, replacing the share's own list
      (the owner's rules for the repository and the secret floor are not in it and are
      not touched).
    * ``allow_ask``: questions on or off.
    * ``max_people``: the ceiling on people who hold a token; never below the number
      who hold one now.
    * ``unblock``: user ids to switch back on.

    Nothing is changed unless every part is acceptable: a refusal leaves the staged row
    as it was, so a caller that catches it and commits anyway changes nothing. A share
    that has ended (revoked, lapsed, or whose repository went) is :class:`Conflict`: it
    is shared again, which makes a new id and leaves the old tokens dead on purpose. So
    is one whose repository is no longer registered under the name and provider it was
    shared with (the reading refuses it too; extending it would only record a consent
    for a repository the owner never agreed to). Switching a person back on that would
    take the number of holders above ``max_people`` is refused as well. A change that
    changes nothing returns ``{}``."""
    from src.db.models import RepoShare

    row = session.get(RepoShare, share_id) if repo_shares.is_share_id(share_id) else None
    if row is None:
        raise NotFound(repo_shares.NO_SUCH_SHARE)
    moment = now or _now()
    if repo_shares.status_of(row, moment) != "active":
        raise Conflict(ENDED)
    if repo_shares.is_damaged(row):
        raise ShareError("the share is damaged: revoke it")
    if not repo_shares.repo_still_registered(row):
        raise Conflict(CHANGED)
    if days is None and deny_globs is None and allow_ask is None and max_people is None \
            and not list(unblock or ()):
        raise ShareError(NOTHING)

    changes: dict[str, dict[str, Any]] = {}
    before_row = (row.expires_at, row.deny_globs, row.allow_ask, row.blocked_user_ids,
                  row.max_people)
    new_end = None
    if days is not None:
        length = _whole(days, "days", 1, repo_shares.max_share_days())
        new_end = moment + timedelta(days=length)
        if new_end <= _aware(row.expires_at):
            raise ShareError(NOT_LATER)
    new_deny = None if deny_globs is None else repo_shares.validate_deny_globs(deny_globs)
    new_ask = None if allow_ask is None else _flag(allow_ask, "allow_ask")
    new_people = None if max_people is None else _whole(
        max_people, "max_people", repo_shares.MIN_PEOPLE, repo_shares.MAX_PEOPLE)
    free = _user_ids(unblock)

    if new_end is not None:
        changes["expires_at"] = {"before": _aware(row.expires_at), "after": new_end}
        row.expires_at = new_end
    if new_deny is not None:
        before = [str(g) for g in (row.deny_globs or [])]
        if new_deny != before:
            changes["deny_globs"] = {"before": before, "after": new_deny}
            row.deny_globs = new_deny
    if new_ask is not None and new_ask != bool(row.allow_ask):
        changes["allow_ask"] = {"before": bool(row.allow_ask), "after": new_ask}
        row.allow_ask = new_ask
    if free:
        before_ids = [str(u) for u in (row.blocked_user_ids or [])]
        after_ids = [u for u in before_ids if u not in free]
        if after_ids != before_ids:
            changes["blocked_user_ids"] = {"before": before_ids, "after": after_ids}
            row.blocked_user_ids = after_ids
    if new_people is not None and new_people != int(row.max_people or repo_shares.DEFAULT_PEOPLE):
        changes["max_people"] = {
            "before": int(row.max_people or repo_shares.DEFAULT_PEOPLE), "after": new_people}
        row.max_people = new_people

    # Judged on the staged row (the session flushes it before it reads the tokens):
    # people switched back on, and a lower ceiling, are both counted.
    if "max_people" in changes or "blocked_user_ids" in changes:
        holding = len(repo_shares.holders(session, share_id, now=moment))
        if holding > int(row.max_people or repo_shares.DEFAULT_PEOPLE):
            # The read above flushed the staged row: put it back as it was.
            (row.expires_at, row.deny_globs, row.allow_ask, row.blocked_user_ids,
             row.max_people) = before_row
            raise ShareError(
                "fewer places than people who hold a token for it: block some first")
    return changes


__all__ = [
    "ALREADY_SHARED", "CHANGED", "ENDED", "MAX_GIVEN", "MAX_RECEIVED", "NO_DEFAULT",
    "NO_SUCH_REPOSITORY", "NO_SUCH_WORKSPACE", "NOT_LATER", "NOTHING", "OWN_WORKSPACE",
    "TOO_MANY", "Conflict", "Made", "NotFound", "create_share", "extend_share",
]
