"""Which repositories and groups an MCP caller may address — the tenant gate.

Graph files live flat at ``<data_dir>/<repo_slug>/graph.fdblite``: the
filesystem does not know which tenant a graph belongs to. The binding lives in
the repo registry (:class:`src.api.auto_review.AutoReviewStore`), and the REST
surface has always consulted it — ``/api/search`` searches only
``list_for_workspace(ws)``, ``/api/docs/{slug}`` 404s a slug the workspace did
not register. The MCP graph tools (``find_symbol``, ``get_symbol``,
``find_callers``, ``find_callees``, ``query_graph``, ``cross_repo_edges``,
``list_repos``, ``list_groups``) checked a scope and nothing else, so under
multi_tenant any token holding ``read:graph`` read every tenant's symbol graph
and ran arbitrary read-only Cypher against it.

The rule, under multi_tenant:

  * the caller must be authenticated and tied to a workspace it actually
    belongs to (a "default" landed on by fallback is not a membership);
  * the slug must be bound to exactly that workspace — unknown, foreign and
    ambiguous (bound to two tenants, which share one graph file) all fail;
  * and the research-access rules (:func:`src.mcp_server.identity.caller_access`)
    must make it researchable.

Every failure reads the same as "not found", so the answer is not an oracle
for which slugs exist in other tenants.

Under single_tenant nothing changes except that a slug which is not a safe
path segment is refused (see :func:`src.config.validate_repo_slug`): one
tenant, one trust domain, and the stdio transport legitimately runs with no
token at all.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import is_valid_repo_slug

logger = logging.getLogger(__name__)


def enforced() -> bool:
    """True when MCP calls must be confined to the caller's workspace."""
    from src.deployment import is_multi_tenant

    return is_multi_tenant()


def workspace_owns_slug(workspace_id: str, repo_slug: str) -> bool:
    """Is ``repo_slug`` registered to ``workspace_id`` and to nobody else?

    Fails closed on an unreadable registry, an empty workspace id, an
    unregistered slug and a slug bound to more than one workspace.
    """
    if not workspace_id or not is_valid_repo_slug(repo_slug):
        return False
    try:
        from src.api.auto_review import get_auto_review_store

        return get_auto_review_store().workspace_for_slug(repo_slug) == workspace_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_tenant_binding_unreadable repo=%s err=%s", repo_slug, exc)
        return False


def workspace_slugs(workspace_id: str) -> set[str]:
    """Every slug bound to ``workspace_id`` alone. Empty on any failure."""
    if not workspace_id:
        return set()
    try:
        from src.api.auto_review import get_auto_review_store

        store = get_auto_review_store()
        slugs = {c.repo_slug for c in store.list_for_workspace(workspace_id)}
        return {s for s in slugs if workspace_owns_slug(workspace_id, s)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_tenant_listing_unreadable ws=%s err=%s", workspace_id, exc)
        return set()


def caller_may_bind(caller) -> bool:  # noqa: ANN001
    """A caller that can own anything under multi_tenant at all."""
    return bool(
        caller.authenticated and caller.workspace_resolved and caller.workspace_id
    )


def authorize_repo(repo_slug: str):  # noqa: ANN201 — RepoAccessDecision | None
    """The caller's access decision for ``repo_slug``, or None for "not found".

    single_tenant: a full decision for every syntactically valid slug — the
    historical behaviour. multi_tenant: see the module docstring.
    """
    from src.access import RepoAccessDecision

    if not is_valid_repo_slug(repo_slug):
        return None
    if not enforced():
        return RepoAccessDecision.full(repo_slug)

    from src.mcp_server.identity import caller_access

    # caller_access applies the workspace binding itself under multi_tenant.
    _caller, access = caller_access([repo_slug])
    dec = access.get(repo_slug)
    if dec is None or not dec.researchable:
        return None
    return dec


def unrestricted(dec) -> bool:  # noqa: ANN001
    """Does ``dec`` grant every path of the repo?

    The raw-Cypher tool cannot filter its rows by path — a query may return
    anything — so it is only allowed where there is nothing to filter.
    """
    if dec is None:
        return False
    if dec.open_default:
        return True
    return any(
        r.visibility == "code" and not r.allow_globs and not r.deny_globs
        for r in dec.rules
    )


def visible_rows(rows: list[dict[str, Any]], dec, key: str = "file") -> list[dict[str, Any]]:  # noqa: ANN001
    """``rows`` without those whose file the decision conceals."""
    if dec is None or dec.open_default:
        return rows
    return [r for r in rows if not r.get(key) or dec.path_visible(str(r.get(key)))]


def authorize_group(group_name: str):  # noqa: ANN201 — RepoGroup | str | None
    """Non-None when the caller may address the group named ``group_name``.

    single_tenant: always (the name itself is returned) — the tools resolve
    it exactly as before, including the legacy flat graph file.
    multi_tenant: the caller's group, and only a group stamped with the
    caller's workspace whose every repository the caller may research — a
    cross-repo graph is made of them. None reads as "not found".
    """
    from src.groups import GroupNotFoundError, get_group_manager

    if not isinstance(group_name, str) or not group_name:
        return None
    if not enforced():
        return group_name
    mgr = get_group_manager()

    from src.mcp_server.identity import resolve_caller

    caller = resolve_caller()
    if not caller_may_bind(caller):
        return None
    try:
        group = mgr.load(group_name, caller.workspace_id)
    except (GroupNotFoundError, Exception):  # noqa: BLE001
        return None
    # `load` falls back to the legacy flat file, which may be another
    # tenant's. The stamp on the group is the authority, not the path.
    if (group.workspace_id or "default") != caller.workspace_id:
        return None
    from src.mcp_server.tools import _repo_id_to_slug

    for repo_id in group.repos:
        if authorize_repo(_repo_id_to_slug(repo_id)) is None:
            return None
    return group


__all__ = [
    "authorize_group",
    "authorize_repo",
    "caller_may_bind",
    "enforced",
    "unrestricted",
    "visible_rows",
    "workspace_owns_slug",
    "workspace_slugs",
]
