"""PageRank over the graph — for ranking symbols in the retriever.

Optional: not critical for the MVP — can be postponed if the retriever
ranks well enough without it.

Implementation: via the FalkorDB GRAPH algorithm extension (CALL algo.pageRank)
or our own iterative cypher. We do not use NetworkX — it keeps the whole
graph in RAM, which does not scale for 25k+ symbols.

TODO Phase 5+ (~150 LoC).
"""

from __future__ import annotations

from src.indexing.graph.graph_store import GraphStore


def compute_pagerank(store: GraphStore, alpha: float = 0.85) -> dict[str, float]:
    """Compute PageRank over all symbols in the graph.

    Returns: symbol_id → score in [0, 1].
    """
    # TODO: FalkorDB algo.pageRank via cypher
    return {}
