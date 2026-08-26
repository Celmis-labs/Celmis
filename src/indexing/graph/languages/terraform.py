"""Terraform extractor (Stage 12.1, May 2026).

Covers: .tf (HCL syntax)

Tree-sitter-terraform node types (verified):
    config_file                  — root
    body                         — wrapper for the block/expression list
    block                        — `<identifier> ["label1"] ["label2"] { body }`
                                   identifier ∈ {terraform, variable, provider,
                                   module, resource, data, output, locals,
                                   moved, import, removed, check}

Block patterns (variants):
    terraform { ... }                    — meta config (providers, backend)
    variable "name" { ... }              — input variable
    provider "name" { ... }              — provider config
    module "alias" { source = "..." }    — module reference → IMPORTS edge
    resource "type" "name" { ... }       — managed resource
    data "type" "name" { ... }           — data source (read-only)
    output "name" { ... }                — output value
    locals { ... }                       — local values
    check "name" { ... }                 — Terraform 1.5+ checks

Symbol model (vendor namespace because it is Terraform-specific):
    file_module       — synthetic per .tf file
    variable          — input variable (with default + type)
    vendor.tf.provider — provider config (e.g. aws, google, azurerm)
    vendor.tf.resource — managed resource (e.g. aws_instance.web)
    vendor.tf.data     — data source (e.g. aws_ami.ubuntu)
    vendor.tf.module   — module reference (e.g. module.vpc)
    vendor.tf.output   — output value

Edge model:
    IMPORTS — module symbol → external source path (Git URL, registry path)
              `source = "terraform-aws-modules/vpc/aws"` → IMPORTS edge

What is NOT covered:
    - Resource attribute interpolation graph (`var.region` references)
    - For_each / count expressions
    - Provider aliases
    - Workspace/state metadata

Stage: 12.1. Implemented (MVP).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


# Top-level block identifier → kind mapping
_BLOCK_KIND = {
    "variable": "variable",
    "provider": "vendor.tf.provider",
    "module": "vendor.tf.module",
    "resource": "vendor.tf.resource",
    "data": "vendor.tf.data",
    "output": "vendor.tf.output",
    "locals": "vendor.tf.locals",
}


@dataclass
class _WalkContext:
    file: str
    source: bytes
    seen_ids: set[str] = field(default_factory=set)


class TerraformExtractor(SymbolExtractor):
    language = "terraform"
    extensions = (".tf",)

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
            parser = get_parser("terraform")
        except Exception as e:  # noqa: BLE001
            return ExtractionResult(parse_errors=[f"parser_init_failed: {e}"])

        tree = parser.parse(source)
        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel, source=source)
        result = ExtractionResult()

        if tree.root_node.has_error:
            result.parse_errors.append(f"parse_errors_present file={rel}")

        # File-module
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).stem, kind="file_module",
            file=rel, start_line=1, end_line=None, language="terraform",
        ))
        ctx.seen_ids.add(file_sym_id)

        # Walk top-level body → blocks
        body = next(
            (c for c in tree.root_node.named_children if c.type == "body"),
            None,
        )
        if body is not None:
            for block in body.named_children:
                if block.type == "block":
                    self._handle_block(block, ctx, result)

        # DEFINED_IN edges
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _handle_block(self, block, ctx: _WalkContext, result: ExtractionResult) -> None:
        """Block structure: identifier + optional string_lit labels + body."""
        children = block.named_children
        if not children:
            return

        ident = children[0]
        if ident.type != "identifier":
            return
        block_kind = ident.text.decode("utf-8", errors="replace")

        # Skip non-symbol blocks (terraform, moved, import, removed, check)
        if block_kind in ("terraform", "moved", "import", "removed", "check"):
            return

        # Extract labels (between identifier and block_start)
        labels: list[str] = []
        for child in children[1:]:
            if child.type == "string_lit":
                labels.append(_strip_quotes(child.text.decode("utf-8", errors="replace")))
            elif child.type == "block_start":
                break

        # locals has no label — skip per-binding (for MVP simplicity)
        if block_kind == "locals":
            return

        # Construct symbol name
        if block_kind == "resource" or block_kind == "data":
            # 2 labels: type + name
            if len(labels) < 2:
                return
            sym_name = f"{labels[0]}.{labels[1]}"
            module_label = labels[0]
        elif block_kind in ("variable", "provider", "module", "output"):
            # 1 label: name
            if not labels:
                return
            sym_name = labels[0]
            module_label = None
        else:
            return

        line = block.start_point[0] + 1
        kind = _BLOCK_KIND.get(block_kind, f"vendor.tf.{block_kind}")
        sym_id = _make_id(ctx, f"{block_kind}.{sym_name}", line)

        result.symbols.append(SymbolInfo(
            id=sym_id, name=sym_name, kind=kind,
            file=ctx.file, start_line=line, end_line=block.end_point[0] + 1,
            language="terraform", is_exported=True,
            module=module_label,
        ))
        ctx.seen_ids.add(sym_id)

        # Module IMPORTS — extract source = "..." attribute
        if block_kind == "module":
            source_value = self._extract_attribute_value(block, "source")
            if source_value:
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind="IMPORTS", confidence="unresolved",
                    raw_target=source_value,
                ))

    def _extract_attribute_value(self, block, attr_name: str) -> str | None:
        """Find `attr_name = "value"` inside the block body. None if not found."""
        body = next(
            (c for c in block.named_children if c.type == "body"),
            None,
        )
        if body is None:
            return None

        for child in body.named_children:
            if child.type != "attribute":
                continue
            # attribute structure: identifier '=' expression
            ident_node = next(
                (c for c in child.named_children if c.type == "identifier"),
                None,
            )
            if ident_node is None:
                continue
            ident_text = ident_node.text.decode("utf-8", errors="replace")
            if ident_text != attr_name:
                continue

            # Value — last named child after '=' which has expression-like type
            for c in reversed(child.named_children):
                if c.type == "expression":
                    # Unwrap expression → string_lit
                    inner = c.named_children
                    if inner:
                        return _extract_string_value(inner[0])
                    continue
                if c is ident_node:
                    break
                value_str = _extract_string_value(c)
                if value_str:
                    return value_str
        return None

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


# ─── helpers ────────────────────────────────────────────────────────


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str, line: int) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{line}"


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        return s[1:-1]
    return s


def _extract_string_value(node) -> str | None:
    """Recursively find string_lit text in the node tree."""
    t = node.type
    if t in ("string_lit", "template_literal", "quoted_template", "string"):
        return _strip_quotes(node.text.decode("utf-8", errors="replace"))
    # Recurse — for wrapper nodes
    for child in node.named_children:
        v = _extract_string_value(child)
        if v is not None:
            return v
    return None
