"""Tests для symbol/edge kinds taxonomy."""

from __future__ import annotations

from src.indexing.graph.kinds import (
    CORE_EDGE_KINDS,
    CORE_SYMBOL_KINDS,
    is_vendor_kind,
    known_edge_kinds,
    known_symbol_kinds,
    validate_edge_kind,
    validate_symbol_kind,
)


class TestCoreKinds:
    def test_symbol_kinds_include_oop_basics(self) -> None:
        for k in ("function", "method", "class", "interface", "struct", "enum"):
            assert k in CORE_SYMBOL_KINDS

    def test_symbol_kinds_include_containers(self) -> None:
        for k in ("namespace", "package", "module"):
            assert k in CORE_SYMBOL_KINDS

    def test_edge_kinds_uppercase(self) -> None:
        """All edge kinds мають бути UPPER_CASE для Cypher consistency."""
        for k in CORE_EDGE_KINDS:
            assert k == k.upper(), f"edge kind {k!r} не uppercase"

    def test_edge_kinds_include_oop(self) -> None:
        for k in ("CALLS", "IMPORTS", "DEFINED_IN", "EXTENDS", "IMPLEMENTS"):
            assert k in CORE_EDGE_KINDS

    def test_edge_kinds_include_infra(self) -> None:
        for k in ("BUILT_FROM", "RUNS_IMAGE", "DEPLOYS", "SELECTS"):
            assert k in CORE_EDGE_KINDS


class TestVendorPrefix:
    def test_basic_vendor(self) -> None:
        assert is_vendor_kind("vendor.ts.export_default") is True
        assert is_vendor_kind("vendor.cpp.template_specialization") is True
        assert is_vendor_kind("vendor.py.metaclass") is True

    def test_invalid_vendor_too_few_parts(self) -> None:
        assert is_vendor_kind("vendor.ts") is False
        assert is_vendor_kind("vendor") is False
        assert is_vendor_kind("vendor.") is False

    def test_invalid_vendor_empty_parts(self) -> None:
        assert is_vendor_kind("vendor..foo") is False
        assert is_vendor_kind("vendor.ts.") is False

    def test_non_vendor_strings(self) -> None:
        assert is_vendor_kind("function") is False
        assert is_vendor_kind("ts.export") is False


class TestValidation:
    def test_validate_core_symbol_kind_no_warning(self, caplog) -> None:
        validate_symbol_kind("function", source="test")
        # Жодного warning не повинно бути
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_validate_vendor_symbol_kind_no_warning(self, caplog) -> None:
        validate_symbol_kind("vendor.ts.export_default", source="ts")
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_validate_unknown_symbol_kind_warns(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            result = validate_symbol_kind("weird_thing", source="test")
        assert result is True  # backward compat — не блокує
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("non_core_symbol_kind" in r.message for r in warnings)
        assert any("weird_thing" in r.message for r in warnings)

    def test_validate_core_edge_kind_no_warning(self, caplog) -> None:
        validate_edge_kind("CALLS", source="test")
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_validate_unknown_edge_kind_warns(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            validate_edge_kind("RANDOM_THING", source="test")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("non_core_edge_kind" in r.message for r in warnings)


class TestUtility:
    def test_known_symbol_kinds_sorted(self) -> None:
        kinds = known_symbol_kinds()
        assert kinds == tuple(sorted(kinds))
        assert len(kinds) == len(CORE_SYMBOL_KINDS)

    def test_known_edge_kinds_sorted(self) -> None:
        kinds = known_edge_kinds()
        assert kinds == tuple(sorted(kinds))
