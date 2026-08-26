"""Tests для ScipEnricher — merging SCIP data у tree-sitter graph."""

from __future__ import annotations

import pytest

from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolInfo
from src.indexing.scip.enricher import ScipEnricher
from src.indexing.scip.reader import (
    ScipDocument,
    ScipIndex,
    ScipOccurrence,
)


@pytest.fixture
def enricher() -> ScipEnricher:
    return ScipEnricher()


def _make_extraction(file: str, symbols: list[SymbolInfo]) -> ExtractionResult:
    return ExtractionResult(symbols=symbols, edges=[])


def _make_def_occurrence(
    symbol: str, line: int, file_path: str = "src/foo.py",
) -> ScipOccurrence:
    """Definition occurrence на specific line (0-indexed у SCIP)."""
    return ScipOccurrence(
        symbol=symbol,
        range=(line, 0, line, 10),
        symbol_roles=1,  # DEFINITION
    )


# ─── Empty / no match ──────────────────────────────────────────────


class TestEmptyEnrichment:
    def test_empty_index(self, enricher: ScipEnricher) -> None:
        result = enricher.enrich({}, ScipIndex())
        assert result.documents_processed == 0
        assert result.definitions_matched == 0

    def test_no_tree_sitter_match(self, enricher: ScipEnricher) -> None:
        """SCIP doc для file що НЕ extracted tree-sitter — у no_match list."""
        scip = ScipIndex()
        scip.documents.append(ScipDocument(
            relative_path="ghost.py",
            occurrences=[_make_def_occurrence("scip-python . . . ghost.foo().", 0)],
        ))

        result = enricher.enrich({}, scip)
        assert result.documents_processed == 1
        assert result.definitions_matched == 0
        assert "ghost.py" in result.files_with_no_match


# ─── Definition matching ──────────────────────────────────────────


class TestDefinitionMatching:
    def test_match_by_line(self, enricher: ScipEnricher) -> None:
        """SCIP def на line 5 (SCIP 0-indexed → ts 1-indexed = 6) → match."""
        ts_sym = SymbolInfo(
            id="src/foo.py::my_func", name="my_func", kind="function",
            file="src/foo.py", start_line=6,  # tree-sitter 1-indexed
            language="python",
        )
        extractions = {"src/foo.py": _make_extraction("src/foo.py", [ts_sym])}

        scip = ScipIndex()
        scip.documents.append(ScipDocument(
            relative_path="src/foo.py",
            occurrences=[
                _make_def_occurrence("scip-python . . . foo/my_func().", 5),
                # SCIP line 5 (0-indexed) → ts line 6 ✓
            ],
        ))

        result = enricher.enrich(extractions, scip)
        assert result.documents_processed == 1
        assert result.definitions_matched == 1

    def test_match_with_line_tolerance(self, enricher: ScipEnricher) -> None:
        """SCIP line off-by-one (decorator-decorated function) — ±1 tolerance."""
        ts_sym = SymbolInfo(
            id="x", name="my_func", kind="function",
            file="src/foo.py", start_line=10,  # actual ts line
            language="python",
        )
        extractions = {"src/foo.py": _make_extraction("src/foo.py", [ts_sym])}

        scip = ScipIndex()
        scip.documents.append(ScipDocument(
            relative_path="src/foo.py",
            occurrences=[
                # SCIP reports line 10 (0-indexed) → ts line 11. Off by 1.
                _make_def_occurrence("scip-python . . . my_func().", 10),
            ],
        ))

        result = enricher.enrich(extractions, scip)
        # Tolerance ±1, ±2 — match has happened
        assert result.definitions_matched == 1

    def test_non_definition_skipped(self, enricher: ScipEnricher) -> None:
        """Read/write access — skipped at definition stage."""
        ts_sym = SymbolInfo(
            id="x", name="my_func", kind="function",
            file="f.py", start_line=1, language="python",
        )
        extractions = {"f.py": _make_extraction("f.py", [ts_sym])}

        scip = ScipIndex()
        scip.documents.append(ScipDocument(
            relative_path="f.py",
            occurrences=[
                ScipOccurrence(
                    symbol="x", range=(0, 0, 0, 5), symbol_roles=8,  # READ
                ),
            ],
        ))

        result = enricher.enrich(extractions, scip)
        # READ_ACCESS не визнаєтmoreяе як definition
        assert result.definitions_matched == 0


# ─── Edge resolution ──────────────────────────────────────────────


class TestEdgeResolution:
    def test_unresolved_call_resolved_via_scip(
        self, enricher: ScipEnricher,
    ) -> None:
        """Tree-sitter unresolved CALLS → SCIP-known def → resolved."""
        # Tree-sitter: caller calls 'helper'
        caller = SymbolInfo(
            id="src/main.py::caller", name="caller", kind="function",
            file="src/main.py", start_line=1, language="python",
        )
        helper_def = SymbolInfo(
            id="src/util.py::helper", name="helper", kind="function",
            file="src/util.py", start_line=10, language="python",
        )
        # Unresolved edge від caller до 'helper' (raw_target string)
        edge = EdgeInfo(
            from_id="src/main.py::caller",
            to_id=None,
            kind="CALLS",
            confidence="unresolved",
            raw_target="helper",
        )

        extractions = {
            "src/main.py": ExtractionResult(symbols=[caller], edges=[edge]),
            "src/util.py": ExtractionResult(symbols=[helper_def], edges=[]),
        }

        # SCIP знає що helper definitions у src/util.py:10 (ts) = line 9 (scip 0-indexed)
        scip = ScipIndex()
        scip.documents.append(ScipDocument(
            relative_path="src/util.py",
            occurrences=[
                _make_def_occurrence("scip-python . . . util/helper().", 9),
            ],
        ))

        result = enricher.enrich(extractions, scip)

        # Definition matched
        assert result.definitions_matched == 1
        # Edge resolved
        assert result.references_resolved == 1
        # Original edge mutated — to_id зараз set
        assert edge.to_id == "src/util.py::helper"
        assert edge.confidence == "strong"

    def test_already_resolved_edge_unchanged(
        self, enricher: ScipEnricher,
    ) -> None:
        """Edge з to_id != None — НЕ обробляється."""
        edge = EdgeInfo(
            from_id="a", to_id="b", kind="CALLS", confidence="weak",
        )
        extractions = {
            "f.py": ExtractionResult(symbols=[], edges=[edge]),
        }
        result = enricher.enrich(extractions, ScipIndex())
        assert result.references_resolved == 0
        # Edge unchanged
        assert edge.to_id == "b"
        assert edge.confidence == "weak"

    def test_non_calls_edge_skipped(self, enricher: ScipEnricher) -> None:
        """IMPORTS edges skipped (тільки CALLS resolution scope для MVP)."""
        edge = EdgeInfo(
            from_id="a", to_id=None, kind="IMPORTS",
            confidence="unresolved", raw_target="some_module",
        )
        extractions = {"f.py": ExtractionResult(symbols=[], edges=[edge])}
        result = enricher.enrich(extractions, ScipIndex())
        assert result.references_resolved == 0
