"""Dockerfile extractor (Stage 4.1, May 2026).

Covers: filename pattern Dockerfile* + *.Dockerfile

Tree-sitter-dockerfile node types (verified discovery):
    source_file                — root
    arg_instruction            — `ARG VERSION=3.13`
    from_instruction           — `FROM image:tag AS alias` (image_spec, image_alias)
    image_spec                 — image_name + optional image_tag
    label_instruction          — `LABEL key=value`
    env_instruction            — `ENV K1=V1 K2=V2`
    workdir_instruction        — `WORKDIR /app`
    copy_instruction           — `COPY src dst` (with optional `--from=X`, `--chown=...`)
    add_instruction            — `ADD url|src dst`
    run_instruction            — `RUN cmd...` shell or json
    user_instruction           — `USER app`
    expose_instruction         — `EXPOSE 8000 8080/tcp`
    volume_instruction         — `VOLUME /data /logs`
    entrypoint_instruction     — `ENTRYPOINT [...]` or shell
    cmd_instruction            — `CMD [...]`
    healthcheck_instruction    — `HEALTHCHECK [params] CMD ...`
    onbuild_instruction        — `ONBUILD <inst>`
    stopsignal_instruction     — `STOPSIGNAL SIGTERM`
    shell_instruction          — `SHELL [...]`
    maintainer_instruction     — deprecated, but still parsed

Symbol model:
    file_module  — synthetic per Dockerfile
    image        — for every FROM stage. Name = alias if present, else "stage_N"
                   Multi-stage Dockerfiles have several image symbols.

Edge model:
    BUILT_FROM   — image → base image (from FROM image_spec)
    EXPOSES      — image → port string ("8000", "8080/tcp")
    COPIES_FROM  — image → previous stage alias (from COPY --from=X)
    USES_CONFIG  — image → env var name (from ENV K=V)
    IMPORTS      — file_module → external URL (from ADD https://...)

What is NOT covered:
    - HEREDOC syntax in RUN/COPY (Docker 24+) — as shell_command text
    - BuildKit secrets / mounts — as param tokens
    - SHELL inheritance between stages
    - Dockerfile linting (Hadolint-style)

Stage: 4.1. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


@dataclass
class _StageState:
    """A single FROM stage in a multi-stage Dockerfile."""

    image_id: str
    name: str  # alias if present, else "stage_N"
    base_image: str  # text from image_spec (for the BUILT_FROM target)
    line: int


@dataclass
class _WalkContext:
    file: str
    source: bytes
    stages: list[_StageState] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)

    @property
    def current_stage(self) -> _StageState | None:
        return self.stages[-1] if self.stages else None


class DockerfileExtractor(SymbolExtractor):
    language = "dockerfile"
    extensions = ()  # dispatch via filename pattern, not extension

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
            parser = get_parser("dockerfile")
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"parser_init_failed: {e}"])

        tree = parser.parse(source)
        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel, source=source)
        result = ExtractionResult()

        if tree.root_node.has_error:
            result.parse_errors.append(f"parse_errors_present file={rel}")

        # File-module symbol
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).name, kind="file_module",
            file=rel, start_line=1, end_line=None, language="dockerfile",
        ))
        ctx.seen_ids.add(file_sym_id)

        for child in tree.root_node.named_children:
            self._dispatch_instruction(child, ctx, result)

        # DEFINED_IN edges from all symbols → file_module
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _dispatch_instruction(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        t = node.type
        if t == "from_instruction":
            self._handle_from(node, ctx, result)
        elif t == "expose_instruction":
            self._handle_expose(node, ctx, result)
        elif t == "copy_instruction":
            self._handle_copy(node, ctx, result)
        elif t == "add_instruction":
            self._handle_add(node, ctx, result)
        elif t == "env_instruction":
            self._handle_env(node, ctx, result)
        elif t == "onbuild_instruction":
            # Recurse — onbuild wraps other instructions
            for inner in node.named_children:
                self._dispatch_instruction(inner, ctx, result)

    def _handle_from(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`FROM image:tag AS alias` — creates an image symbol + BUILT_FROM edge."""
        # image_spec — base image
        image_spec = next(
            (c for c in node.named_children if c.type == "image_spec"),
            None,
        )
        # image_alias — AS name
        alias_node = next(
            (c for c in node.named_children if c.type == "image_alias"),
            None,
        )

        base_image = ""
        if image_spec is not None:
            base_image = image_spec.text.decode("utf-8", errors="replace").strip()

        if alias_node is not None:
            stage_name = alias_node.text.decode("utf-8", errors="replace")
        else:
            stage_name = f"stage_{len(ctx.stages)}"

        line = node.start_point[0] + 1
        image_id = _make_id(ctx, stage_name, line)
        result.symbols.append(SymbolInfo(
            id=image_id, name=stage_name, kind="image",
            file=ctx.file, start_line=line, end_line=node.end_point[0] + 1,
            language="dockerfile", is_exported=True,
        ))
        ctx.seen_ids.add(image_id)

        # BUILT_FROM edge → base image (text-based target)
        if base_image:
            result.edges.append(EdgeInfo(
                from_id=image_id, to_id=None,
                kind="BUILT_FROM", confidence="unresolved",
                raw_target=base_image,
            ))

        ctx.stages.append(_StageState(
            image_id=image_id, name=stage_name,
            base_image=base_image, line=line,
        ))

    def _handle_expose(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`EXPOSE 8000 8080/tcp` — port edges from the current stage."""
        stage = ctx.current_stage
        # stage is None → EXPOSE before the first FROM (rare); it attaches to
        # file_module instead.
        from_id = _file_symbol_id(ctx.file) if stage is None else stage.image_id

        for port_node in node.named_children:
            if port_node.type != "expose_port":
                continue
            port = port_node.text.decode("utf-8", errors="replace").strip()
            result.edges.append(EdgeInfo(
                from_id=from_id, to_id=None,
                kind="EXPOSES", confidence="strong",
                raw_target=port,
            ))

    def _handle_copy(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`COPY src dst` — no edges if it is an ordinary one.
        `COPY --from=stage src dst` → COPIES_FROM edge to the stage.
        """
        stage = ctx.current_stage
        if stage is None:
            return

        # Check params for `--from=X`
        from_stage: str | None = None
        for child in node.named_children:
            if child.type != "param":
                continue
            param_text = child.text.decode("utf-8", errors="replace")
            # `--from=builder` → 'builder'
            if param_text.startswith("--from="):
                from_stage = param_text[len("--from="):].strip()
                break

        if from_stage:
            result.edges.append(EdgeInfo(
                from_id=stage.image_id, to_id=None,
                kind="COPIES_FROM", confidence="unresolved",
                raw_target=from_stage,
            ))

    def _handle_add(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`ADD url dst` — if src is a URL → IMPORTS edge."""
        # First path = src, last = dst
        paths = [c for c in node.named_children if c.type == "path"]
        if len(paths) < 2:
            return
        src = paths[0].text.decode("utf-8", errors="replace").strip()

        if src.startswith(("http://", "https://", "git://", "ftp://")):
            from_id = _file_symbol_id(ctx.file)
            result.edges.append(EdgeInfo(
                from_id=from_id, to_id=None,
                kind="IMPORTS", confidence="strong",
                raw_target=src,
            ))

    def _handle_env(
        self, node, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """`ENV K=V` — one env_pair → USES_CONFIG edge from the current stage."""
        stage = ctx.current_stage
        if stage is None:
            return

        for pair in node.named_children:
            if pair.type != "env_pair":
                continue
            # env_pair has key + value (unquoted_string + unquoted_string)
            key_node = next(
                (c for c in pair.named_children if c.type == "unquoted_string"),
                None,
            )
            if key_node is None:
                continue
            key = key_node.text.decode("utf-8", errors="replace")
            result.edges.append(EdgeInfo(
                from_id=stage.image_id, to_id=None,
                kind="USES_CONFIG", confidence="strong",
                raw_target=key,
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


def _make_id(ctx: _WalkContext, name: str, line: int) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{line}"
