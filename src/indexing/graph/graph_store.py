"""GraphStore — FalkorDBLite backend (BSD-3, embedded Redis + FalkorDB).

NetworkX (in-memory) is deliberately NOT supported as a backend: for
acme-frontend we expect ~25k symbols + ~100k edges, and in-memory storage
without persistence does not scale and loses the work between runs.

Cypher dialect: the OpenCypher subset that FalkorDB supports. Key patterns:
    - UNWIND $batch AS row MERGE (s:Symbol {id: row.id}) SET s += row
    - MATCH path = (a)-[:CALLS|IMPORTS*1..3]->(b) RETURN length(path)
    - CREATE INDEX FOR (n:Symbol) ON (n.id)

API (research, April 2026):
    QueryResult attrs: header, result_set, nodes_created, relationships_created,
                      properties_set, run_time_ms, indices_created, ...
    NB: dynamic relationship kinds via `[r:$kind]` do NOT work — we use
    f-string composition with a whitelist (ALLOWED_EDGE_KINDS).

Phase 4 — implemented.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.kinds import CORE_EDGE_KINDS

logger = logging.getLogger(__name__)


# Whitelist edge kinds — every value is embedded into Cypher as a literal,
# so it MUST be checked against the whitelist (Cypher injection guard).
#
# The whitelist is assembled from CORE_EDGE_KINDS (kinds.py) + legacy edge
# kinds:
#     TEMPLATE_REF — Vue templates (Phase 5b, existing)
#     REFERENCES   — generic ad-hoc reference edges
# Cross-repo:
#     REFERENCES_REPO, BUILD_CONTEXT — Stage 6 cross-repo materialization
#
# Extension (May 2026): support for edges from all 9 extractors (Stage 2.B + 4).
ALLOWED_EDGE_KINDS = frozenset(
    set(CORE_EDGE_KINDS) | {
        "TEMPLATE_REF", "REFERENCES",
        "REFERENCES_REPO", "BUILD_CONTEXT",
    }
)

# Default batch size for UNWIND — the research said 1k-5k, but the redislite
# subprocess crashes on UNWIND batches of ~1k elements (especially with
# MATCH+MERGE on both sides of the edge). 200 is stable on 25k symbols /
# 100k edges.
DEFAULT_BATCH_SIZE = 200


# ─── data structures ───────────────────────────────────────────────


@dataclass
class GraphExpansion:
    """Result of a BFS expansion from the seed symbols."""

    roots: list[SymbolInfo] = field(default_factory=list)
    reached: list[SymbolInfo] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from_id, to_id, kind)
    truncated: bool = False  # True if the max_nodes limit was reached


# ─── interface ─────────────────────────────────────────────────────


class GraphStore(ABC):
    """Contract of the graph store."""

    @abstractmethod
    def add_symbol(self, sym: SymbolInfo) -> None: ...

    @abstractmethod
    def add_symbols_batch(self, syms: list[SymbolInfo]) -> int: ...

    def delete_by_file(self, file_path: str) -> int:
        """Optional — implementations without incremental support may
        raise NotImplementedError. Default: no-op returning 0."""
        return 0

    def delete_by_files(self, paths: list[str]) -> int:
        return 0

    @abstractmethod
    def add_edge(self, edge: EdgeInfo) -> None: ...

    @abstractmethod
    def add_edges_batch(self, edges: list[EdgeInfo]) -> int: ...

    @abstractmethod
    def get_symbol(self, symbol_id: str) -> SymbolInfo | None: ...

    @abstractmethod
    def find_by_name(self, name: str, limit: int = 10) -> list[SymbolInfo]: ...

    @abstractmethod
    def bfs_expand(
        self,
        seed_ids: list[str],
        depth: int = 2,
        max_nodes: int = 200,
        edge_kinds: list[str] | None = None,
    ) -> GraphExpansion: ...

    @abstractmethod
    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Raw Cypher → list[dict]. Only OpenCypher-compatible syntax is supported."""

    @abstractmethod
    def commit(self) -> None:
        """Sync flush to disk (RDB save). Expensive — call rarely."""

    @abstractmethod
    def close(self) -> None: ...


# ─── FalkorDBLite implementation ────────────────────────────────────


# We store these fields in the properties of the Symbol node.
# Ports SymbolInfo dataclass → Cypher map (without None fields).
_SYMBOL_PROPS = (
    "id", "name", "kind", "file", "start_line", "end_line",
    "language", "signature", "docstring", "is_exported", "module",
)


def _symbol_to_props(sym: SymbolInfo) -> dict[str, Any]:
    """SymbolInfo → dict suitable for an UNWIND batch (without None)."""
    out: dict[str, Any] = {}
    for k in _SYMBOL_PROPS:
        v = getattr(sym, k, None)
        if v is not None:
            out[k] = v
    return out


def _row_to_symbol(row: list[Any], cols: list[str]) -> SymbolInfo:
    """Cypher row + header → SymbolInfo. Missing fields → SymbolInfo defaults."""
    d = dict(zip(cols, row, strict=True))
    return SymbolInfo(
        id=str(d.get("id", "")),
        name=str(d.get("name", "")),
        kind=str(d.get("kind", "")),
        file=str(d.get("file", "")),
        start_line=int(d.get("start_line") or 0),
        end_line=int(d["end_line"]) if d.get("end_line") is not None else None,
        language=str(d.get("language", "")),
        signature=d.get("signature"),
        docstring=d.get("docstring"),
        is_exported=bool(d.get("is_exported", False)),
        module=d.get("module"),
    )


def _result_to_dicts(result: Any) -> list[dict]:
    """QueryResult → list[dict] using the header column names."""
    cols = [h[1] for h in result.header]
    return [dict(zip(cols, row, strict=True)) for row in result.result_set]


def _validate_edge_kinds(kinds: list[str] | None) -> list[str]:
    """Check against the whitelist — protection against Cypher injection
    via f-string."""
    if not kinds:
        return list(ALLOWED_EDGE_KINDS)
    bad = [k for k in kinds if k not in ALLOWED_EDGE_KINDS]
    if bad:
        raise ValueError(f"Unknown edge kinds: {bad}. Allowed: {sorted(ALLOWED_EDGE_KINDS)}")
    return kinds


class FalkorDBLiteStore(GraphStore):
    """Backend on FalkorDBLite (via redislite.falkordb_client).

    One writer process per db file (file-locked).
    Reads are thread-safe, but limited to 1 process because of redislite.
    """

    def __init__(self, db_path: Path, graph_name: str = "code") -> None:
        from redislite.falkordb_client import FalkorDB

        self.db_path = Path(db_path)
        self.graph_name = graph_name
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fdb = FalkorDB(str(self.db_path))
        self._g = self._fdb.select_graph(graph_name)
        self._closed = False
        self._ensure_indexes()
        logger.info("falkordb_opened path=%s graph=%s", self.db_path, graph_name)

    # ─── indexes ───────────────────────────────────────────────────

    def _ensure_indexes(self) -> None:
        """Idempotent — `CREATE INDEX` fails if it already exists, we catch it."""
        for cypher in (
            "CREATE INDEX FOR (n:Symbol) ON (n.id)",
            "CREATE INDEX FOR (n:Symbol) ON (n.name)",
            "CREATE INDEX FOR (n:Symbol) ON (n.file)",
        ):
            try:
                self._g.query(cypher)
            except Exception as e:  # noqa: BLE001
                # FalkorDB raises ResponseError if the index exists — normal
                msg = str(e).lower()
                if "already exists" not in msg and "already indexed" not in msg:
                    logger.warning("index_create_failed cypher=%s err=%s", cypher, e)

    # ─── add symbols ───────────────────────────────────────────────

    def add_symbol(self, sym: SymbolInfo) -> None:
        self.add_symbols_batch([sym])

    def delete_by_file(self, file_path: str) -> int:
        """Delete every symbol whose `file_path` matches. DETACH DELETE
        removes incident edges too. Returns approximate deleted count.

        Called by the incremental indexer on git-diff `M` / `D` before
        re-extracting the current file contents.
        """
        try:
            res = self._g.query(
                "MATCH (s:Symbol {file_path: $fp}) "
                "WITH s, count(s) AS c "
                "DETACH DELETE s "
                "RETURN sum(c) AS deleted",
                params={"fp": file_path},
            )
            rows = list(res.result_set) if hasattr(res, "result_set") else []
            n = int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0
            logger.debug("symbols_deleted_by_file path=%s n=%d", file_path, n)
            return n
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_by_file_failed path=%s err=%s", file_path, exc)
            return 0

    def delete_by_files(self, paths: list[str]) -> int:
        """Bulk delete. Runs per-file — FalkorDB Cypher doesn't UNWIND
        DETACH DELETE cleanly."""
        return sum(self.delete_by_file(p) for p in paths)

    def add_symbols_batch(self, syms: list[SymbolInfo]) -> int:
        """UNWIND-based batch upsert. Returns the inserted/updated count."""
        if not syms:
            return 0
        total = 0
        for chunk in _chunks(syms, DEFAULT_BATCH_SIZE):
            batch = [_symbol_to_props(s) for s in chunk]
            res = self._g.query(
                "UNWIND $batch AS row "
                "MERGE (s:Symbol {id: row.id}) "
                "SET s += row",
                params={"batch": batch},
            )
            total += int(res.nodes_created or 0)
        logger.debug("symbols_batch_upserted nodes_created=%d total=%d", total, len(syms))
        return total

    # ─── add edges ─────────────────────────────────────────────────

    def add_edge(self, edge: EdgeInfo) -> None:
        if edge.to_id is None:
            return  # unresolved — skip it
        self.add_edges_batch([edge])

    def add_edges_batch(self, edges: list[EdgeInfo]) -> int:
        """UNWIND-based batch edge MERGE. Edges are grouped by kind
        (because `:$kind` is not supported — one Cypher per kind is needed).
        """
        if not edges:
            return 0
        # Filter out unresolved
        resolved = [e for e in edges if e.to_id is not None]
        if not resolved:
            return 0

        # Group by kind (whitelist-validated) — guard against Cypher injection
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for e in resolved:
            if e.kind not in ALLOWED_EDGE_KINDS:
                logger.warning("edge_kind_skipped kind=%s", e.kind)
                continue
            by_kind.setdefault(e.kind, []).append({
                "f": e.from_id,
                "t": e.to_id,
                "confidence": e.confidence,
            })

        total = 0
        for kind, batch in by_kind.items():
            # Drop confidence from the payload (to reduce payload + Cypher
            # complexity). FalkorDB ON CREATE/MATCH SET crashes redislite on
            # 200+ batches — confidence on an edge is not used in the
            # retriever, only in resolver-stats.
            simple_batch = [{"f": e["f"], "t": e["t"]} for e in batch]
            for chunk in _chunks(simple_batch, DEFAULT_BATCH_SIZE):
                # kind is already whitelisted, safe to embed into Cypher
                res = self._g.query(
                    f"UNWIND $batch AS e "
                    f"MATCH (a:Symbol {{id: e.f}}), (b:Symbol {{id: e.t}}) "
                    f"MERGE (a)-[:{kind}]->(b)",
                    params={"batch": chunk},
                )
                total += int(res.relationships_created or 0)
        logger.debug("edges_batch_upserted rels_created=%d", total)
        return total

    # ─── lookups ───────────────────────────────────────────────────

    def get_symbol(self, symbol_id: str) -> SymbolInfo | None:
        res = self._g.ro_query(
            "MATCH (s:Symbol {id: $id}) "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line, "
            "       s.language AS language, s.signature AS signature, "
            "       s.docstring AS docstring, s.is_exported AS is_exported, "
            "       s.module AS module "
            "LIMIT 1",
            params={"id": symbol_id},
        )
        if not res.result_set:
            return None
        cols = [h[1] for h in res.header]
        return _row_to_symbol(res.result_set[0], cols)

    def find_by_name(self, name: str, limit: int = 10) -> list[SymbolInfo]:
        res = self._g.ro_query(
            "MATCH (s:Symbol {name: $name}) "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line, "
            "       s.language AS language, s.signature AS signature, "
            "       s.docstring AS docstring, s.is_exported AS is_exported, "
            "       s.module AS module "
            "LIMIT $limit",
            params={"name": name, "limit": limit},
        )
        cols = [h[1] for h in res.header]
        return [_row_to_symbol(row, cols) for row in res.result_set]

    def find_by_name_like(self, q: str, limit: int = 20) -> list[SymbolInfo]:
        """Case-insensitive substring lookup — the UI search box path
        (find_by_name stays exact for MCP tool back-compat)."""
        res = self._g.ro_query(
            "MATCH (s:Symbol) WHERE toLower(s.name) CONTAINS toLower($q) "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line, "
            "       s.language AS language, s.signature AS signature, "
            "       s.docstring AS docstring, s.is_exported AS is_exported, "
            "       s.module AS module "
            "LIMIT $limit",
            params={"q": q, "limit": limit},
        )
        cols = [h[1] for h in res.header]
        return [_row_to_symbol(row, cols) for row in res.result_set]

    def symbols_in_path(self, prefix: str, limit: int = 200) -> list[SymbolInfo]:
        """Every symbol declared under a directory, biggest first.

        Module discovery is path-based and never fills `Module.symbols`, so
        nothing downstream knew what a module actually declares. The graph does
        — this is how the two get reconnected.

        `file_module` nodes are excluded: they are one pseudo-symbol per file
        and would crowd out the functions and classes a reader cares about.
        """
        pattern = prefix.rstrip("/") + "/"
        res = self._g.ro_query(
            "MATCH (s:Symbol) "
            "WHERE s.kind <> 'file_module' AND s.file STARTS WITH $prefix "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line, "
            "       s.language AS language, s.signature AS signature, "
            "       s.docstring AS docstring, s.is_exported AS is_exported, "
            "       s.module AS module "
            "ORDER BY s.end_line - s.start_line DESC "
            "LIMIT $limit",
            params={"prefix": pattern, "limit": limit},
        )
        cols = [h[1] for h in res.header]
        return [_row_to_symbol(row, cols) for row in res.result_set]

    # ─── multi-hop expansion ───────────────────────────────────────

    def bfs_expand(
        self,
        seed_ids: list[str],
        depth: int = 2,
        max_nodes: int = 200,
        edge_kinds: list[str] | None = None,
    ) -> GraphExpansion:
        """Iterative BFS — one hop at a time with early termination.

        Phase 12 fix: variable-length cypher (`*1..N`) on hot symbols
        (axiosFactory, Detail and so on) expands exponentially many paths
        before the LIMIT, which gives 15+ minutes per query. Iterative BFS
        controls the bandwidth — max_nodes is checked after every hop.

        Args:
            seed_ids: starting symbol ids.
            depth: max hops (1..N).
            max_nodes: limit on reached symbols (roots included).
            edge_kinds: edge filter; None → all ALLOWED_EDGE_KINDS.
        """
        if not seed_ids:
            return GraphExpansion()

        depth = max(1, min(depth, 10))  # sanity bound
        kinds = _validate_edge_kinds(edge_kinds)
        kind_pattern = "|".join(kinds)  # whitelist-validated → safe in f-string

        exp = GraphExpansion()

        # 1) roots — detailed information about the seed symbols
        roots_res = self._g.ro_query(
            "MATCH (s:Symbol) WHERE s.id IN $ids "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line, "
            "       s.language AS language, s.signature AS signature, "
            "       s.docstring AS docstring, s.is_exported AS is_exported, "
            "       s.module AS module",
            params={"ids": seed_ids},
        )
        cols = [h[1] for h in roots_res.header]
        exp.roots = [_row_to_symbol(row, cols) for row in roots_res.result_set]

        seen_ids: set[str] = {s.id for s in exp.roots}
        frontier: list[str] = [s.id for s in exp.roots]
        edges_seen: set[tuple[str, str, str]] = set()

        # 2) iterative BFS, one hop at a time. Every hop is bounded by a LIMIT.
        for _hop in range(depth):
            if not frontier or len(seen_ids) >= max_nodes:
                if len(seen_ids) >= max_nodes:
                    exp.truncated = True
                break

            # Per-hop budget — how much we can still add
            budget = max_nodes - len(seen_ids)
            if budget <= 0:
                exp.truncated = True
                break

            # Limit-per-hop: so that a single hop does not eat the whole budget
            # if the frontier is hot. We take more than the budget to have some
            # slack, but not unbounded.
            per_hop_cap = min(budget * 4, 2000)

            res = self._g.ro_query(
                f"MATCH (a:Symbol)-[r:{kind_pattern}]-(b:Symbol) "
                "WHERE a.id IN $frontier "
                "RETURN DISTINCT "
                "  b.id AS id, b.name AS name, b.kind AS kind, b.file AS file, "
                "  b.start_line AS start_line, b.end_line AS end_line, "
                "  b.language AS language, b.signature AS signature, "
                "  b.docstring AS docstring, b.is_exported AS is_exported, "
                "  b.module AS module, "
                "  a.id AS from_id, type(r) AS kind_type "
                "LIMIT $cap",
                params={"frontier": frontier, "cap": per_hop_cap},
            )
            cols = [h[1] for h in res.header]
            new_frontier: list[str] = []
            for row in res.result_set:
                d = dict(zip(cols, row, strict=True))
                bid = str(d.get("id") or "")
                if not bid:
                    continue
                # Edge accumulate
                from_id = str(d.get("from_id") or "")
                kind_type = str(d.get("kind_type") or "")
                if from_id:
                    edge_key = (from_id, bid, kind_type)
                    if edge_key not in edges_seen:
                        edges_seen.add(edge_key)
                # New node
                if bid not in seen_ids:
                    if len(seen_ids) >= max_nodes:
                        exp.truncated = True
                        break
                    seen_ids.add(bid)
                    sym = _row_to_symbol(row, cols)
                    exp.reached.append(sym)
                    new_frontier.append(bid)

            frontier = new_frontier
            if exp.truncated:
                break

        exp.edges = list(edges_seen)

        logger.info(
            "bfs_expand seeds=%d roots=%d reached=%d edges=%d truncated=%s",
            len(seed_ids), len(exp.roots), len(exp.reached), len(exp.edges), exp.truncated,
        )
        return exp

    # ─── raw cypher escape hatch ───────────────────────────────────

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        res = self._g.query(cypher, params=params or {})
        return _result_to_dicts(res)

    # ─── lifecycle ─────────────────────────────────────────────────

    def commit(self) -> None:
        """Force RDB snapshot to disk. Expensive — call at the end of an
        index pass."""
        try:
            self._fdb.connection.bgsave()
        except Exception as e:  # noqa: BLE001
            logger.warning("bgsave_failed err=%s", e)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._fdb.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("close_failed err=%s", e)
        self._closed = True

    def __enter__(self) -> FalkorDBLiteStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ─── helpers ───────────────────────────────────────────────────────


def _chunks(items, size: int):
    """Yield successive size-chunks from items."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ─── factory ───────────────────────────────────────────────────────


def make_graph_store(db_path: Path) -> GraphStore:
    """Factory — returns a FalkorDBLiteStore."""
    return FalkorDBLiteStore(db_path)
