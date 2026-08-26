"""SCIP JSON reader — parses Sourcegraph SCIP output.

SCIP has a canonical protobuf binary form, but the `scip` CLI can convert it to
JSON via `scip convert --from binary --to json`. That is human-readable +
parseable without the protobuf compilation overhead.

JSON schema (Sourcegraph SCIP spec, current as of May 2026):
    {
      "metadata": {"version": "...", "tool_info": {...}, "project_root": "..."},
      "documents": [
        {
          "language": "Python",
          "relative_path": "src/foo.py",
          "occurrences": [
            {
              "range": [start_line, start_col, end_line, end_col],
              "symbol": "scip-python python . . . module/Class#method().",
              "symbol_roles": 1   // 1=Definition, 8=ReadAccess, 16=WriteAccess
            }
          ],
          "symbols": [
            {
              "symbol": "scip-python python . . . module/Class#",
              "documentation": ["..."],
              "kind": 5  // Class | Method | Function ...
            }
          ]
        }
      ],
      "external_symbols": [...]   // refs to third-party packages
    }

Symbol grammar (SCIP scheme):
    "scip-python python <package_manager> <package_name> <version> <descriptor>"
    e.g.  "scip-python python pip click 8.1.0 click/core.py/Command#"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SymbolRole(IntEnum):
    """Sourcegraph SCIP SymbolRole enum (subset).

    Bitmask — several roles can be combined (because they are bit flags).
    """

    UNSPECIFIED = 0
    DEFINITION = 1
    IMPORT = 2
    WRITE_ACCESS = 4
    READ_ACCESS = 8
    GENERATED = 16
    TEST = 32

    @classmethod
    def has_role(cls, mask: int, role: SymbolRole) -> bool:
        return bool(mask & int(role))


class SymbolKind(IntEnum):
    """SCIP SymbolKind — the symbol type (Class/Method/Field/Module/etc.)."""

    UNSPECIFIED = 0
    ARRAY = 1
    BOOLEAN = 13
    CLASS = 5
    CONSTANT = 14
    CONSTRUCTOR = 15
    ENUM = 6
    ENUM_MEMBER = 16
    EVENT = 17
    FIELD = 18
    FILE = 19
    FUNCTION = 20
    INTERFACE = 7
    KEY = 21
    METHOD = 11
    MODULE = 1  # NOTE: array=1, module=1 — SCIP enum conflict. Default — we use
    NAMESPACE = 22  # separately
    NULL = 23
    NUMBER = 24
    OBJECT = 25
    OPERATOR = 26
    PACKAGE = 27
    PARAMETER = 28
    PROPERTY = 29
    STRING = 30
    STRUCT = 31
    TRAIT = 9
    TYPE = 10
    TYPE_PARAMETER = 33
    UNION = 34
    VARIABLE = 35


@dataclass
class ScipOccurrence:
    """A single occurrence of a symbol in a source file."""

    symbol: str  # SCIP symbol identifier (grammar above)
    range: tuple[int, int, int, int]  # (start_line, start_col, end_line, end_col) — 0-indexed
    symbol_roles: int  # SymbolRole bitmask

    @property
    def is_definition(self) -> bool:
        return SymbolRole.has_role(self.symbol_roles, SymbolRole.DEFINITION)

    @property
    def is_read_access(self) -> bool:
        return SymbolRole.has_role(self.symbol_roles, SymbolRole.READ_ACCESS)

    @property
    def is_write_access(self) -> bool:
        return SymbolRole.has_role(self.symbol_roles, SymbolRole.WRITE_ACCESS)

    @property
    def start_line(self) -> int:
        """1-indexed (as in the tree-sitter convention)."""
        return self.range[0] + 1


@dataclass
class ScipSymbol:
    """Symbol metadata: documentation + kind + relationships."""

    symbol: str  # SCIP identifier
    documentation: list[str] = field(default_factory=list)
    kind: int = 0  # SymbolKind
    relationships: list[dict] = field(default_factory=list)


@dataclass
class ScipDocument:
    """Per-file collection of occurrences + symbols."""

    relative_path: str
    language: str = ""
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbol] = field(default_factory=list)


@dataclass
class ScipIndex:
    """Top-level SCIP index — entire repository scan."""

    project_root: str = ""
    tool_info_name: str = ""
    tool_info_version: str = ""
    documents: list[ScipDocument] = field(default_factory=list)
    external_symbols: list[ScipSymbol] = field(default_factory=list)


# ─── Reader ────────────────────────────────────────────────────────


class ScipReader:
    """Read SCIP JSON file → ScipIndex."""

    def read_file(self, path: Path) -> ScipIndex:
        """Parse SCIP JSON file."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return self.read_dict(data)

    def read_bytes(self, data: bytes) -> ScipIndex:
        """Parse SCIP JSON from memory."""
        parsed = json.loads(data.decode("utf-8"))
        return self.read_dict(parsed)

    def read_dict(self, data: dict[str, Any]) -> ScipIndex:
        """Parse pre-loaded JSON dict → ScipIndex."""
        if not isinstance(data, dict):
            raise ValueError("SCIP JSON must be top-level object")

        metadata = data.get("metadata") or {}
        tool_info = metadata.get("tool_info") or {}

        index = ScipIndex(
            project_root=str(metadata.get("project_root") or ""),
            tool_info_name=str(tool_info.get("name") or ""),
            tool_info_version=str(tool_info.get("version") or ""),
        )

        for doc_data in data.get("documents") or []:
            if not isinstance(doc_data, dict):
                continue
            doc = self._parse_document(doc_data)
            if doc is not None:
                index.documents.append(doc)

        for sym_data in data.get("external_symbols") or []:
            if isinstance(sym_data, dict):
                sym = self._parse_symbol(sym_data)
                if sym is not None:
                    index.external_symbols.append(sym)

        logger.info(
            "scip_index_read documents=%d external_symbols=%d tool=%s/%s",
            len(index.documents), len(index.external_symbols),
            index.tool_info_name, index.tool_info_version,
        )
        return index

    def _parse_document(self, data: dict) -> ScipDocument | None:
        rel_path = data.get("relative_path")
        if not isinstance(rel_path, str):
            return None

        doc = ScipDocument(
            relative_path=rel_path,
            language=str(data.get("language") or ""),
        )

        for occ_data in data.get("occurrences") or []:
            if not isinstance(occ_data, dict):
                continue
            occ = self._parse_occurrence(occ_data)
            if occ is not None:
                doc.occurrences.append(occ)

        for sym_data in data.get("symbols") or []:
            if not isinstance(sym_data, dict):
                continue
            sym = self._parse_symbol(sym_data)
            if sym is not None:
                doc.symbols.append(sym)

        return doc

    def _parse_occurrence(self, data: dict) -> ScipOccurrence | None:
        symbol = data.get("symbol")
        rng = data.get("range")
        if not isinstance(symbol, str) or not isinstance(rng, list):
            return None

        # A SCIP range can be a 3-tuple (start_line, start_col, end_col — same line)
        # OR a 4-tuple (start_line, start_col, end_line, end_col).
        if len(rng) == 3:
            start_line, start_col, end_col = rng
            end_line = start_line
        elif len(rng) == 4:
            start_line, start_col, end_line, end_col = rng
        else:
            return None

        try:
            return ScipOccurrence(
                symbol=symbol,
                range=(int(start_line), int(start_col), int(end_line), int(end_col)),
                symbol_roles=int(data.get("symbol_roles") or 0),
            )
        except (TypeError, ValueError):
            return None

    def _parse_symbol(self, data: dict) -> ScipSymbol | None:
        symbol = data.get("symbol")
        if not isinstance(symbol, str):
            return None

        documentation = data.get("documentation") or []
        if not isinstance(documentation, list):
            documentation = []

        return ScipSymbol(
            symbol=symbol,
            documentation=[str(d) for d in documentation if isinstance(d, str)],
            kind=int(data.get("kind") or 0),
            relationships=list(data.get("relationships") or []),
        )


# ─── Symbol grammar parsing ─────────────────────────────────────────


@dataclass(frozen=True)
class ParsedScipSymbol:
    """Decoded SCIP symbol identifier.

    Grammar (Sourcegraph spec):
        scheme '<space>' manager '<space>' name '<space>' version '<space>' descriptor

    Example:
        'scip-python python pip click 8.1.0 click/core.py/Command#'
        scheme=scip-python, manager=python (= local), name=pip, version=click, …
    In reality Python scip has fewer segments — this is a prototype
    implementation.
    """

    scheme: str
    descriptor: str  # last segment — packagepath + entity name
    raw: str

    @property
    def is_local(self) -> bool:
        """`local 0`, `local 1` etc. — anonymous local symbols (vars/params)."""
        return self.scheme == "local"


def parse_scip_symbol(raw: str) -> ParsedScipSymbol:
    """Lightweight SCIP symbol parsing.

    We do not attempt the full SCIP grammar — we take:
        - scheme (the first token)
        - descriptor (last entity-like part)
    For the majority of cases this is enough for symbol identity matching.
    """
    raw = raw.strip()
    if not raw:
        return ParsedScipSymbol(scheme="", descriptor="", raw=raw)

    parts = raw.split(" ", 1)
    scheme = parts[0]

    # Last meaningful segment — descriptor (path + entity name with / # . separators)
    descriptor = parts[1] if len(parts) > 1 else ""

    return ParsedScipSymbol(scheme=scheme, descriptor=descriptor, raw=raw)
