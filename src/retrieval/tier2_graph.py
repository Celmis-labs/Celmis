"""Tier 2 — structural navigation over the code graph (FalkorDBLite backend).

v3.0: switched from CGC to our own `FalkorDBLiteStore`. The API stays
compatible with module_prd.py / qa/orchestrator.py:
    - GraphExpansion (as before) — roots, callers, callees, all_symbols, edges
    - GraphRetriever.expand(repo_path, seed_symbols, depth) — also compatible

Specifically for our graph:
    - `seed_symbols` — a list of symbol NAMES (find_by_name)
    - undirected multi-hop BFS via GraphStore.bfs_expand
    - all_symbols, callers, callees are pulled out of reached
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings, get_settings
from src.indexing.graph.extractor import SymbolInfo as GraphSymbolInfo
from src.indexing.graph.graph_store import GraphStore, make_graph_store

logger = logging.getLogger(__name__)


# ─── Compatibility shim for existing API ──────────────────────────────


@dataclass
class _CompatSymbol:
    """Lightweight compatible SymbolInfo (raw graph model has more fields)."""

    name: str
    kind: str
    file: str
    line: int  # alias for start_line — compatibility with module_prd.py
    end_line: int | None = None
    language: str = ""
    signature: str | None = None
    docstring: str | None = None

    @classmethod
    def from_graph(cls, gs: GraphSymbolInfo) -> _CompatSymbol:
        return cls(
            name=gs.name,
            kind=gs.kind,
            file=gs.file,
            line=gs.start_line,
            end_line=gs.end_line,
            language=gs.language,
            signature=gs.signature,
            docstring=gs.docstring,
        )


# Exposed as SymbolInfo for compatibility with module_prd.py
SymbolInfo = _CompatSymbol


@dataclass
class GraphExpansion:
    """Result of tier-2 retrieval — the structure around the seed symbols."""

    roots: list[SymbolInfo] = field(default_factory=list)
    callers: list[SymbolInfo] = field(default_factory=list)
    callees: list[SymbolInfo] = field(default_factory=list)
    all_symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, kind)

    def as_llm_context(self) -> dict:
        """Compact form for the prompt."""
        return {
            "roots": [f"{s.name} @ {s.file}:{s.line}" for s in self.roots],
            "callers": [f"{s.name} @ {s.file}:{s.line}" for s in self.callers],
            "callees": [f"{s.name} @ {s.file}:{s.line}" for s in self.callees],
            "edges_count": len(self.edges),
            "sample_edges": [f"{a} --{k}--> {b}" for a, b, k in self.edges[:20]],
        }

    def all_file_locations(self) -> list[tuple[str, int, int | None]]:
        """List of {file, line, end_line} for Tier 3 reading."""
        out: list[tuple[str, int, int | None]] = []
        for s in self.all_symbols.values():
            # Exclude synthetic file_module symbols — they have no code body
            if s.kind == "file_module":
                continue
            if s.file and s.line:
                out.append((s.file, s.line, s.end_line))
        return out


# ─── Retriever ─────────────────────────────────────────────────────


class GraphRetriever:
    """Build a GraphExpansion starting from the seed symbols.

    Backend: FalkorDBLiteStore (via the GraphStore interface).
    One graph file per repo — opened lazily by repo_slug.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        store: GraphStore | None = None,
        repo_slug: str | None = None,
    ) -> None:
        """
        Args:
            store: explicit GraphStore (for tests). If None — lazily resolved
                   by repo_path on every expand().
            repo_slug: if given — the store is opened right away for this slug.
        """
        self.settings = settings or get_settings()
        self._explicit_store = store
        self._stores: dict[str, GraphStore] = {}
        if repo_slug and store is None:
            self._stores[repo_slug] = self._open_store(repo_slug)

    def _open_store(self, repo_slug: str) -> GraphStore:
        db_path = self.settings.repo_graph_path(repo_slug)
        return make_graph_store(db_path)

    def _store_for(self, repo_path: Path) -> GraphStore:
        """Get or open the store for a repo. Slug = directory name."""
        if self._explicit_store is not None:
            return self._explicit_store
        slug = repo_path.name
        if slug not in self._stores:
            self._stores[slug] = self._open_store(slug)
        return self._stores[slug]

    def expand(
        self,
        repo_path: Path,
        seed_symbols: list[str],
        depth: int | None = None,
    ) -> GraphExpansion:
        """Find symbols by name + multi-hop BFS expansion."""
        depth = depth or self.settings.retrieval_graph_depth
        max_nodes = self.settings.max_graph_nodes_per_query

        exp = GraphExpansion()
        if not seed_symbols:
            return exp

        store = self._store_for(repo_path)

        # 1. Resolve seed names → graph symbol_ids
        seen_seed_ids: set[str] = set()
        seed_ids: list[str] = []
        for name in seed_symbols:
            if not name:
                continue
            try:
                matches = store.find_by_name(name, limit=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("find_by_name_failed name=%s err=%s", name, exc)
                continue
            for m in matches:
                if m.id not in seen_seed_ids:
                    seen_seed_ids.add(m.id)
                    seed_ids.append(m.id)
                    cs = SymbolInfo.from_graph(m)
                    exp.roots.append(cs)
                    exp.all_symbols[m.id] = cs

        if not seed_ids:
            return exp

        # 2. BFS expansion from the graph store (undirected — as in Phase 5d)
        try:
            graph_exp = store.bfs_expand(
                seed_ids=seed_ids,
                depth=depth,
                max_nodes=max_nodes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bfs_expand_failed err=%s", exc)
            return exp

        # 3. Convert reached → exp.all_symbols + approximate classification
        # callers/callees: we use the edges to determine the direction.
        # If edge from_id ∈ seeds and to_id == reached → callee
        # If to_id ∈ seeds and from_id == reached → caller
        seed_set = set(seed_ids)

        for sym in graph_exp.reached:
            cs = SymbolInfo.from_graph(sym)
            if sym.id not in exp.all_symbols:
                exp.all_symbols[sym.id] = cs

        for from_id, to_id, kind in graph_exp.edges:
            exp.edges.append((from_id, to_id, kind))
            # Classification: only on the 1-hop between seed and reached
            if from_id in seed_set and to_id in exp.all_symbols:
                target = exp.all_symbols[to_id]
                if target not in exp.callees:
                    exp.callees.append(target)
            elif to_id in seed_set and from_id in exp.all_symbols:
                source = exp.all_symbols[from_id]
                if source not in exp.callers:
                    exp.callers.append(source)

        # 4. Post-expansion: for every reached file_module we add its non-fm
        # symbols. Without this BFS gets stuck on the file_module hubs and does
        # not give the LLM the real function/method bodies that are needed for
        # technical answers.
        file_module_ids = [
            sid for sid, s in exp.all_symbols.items() if s.kind == "file_module"
        ]
        budget = max_nodes - len(exp.all_symbols)
        if file_module_ids and budget > 0:
            try:
                rows = store.query(
                    "MATCH (s:Symbol)-[:DEFINED_IN]->(fm:Symbol) "
                    "WHERE fm.id IN $ids AND s.kind <> 'file_module' "
                    "RETURN s.id AS id, s.name AS name, s.kind AS kind, "
                    "       s.file AS file, s.start_line AS start_line, "
                    "       s.end_line AS end_line, s.language AS language, "
                    "       s.signature AS signature, s.docstring AS docstring, "
                    "       s.is_exported AS is_exported, s.module AS module "
                    "LIMIT $cap",
                    params={"ids": file_module_ids, "cap": budget},
                )
                for r in rows:
                    sid = str(r.get("id", ""))
                    if not sid or sid in exp.all_symbols:
                        continue
                    from src.indexing.graph.extractor import SymbolInfo as GSym
                    gsym = GSym(
                        id=sid,
                        name=str(r.get("name", "")),
                        kind=str(r.get("kind", "")),
                        file=str(r.get("file", "")),
                        start_line=int(r.get("start_line") or 0),
                        end_line=int(r["end_line"]) if r.get("end_line") is not None else None,
                        language=str(r.get("language", "")),
                        signature=r.get("signature"),
                        docstring=r.get("docstring"),
                        is_exported=bool(r.get("is_exported", False)),
                        module=r.get("module"),
                    )
                    exp.all_symbols[sid] = SymbolInfo.from_graph(gsym)
            except Exception as exc:  # noqa: BLE001
                logger.warning("file_module_expand_failed err=%s", exc)

        logger.info(
            "graph_expand seeds=%d roots=%d reached=%d edges=%d (incl. fm-content)",
            len(seed_symbols),
            len(exp.roots),
            len(exp.all_symbols) - len(exp.roots),
            len(exp.edges),
        )
        return exp

    def close(self) -> None:
        """Close all open stores (for shutdown)."""
        for store in self._stores.values():
            # Shutdown path: one unhealthy handle must not strand the others.
            with contextlib.suppress(Exception):
                store.close()
        self._stores.clear()
