"""How much a symbol is used — the ranking signal behind `find` and `map`.

Not PageRank (yet): the score is the NORMALISED IN-DEGREE, the number of
incoming CALLS and IMPORTS edges, scaled to [0, 1] by a log curve so one
symbol everybody calls does not flatten the rest to zero. It is the signal a
reader means by "the important one" when three symbols share a name, it costs
one aggregation query, and it leaves the signature (`symbol_id -> score`) open
for a real iterative PageRank later without touching a caller.

Written onto the graph as `s.rank` at the end of an index pass
(:func:`write_ranks`), so a search can read it without counting edges. The
dev profile's `find` also reads the live in-degree, so a graph indexed before
this existed ranks correctly too.
"""

from __future__ import annotations

import logging
import math

from src.indexing.graph.graph_store import GraphStore

logger = logging.getLogger(__name__)

_WRITE_CHUNK = 500


def compute_pagerank(store: GraphStore, alpha: float = 0.85) -> dict[str, float]:
    """Score every symbol that is used by something: symbol_id -> (0, 1].

    `alpha` is accepted for the day this becomes iterative and is unused now.
    Symbols nothing points at are absent (score 0) rather than listed, which
    keeps the map small on a graph where most symbols are leaves.
    """
    del alpha
    rows = store.query(
        "MATCH (s:Symbol)<-[r:CALLS|IMPORTS]-() "
        "WHERE s.kind <> 'file_module' "
        "RETURN s.id AS id, count(r) AS d"
    )
    degrees = {str(r["id"]): int(r["d"]) for r in rows if r.get("id")}
    if not degrees:
        return {}
    top = math.log1p(max(degrees.values()))
    return {sid: round(math.log1p(d) / top, 4) for sid, d in degrees.items()}


def write_ranks(store: GraphStore) -> int:
    """Persist :func:`compute_pagerank` as `s.rank`. Returns how many symbols
    got one (everything else is reset to 0). Never raises: ranking is a hint for search, and an index that
    succeeded must not be reported as failed because it could not be written."""
    try:
        scores = compute_pagerank(store)
        items = [{"id": k, "rank": v} for k, v in scores.items()]
        # A symbol that lost its last caller must not keep the rank it had: the
        # incremental pass re-runs this over a graph that still carries old values.
        store.query("MATCH (s:Symbol) WHERE s.rank IS NOT NULL AND s.rank <> 0 SET s.rank = 0")
        for i in range(0, len(items), _WRITE_CHUNK):
            store.query(
                "UNWIND $rows AS row MATCH (s:Symbol {id: row.id}) SET s.rank = row.rank",
                params={"rows": items[i:i + _WRITE_CHUNK]},
            )
        return len(items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_ranks_failed err=%s", exc)
        return 0
