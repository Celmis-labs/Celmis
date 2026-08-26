"""SymbolExtractor — the contract for all per-language adapters.

Every adapter (typescript.py, vue.py, ...) implements the `extract(file_path)`
method and returns (symbols, edges). The resolver then post-processes the
incomplete edges (where to=None).

Implementation — Phase 5.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SymbolInfo:
    """A single symbol in the graph (function/class/method/export)."""

    id: str  # unique: "{file}::{name}" or "{file}::{name}@{line}"
    name: str
    kind: str  # "function" | "class" | "method" | "import" | "export" | "variable"
    file: str  # relative path to the repo root
    start_line: int
    end_line: int | None = None
    language: str = ""
    signature: str | None = None
    docstring: str | None = None
    is_exported: bool = False
    module: str | None = None  # logical module path

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class EdgeInfo:
    """An edge in the graph."""

    from_id: str
    to_id: str | None  # None → needs resolution via resolver.py
    kind: str  # "CALLS" | "IMPORTS" | "DEFINED_IN" | "TEMPLATE_REF"
    confidence: str = "strong"  # "strong" | "weak" | "unresolved"
    raw_target: str | None = None  # for unresolved: the original name/path


@dataclass
class ExtractionResult:
    """The result of a single extraction — symbols + edges from one file."""

    symbols: list[SymbolInfo] = field(default_factory=list)
    edges: list[EdgeInfo] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


class SymbolExtractor(ABC):
    """Contract: a per-language adapter."""

    language: str = ""  # "typescript" | "vue" | ...
    extensions: tuple[str, ...] = ()  # (".ts", ".tsx", ...)

    @abstractmethod
    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        """Parse the file and return symbols + edges.

        Args:
            file_path: path to the file (for metadata).
            source: optional — the file's bytes. If None — we read from disk.
        """
        raise NotImplementedError
