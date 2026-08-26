"""Tests для ScipReader (JSON parsing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.indexing.scip.reader import (
    ScipReader,
    SymbolRole,
    parse_scip_symbol,
)


@pytest.fixture
def reader() -> ScipReader:
    return ScipReader()


# ─── Empty / minimal ───────────────────────────────────────────────


class TestMinimalIndex:
    def test_empty_index(self, reader: ScipReader) -> None:
        idx = reader.read_dict({"metadata": {}})
        assert idx.documents == []
        assert idx.external_symbols == []

    def test_metadata_extraction(self, reader: ScipReader) -> None:
        idx = reader.read_dict({
            "metadata": {
                "version": "0.3.0",
                "tool_info": {"name": "scip-python", "version": "0.5.0"},
                "project_root": "file:///workspace",
            },
        })
        assert idx.tool_info_name == "scip-python"
        assert idx.tool_info_version == "0.5.0"
        assert idx.project_root == "file:///workspace"

    def test_invalid_input_raises(self, reader: ScipReader) -> None:
        with pytest.raises(ValueError, match="top-level object"):
            reader.read_dict([])


# ─── Documents ──────────────────────────────────────────────────────


class TestDocumentParsing:
    def test_basic_document(self, reader: ScipReader) -> None:
        idx = reader.read_dict({
            "metadata": {},
            "documents": [
                {
                    "relative_path": "src/foo.py",
                    "language": "Python",
                    "occurrences": [
                        {
                            "range": [10, 4, 10, 12],
                            "symbol": "scip-python . . . src/foo.py/my_func().",
                            "symbol_roles": 1,  # Definition
                        },
                    ],
                    "symbols": [
                        {
                            "symbol": "scip-python . . . src/foo.py/my_func().",
                            "documentation": ["Test function"],
                            "kind": 20,  # FUNCTION
                        },
                    ],
                },
            ],
        })

        assert len(idx.documents) == 1
        doc = idx.documents[0]
        assert doc.relative_path == "src/foo.py"
        assert doc.language == "Python"
        assert len(doc.occurrences) == 1

        occ = doc.occurrences[0]
        assert occ.symbol == "scip-python . . . src/foo.py/my_func()."
        assert occ.range == (10, 4, 10, 12)
        assert occ.is_definition is True
        assert occ.is_read_access is False
        # 1-indexed
        assert occ.start_line == 11

    def test_3_tuple_range(self, reader: ScipReader) -> None:
        """SCIP має 3-tuple range коли start_line == end_line."""
        idx = reader.read_dict({
            "metadata": {},
            "documents": [
                {
                    "relative_path": "src/foo.py",
                    "occurrences": [
                        {"range": [5, 4, 12], "symbol": "scip-python . . . x.", "symbol_roles": 8},
                    ],
                },
            ],
        })
        occ = idx.documents[0].occurrences[0]
        assert occ.range == (5, 4, 5, 12)
        assert occ.is_read_access is True

    def test_multiple_occurrences(self, reader: ScipReader) -> None:
        idx = reader.read_dict({
            "metadata": {},
            "documents": [{
                "relative_path": "f.py",
                "occurrences": [
                    {"range": [1, 0, 1, 5], "symbol": "scip-python . . . a.", "symbol_roles": 1},
                    {"range": [2, 0, 2, 5], "symbol": "scip-python . . . b.", "symbol_roles": 8},
                    {"range": [3, 0, 3, 5], "symbol": "scip-python . . . c.", "symbol_roles": 4},
                ],
            }],
        })
        occs = idx.documents[0].occurrences
        assert len(occs) == 3
        assert occs[0].is_definition is True
        assert occs[1].is_read_access is True
        assert occs[2].is_write_access is True

    def test_invalid_occurrence_skipped(self, reader: ScipReader) -> None:
        idx = reader.read_dict({
            "metadata": {},
            "documents": [{
                "relative_path": "f.py",
                "occurrences": [
                    {"range": [1, 0, 1, 5], "symbol": "ok", "symbol_roles": 1},
                    {"range": [], "symbol": "broken_range"},  # invalid
                    "not-a-dict",                              # invalid
                    {"range": [2, 0, 2, 5], "symbol": "ok2", "symbol_roles": 1},
                ],
            }],
        })
        occs = idx.documents[0].occurrences
        assert len(occs) == 2  # лиш 2 valid


# ─── External symbols ──────────────────────────────────────────────


class TestExternalSymbols:
    def test_external_symbols_parsed(self, reader: ScipReader) -> None:
        idx = reader.read_dict({
            "metadata": {},
            "external_symbols": [
                {
                    "symbol": "scip-python python pip click 8.1.0 click/__init__.py/Command#",
                    "documentation": ["click.Command class"],
                    "kind": 5,
                },
            ],
        })
        assert len(idx.external_symbols) == 1
        ext = idx.external_symbols[0]
        assert "click" in ext.symbol
        assert ext.documentation == ["click.Command class"]


# ─── SymbolRole bitmask ────────────────────────────────────────────


class TestSymbolRole:
    def test_definition_role(self) -> None:
        assert SymbolRole.has_role(1, SymbolRole.DEFINITION) is True
        assert SymbolRole.has_role(1, SymbolRole.READ_ACCESS) is False

    def test_combined_roles(self) -> None:
        """Definition + ReadAccess (rare але valid)."""
        mask = int(SymbolRole.DEFINITION) | int(SymbolRole.READ_ACCESS)
        assert SymbolRole.has_role(mask, SymbolRole.DEFINITION) is True
        assert SymbolRole.has_role(mask, SymbolRole.READ_ACCESS) is True


# ─── Symbol grammar parsing ────────────────────────────────────────


class TestSymbolParsing:
    def test_basic_symbol(self) -> None:
        parsed = parse_scip_symbol(
            "scip-python python pip click 8.1.0 click/core.py/Command#"
        )
        assert parsed.scheme == "scip-python"
        assert "click/core.py/Command#" in parsed.descriptor
        assert parsed.is_local is False

    def test_local_symbol(self) -> None:
        parsed = parse_scip_symbol("local 0")
        assert parsed.scheme == "local"
        assert parsed.is_local is True

    def test_empty_symbol(self) -> None:
        parsed = parse_scip_symbol("")
        assert parsed.scheme == ""


# ─── File I/O ──────────────────────────────────────────────────────


class TestFileRead:
    def test_read_from_file(self, reader: ScipReader, tmp_path: Path) -> None:
        scip_data = {
            "metadata": {"tool_info": {"name": "scip-python"}},
            "documents": [{
                "relative_path": "test.py",
                "occurrences": [
                    {"range": [1, 0, 1, 5], "symbol": "x", "symbol_roles": 1},
                ],
            }],
        }
        scip_file = tmp_path / "index.json"
        scip_file.write_text(json.dumps(scip_data))

        idx = reader.read_file(scip_file)
        assert idx.tool_info_name == "scip-python"
        assert len(idx.documents) == 1

    def test_read_from_bytes(self, reader: ScipReader) -> None:
        data = b'{"metadata": {}, "documents": [{"relative_path": "a.py"}]}'
        idx = reader.read_bytes(data)
        assert idx.documents[0].relative_path == "a.py"
