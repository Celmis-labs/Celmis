"""SCIP (Sourcegraph Code Intelligence Protocol) integration foundation.

Stage 14 (May 2026, Sourcegraph SCIP protocol):
    SCIP — a type-aware code intelligence format from Sourcegraph. It complements
    tree-sitter structural extraction with full type resolution (cross-file
    references, types, inheritance) that tree-sitter cannot do on its own for
    dynamic languages (Python/PHP) or complex type systems (Java generics,
    C# nullable).

Architecture:
    1. **ScipReader** — parses SCIP JSON output (via `scip convert --to json`)
    2. **ScipPythonIndexer** — subprocess wrapper for the scip-python binary
    3. **ScipEnricher** — merges SCIP records into the existing tree-sitter graph

Indexers as of May 2026:
    scip-python    — Sourcegraph fork pyright (Apache-2.0). npm: @sourcegraph/scip-python
    scip-typescript — same. npm: @sourcegraph/scip-typescript
    scip-java       — semanticdb-javac. Maven plugin or CLI
    scip-go         — Go AST. CLI binary
    scip-clang      — LLVM-based. Requires compile_commands.json
    scip-dotnet     — Roslyn-based. Beta as of May 2026

NOTE: SCIP requires per-language toolchains installed (Node, JDK, Go, etc.).
This module is an abstraction. Real subprocess execution + binary management —
V2, when the user explicitly opts in.

Stage 14 — abstraction + JSON reader + enrichment merger implemented.
Subprocess execution fails gracefully if the binary is missing.
"""

from src.indexing.scip.enricher import ScipEnricher
from src.indexing.scip.reader import (
    ScipDocument,
    ScipIndex,
    ScipOccurrence,
    ScipReader,
    ScipSymbol,
    SymbolRole,
)
from src.indexing.scip.runner import ScipExternalRunner, ScipRunnerError

__all__ = [
    "ScipDocument",
    "ScipEnricher",
    "ScipExternalRunner",
    "ScipIndex",
    "ScipOccurrence",
    "ScipReader",
    "ScipRunnerError",
    "ScipSymbol",
    "SymbolRole",
]
