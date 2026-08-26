"""Indexing — building the code graph + SAST scan.

v3.0: CGC removed. The graph goes through src.indexing.graph.* (Phase 5).
SymbolInfo now lives in src.indexing.modules as a shim for backward compat
with module_prd.py (path-based discovery, for now without graph enrichment).
"""

from src.indexing.modules import Module, ModuleDiscovery, SymbolInfo
from src.indexing.semgrep import SemgrepFinding, SemgrepRunner

__all__ = [
    "SymbolInfo",
    "SemgrepRunner",
    "SemgrepFinding",
    "Module",
    "ModuleDiscovery",
]
