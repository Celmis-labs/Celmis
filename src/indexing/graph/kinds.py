"""Symbol/Edge kinds taxonomy — closed core enum + reserved vendor namespace.

Option D (decision fixed in May 2026):
    - **Core kinds** — a closed enum, type-safe for cross-language Cypher queries
    - **Vendor kinds** — the format `vendor.{lang}.{kind}` for language-specific
      cases that do not fit into core (for example `vendor.ts.export_default`,
      `vendor.cpp.template_specialization`)

Validation:
    - `validate_symbol_kind()` / `validate_edge_kind()` — issue a warning when a
      non-core kind is used without the `vendor.` prefix (drift detection)
    - do NOT raise — so that existing code does not break, but the logs catch the drift

Why not a Literal-only enum:
    1. the tree-sitter-vue extractor already emits "export_default" — the migration
       to `vendor.ts.export_default` is gradual
    2. the field on SymbolInfo stays `kind: str` — runtime typing does not change
    3. future extractors may have language-specific nuances
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger(__name__)


# ─── Core symbol kinds ───────────────────────────────────────────────


# General OOP/functional structures — must be identical across languages
# for cross-language queries (`MATCH (s:Symbol {kind: 'class'}) ...`).
CORE_SYMBOL_KINDS: Final[frozenset[str]] = frozenset({
    # ─ Functions / methods ─
    "function",        # standalone function (all languages)
    "method",          # method in a class/struct
    # ─ Types ─
    "class",           # class / abstract class (Python, Java, C#, C++, PHP, TS)
    "interface",       # interface (Java, C#, TS, Go interface_type)
    "struct",          # struct (Go, C++, C#)
    "enum",            # enum (all languages with enum support)
    # ─ Containers ─
    "namespace",       # namespace (C#, C++, PHP)
    "package",         # package (Java, Go)
    "module",          # module (logical — Python module, JS/TS module)
    # ─ Members ─
    "variable",        # local + module-level variable / let / var / const
    "field",           # member field/property in a class/struct
    "constant",        # const / final immutable bindings
    # ─ Synthetic ─
    "file_module",     # synthetic per-file root symbol — for the DEFINED_IN graph
    # ─ Infrastructure (Stage 4 — Phase C) ─
    "image",           # Docker image (`FROM x:tag`)
    "service",         # docker-compose service / k8s Service
    "deployment",      # k8s Deployment / DaemonSet / StatefulSet
    "container",       # container in a Pod/Deployment
    "configmap",       # k8s ConfigMap
    "secret",          # k8s Secret
    "ingress",         # k8s Ingress
})


# ─── Core edge kinds ─────────────────────────────────────────────────


CORE_EDGE_KINDS: Final[frozenset[str]] = frozenset({
    # ─ Code-level (Stage 2 — Phase B) ─
    "CALLS",           # function/method call
    "IMPORTS",         # import / use / using / include
    "DEFINED_IN",      # symbol lives in a file/class/namespace
    "EXTENDS",         # subclass extends superclass (Java, PHP, C#, C++)
    "IMPLEMENTS",      # class implements interface (Java, C#, Go satisfies)
    # ─ Infrastructure (Stage 4 — Phase C) ─
    "BUILT_FROM",      # Docker image FROM
    "EXPOSES",         # Dockerfile EXPOSE / Service port mapping
    "COPIES_FROM",     # Dockerfile COPY src dst
    "RUNS_IMAGE",      # k8s Pod/Deployment runs Image
    "DEPLOYS",         # service deployed via Manifest
    "SELECTS",         # k8s Service selects Pods via labels
    "USES_CONFIG",     # Pod uses ConfigMap/Secret
    "MOUNTS",          # volume mount
})


# ─── Validation helpers ─────────────────────────────────────────────


def is_vendor_kind(kind: str) -> bool:
    """True if the kind has the vendor prefix `vendor.{lang}.{name}`."""
    if not kind.startswith("vendor."):
        return False
    parts = kind.split(".")
    # At least 3 segments: vendor, lang, name
    return len(parts) >= 3 and all(p for p in parts)


def validate_symbol_kind(kind: str, *, source: str = "") -> bool:
    """Check whether the kind is allowed (core or vendor.).

    Returns True. Issues a warning if the kind is neither core nor vendor — to
    detect drift in the conventions. Does NOT raise — backward compat.

    Args:
        kind: the value of `SymbolInfo.kind`
        source: context for logging (for example the extractor name)
    """
    if kind in CORE_SYMBOL_KINDS:
        return True
    if is_vendor_kind(kind):
        return True
    logger.warning(
        "non_core_symbol_kind kind=%s source=%s — use vendor.{lang}.{name}",
        kind, source,
    )
    return True  # backward compat — we do not block


def validate_edge_kind(kind: str, *, source: str = "") -> bool:
    """The same for edge kinds. Vendor edges are used rarely; always the
    UPPER_CASE convention for cypher consistency.
    """
    if kind in CORE_EDGE_KINDS:
        return True
    if is_vendor_kind(kind):
        return True
    logger.warning(
        "non_core_edge_kind kind=%s source=%s — use UPPER_CASE or vendor.{lang}.{NAME}",
        kind, source,
    )
    return True


# ─── Utility ────────────────────────────────────────────────────────


def known_symbol_kinds() -> tuple[str, ...]:
    """Snapshot of the core kinds — for CLI help / docs."""
    return tuple(sorted(CORE_SYMBOL_KINDS))


def known_edge_kinds() -> tuple[str, ...]:
    return tuple(sorted(CORE_EDGE_KINDS))
