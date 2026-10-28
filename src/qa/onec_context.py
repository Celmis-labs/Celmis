"""The whole algorithm of a 1C question, not one function of it.

A consultant asks "explain the cutting logic", and the symbols the words land on
are a metadata object (through its synonym), a module, or the one method with
the word in its name. The answer lives in that object's other methods and in
what they call. Two steps, both on rows the caller has ALREADY filtered through
the access decision (a hidden file is not in ``rows``, so it is never expanded
into, never walked to — nothing here reads a file or checks a path):

* ``expand_container`` — the methods of a matched object or module, at a
  decayed score, the exported and the question-relevant ones first;
* ``walk`` — a budgeted breadth-first walk over CALLS edges, downward for an
  explanation or a trace, upward for "what depends on it".

The module reads the graph through ``neighbours`` only; the Q&A retriever owns
the store, the access rules and the excerpts.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping

from src.qa.query_terms import SymbolHit, fold

logger = logging.getLogger(__name__)

# Expanding an object or module into its methods.
EXPAND_DECAY = 0.8        # a method scores this share of the object that led to it
EXPAND_PER_OBJECT = 6     # methods taken from one object or module
EXPAND_OBJECTS = 3        # matched objects/modules that are expanded per question
EXPAND_MIN_SHARE = 0.5    # …if their score is at least this share of the best match
EXPAND_MAX_TOTAL = 12     # expanded methods per question, all objects together

# Walking the calls. The budget is in characters because that is what the
# prompt is limited by; a body is not read here, so its size is estimated from
# its lines (``CHARS_PER_LINE``) and capped like the excerpt that will show it.
WALK_DEPTH = {"explain": 2, "trace": 3, "impact": 2, "data": 2}
WALK_SEEDS = 4            # top matches the walk starts from
WALK_FANOUT = 5           # neighbours followed from one method
WALK_MAX_SYMBOLS = 24
WALK_MAX_CHARS = 48_000   # all walked bodies together (the prompt's code budget is 120k)
BODY_MAX_CHARS = 6_000    # one body; the same cap as a matched method's excerpt
CHARS_PER_LINE = 60

CALLABLE_KINDS = ("method", "function")
_EXPORTED = re.compile(r"\b(?:Экспорт|Export)\b", re.IGNORECASE)
_COMMON_MODULES = "CommonModules."

#: ``(walked_id, reached_id)`` pairs for the ids it is given.
Neighbours = Callable[[list[str]], list[tuple[str, str]]]


def is_container(hit: SymbolHit) -> bool:
    """A matched metadata object or module file: a place, not an algorithm."""
    return hit.kind == "file_module" or hit.kind.startswith("vendor.onec.")


def _exported(row: Mapping) -> bool:
    return bool(_EXPORTED.search(str(row.get("signature") or "")))


def _own_matches(row: Mapping, stems: Iterable[str]) -> int:
    """Stems found in the method's own name or documentation."""
    name = str(row.get("name") or "").rsplit(".", 1)[-1]
    text = fold(f"{name} {row.get('docstring') or ''}")
    return sum(1 for s in stems if s in text)


def _hit(row: Mapping, score: float) -> SymbolHit:
    start = int(row.get("start_line") or 0)
    return SymbolHit(
        id=str(row.get("id") or ""), name=str(row.get("name") or ""),
        kind=str(row.get("kind") or ""), file=str(row.get("file") or ""),
        start_line=start, end_line=int(row.get("end_line") or start), score=score,
        docstring=str(row.get("docstring") or ""), signature=str(row.get("signature") or ""))


def _members(container: SymbolHit, rows: Iterable[Mapping]) -> list[Mapping]:
    """Methods that belong to a module (same file) or to an object (their
    container is named ``<Type>.<Object>.<Module>``; a common module's methods
    are named after the module alone)."""
    callable_rows = (r for r in rows if r.get("kind") in CALLABLE_KINDS)
    if container.kind == "file_module":
        return [r for r in callable_rows if r.get("file") == container.file]
    prefixes = [fold(container.name) + "."]
    if container.name.startswith(_COMMON_MODULES):
        prefixes.append(fold(container.name[len(_COMMON_MODULES):]) + ".")
    return [r for r in callable_rows if fold(str(r.get("name") or "")).startswith(tuple(prefixes))]


def expand_container(
    container: SymbolHit, rows: Iterable[Mapping], stems: Iterable[str],
    *, taken: Collection[str] = (), limit: int = EXPAND_PER_OBJECT,
) -> list[SymbolHit]:
    """The methods of a matched object or module, best first, at
    ``container.score * EXPAND_DECAY``. Methods whose own name or docs carry the
    question's stems come before those that do not, exported ones before
    private, then source order; ``taken`` ids (already matched on their own)
    are left out."""
    stems = list(stems)
    members = [r for r in _members(container, rows) if r.get("id") not in taken]
    members.sort(key=lambda r: (-_own_matches(r, stems), not _exported(r),
                                str(r.get("file") or ""), int(r.get("start_line") or 0)))
    base = container.score * EXPAND_DECAY
    # a hair of difference per rank keeps the order above after a sort by score
    return [_hit(r, base * (1 - 0.01 * i)) for i, r in enumerate(members[:limit])]


def body_cost(hit: SymbolHit, body_max: int = BODY_MAX_CHARS) -> int:
    """Estimated characters of a method body in the prompt."""
    lines = max(1, hit.end_line - hit.start_line + 1)
    return min(body_max, lines * CHARS_PER_LINE)


def call_edges(store, ids: list[str], *, callers: bool = False) -> list[tuple[str, str]]:
    """CALLS edges leaving ``ids`` (the callees), or arriving at them (the
    callers, with ``callers=True``), as ``(walked_id, reached_id)``."""
    if not ids:
        return []
    arrow = "(b:Symbol)-[:CALLS]->(a:Symbol)" if callers else "(a:Symbol)-[:CALLS]->(b:Symbol)"
    rows = store.query(
        f"MATCH {arrow} WHERE a.id IN $ids RETURN a.id AS src, b.id AS dst LIMIT 2000",
        {"ids": ids})
    return [(str(r["src"]), str(r["dst"])) for r in rows]


def data_flow_edges(store, ids: list[str]) -> list[tuple[str, str]]:
    """Where READS/WRITES edges join the walk of a ``data`` question, as
    ``(walked_id, reached_id)`` pairs of method ids like :func:`call_edges`.

    A data question follows the data, not the calls: from a method to the
    methods that write what it reads or writes (where its data comes from) and
    to those that read what it writes (where it goes). Two methods that only
    read the same register share nothing worth reading. A repository indexed
    before these edges existed has none, and its data question is walked like
    an explanation."""
    if not ids:
        return []
    rows = store.query(
        "MATCH (a:Symbol)-[:READS|WRITES]->(o:Symbol)<-[:WRITES]-(b:Symbol) "
        "WHERE a.id IN $ids AND b.id <> a.id RETURN DISTINCT a.id AS src, b.id AS dst LIMIT 2000",
        {"ids": ids})
    rows += store.query(
        "MATCH (a:Symbol)-[:WRITES]->(o:Symbol)<-[:READS]-(b:Symbol) "
        "WHERE a.id IN $ids AND b.id <> a.id RETURN DISTINCT a.id AS src, b.id AS dst LIMIT 2000",
        {"ids": ids})
    return list(dict.fromkeys((str(r["src"]), str(r["dst"])) for r in rows))


def neighbours(store, intent: str) -> Neighbours:
    """The edge reader a walk of ``intent`` uses: callers for an impact
    question, callees (plus data flow for a data question) otherwise."""
    if intent == "impact":
        return lambda ids: call_edges(store, ids, callers=True)
    if intent == "data":
        return lambda ids: call_edges(store, ids) + data_flow_edges(store, ids)
    return lambda ids: call_edges(store, ids)


def walk(
    seeds: list[SymbolHit], rows_by_id: Mapping[str, Mapping], reach: Neighbours,
    stems: Iterable[str], *, depth: int, taken: Collection[str] = (),
    fanout: int = WALK_FANOUT, budget: int = WALK_MAX_CHARS,
    max_symbols: int = WALK_MAX_SYMBOLS, body_max: int = BODY_MAX_CHARS,
) -> list[SymbolHit]:
    """Methods reached from ``seeds`` in at most ``depth`` hops, nearest first.

    Only ids in ``rows_by_id`` are followed — the caller's access-filtered rows —
    and only methods whose qualified name is unique in them: the resolver links
    a call to the first of several same-named methods, and following that guess
    would put an unrelated body in front of the model. Each method follows at
    most ``fanout`` neighbours (question-relevant, exported, then source order);
    the walk ends when the estimated bodies would pass ``budget`` characters or
    ``max_symbols`` methods."""
    stems = list(stems)
    names = Counter(fold(str(r.get("name") or ""))
                    for r in rows_by_id.values() if r.get("kind") in CALLABLE_KINDS)
    seen = set(taken) | {s.id for s in seeds}
    score_of = {s.id: s.score for s in seeds}
    frontier = [s.id for s in seeds]
    out: list[SymbolHit] = []
    spent = 0
    for hop in range(1, depth + 1):
        if not frontier:
            break
        by_source: dict[str, list[str]] = {}
        for src, dst in reach(frontier):
            by_source.setdefault(src, []).append(dst)
        nxt: list[str] = []
        for src in frontier:
            options = []
            for dst in dict.fromkeys(by_source.get(src, [])):
                row = rows_by_id.get(dst)
                if (dst in seen or row is None or row.get("kind") not in CALLABLE_KINDS
                        or names[fold(str(row.get("name") or ""))] != 1):
                    continue
                options.append(row)
            options.sort(key=lambda r: (-_own_matches(r, stems), not _exported(r),
                                        str(r.get("file") or ""), int(r.get("start_line") or 0)))
            for row in options[:fanout]:
                hit = _hit(row, score_of[src] * 0.5)
                cost = body_cost(hit, body_max)
                if spent + cost > budget or len(out) >= max_symbols:
                    logger.debug("walk_budget_reached hop=%d symbols=%d chars=%d",
                                 hop, len(out), spent)
                    return out
                spent += cost
                seen.add(hit.id)
                score_of[hit.id] = hit.score
                out.append(hit)
                nxt.append(hit.id)
        frontier = nxt
    return out
