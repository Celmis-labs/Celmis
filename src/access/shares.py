"""Reading a repository another workspace has shared: the conditions, and the bookkeeping.

A *share* (``repo_shares``) is one workspace's consent that ONE other workspace may
read one of its repositories, for research, for a while. It is not a grant. A person
in the receiving workspace reads the repository only when the superadmin has named
the share on that person's MCP token (``mcp_tokens.shared_repos``) and every
condition below still holds ON THE CALL, read from the database each time (the
30 s token cache holds the token's list of ids and nothing else):

  C1  the token is a ``pat`` of the ``dev`` profile that cannot write, alive, and
      its holder is still a member of the receiving workspace (decided by the
      identity resolver, which hands this module the ids only for such a token);
  C2  the share's id is on the token;
  C3  the share is for the token's workspace, and neither side is the built-in
      ``default`` workspace (:func:`_is_a_side`);
  C4  the share is not revoked and has not expired;
  C5  the repository is still registered to the owner under the same provider and
      name as when it was shared, to nobody else, and not to the receiver too;
  C6  the owner has not switched this person off (a list of people, or of hidden
      paths, that cannot be read as a list of strings refuses the share:
      :func:`_string_list`);
  C7  the path is not hidden (the owner's rules for the repository, the share's
      own list, and the secret floor — see :class:`_SharedRule`);
  C8  for ``ask`` only: the owner allowed it (``ask_ok`` on the decision).

Anything missing, unreadable or odd is "no access": the module fails closed, and
a refused share is indistinguishable from a repository that does not exist.

Why the ids live beside the repo list and not in it: ``repo_patterns`` is matched
with globs, and a glob that could be made to cover a shared repository is exactly
the bug this design rules out. Nothing here is matched against a pattern.

This module is AGPL on purpose. Creating or extending a share is the licensed part
(``src/ee/sharing``); enforcing, narrowing, revoking and reading usage are not, so
that a build without the licence is never less safe than one with it. Nothing here
imports ``src.ee``.

Every function that changes rows STAGES the change and does not commit: the caller
changes a share in the same transaction as the thing that made it necessary
(removing a repository, deleting a workspace), as ``token_store.revoke_held`` does.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard

from src.access.resolver import RepoAccessDecision, _RuleView

logger = logging.getLogger(__name__)

#: A share id: the lower-case uuid4 the table generates. Checked wherever one comes
#: from outside, and before one reaches a ``LIKE``.
SHARE_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

MIN_PEOPLE, MAX_PEOPLE, DEFAULT_PEOPLE = 1, 25, 5
#: A share lasts this long unless the owner says otherwise (never more than the
#: token ceiling, :func:`max_share_days`).
DEFAULT_DAYS = 30
MAX_DENY_GLOBS = 100
MAX_GLOB_LENGTH = 200
MAX_BLOCKED = 50
#: Shares one token may carry.
MAX_SHARES_PER_TOKEN = 10
#: Repositories ``refs`` looks through for the other side of a shared repository.
MAX_LINKED = 20

#: Why a share ended. ``expired`` is written when a lapsed share is replaced.
REASONS = ("owner", "superadmin", "repo_removed", "workspace_deleted", "expired")

#: Said to whoever asks for more through the narrowing route. The licensed route is
#: named because the sentence is also read by an installation that HAS the licence
#: (the narrowing route never widens, whoever asks), which would otherwise be told it
#: needs one.
WIDENING = (
    "widening a share needs a Celmis Enterprise licence (sharing); "
    "with one, use POST /api/sharing-grants/{id}/extend")

#: The one answer for every share that cannot be put on a token: unknown, malformed,
#: revoked, expired, for another workspace, or switched off for this person. Which
#: of them it is would tell a caller what exists in somebody else's workspace.
NO_SUCH_SHARE = "No such share."


class ShareError(ValueError):
    """A request about a share that cannot be honoured."""


class WideningError(ShareError):
    """The request would give more than the share gives now. Narrowing, blocking
    and revoking are free; widening is the licensed part."""

    def __init__(self) -> None:
        super().__init__(WIDENING)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_share_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and SHARE_ID_RE.fullmatch(value) is not None


def clean_ids(values: Iterable[object] | None) -> list[str]:
    """The well-formed share ids of ``values``, once each, in order. A list that
    is not a list of ids (a corrupt column) yields nothing."""
    if values is None or isinstance(values, str | bytes):
        return []
    try:
        return list(dict.fromkeys(v for v in values if is_share_id(v)))
    except TypeError:
        return []


def max_share_days() -> int:
    """The longest a share may last: the ceiling a token has (``CELMIS_MCP_TOKEN_MAX_DAYS``)."""
    from src.mcp_server.token_store import max_days

    return max_days()


def anchor_glob(glob: str) -> str:
    """A deny pattern in the form :func:`src.access.resolver.glob_match` reads as meant.

    Every pattern is relative to the root of the repository, but the matcher compares
    it with paths that have no leading ``/`` or ``./``, so ``/secrets/**`` or
    ``./secrets/**`` (the way a ``.gitignore`` is written) matches nothing and the
    owner, who sees no error, believes the directory is hidden. A pattern that ends
    in ``/`` and has a wildcard (``src/**/``) matched only paths that end in ``/``,
    that is no file; as for a pattern without a wildcard, the slash means the
    directory and everything under it. A pattern that is nothing but slashes and
    dots means the whole repository, never nothing. Only ever hides more."""
    text = glob.strip().replace("\\", "/")
    while text.startswith(("./", "/")):
        text = text[2:] if text.startswith("./") else text[1:]
    if not text or text == ".":
        return "**"
    if text.endswith("/") and any(c in text for c in "*?"):
        text = text.rstrip("/")
        if not text.endswith("**"):
            text += "/**"
    return text


def validate_deny_globs(globs: Iterable[object] | None) -> list[str]:
    """The owner's extra hidden paths, trimmed and de-duplicated.

    At most :data:`MAX_DENY_GLOBS` entries of :data:`MAX_GLOB_LENGTH` characters,
    no NUL. The patterns are read by :func:`src.access.resolver.glob_match`, which
    cannot fail on any string, so nothing is parsed here; each is written the way
    that matcher reads it (:func:`anchor_glob`)."""
    if globs is None:
        return []
    if isinstance(globs, str | bytes):
        raise ShareError("deny_globs must be a list of path patterns")
    out: list[str] = []
    for raw in globs:
        if not isinstance(raw, str):
            raise ShareError("a path pattern must be text")
        glob = raw.strip()
        if not glob:
            continue
        if "\x00" in glob:
            raise ShareError("a path pattern cannot contain a NUL character")
        if len(glob) > MAX_GLOB_LENGTH:
            raise ShareError(f"a path pattern is at most {MAX_GLOB_LENGTH} characters")
        glob = anchor_glob(glob)
        if glob not in out:
            out.append(glob)
    if len(out) > MAX_DENY_GLOBS:
        raise ShareError(f"at most {MAX_DENY_GLOBS} path patterns")
    return out


def status_of(row, now: datetime | None = None) -> str:  # noqa: ANN001
    """``active`` | ``expired`` | ``revoked`` for a ``repo_shares`` row."""
    if row.revoked_at is not None:
        return "revoked"
    if _aware(row.expires_at) <= (now or _now()):
        return "expired"
    return "active"


# ════════════════════════════════════════════════════════════════════
# The decision a share yields
# ════════════════════════════════════════════════════════════════════


class _SharedRule(_RuleView):
    """The only rule of a shared repository: ``code``, minus what is hidden.

    Hidden is the owner's ``deny_globs`` and the share's, plus the secret floor
    (:func:`src.security.secret_files.is_secret_path`) whatever any glob says. The
    dev tools and the retriever apply that floor to everything they show; it is in
    the rule as well so that a reader which asks only the decision cannot show a
    key file of another workspace either. ``allow_globs`` and ``visibility`` of the
    owner's rules are not applied: the share is the explicit permission, and it is
    the same reading a token gets for its own repositories today.
    """

    def hides_path(self, rel_path: str) -> bool:
        from src.security.secret_files import is_secret_path

        return is_secret_path(rel_path) or super().hides_path(rel_path)


@dataclass(frozen=True)
class SharedGrant:
    """A share that holds for this caller on this call."""

    share_id: str
    repo_slug: str
    owner_workspace_id: str
    grantee_workspace_id: str
    deny_globs: tuple[str, ...]
    allow_ask: bool
    expires_at: datetime

    def decision(self) -> RepoAccessDecision:
        """``code`` on the repository, minus the hidden paths. Always an explicit
        rule, never ``open_default``: a shared repository has no "no rule, so
        everything" reading."""
        return RepoAccessDecision(
            repo_slug=self.repo_slug,
            visibility="code",
            rules=(_SharedRule("code", (), self.deny_globs, ()),),
            open_default=False,
            deny_globs=self.deny_globs,
            via_share=self.share_id,
            data_workspace=self.owner_workspace_id,
            ask_ok=self.allow_ask,
        )


# ════════════════════════════════════════════════════════════════════
# C2–C6: which shares hold
# ════════════════════════════════════════════════════════════════════


def _string_list(value: object) -> list[str] | None:
    """``value`` as a list of strings; ``[]`` for a missing one; ``None`` for
    anything else.

    The lists a share carries (what it hides, whom it switched off) are written by
    our own code as lists of strings. Anything else in the column is not a smaller
    list, it is damage: read leniently, a string would become its characters and
    hide or block nothing. So a share whose list cannot be read as such is not
    held (I1: an unknown value refuses)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _is_a_side(workspace_id: object) -> bool:
    """May this workspace be one side of a share? Not the built-in ``default`` one
    (D14): records of that workspace carry no tenant (``normalize_workspace_id``),
    so nothing that filters on the tenant, vectors included, could tell its
    repositories from anybody's. A blank id is no workspace at all."""
    from src.security.audit import normalize_workspace_id

    return normalize_workspace_id(workspace_id) is not None


def may_take_part(workspace_id: object) -> bool:
    """Whether ``workspace_id`` may be a side of a new share (the creating side asks
    this; reading asks it again on every call)."""
    return _is_a_side(workspace_id)


def is_damaged(row) -> bool:  # noqa: ANN001
    """A share whose list of hidden paths or of switched-off people cannot be read as a
    list of strings. It is not held (:func:`_string_list`), and nobody rewrites it:
    changing a damaged list into "what was there plus this" would turn a share that is
    refused into one that is held, with whatever the damage left."""
    return _string_list(row.deny_globs) is None or _string_list(row.blocked_user_ids) is None


def repo_still_registered(row) -> bool:  # noqa: ANN001
    """C5: the repository is still the one that was shared, and only the owner's.

    Public because the licensed half asks it too: a share whose repository is no longer
    the one the owner agreed to is not extended (``src/ee/sharing/service.py``)."""
    try:
        from src.api.auto_review import get_auto_review_store

        store = get_auto_review_store()
        cfg = store.get_in_workspace(row.owner_workspace_id, row.repo_slug)
        if cfg is None:
            return False
        if (str(cfg.provider or "").strip().lower()
                != str(row.repo_provider or "").strip().lower()):
            return False
        if (str(cfg.full_name or "").strip().lower()
                != str(row.repo_full_name or "").strip().lower()):
            return False
        # Bound to one workspace and to the owner: a slug that two workspaces
        # registered is nobody's (the graph and the clone on disk are keyed by
        # slug alone), and the receiver having it too is that case.
        if store.workspace_for_slug(row.repo_slug) != row.owner_workspace_id:
            return False
        return store.get_in_workspace(row.grantee_workspace_id, row.repo_slug) is None
    except Exception as exc:  # noqa: BLE001 — an unreadable registry grants nothing
        logger.warning("share_registry_unreadable share=%s err=%s", row.id, type(exc).__name__)
        return False


def _owner_denies(session, owner: str, slugs: list[str]) -> dict[str, list[str] | None]:  # noqa: ANN001
    """``{slug: deny globs}`` of every rule the owner has on those repositories,
    whatever team it is for: what the owner hides from anybody short of a rule
    that says otherwise, a share does not show either. ``None`` for a repository
    with a rule whose list cannot be read (:func:`_string_list`): the owner's
    wishes are unknown, so the share is not held."""
    from sqlalchemy import select

    from src.db.models import RepoAccessRule

    out: dict[str, list[str] | None] = {}
    rules = session.execute(
        select(RepoAccessRule).where(
            RepoAccessRule.workspace_id == owner,
            RepoAccessRule.repo_slug.in_(slugs),
        )
    ).scalars().all()
    for rule in rules:
        globs = _string_list(rule.deny_globs)
        current = out.setdefault(rule.repo_slug, [])
        out[rule.repo_slug] = None if globs is None or current is None else [*current, *globs]
    return out


def live_grants(
    session,  # noqa: ANN001
    *,
    share_ids: Iterable[str],
    workspace_id: str,
    user_id: str,
    slugs: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, SharedGrant]:
    """``{repo slug: grant}`` for the shares of ``share_ids`` that hold for
    ``user_id`` in ``workspace_id`` right now (C2–C6).

    ``slugs`` limits the work to those repositories. A database or registry that
    cannot be read yields no grant. Never raises for a bad id or a missing row."""
    ids = clean_ids(share_ids)[:MAX_SHARES_PER_TOKEN * 4]
    if not ids or not workspace_id or not user_id or not _is_a_side(workspace_id):
        return {}
    from sqlalchemy import select

    from src.db.models import RepoShare

    moment = now or _now()
    wanted = None if slugs is None else set(slugs)
    rows = session.execute(
        select(RepoShare).where(
            RepoShare.id.in_(ids),
            RepoShare.grantee_workspace_id == workspace_id,
            RepoShare.revoked_at.is_(None),
            RepoShare.expires_at > moment,
        ).order_by(RepoShare.id)
    ).scalars().all()

    held: list = []
    own_deny: dict[str, list[str]] = {}
    for row in rows:
        if wanted is not None and row.repo_slug not in wanted:
            continue
        if _aware(row.expires_at) <= moment or row.revoked_at is not None:
            continue
        if not (_is_a_side(row.owner_workspace_id) and _is_a_side(row.grantee_workspace_id)):
            continue
        blocked, hidden = _string_list(row.blocked_user_ids), _string_list(row.deny_globs)
        if blocked is None or hidden is None:
            logger.warning("share_list_unreadable share=%s — refused", row.id)
            continue
        if user_id in blocked:
            continue
        if not repo_still_registered(row):
            continue
        own_deny[row.id] = hidden
        held.append(row)
    if not held:
        return {}

    denies: dict[str, dict[str, list[str] | None]] = {}
    for owner in {r.owner_workspace_id for r in held}:
        denies[owner] = _owner_denies(
            session, owner, [r.repo_slug for r in held if r.owner_workspace_id == owner])

    out: dict[str, SharedGrant] = {}
    for row in held:
        owners = denies[row.owner_workspace_id].get(row.repo_slug, [])
        if owners is None:
            logger.warning("share_owner_rule_unreadable share=%s — refused", row.id)
            continue
        # Written as the matcher reads them: a leading slash in what the owner typed
        # (here or in a rule of its own) must not leave a path open.
        globs = tuple(dict.fromkeys(
            anchor_glob(g) for g in [*owners, *own_deny[row.id]]))
        out[row.repo_slug] = SharedGrant(
            share_id=row.id, repo_slug=row.repo_slug,
            owner_workspace_id=row.owner_workspace_id,
            grantee_workspace_id=row.grantee_workspace_id,
            deny_globs=globs, allow_ask=bool(row.allow_ask),
            expires_at=_aware(row.expires_at),
        )
    # Two live shares of one slug to one workspace cannot be created (the unique
    # index); if they ever exist, the repository is nobody's rather than anybody's.
    seen = Counter(r.repo_slug for r in held)
    for slug, count in seen.items():
        if count > 1:
            logger.warning("share_slug_twice slug_count=%d — refused", count)
            out.pop(slug, None)
    return out


def check_attachable(
    session,  # noqa: ANN001
    share_ids: Iterable[object],
    *,
    workspace_id: str,
    user_id: str,
    now: datetime | None = None,
) -> list[str]:
    """The well-formed ``share_ids``, once each, when every one of them could be
    read by ``user_id`` through a token of ``workspace_id`` today; otherwise raise
    :class:`ShareError` (:data:`NO_SUCH_SHARE`) without saying which one failed.

    The issuing side calls this before it writes ``shared_repos``, so a token is
    never made for a share that would be dead on its first call. It uses the same
    check as every call (:func:`live_grants`), not a second copy of it."""
    wanted: list[str] = []
    for candidate in dict.fromkeys(share_ids):
        if not is_share_id(candidate):
            raise ShareError(NO_SUCH_SHARE)
        wanted.append(candidate)
    if not wanted:
        return []
    held = live_grants(session, share_ids=wanted, workspace_id=workspace_id,
                       user_id=user_id, now=now)
    if {g.share_id for g in held.values()} != set(wanted):
        raise ShareError(NO_SUCH_SHARE)
    return wanted


def ask_targets(
    session,  # noqa: ANN001
    *,
    share_ids: Iterable[str],
    workspace_id: str,
    user_id: str,
    slugs: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, SharedGrant]:
    """The shared repositories ``ask`` may use: those that hold (C2–C6) and whose
    owner allowed it (C8)."""
    return {
        slug: grant
        for slug, grant in live_grants(
            session, share_ids=share_ids, workspace_id=workspace_id,
            user_id=user_id, slugs=slugs, now=now).items()
        if grant.allow_ask
    }


# ════════════════════════════════════════════════════════════════════
# Who holds a share
# ════════════════════════════════════════════════════════════════════


def holders(session, share_id: str, *, now: datetime | None = None) -> dict[str, list[str]]:  # noqa: ANN001
    """``{user id: [token ids]}`` of the people whose live token carries the share.

    Only a token the share can work on counts: a ``pat`` of the ``dev`` profile
    that cannot write, not revoked, not expired, in the receiving workspace. And
    only a person the owner has not switched off: a blocked person's token reads
    nothing (C6), so it must not keep a place in ``max_people`` either; otherwise
    blocking somebody would leave the owner unable to lower the ceiling below the
    number of people who can no longer read. (A list of blocked people that cannot
    be read as a list counts everybody: the conservative direction for a ceiling.)
    The ids are compared in Python, not with a ``LIKE`` on the JSON column: a
    workspace has few tokens, and the answer must not depend on how a database
    renders JSON."""
    if not is_share_id(share_id):
        return {}
    from sqlalchemy import select

    from src.db.models import McpToken, RepoShare

    share = session.get(RepoShare, share_id)
    if share is None:
        return {}
    blocked = set(_string_list(share.blocked_user_ids) or [])
    moment = now or _now()
    rows = session.execute(
        select(McpToken).where(
            McpToken.workspace_id == share.grantee_workspace_id,
            McpToken.kind == "pat",
            McpToken.profile == "dev",
            McpToken.revoked_at.is_(None),
            McpToken.expires_at > moment,
        )
    ).scalars().all()
    out: dict[str, list[str]] = {}
    for token in rows:
        if token.allow_write or token.user_id in blocked:
            continue
        if share_id not in clean_ids(token.shared_repos):
            continue
        out.setdefault(token.user_id, []).append(token.id)
    return out


def lock_places(session, share_ids: Iterable[object]) -> None:  # noqa: ANN001
    """Hold the rows of these shares until the transaction ends, so that counting the
    people who hold a share and then giving a token to one more is one step.

    ``max_people`` is a ceiling on a count that the same request then raises. Two
    requests side by side would both count the last free place as free, and both
    write a token; the ceiling the owner chose would be exceeded without any error. The
    request that gets the lock first writes its token and commits; the other waits
    here, and its count (which comes after) includes that token. The rows are taken
    in a fixed order, so two requests for overlapping sets cannot wait for each other.

    Every step that reads the places and then changes who holds one (issuing a token,
    putting a share on a token, lowering the ceiling) calls this first, in the session
    that writes. A database without row locks (the SQLite of the test worlds) ignores
    the lock, and then the steps are no more atomic than they were."""
    ids = sorted(clean_ids(list(share_ids)))
    if not ids:
        return
    from sqlalchemy import select

    from src.db.models import RepoShare

    session.execute(
        select(RepoShare).where(RepoShare.id.in_(ids)).order_by(RepoShare.id)
        .with_for_update().execution_options(populate_existing=True)
    ).scalars().all()


def has_room(session, share_id: str, user_id: str) -> bool:  # noqa: ANN001
    """May ``user_id`` hold a token for the share: they already do, or fewer than
    ``max_people`` different people do. False for a share that does not exist.

    The answer is only as good as the moment: call :func:`lock_places` first, in the
    session that goes on to write the token."""
    from src.db.models import RepoShare

    share = session.get(RepoShare, share_id) if is_share_id(share_id) else None
    if share is None:
        return False
    people = holders(session, share_id)
    return user_id in people or len(people) < int(share.max_people or DEFAULT_PEOPLE)


def describe(
    session,  # noqa: ANN001
    share_ids: Iterable[str],
    *,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """What a token page shows for each share: ``{share_id, slug, owner_name,
    expires_at, status}``. With ``workspace_id`` only the shares for that
    workspace; a share that is not found is left out, not explained."""
    ids = clean_ids(share_ids)
    if not ids:
        return []
    from sqlalchemy import select

    from src.db.models import RepoShare, Workspace

    moment = now or _now()
    rows = {r.id: r for r in session.execute(
        select(RepoShare).where(RepoShare.id.in_(ids))).scalars().all()}
    owners = {w.id: w.name for w in session.execute(
        select(Workspace).where(
            Workspace.id.in_(sorted({r.owner_workspace_id for r in rows.values()})))
    ).scalars().all()} if rows else {}
    out: list[dict[str, Any]] = []
    for share_id in ids:
        row = rows.get(share_id)
        if row is None or (workspace_id is not None
                           and row.grantee_workspace_id != workspace_id):
            continue
        out.append({
            "share_id": row.id, "slug": row.repo_slug,
            "owner_name": owners.get(row.owner_workspace_id, ""),
            "expires_at": _aware(row.expires_at), "status": status_of(row, moment),
        })
    return out


def owner_workspaces(session, share_ids: Iterable[str]) -> dict[str, str]:  # noqa: ANN001
    """``{share id: owner workspace id}`` for the shares that exist. The owner's side
    of an audit row is written into this workspace, never into the receiver's."""
    ids = clean_ids(share_ids)
    if not ids:
        return {}
    from sqlalchemy import select

    from src.db.models import RepoShare

    return {r.id: r.owner_workspace_id for r in session.execute(
        select(RepoShare).where(RepoShare.id.in_(ids))).scalars().all()}


def labels(session, share_ids: Iterable[str]) -> dict[str, str]:  # noqa: ANN001
    """``{share id: "slug@owner name"}``: how the superadmin's call log names a
    share. The receiving workspace's own pages never get this, and the log row holds
    only the id (see ``mcp_call_log.shares``). An id with no row is its own label."""
    ids = clean_ids(share_ids)
    found = {d["share_id"]: f"{d['slug']}@{d['owner_name']}" for d in describe(session, ids)}
    return {i: found.get(i, i) for i in ids}


def audit_owner(
    action: str,
    *,
    owner_workspace_id: str,
    share_id: str,
    actor: str = "",
    actor_id: str | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one ``sharing.*`` row into the OWNER's audit trail. The receiver's trail
    has no row for what happens to a share (it learns of a token it issued from its
    own ``mcp.token_issued``, which carries a number). Never raises, never blocks
    the operation it records; ``detail`` is ids, counts and field names, no secret."""
    try:
        # Looked up here, not imported at the top: a test replaces the function.
        from src.security import audit as _audit

        _audit.record_action(
            action=action, actor=actor or None, actor_id=actor_id,
            workspace_id=owner_workspace_id, target=share_id, ip=ip, detail=detail or {})
    except Exception:  # noqa: BLE001
        logger.warning("share_audit_failed action=%s", action)


# ════════════════════════════════════════════════════════════════════
# Ending and narrowing (free in every build)
# ════════════════════════════════════════════════════════════════════


def _end(rows, *, by: str, reason: str, now: datetime) -> list[str]:  # noqa: ANN001
    if reason not in REASONS:
        raise ShareError(f"unknown reason {reason!r}")
    ended: list[str] = []
    for row in rows:
        if row.revoked_at is not None:
            continue
        row.revoked_at = now
        row.revoked_by = by[:200]
        row.revoke_reason = reason
        ended.append(row.id)
    return ended


def revoke_share(session, share_id: str, *, by: str, reason: str = "owner") -> bool:  # noqa: ANN001
    """End one share from the next call on. True when it was live; staged, not committed."""
    from src.db.models import RepoShare

    row = session.get(RepoShare, share_id) if is_share_id(share_id) else None
    if row is None:
        return False
    return bool(_end([row], by=by, reason=reason, now=_now()))


def live_of_repo(owner_workspace_id: str | None, repo_slug: str):  # noqa: ANN201
    """The SELECT of the live shares of a repository, for a caller that runs it on
    an async session. ``owner_workspace_id=None`` means every owner: the command
    line purges a slug without naming a workspace, and the clone it deletes serves
    all of them."""
    from sqlalchemy import select

    from src.db.models import RepoShare

    stmt = select(RepoShare).where(
        RepoShare.repo_slug == repo_slug, RepoShare.revoked_at.is_(None))
    if owner_workspace_id is not None:
        stmt = stmt.where(RepoShare.owner_workspace_id == owner_workspace_id)
    return stmt


def end_rows(rows, *, by: str, reason: str) -> list[str]:  # noqa: ANN001
    """End the rows still live; returns their ids. Staged, not committed. The one
    way a share ends, whoever calls it (a route, a purge, a workspace deletion)."""
    return _end(rows, by=by, reason=reason, now=_now())


def end_statement(owner_workspace_id: str | None, repo_slug: str, *, by: str,
                  reason: str = "repo_removed"):  # noqa: ANN201
    """The UPDATE that ends the live shares of a repository, for a purge that runs
    on an async session and wants a count, not the rows."""
    if reason not in REASONS:
        raise ShareError(f"unknown reason {reason!r}")
    from sqlalchemy import update

    from src.db.models import RepoShare

    stmt = update(RepoShare).where(
        RepoShare.repo_slug == repo_slug, RepoShare.revoked_at.is_(None))
    if owner_workspace_id is not None:
        stmt = stmt.where(RepoShare.owner_workspace_id == owner_workspace_id)
    return stmt.values(revoked_at=_now(), revoked_by=by[:200], revoke_reason=reason)


def revoke_for_repo(
    session,  # noqa: ANN001
    owner_workspace_id: str | None,
    repo_slug: str,
    *,
    reason: str = "repo_removed",
    by: str = "system",
) -> list[str]:
    """End every live share of a repository the owner is removing or purging, in
    the transaction that removes it. Registering the same slug again then starts
    from nothing; the identity check (C5) is only the second lock."""
    rows = session.execute(live_of_repo(owner_workspace_id, repo_slug)).scalars().all()
    return _end(rows, by=by, reason=reason, now=_now())


def revoke_for_workspace(
    session,  # noqa: ANN001
    workspace_id: str,
    *,
    reason: str = "workspace_deleted",
    by: str = "system",
) -> list[str]:
    """End every live share a workspace gives or receives (it is being deleted)."""
    from sqlalchemy import or_, select

    from src.db.models import RepoShare

    rows = session.execute(
        select(RepoShare).where(
            or_(RepoShare.owner_workspace_id == workspace_id,
                RepoShare.grantee_workspace_id == workspace_id),
            RepoShare.revoked_at.is_(None),
        )
    ).scalars().all()
    return _end(rows, by=by, reason=reason, now=_now())


def narrow(
    session,  # noqa: ANN001
    share_id: str,
    *,
    add_deny: Iterable[str] = (),
    expires_at: datetime | None = None,
    allow_ask: bool | None = None,
    max_people: int | None = None,
    block: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Give less than the share gives now. Returns ``{field: {before, after}}`` of
    what changed, for the audit; stages the change, does not commit.

    Only narrowing is allowed here: more hidden paths, an earlier end, ``ask`` off,
    fewer people (never fewer than hold it), one more person switched off. Anything
    that gives more raises :class:`WideningError`; a share that is no longer live
    cannot be narrowed (revoking it was the way)."""
    from src.db.models import RepoShare

    if is_share_id(share_id):
        lock_places(session, [share_id])  # the ceiling below counts people a token may add
    row = session.get(RepoShare, share_id) if is_share_id(share_id) else None
    if row is None:
        raise ShareError("no such share")
    now = _now()
    if status_of(row, now) != "active":
        raise ShareError("the share is no longer active")
    if is_damaged(row):
        # The owner ends it and shares again.
        raise ShareError("the share is damaged: revoke it")
    changes: dict[str, dict[str, Any]] = {}

    extra = validate_deny_globs(add_deny)
    if extra:
        before = [str(g) for g in (row.deny_globs or [])]
        after = list(dict.fromkeys([*before, *extra]))
        if after != before:
            if len(after) > MAX_DENY_GLOBS:
                raise ShareError(f"at most {MAX_DENY_GLOBS} path patterns")
            row.deny_globs = after
            changes["deny_globs"] = {"before": before, "after": after}

    if expires_at is not None:
        new_end = _aware(expires_at)
        if new_end > _aware(row.expires_at):
            raise WideningError
        if new_end <= now:
            raise ShareError("an end in the past is a revocation: revoke the share")
        if new_end != _aware(row.expires_at):
            changes["expires_at"] = {"before": _aware(row.expires_at), "after": new_end}
            row.expires_at = new_end

    if allow_ask is not None and bool(allow_ask) != bool(row.allow_ask):
        if allow_ask:
            raise WideningError
        changes["allow_ask"] = {"before": True, "after": False}
        row.allow_ask = False

    people = [str(u).strip() for u in block if isinstance(u, str) and str(u).strip()]
    if people:
        if any(len(u) > 200 or "\x00" in u for u in people):
            raise ShareError("not a user id")
        before_ids = [str(u) for u in (row.blocked_user_ids or [])]
        after_ids = list(dict.fromkeys([*before_ids, *people]))
        if after_ids != before_ids:
            if len(after_ids) > MAX_BLOCKED:
                raise ShareError(f"at most {MAX_BLOCKED} people can be blocked")
            row.blocked_user_ids = after_ids
            changes["blocked_user_ids"] = {"before": before_ids, "after": after_ids}

    # After the people are switched off, so that one request can both block somebody
    # and lower the ceiling to the people who are left: a blocked person holds no
    # place (see `holders`), and the row staged above is what `holders` reads.
    if max_people is not None:
        try:
            wanted = int(max_people)
        except (TypeError, ValueError) as exc:
            raise ShareError("max_people must be a number") from exc
        current = int(row.max_people or DEFAULT_PEOPLE)
        if wanted > current:
            raise WideningError
        if not MIN_PEOPLE <= wanted <= MAX_PEOPLE:
            raise ShareError(f"max_people is {MIN_PEOPLE} to {MAX_PEOPLE}")
        if wanted < len(holders(session, share_id, now=now)):
            raise ShareError("fewer people than hold a token for it now: block some first")
        if wanted != current:
            changes["max_people"] = {"before": current, "after": wanted}
            row.max_people = wanted
    return changes


# ════════════════════════════════════════════════════════════════════
# Cross-repository references
# ════════════════════════════════════════════════════════════════════


def linked_repos(scope, slug: str, *, limit: int = MAX_LINKED) -> list[str]:  # noqa: ANN001
    """The repositories ``refs`` looks through for the other side of ``slug``.

    For a shared repository: every repository of the caller's own that the caller
    reads in full. For an own one: the shared repositories the caller reads. Never
    the owner's groups or other repositories of the owner, and never one the caller
    cannot read in full: ``scope.decisions`` holds only what the caller may
    research. No ``slug`` the caller cannot read yields nothing."""
    decisions: Mapping[str, RepoAccessDecision] = getattr(scope, "decisions", None) or {}
    me = decisions.get(slug)
    if me is None or not me.code_visible:
        return []
    from_shared = bool(me.via_share)
    return sorted(
        s for s, d in decisions.items()
        if s != slug and d.code_visible and bool(d.via_share) != from_shared
    )[:max(0, limit)]


__all__ = [
    "DEFAULT_DAYS", "DEFAULT_PEOPLE", "MAX_BLOCKED", "MAX_DENY_GLOBS", "MAX_GLOB_LENGTH",
    "MAX_LINKED", "MAX_PEOPLE", "MAX_SHARES_PER_TOKEN", "MIN_PEOPLE", "NO_SUCH_SHARE",
    "REASONS", "SHARE_ID_RE", "SharedGrant", "ShareError", "WIDENING", "WideningError",
    "ask_targets", "audit_owner", "check_attachable", "clean_ids", "describe", "end_rows",
    "end_statement", "has_room", "holders", "is_damaged", "is_share_id", "labels",
    "linked_repos", "live_grants", "live_of_repo", "lock_places", "max_share_days",
    "may_take_part",
    "narrow", "owner_workspaces", "repo_still_registered",
    "revoke_for_repo", "revoke_for_workspace", "revoke_share", "status_of",
    "validate_deny_globs",
]
