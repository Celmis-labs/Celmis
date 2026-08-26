"""MCP tool implementations — low-level graph queries for all tools.

This module is pure functions with no MCP-specific dependencies. It is tested
in isolation. `server.py` then wraps every function in an `@mcp.tool()`
decorator.

Security:
    - Read-only Cypher (all queries start with MATCH/RETURN — write keywords blocked)
    - Whitelist Cypher prefix check in `query_graph()`
    - Symbol IDs are not sanitised — FalkorDB parameterises them via $params
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from src.config import Settings, get_settings
from src.groups import GroupNotFoundError, get_group_manager
from src.indexing.graph.extractor import SymbolInfo
from src.indexing.graph.graph_store import (
    make_graph_store,
)

logger = logging.getLogger(__name__)


# ─── data structures for tool responses ──────────────────────────────


@dataclass
class GroupSummary:
    name: str
    description: str
    repos: list[str]
    repo_count: int
    indexed: bool  # whether the graph file exists
    cross_repo_indexed: bool
    cross_repo_edges: int  # 0 if not yet materialized


@dataclass
class RepoSummary:
    slug: str
    full_path: str  # owner/name
    indexed: bool
    graph_path: str | None
    symbol_count: int  # 0 if not indexed


# ─── Cypher safety ──────────────────────────────────────────────────


_WRITE_KEYWORDS = (
    "CREATE", "DELETE", "DETACH", "REMOVE", "SET", "MERGE",
    "DROP", "FOREACH", "CALL", "LOAD", "USING",
)


def _is_read_only_cypher(cypher: str) -> bool:
    """Check that the Cypher contains only read-only keywords.

    Cypher MATCH/RETURN/WHERE/WITH/UNWIND/ORDER/LIMIT/SKIP/AS — OK.
    CREATE/DELETE/SET/MERGE/REMOVE — blocked.

    Not perfect (it does not parse — so as to avoid a dependency on a Cypher
    parser), but enough for MVP protection.
    """
    upper = cypher.upper()
    # Strip string literals so that keywords inside names do not turn into
    # false positives (for simplicity: replace 'X' with spaces)
    import re
    stripped = re.sub(r"'[^']*'", "''", upper)
    stripped = re.sub(r'"[^"]*"', '""', stripped)
    # Strip backtick-quoted identifiers
    stripped = re.sub(r"`[^`]*`", "``", stripped)

    # Tokenize on word boundaries — so that MATCH inside MATCHING is not a
    # false positive
    tokens = re.findall(r"\b[A-Z]+\b", stripped)
    return not any(tok in _WRITE_KEYWORDS for tok in tokens)


# ─── Group / repo discovery ─────────────────────────────────────────


def list_groups(
    settings: Settings | None = None,
    workspace_id: str | None = None,
) -> list[GroupSummary]:
    """Groups with summary info. With `workspace_id`, only that tenant's.

    Walked as PATHS. Listing names installation-wide and then opening each by
    bare name meant a tenant-scoped group was listed and could not be opened,
    so it vanished behind a warning — and the two spellings disagreed about
    which groups exist at all.

    The cross-repo edge count comes from the group's own graph file rather
    than one built from the bare name, which two tenants sharing a group name
    would otherwise share.
    """
    settings = settings or get_settings()
    mgr = get_group_manager()
    out: list[GroupSummary] = []
    for _path, g in mgr.iter_groups(workspace_id):
        cross_repo_path = mgr.graph_path(g)
        cross_indexed = cross_repo_path.exists()
        cross_edges = 0
        if cross_indexed:
            try:
                store = make_graph_store(cross_repo_path)
                try:
                    res = store.query("MATCH ()-[r]->() RETURN count(r) AS c")
                    cross_edges = int(res[0]["c"]) if res else 0
                finally:
                    store.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("cross_repo_count_failed err=%s", exc)
        out.append(GroupSummary(
            name=g.name,
            description=g.description,
            repos=list(g.repos),
            repo_count=len(g.repos),
            indexed=any(
                _repo_graph_exists(_repo_id_to_slug(r), settings) for r in g.repos
            ),
            cross_repo_indexed=cross_indexed,
            cross_repo_edges=cross_edges,
        ))
    return out


def list_repos(
    group_name: str | None = None,
    settings: Settings | None = None,
) -> list[RepoSummary]:
    """List of repos: either in a specific group, or every one indexed on disk."""
    settings = settings or get_settings()

    if group_name:
        mgr = get_group_manager()
        try:
            g = mgr.load(group_name)
        except GroupNotFoundError:
            return []
        return [_repo_to_summary(repo_id, settings) for repo_id in g.repos]

    # Every indexed repo from disk
    if not settings.repos_dir.exists():
        return []
    out: list[RepoSummary] = []
    for sub in sorted(settings.repos_dir.iterdir()):
        if not sub.is_dir() or not (sub / ".git").exists():
            continue
        out.append(_slug_to_summary(sub.name, settings))
    return out


# ─── Symbol queries ─────────────────────────────────────────────────


def find_symbol(
    name: str,
    repo_slug: str,
    limit: int = 20,
    settings: Settings | None = None,
    exact: bool = True,
) -> list[dict[str, Any]]:
    """Find symbols by name in a specific repo. exact=True — exact match
    (MCP tools); exact=False — case-insensitive substring (UI search).

    Returns: list of dict, each with repo_slug + symbol fields.
    """
    settings = settings or get_settings()
    db_path = settings.repo_graph_path(repo_slug)
    if not db_path.exists():
        return []

    store = make_graph_store(db_path)
    try:
        if exact:
            symbols = store.find_by_name(name, limit=limit)
        else:
            symbols = store.find_by_name_like(name, limit=limit)
    finally:
        store.close()

    return [_symbol_to_dict(s, repo_slug) for s in symbols]


def get_symbol(
    symbol_id: str,
    repo_slug: str,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Pull the full information about a symbol by id."""
    settings = settings or get_settings()
    db_path = settings.repo_graph_path(repo_slug)
    if not db_path.exists():
        return None

    store = make_graph_store(db_path)
    try:
        sym = store.get_symbol(symbol_id)
    finally:
        store.close()

    return _symbol_to_dict(sym, repo_slug) if sym else None


def find_callers(
    symbol_id: str,
    repo_slug: str,
    depth: int = 2,
    max_nodes: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Find symbols that call the target (incoming CALLS edges).

    BFS expansion over CALLS edges. depth=1 — direct callers; >1 — transitive.
    """
    settings = settings or get_settings()
    db_path = settings.repo_graph_path(repo_slug)
    if not db_path.exists():
        return _empty_expansion(repo_slug)

    store = make_graph_store(db_path)
    try:
        # A NAME IS NOT AN ADDRESS, AND THIS PARAMETER INVITED THE MISTAKE.
        # The query matches `b.id = $id`, and an id is "{file}::{name}" (see
        # ExtractionResult in src/indexing/graph/extractor.py). Three of the
        # four callers in this repository passed a bare name — the breaking
        # change agent passes one parsed out of a diff, which has no file to
        # attach — so the query matched nothing and each of them reported
        # "no consumers" for every symbol it ever looked at. A silent zero
        # that reads exactly like a truthful answer.
        #
        # Resolving here rather than at the call sites, because the next
        # caller will pass a name too.
        targets = _resolve_targets(store, symbol_id)
        seen: set[str] = set()
        callers: list[dict[str, Any]] = []
        for target in targets:
            for row in _query_incoming_callers(store, target, depth, max_nodes):
                key = str(row.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                callers.append(row)
            if len(callers) >= max_nodes:
                callers = callers[:max_nodes]
                break
    finally:
        store.close()

    return {
        "repo": repo_slug,
        "target_id": symbol_id,
        "resolved_ids": targets,
        "depth": depth,
        "callers": callers,
        "truncated": len(callers) >= max_nodes,
    }


def find_callees(
    symbol_id: str,
    repo_slug: str,
    depth: int = 2,
    max_nodes: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Find symbols that are called by the target (outgoing CALLS edges)."""
    settings = settings or get_settings()
    db_path = settings.repo_graph_path(repo_slug)
    if not db_path.exists():
        return _empty_expansion(repo_slug)

    store = make_graph_store(db_path)
    try:
        # Same resolution as find_callers: the outgoing query matches on id.
        seen: set[str] = set()
        callees = []
        for target in _resolve_targets(store, symbol_id):
            for row in _query_outgoing_callees(store, target, depth, max_nodes):
                key = str(row.get("id"))
                if key not in seen:
                    seen.add(key)
                    callees.append(row)
            # The same cap find_callers has. A name can resolve to twenty ids,
            # and twenty queries of LIMIT 100 each is two thousand rows against
            # a documented max_nodes of 100 — with `truncated` computed off
            # that same number, so the answer would understate itself.
            if len(callees) >= max_nodes:
                callees = callees[:max_nodes]
                break
    finally:
        store.close()

    return {
        "repo": repo_slug,
        "source_id": symbol_id,
        "depth": depth,
        "callees": callees,
        "truncated": len(callees) >= max_nodes,
    }


# ─── Cross-repo queries ─────────────────────────────────────────────


def _group_graph_path(group_name: str, settings: Settings,
                      workspace_id: str | None = None):
    """Where this group's cross-repo edges live.

    Built from the bare name, this missed every tenant-scoped group: the YAML
    moved into a tenant directory and the graph moved with it. Ask the manager
    rather than rebuilding the layout in five places.
    """
    from src.groups.manager import GroupManager, GroupNotFoundError

    mgr = GroupManager(settings=settings)
    try:
        return mgr.graph_path(mgr.load(group_name, workspace_id))
    except (GroupNotFoundError, Exception):  # noqa: BLE001
        return settings.workspace_dir / "groups" / f"{group_name}.fdblite"


def cross_repo_edges(
    group_name: str,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Pull every cross-repo edge for the group."""
    settings = settings or get_settings()
    db_path = _group_graph_path(group_name, settings)
    if not db_path.exists():
        return []

    store = make_graph_store(db_path)
    try:
        res = store.query(
            "MATCH (a:Symbol)-[r]->(b:Symbol) "
            "RETURN a.id AS from_id, a.module AS from_repo, "
            "       b.id AS to_id, b.module AS to_repo, "
            "       type(r) AS kind"
        )
    finally:
        store.close()

    return [
        {
            "from_repo": str(row.get("from_repo", "")),
            "from_id": str(row.get("from_id", "")),
            "to_repo": str(row.get("to_repo", "")),
            "to_id": str(row.get("to_id", "")),
            "kind": str(row.get("kind", "")),
        }
        for row in res
    ]


# ─── Raw Cypher escape hatch ────────────────────────────────────────


def query_graph(
    cypher: str,
    repo_slug: str | None = None,
    group_name: str | None = None,
    params: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Read-only Cypher escape hatch.

    Target DB: repo_slug → per-repo graph; group_name → cross-repo graph.
    If both are None → ValueError.

    Security: Cypher whitelist (write keywords blocked).
    """
    settings = settings or get_settings()

    if not _is_read_only_cypher(cypher):
        return {
            "ok": False,
            "error": (
                "Cypher contains write keywords (CREATE/DELETE/SET/MERGE/REMOVE). "
                "Only read-only queries are permitted."
            ),
            "rows": [],
        }

    if repo_slug:
        db_path = settings.repo_graph_path(repo_slug)
    elif group_name:
        db_path = _group_graph_path(group_name, settings)
    else:
        return {
            "ok": False,
            "error": "Either repo_slug or group_name must be provided.",
            "rows": [],
        }

    if not db_path.exists():
        return {
            "ok": False,
            "error": f"Graph not found at {db_path}",
            "rows": [],
        }

    store = make_graph_store(db_path)
    try:
        rows = store.query(cypher, params=params or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "rows": []}
    finally:
        store.close()

    return {"ok": True, "rows": rows, "row_count": len(rows)}


# ─── helpers ────────────────────────────────────────────────────────


def _symbol_to_dict(sym: SymbolInfo, repo_slug: str) -> dict[str, Any]:
    d = asdict(sym)
    d["repo_slug"] = repo_slug
    return d


def _empty_expansion(repo_slug: str) -> dict[str, Any]:
    return {"repo": repo_slug, "callers": [], "callees": [], "truncated": False}


def _resolve_targets(store, symbol_id: str, limit: int = 20) -> list[str]:
    """The graph ids a caller meant.

    An id already carries its file ("src/billing.py::apply_refund"); a bare
    name does not, and the same name can exist in several files, so a name
    resolves to every id that bears it. Returns the id unchanged when it looks
    like one, so callers that pass a real id keep their exact behaviour.
    """
    if "::" in symbol_id:
        return [symbol_id]
    try:
        found = store.find_by_name(symbol_id, limit=limit)
    except Exception:  # noqa: BLE001
        return [symbol_id]
    ids = [s.id for s in found if getattr(s, "id", None)]
    # No match: keep the original so the caller still gets an empty result
    # rather than a different question being answered.
    return ids or [symbol_id]


def _query_incoming_callers(
    store, symbol_id: str, depth: int, max_nodes: int,
) -> list[dict[str, Any]]:
    """Direct + transitive incoming via CALLS+IMPORTS+DEFINED_IN."""
    depth = max(1, min(depth, 6))
    res = store.query(
        f"MATCH path = (a:Symbol)-[:CALLS|IMPORTS*1..{depth}]->(b:Symbol) "
        "WHERE b.id = $id "
        "WITH a, length(path) AS hops "
        "RETURN DISTINCT a.id AS id, a.name AS name, a.kind AS kind, "
        "       a.file AS file, a.start_line AS start_line, "
        "       min(hops) AS hops "
        "ORDER BY hops, a.file LIMIT $limit",
        params={"id": symbol_id, "limit": max_nodes},
    )
    return res


def _query_outgoing_callees(
    store, symbol_id: str, depth: int, max_nodes: int,
) -> list[dict[str, Any]]:
    depth = max(1, min(depth, 6))
    res = store.query(
        f"MATCH path = (a:Symbol)-[:CALLS|IMPORTS*1..{depth}]->(b:Symbol) "
        "WHERE a.id = $id "
        "WITH b, length(path) AS hops "
        "RETURN DISTINCT b.id AS id, b.name AS name, b.kind AS kind, "
        "       b.file AS file, b.start_line AS start_line, "
        "       min(hops) AS hops "
        "ORDER BY hops, b.file LIMIT $limit",
        params={"id": symbol_id, "limit": max_nodes},
    )
    return res


def _repo_id_to_slug(repo_id: str) -> str:
    """Pull the slug out of a repo identifier (URL/slug form)."""
    try:
        from src.sync.git_providers import parse_repo_url
        return parse_repo_url(repo_id).slug
    except Exception:  # noqa: BLE001
        return repo_id.replace("/", "-")


def _repo_graph_exists(slug: str, settings: Settings) -> bool:
    return settings.repo_graph_path(slug).exists()


def _repo_to_summary(repo_id: str, settings: Settings) -> RepoSummary:
    try:
        from src.sync.git_providers import parse_repo_url
        parsed = parse_repo_url(repo_id)
        slug = parsed.slug
        full_path = parsed.full_path
    except Exception:  # noqa: BLE001
        slug = _repo_id_to_slug(repo_id)
        full_path = repo_id

    return _slug_to_summary(slug, settings, full_path)


def _slug_to_summary(
    slug: str, settings: Settings, full_path: str | None = None,
) -> RepoSummary:
    db_path = settings.repo_graph_path(slug)
    indexed = db_path.exists()
    symbol_count = 0
    if indexed:
        try:
            store = make_graph_store(db_path)
            try:
                res = store.query("MATCH (s:Symbol) RETURN count(s) AS c")
                symbol_count = int(res[0]["c"]) if res else 0
            finally:
                store.close()
        except Exception:  # noqa: BLE001
            pass

    return RepoSummary(
        slug=slug,
        full_path=full_path or slug,
        indexed=indexed,
        graph_path=str(db_path) if indexed else None,
        symbol_count=symbol_count,
    )


__all__ = [
    "GroupSummary",
    "RepoSummary",
    "cross_repo_edges",
    "find_callees",
    "find_callers",
    "find_symbol",
    "get_symbol",
    "list_groups",
    "list_repos",
    "query_graph",
]
