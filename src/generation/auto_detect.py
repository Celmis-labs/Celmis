"""Auto-detection of FeatureSpec + Integrations from the already generated
module notes + graph.

**Features:** keyword-overlap clustering. 2+ modules with N+ shared keywords →
feature group. The name is the most frequent shared keyword.

**Integrations:** symbols in the graph that:
    - kind ∈ {class, export_default, function} and is_exported=True
    - are in many (≥3) incoming IMPORTS edges from outside their module path

Phase 9. Implemented without sklearn — simple threshold-based grouping.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.config import Settings, get_settings
from src.generation.feature_doc import FeatureSpec
from src.indexing.graph.graph_store import make_graph_store
from src.vault.reader import VaultReader

logger = logging.getLogger(__name__)


# How many shared keywords are needed for modules to count as "one feature"
_FEATURE_KEYWORD_OVERLAP = 2
# Minimum number of modules in a feature
_FEATURE_MIN_MODULES = 2
# Maximum features in a single repo (protection against feature explosion)
_MAX_FEATURES = 20


# Technical words that carry no feature semantics — excluded from clustering
_GENERIC_KEYWORDS = frozenset({
    "module", "modules", "src", "components", "component", "utils", "util",
    "model", "models", "service", "services", "controller", "controllers",
    "helper", "helpers", "common", "shared", "core", "base", "main",
    "_root", "index", "default", "constructor", "render", "props", "state",
    "vue", "ts", "js", "tsx", "jsx", "this", "self", "data",
})


# Minimum incoming imports (from outside the module path) — to classify as an integration
_INTEGRATION_MIN_IN_DEGREE = 3


# ────────────────────────── data ──────────────────────────


@dataclass
class IntegrationCandidate:
    """A detected exported service — a candidate for integration_doc."""

    name: str
    entry_points: list[str]  # symbol names that are exposed
    importing_modules: list[str]  # who imports it


# ────────────────────────── features ──────────────────────


def detect_features(
    repo_slug: str,
    settings: Settings | None = None,
) -> list[FeatureSpec]:
    """Read all module notes in the vault and cluster them by keyword overlap.

    Returns: list[FeatureSpec] (max _MAX_FEATURES).
    """
    settings = settings or get_settings()
    reader = VaultReader(settings)
    notes = reader.list_notes(repo_slug, note_type="module")

    if len(notes) < _FEATURE_MIN_MODULES:
        logger.info("detect_features_skipped notes=%d (need ≥%d)", len(notes), _FEATURE_MIN_MODULES)
        return []

    # 1. Extract keywords + symbols from each module note
    module_keywords: dict[str, set[str]] = {}
    module_symbols: dict[str, set[str]] = {}
    for note in notes:
        # the `module` field in the frontmatter is the module name (from ModuleDiscovery)
        module_name = note.metadata.get("module") or note.relative_path.replace(".md", "")
        kws = {
            k.lower() for k in note.keywords
            if isinstance(k, str) and k.lower() not in _GENERIC_KEYWORDS and len(k) >= 3
        }
        if kws:
            module_keywords[str(module_name)] = kws
        syms = {s for s in note.symbols if isinstance(s, str)}
        module_symbols[str(module_name)] = syms

    # 2. Find shared keywords among pairs of modules
    # For each keyword — the list of modules that contain it
    keyword_to_modules: dict[str, set[str]] = defaultdict(set)
    for module, kws in module_keywords.items():
        for kw in kws:
            keyword_to_modules[kw].add(module)

    # 3. Build "feature seeds": keyword + its modules (≥2)
    feature_groups: list[tuple[str, set[str]]] = []
    for kw, modules in keyword_to_modules.items():
        if len(modules) < _FEATURE_MIN_MODULES:
            continue
        feature_groups.append((kw, modules))

    # 4. Dedup overlapping groups: if 2 keywords link the same modules, we take
    # the more specific one (the rarer keyword).
    # Sort by size — bigger groups first.
    feature_groups.sort(key=lambda kg: (-len(kg[1]), kg[0]))

    seen_signatures: set[frozenset[str]] = set()
    deduped: list[tuple[str, set[str]]] = []
    for kw, modules in feature_groups:
        sig = frozenset(modules)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        deduped.append((kw, modules))

    # 5. Check: the modules must have an overlap ≥ _FEATURE_KEYWORD_OVERLAP
    # (the current rule — beyond each kw — but if 2 modules share only 1 kw,
    # that is a weak signal; we check the count of shared keywords)
    final: list[FeatureSpec] = []
    for kw, modules in deduped[:_MAX_FEATURES]:
        modules_list = sorted(modules)
        # Count the keywords actually shared between these modules
        shared_kws_count = Counter()
        for m in modules_list:
            for k in module_keywords.get(m, set()):
                shared_kws_count[k] += 1
        strongly_shared = [k for k, c in shared_kws_count.items() if c >= len(modules_list)]
        if len(strongly_shared) < _FEATURE_KEYWORD_OVERLAP:
            continue

        # Seed symbols = the symbols occurring most often across these modules.
        all_syms = Counter()
        for m in modules_list:
            for s in module_symbols.get(m, set()):
                all_syms[s] += 1
        seed_symbols = [s for s, _ in all_syms.most_common(10)]

        # Feature name = sluggified keyword
        name = _slugify(kw)
        final.append(FeatureSpec(
            name=name,
            description=f"Auto-detected feature: modules share the keyword '{kw}'",
            seed_symbols=seed_symbols,
            modules=modules_list,
        ))

    logger.info("detect_features found=%d (max=%d)", len(final), _MAX_FEATURES)
    return final


# ────────────────────────── integrations ──────────────────


def detect_integrations(
    repo_slug: str,
    settings: Settings | None = None,
) -> list[IntegrationCandidate]:
    """Find symbols with a high incoming-IMPORTS degree from outside their module.

    Approach:
        1. Walk all IMPORTS edges
        2. For each target — count the import callers
        3. If target.is_exported AND in_degree ≥ threshold → integration
    """
    settings = settings or get_settings()
    db_path = settings.repo_graph_path(repo_slug)
    if not db_path.exists():
        logger.warning("detect_integrations no_graph at %s", db_path)
        return []

    store = make_graph_store(db_path)
    try:
        # Walk through the target symbols with the largest in-degree
        rows = store.query(
            "MATCH (a:Symbol)-[:IMPORTS]->(b:Symbol) "
            "WHERE b.is_exported = true AND b.kind <> 'file_module' "
            "RETURN b.id AS target_id, b.name AS target_name, b.kind AS kind, "
            "       b.file AS file, b.module AS module, count(a) AS in_degree "
            "ORDER BY in_degree DESC "
            "LIMIT 50"
        )

        candidates: list[IntegrationCandidate] = []
        for r in rows:
            in_degree = int(r.get("in_degree", 0) or 0)
            if in_degree < _INTEGRATION_MIN_IN_DEGREE:
                break  # rows sorted DESC

            name = r.get("target_name", "")
            if not name or name.startswith("_"):
                continue

            # Who imports it
            importers = store.query(
                "MATCH (a:Symbol)-[:IMPORTS]->(b:Symbol {id: $tid}) "
                "RETURN DISTINCT a.file AS f LIMIT 10",
                params={"tid": r["target_id"]},
            )
            importing_modules = sorted({_module_for_file(im.get("f", "")) for im in importers if im.get("f")})

            candidates.append(IntegrationCandidate(
                name=_slugify(name),
                entry_points=[name],
                importing_modules=importing_modules,
            ))

        logger.info("detect_integrations found=%d", len(candidates))
        return candidates
    finally:
        store.close()


# ────────────────────────── helpers ──────────────────────


def _slugify(text: str) -> str:
    """name → safe-filename slug."""
    import re
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s_]+", "-", s).strip("-")
    return s or "unnamed"


def _module_for_file(file_path: str) -> str:
    """Extract the 'module name' from a file path. Heuristic: up to 3 levels."""
    parts = Path(file_path).parts
    # Skip leading "src", "acme-modules" and the like
    relevant = [p for p in parts[:5] if p not in ("src", "acme-modules", "lib", "app")]
    return "-".join(relevant[:3]) if relevant else "root"
