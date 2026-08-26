"""Docker Compose extractor (Stage 4.2, May 2026).

Covers filename patterns:
    docker-compose.yml | docker-compose.yaml
    compose.yml | compose.yaml          (compose-spec, no version field)
    docker-compose.*.yml | compose.*.yml (override stack files)

NOT tree-sitter-based — we use PyYAML 6.0 for structure preservation.

Compose schema (compose-spec, current as of May 2026):
    services:
        <name>:
            image: foo:tag           → RUNS_IMAGE edge
            build: ./path | {context, dockerfile}  → BUILT_FROM edge
            ports: ["8000:8000"]    → EXPOSES edges
            environment: [...] | {} → USES_CONFIG edges
            env_file: [...]         → USES_CONFIG edges (file-level)
            depends_on: [...]       → DEPLOYS edges (service → service)
            volumes: [...]          → MOUNTS edges
            networks: [...]
    volumes: { ... }    — top-level volume defs
    networks: { ... }   — top-level network defs
    secrets: { ... }    — top-level secrets

Symbol model:
    file_module — synthetic per compose file
    service     — for each service in the services map

Edge model:
    RUNS_IMAGE   — service → image string
    BUILT_FROM   — service → build path (Dockerfile location)
    EXPOSES      — service → port mapping ("8000:8080" or "8000")
    DEPLOYS      — service → other_service (from depends_on)
    USES_CONFIG  — service → env var name OR env_file path
    MOUNTS       — service → volume name | host path

What is NOT covered:
    - profiles (compose-spec selective deploy)
    - configs (Swarm-style config injection)
    - x-* extension fields
    - YAML anchors/merges resolution — PyYAML handles automatically
    - networks topology — only as an attribute string

Stage: 4.2. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


@dataclass
class _WalkContext:
    file: str
    seen_ids: set[str] = field(default_factory=set)


class DockerComposeExtractor(SymbolExtractor):
    language = "compose"
    extensions = ()  # dispatch via filename pattern

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

        if not isinstance(data, dict):
            result.parse_errors.append(f"not_a_mapping file={rel}")
            return result

        # File-module symbol
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).name, kind="file_module",
            file=rel, start_line=1, end_line=None, language="compose",
        ))
        ctx.seen_ids.add(file_sym_id)

        services = data.get("services", {})
        if not isinstance(services, dict):
            return result

        for service_name, service_def in services.items():
            if not isinstance(service_def, dict):
                continue
            self._handle_service(service_name, service_def, ctx, result)

        # DEFINED_IN edges
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _handle_service(
        self,
        name: str,
        spec: dict,
        ctx: _WalkContext,
        result: ExtractionResult,
    ) -> None:
        sym_id = _make_id(ctx, name)
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind="service",
            file=ctx.file, start_line=1, end_line=None,
            language="compose", is_exported=True,
        ))
        ctx.seen_ids.add(sym_id)

        # image: foo:tag → RUNS_IMAGE
        image = spec.get("image")
        if isinstance(image, str) and image.strip():
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=image.strip(),
            ))

        # build → BUILT_FROM (path to the Dockerfile location)
        build = spec.get("build")
        build_target: str | None = None
        if isinstance(build, str):
            # Short syntax: `build: ./backend` means ./backend/Dockerfile
            build_target = f"{build.rstrip('/')}/Dockerfile"
        elif isinstance(build, dict):
            ctx_path = build.get("context", ".")
            dockerfile = build.get("dockerfile", "Dockerfile")
            # Canonical 'context/dockerfile'
            build_target = f"{ctx_path.rstrip('/')}/{dockerfile}"
        if build_target:
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="BUILT_FROM", confidence="strong",
                raw_target=build_target,
            ))

        # ports → EXPOSES
        ports = spec.get("ports", [])
        if isinstance(ports, list):
            for p in ports:
                # May be a string ("8000:8080") or a dict (long syntax)
                target = None
                if isinstance(p, str):
                    target = p
                elif isinstance(p, dict):
                    published = p.get("published")
                    target_port = p.get("target")
                    if published is not None:
                        target = f"{published}:{target_port}" if target_port else str(published)
                    elif target_port is not None:
                        target = str(target_port)
                if target:
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="EXPOSES", confidence="strong",
                        raw_target=str(target),
                    ))

        # depends_on → DEPLOYS
        deps = spec.get("depends_on")
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="DEPLOYS", confidence="unresolved",
                        raw_target=dep,
                    ))
        elif isinstance(deps, dict):
            # Long syntax: { service_name: { condition: ... } }
            for dep_name in deps:
                if isinstance(dep_name, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="DEPLOYS", confidence="unresolved",
                        raw_target=dep_name,
                    ))

        # environment → USES_CONFIG (env keys)
        env = spec.get("environment")
        if isinstance(env, list):
            for item in env:
                if isinstance(item, str):
                    # "KEY=VALUE" or just "KEY"
                    key = item.split("=", 1)[0]
                    if key:
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=None,
                            kind="USES_CONFIG", confidence="strong",
                            raw_target=key,
                        ))
        elif isinstance(env, dict):
            for key in env:
                if isinstance(key, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=key,
                    ))

        # env_file → USES_CONFIG (file-level)
        env_file = spec.get("env_file")
        if isinstance(env_file, str):
            env_file = [env_file]
        if isinstance(env_file, list):
            for ef in env_file:
                if isinstance(ef, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=f"file:{ef}",
                    ))
                elif isinstance(ef, dict):
                    # compose-spec: { path: ..., required: true }
                    path = ef.get("path")
                    if isinstance(path, str):
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=None,
                            kind="USES_CONFIG", confidence="strong",
                            raw_target=f"file:{path}",
                        ))

        # volumes → MOUNTS
        vols = spec.get("volumes", [])
        if isinstance(vols, list):
            for v in vols:
                if isinstance(v, str):
                    # "named-volume:/path/inside" or "/host/path:/container/path"
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="MOUNTS", confidence="strong",
                        raw_target=v,
                    ))
                elif isinstance(v, dict):
                    # Long syntax: { type, source, target }
                    src = v.get("source")
                    tgt = v.get("target")
                    if isinstance(src, str) and isinstance(tgt, str):
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=None,
                            kind="MOUNTS", confidence="strong",
                            raw_target=f"{src}:{tgt}",
                        ))

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
    # Duplicate service name in one file — invalid compose, but handled safely
    return f"{base}@{len(ctx.seen_ids)}"
