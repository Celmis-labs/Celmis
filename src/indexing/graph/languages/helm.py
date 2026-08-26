"""Helm chart extractor (Stage 12.2, May 2026).

Covers:
    Chart.yaml         — chart metadata + dependencies
    values.yaml        — default values (top-level keys → ConfigKey symbols)
    requirements.yaml  — legacy Helm 2 dependencies

Two modes:
    1. **Render mode** (preferred — requires the `helm` binary in PATH):
       subprocess `helm template` → rendered YAML → reuse K8sManifestExtractor
       NOTE: render mode currently NOT triggered automatically — every template
       must be rendered beforehand. Future enhancement via the registry.

    2. **Metadata mode** (default fallback):
       Parse Chart.yaml + values.yaml as plain YAML.
       Without rendering, the `templates/*.yaml` templates are skipped (their
       content is not deterministic without `helm template` rendering).

Symbol model (vendor namespace because it is Helm-specific):
    file_module               — synthetic per Chart.yaml
    vendor.helm.chart         — chart entity (with name + version + appVersion)
    vendor.helm.dependency    — sub-chart dependency (with repo URL)
    vendor.helm.value         — top-level key from values.yaml

Edge model:
    IMPORTS — chart symbol → dependency repo URL (from dependencies[].repository)
    USES_CONFIG — chart → top-level value name

What is NOT covered at this stage:
    - Rendering templates via the helm binary (deferred to V2)
    - Schema validation via values.schema.json
    - Chart hooks (preinstall, postinstall etc.)
    - Distinguishing library charts from application charts

Stage: 12.2. Implemented (metadata-mode MVP).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


def is_helm_chart_yaml(content_head: bytes) -> bool:
    """Content-sniffer for LanguageRegistry.

    A Helm Chart.yaml has `apiVersion: v2` (Helm 3) or `v1` (legacy) +
    `name:` + `version:` (chart version). Distinguish it from generic YAML:
    the presence of both `name` AND `version` (and `apiVersion` does not point
    at a K8s resource — apiVersion 'v1'/'v2' without kind = Helm).
    """
    try:
        text = content_head.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return False

    has_apiversion = "apiVersion:" in text
    has_name = "\nname:" in text or text.startswith("name:")
    has_version = "\nversion:" in text
    # Distinguish from K8s manifests — a Helm Chart.yaml has no `kind:` field
    has_kind = "\nkind:" in text or text.startswith("kind:")

    return has_apiversion and has_name and has_version and not has_kind


@dataclass
class _WalkContext:
    file: str
    seen_ids: set[str] = field(default_factory=set)


class HelmExtractor(SymbolExtractor):
    language = "helm"
    extensions = ()  # filename + content sniff dispatch

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as e:
            return ExtractionResult(parse_errors=[f"yaml_parse_error: {e}"])

        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel)
        result = ExtractionResult()

        # File-module symbol
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).name, kind="file_module",
            file=rel, start_line=1, end_line=None, language="helm",
        ))
        ctx.seen_ids.add(file_sym_id)

        if not isinstance(data, dict):
            result.parse_errors.append(f"not_a_mapping file={rel}")
            return result

        # Detect file type by structure
        filename = Path(rel).name.lower()
        if filename in ("chart.yaml", "chart.yml"):
            self._handle_chart_yaml(data, ctx, result)
        elif filename in ("values.yaml", "values.yml"):
            self._handle_values_yaml(data, ctx, result)
        elif filename in ("requirements.yaml", "requirements.yml"):
            self._handle_requirements_yaml(data, ctx, result)

        # DEFINED_IN edges
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _handle_chart_yaml(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Chart.yaml — chart metadata + dependencies."""
        chart_name = str(data.get("name", "") or "")
        chart_version = str(data.get("version", "") or "")
        if not chart_name:
            return

        chart_id = _make_id(ctx, f"chart.{chart_name}")
        result.symbols.append(SymbolInfo(
            id=chart_id,
            name=chart_name,
            kind="vendor.helm.chart",
            file=ctx.file,
            start_line=1,
            end_line=None,
            language="helm",
            is_exported=True,
            module=chart_version or None,
        ))
        ctx.seen_ids.add(chart_id)

        # Dependencies (Helm 3): list of {name, version, repository}
        deps = data.get("dependencies", []) or []
        if isinstance(deps, list):
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                self._add_dependency(dep, chart_id, ctx, result)

    def _handle_requirements_yaml(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Legacy Helm 2 requirements.yaml — same dependency structure."""
        deps = data.get("dependencies", []) or []
        if not isinstance(deps, list):
            return

        # No chart symbol — requirements.yaml has no chart name. Use file as anchor.
        anchor_id = _file_symbol_id(ctx.file)
        for dep in deps:
            if isinstance(dep, dict):
                self._add_dependency(dep, anchor_id, ctx, result)

    def _add_dependency(
        self, dep: dict, parent_id: str,
        ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Single dependency entry → vendor.helm.dependency symbol + IMPORTS edge."""
        dep_name = dep.get("name")
        dep_repo = dep.get("repository")
        dep_version = dep.get("version")
        if not isinstance(dep_name, str):
            return

        dep_id = _make_id(ctx, f"dep.{dep_name}")
        result.symbols.append(SymbolInfo(
            id=dep_id,
            name=dep_name,
            kind="vendor.helm.dependency",
            file=ctx.file,
            start_line=1,
            end_line=None,
            language="helm",
            is_exported=True,
            module=str(dep_version) if dep_version else None,
        ))
        ctx.seen_ids.add(dep_id)

        # IMPORTS edge to the repo (chart registry or OCI URL)
        if isinstance(dep_repo, str) and dep_repo.strip():
            target = f"{dep_repo}::{dep_name}"
            if dep_version:
                target = f"{target}@{dep_version}"
            result.edges.append(EdgeInfo(
                from_id=parent_id,
                to_id=None,
                kind="IMPORTS",
                confidence="strong",
                raw_target=target,
            ))

    def _handle_values_yaml(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """values.yaml — top-level keys → vendor.helm.value symbols."""
        for key in data:
            if not isinstance(key, str):
                continue
            sym_id = _make_id(ctx, f"value.{key}")
            result.symbols.append(SymbolInfo(
                id=sym_id,
                name=key,
                kind="vendor.helm.value",
                file=ctx.file,
                start_line=1,
                end_line=None,
                language="helm",
                is_exported=True,
            ))
            ctx.seen_ids.add(sym_id)

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{len(ctx.seen_ids)}"
