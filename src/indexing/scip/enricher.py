"""SCIP enricher — merges ScipIndex data into the existing tree-sitter graph.

Goal: tree-sitter gives structural symbols (functions, classes, lexical refs).
SCIP adds **resolved cross-file references** + types + canonical symbol IDs.

Strategy:
    1. Walk SCIP occurrences per document
    2. If occurrence == DEFINITION — find the matching tree-sitter symbol via
       (file, line) coords + name match
    3. If occurrence == READ/WRITE — create a CALLS-like edge from the caller
       site to the definition

Resolved CALLS edges stage:
    Tree-sitter emits `(caller_id) -[CALLS]-> (raw_target=callee_name)` —
    target unresolved. SCIP knows both ends:
        - DEFINITION occurrence in the source file
        - READ_ACCESS occurrences in the callsites
    We can resolve unresolved tree-sitter edges → strong-confidence edges.

This is a foundation. Real merger logic needs access to the per-repo graph
store + deep symbol identity matching. In the Stage 14 implementation — a
minimal viable abstraction. Production enrichment — V2.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from src.indexing.graph.extractor import ExtractionResult, SymbolInfo
from src.indexing.scip.reader import (
    ScipDocument,
    ScipIndex,
    parse_scip_symbol,
)

logger = logging.getLogger(__name__)


@dataclass
class ScipEnrichmentResult:
    """Stats for a single enrichment pass."""

    documents_processed: int = 0
    definitions_matched: int = 0
    references_resolved: int = 0  # tree-sitter unresolved → resolved
    new_edges_added: int = 0
    files_with_no_match: list[str] = field(default_factory=list)


class ScipEnricher:
    """Merges SCIP data into per-repo extraction results.

    Caller flow:
        # Per-repo extractions from tree-sitter
        extractions: dict[file_path, ExtractionResult] = ...
        # SCIP run
        scip_index = runner.run("python", repo_path).index
        # Enrich
        enricher = ScipEnricher()
        stats = enricher.enrich(extractions, scip_index)
        # extractions is now mutated with resolved edges + extra metadata
    """

    def enrich(
        self,
        extractions: dict[str, ExtractionResult],
        scip_index: ScipIndex,
    ) -> ScipEnrichmentResult:
        """Mutates extractions in place. Returns stats.

        Args:
            extractions: file relative path → tree-sitter ExtractionResult
            scip_index: parsed SCIP data
        """
        result = ScipEnrichmentResult()

        # Build symbol lookup: file → (line → list of SymbolInfo)
        # For a fast match: when a SCIP occurrence is on line N in file X, we
        # look for a tree-sitter symbol with start_line ≈ N+1 in the same file.
        sym_index: dict[str, dict[int, list[SymbolInfo]]] = {}
        for file_path, ext_result in extractions.items():
            line_map: dict[int, list[SymbolInfo]] = defaultdict(list)
            for sym in ext_result.symbols:
                line_map[sym.start_line].append(sym)
            sym_index[file_path] = line_map

        # Build SCIP definition lookup: SCIP symbol id → tree-sitter symbol id
        # for resolving unresolved CALLS edges.
        scip_to_ts: dict[str, str] = {}

        for doc in scip_index.documents:
            result.documents_processed += 1
            self._process_document(doc, sym_index, scip_to_ts, result)

        # Phase 2: resolve tree-sitter unresolved edges via SCIP knowledge
        for ext_result in extractions.values():
            for edge in ext_result.edges:
                if edge.to_id is not None:
                    continue  # already resolved
                if edge.kind != "CALLS":
                    continue  # only CALLS resolution is in scope for the MVP

                # Match raw_target (callee_name) against SCIP-known definitions.
                # Scip-to-ts map: scip_symbol_id → ts_symbol_id. We look for
                # which SCIP symbol has a descriptor that ends with the callee
                # name.
                callee = edge.raw_target or ""
                ts_target = self._find_definition_for_callee(
                    callee, scip_to_ts,
                )
                if ts_target is not None:
                    edge.to_id = ts_target
                    edge.confidence = "strong"  # from SCIP — high confidence
                    result.references_resolved += 1

        logger.info(
            "scip_enriched docs=%d definitions=%d resolved=%d new_edges=%d",
            result.documents_processed, result.definitions_matched,
            result.references_resolved, result.new_edges_added,
        )
        return result

    def _process_document(
        self,
        doc: ScipDocument,
        sym_index: dict[str, dict[int, list[SymbolInfo]]],
        scip_to_ts: dict[str, str],
        result: ScipEnrichmentResult,
    ) -> None:
        """Process one SCIP document — match definitions to tree-sitter syms."""
        ts_lines = sym_index.get(doc.relative_path)
        if ts_lines is None:
            result.files_with_no_match.append(doc.relative_path)
            return

        for occ in doc.occurrences:
            if not occ.is_definition:
                continue
            ts_line = occ.start_line
            candidates = ts_lines.get(ts_line, [])
            if not candidates:
                # Try ±1 line tolerance — SCIP may report the row of the symbol
                # name, tree-sitter — the initial token. A decorator shifts the
                # line too.
                for delta in (-1, 1, -2, 2):
                    candidates = ts_lines.get(ts_line + delta, [])
                    if candidates:
                        break

            if candidates:
                # Use first candidate as best match (refine later if multiple)
                ts_sym = candidates[0]
                scip_to_ts[occ.symbol] = ts_sym.id
                result.definitions_matched += 1

    def _find_definition_for_callee(
        self,
        callee_name: str,
        scip_to_ts: dict[str, str],
    ) -> str | None:
        """Match a tree-sitter callee name (raw symbol) to a SCIP-known definition.

        Heuristic: the SCIP symbol descriptor ends with name + descriptor suffix
        (`name#`, `name().`, etc.). A simple suffix match.
        """
        if not callee_name:
            return None

        for scip_sym, ts_id in scip_to_ts.items():
            parsed = parse_scip_symbol(scip_sym)
            descriptor = parsed.descriptor
            # Common Python descriptors:
            # 'mod/Class#'         — class
            # 'mod/Class#method().' — method
            # 'mod/func().'         — function
            # 'mod/CONST.'          — constant
            #
            # Extracting the last entity name from the descriptor:
            # split by '/' → last segment, strip suffix chars
            if not descriptor:
                continue
            last_part = descriptor.split("/")[-1]
            entity = last_part.rstrip("().# ")
            if entity == callee_name:
                return ts_id

        return None
