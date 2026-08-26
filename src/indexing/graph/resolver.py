"""Heuristic name resolver — Phase 5c.

Works over unresolved EdgeInfo (where to_id is None) after the extractors have
run. Strategy:

1. **External deps** — `import axios from 'axios'` → in
   RepoContext.external_deps: tag as external, **we drop the edge** (the graph
   does not go beyond the repo).

2. **Path alias resolution** — `import x from '@/utils/foo'`:
   via RepoContext.path_aliases ('@/' → 'src/') → we look for
   `src/utils/foo.{ts,tsx,js,jsx,cjs,mjs,vue}`.

3. **Relative path resolution** — `import x from '../bar'`:
   relative to the importing file → resolve inside repo_root.

4. **Bare path** (no alias and no `.` prefix):
   try it as an alias-prefix (baseUrl may be configured without paths),
   then from repo_root.

5. **CALLS resolution** — after the symbols have been inserted into the store,
   we have symbol_index: name → list[symbol_id].
   - If unique → strong edge
   - >1 → weak edge to the first one (the LLM will sort it out)
   - 0 → skip (external or dynamic)

Expected precision: 80-85% strong + ~10% weak + ~5% unresolved.

Phase: 5c. Implemented.
"""

from __future__ import annotations

import logging
import os.path
from collections import defaultdict
from pathlib import Path

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, SymbolInfo

logger = logging.getLogger(__name__)


# Extensions that can be resolution targets
_RESOLVABLE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".vue")
# Index files that the JS/TS resolver proxies to
_INDEX_FILES = tuple(f"index{e}" for e in _RESOLVABLE_EXTS)


class HeuristicResolver:
    """Resolution of unresolved CALLS and IMPORTS edges.

    Created once after extraction, holds the symbol_index and the RepoContext.
    """

    def __init__(self, ctx: RepoContext, symbols: list[SymbolInfo]):
        self.ctx = ctx
        # name → list[symbol_id]
        self.by_name: dict[str, list[str]] = defaultdict(list)
        # file → symbol_id (for DEFINED_IN edges, optional)
        self.by_file_default: dict[str, str] = {}

        for s in symbols:
            self.by_name[s.name].append(s.id)
            # Default export — synthetic "default" symbol per file.
            # We check kind, not name — in `export default IDENT` symbol.name
            # is now = the identifier text, not "default" (for DoD
            # discoverability).
            if s.kind == "export_default":
                self.by_file_default[s.file] = s.id

    # ─── public API ────────────────────────────────────────────────

    def resolve_edges(
        self,
        edges: list[EdgeInfo],
        from_file_lookup: dict[str, str] | None = None,
    ) -> list[EdgeInfo]:
        """Resolve un-resolved edges. Returns a NEW list (does not mutate).

        Args:
            edges: edges from extractors (may have to_id=None).
            from_file_lookup: optional mapping symbol_id → file. If None —
                              we pull it from self.by_name (slower).
        """
        out: list[EdgeInfo] = []
        stats = {"strong": 0, "weak": 0, "external": 0, "unresolved": 0, "kept": 0}

        for edge in edges:
            if edge.to_id is not None:
                # already resolved
                out.append(edge)
                stats["kept"] += 1
                continue

            if edge.kind == "IMPORTS":
                resolved = self._resolve_import(edge)
            elif edge.kind == "CALLS":
                resolved = self._resolve_call(edge)
            else:
                resolved = edge  # no special logic

            if resolved is None:
                stats["external"] += 1
                continue  # external dep — we do not add it to the graph

            out.append(resolved)
            if resolved.confidence == "strong":
                stats["strong"] += 1
            elif resolved.confidence == "weak":
                stats["weak"] += 1
            elif resolved.confidence == "unresolved":
                stats["unresolved"] += 1

        logger.info(
            "resolver_stats kept=%d strong=%d weak=%d unresolved=%d external_dropped=%d",
            stats["kept"], stats["strong"], stats["weak"], stats["unresolved"], stats["external"],
        )
        return out

    # ─── IMPORTS resolution ────────────────────────────────────────

    def _resolve_import(self, edge: EdgeInfo) -> EdgeInfo | None:
        """raw_target format: '<source_path>::<name>' or '<source_path>'."""
        if not edge.raw_target:
            return self._unresolved(edge)

        if "::" in edge.raw_target:
            source_path, name = edge.raw_target.split("::", 1)
        else:
            source_path = edge.raw_target
            name = ""  # side-effect import

        # 1. External dep?
        if self._is_external(source_path):
            return None  # we drop the edge

        # 2. Find the target file
        target_file = self._resolve_path(source_path, edge.from_id)
        if target_file is None:
            return self._unresolved(edge)

        # 3. Find the symbol in target_file
        target_id = self._find_symbol_in_file(target_file, name)
        if target_id is None:
            # The file was found but the symbol was not — that is OK for
            # side-effect imports or when the export was not registered
            # (because the extractor may have holes)
            return EdgeInfo(
                from_id=edge.from_id,
                to_id=None,
                kind=edge.kind,
                confidence="unresolved",
                raw_target=edge.raw_target,
            )

        return EdgeInfo(
            from_id=edge.from_id,
            to_id=target_id,
            kind=edge.kind,
            confidence="strong",
            raw_target=edge.raw_target,
        )

    # ─── CALLS resolution ──────────────────────────────────────────

    def _resolve_call(self, edge: EdgeInfo) -> EdgeInfo:
        """Simple unique-name match. Unresolved ones keep
        confidence='unresolved'."""
        if not edge.raw_target:
            return self._unresolved(edge)

        candidates = self.by_name.get(edge.raw_target, [])

        if not candidates:
            # External function or dynamic — we leave it unresolved
            return self._unresolved(edge)

        if len(candidates) == 1:
            return EdgeInfo(
                from_id=edge.from_id,
                to_id=candidates[0],
                kind=edge.kind,
                confidence="strong",
                raw_target=edge.raw_target,
            )

        # ambiguous: several symbols with the same name in the repo
        return EdgeInfo(
            from_id=edge.from_id,
            to_id=candidates[0],  # the first one — determinism
            kind=edge.kind,
            confidence="weak",
            raw_target=edge.raw_target,
        )

    # ─── path resolution ───────────────────────────────────────────

    def _is_external(self, source_path: str) -> bool:
        """`axios`, `lodash/get`, `@scoped/pkg` — external if in the
        package.json deps."""
        if not source_path:
            return False
        if source_path.startswith((".", "/")):
            return False
        # The first segment is the package
        first = source_path.split("/", 1)[0]
        if first.startswith("@"):  # @scope/pkg
            scope = first
            second = source_path.split("/", 2)
            pkg = "/".join(second[:2]) if len(second) >= 2 else scope
            return pkg in self.ctx.external_deps or scope in self.ctx.external_deps
        return first in self.ctx.external_deps

    def _resolve_path(self, source_path: str, from_id: str) -> str | None:
        """source_path → path relative to the repo root, or None.

        from_id may contain a file path for resolving relative imports.
        """
        if not source_path:
            return None

        # Aliases (from tsconfig paths)
        for prefix, target in self.ctx.path_aliases.items():
            if source_path.startswith(prefix):
                rest = source_path[len(prefix):]
                candidate = f"{target}{rest}"
                resolved = self._try_extensions(candidate)
                if resolved:
                    return resolved
            # Some tsconfig paths contain "src/*": [...] — that is, a prefix
            # without a backslash. We catch both variants via rstrip("/")
            prefix_no_slash = prefix.rstrip("/")
            if prefix_no_slash and source_path == prefix_no_slash:
                resolved = self._try_extensions(target.rstrip("/"))
                if resolved:
                    return resolved

        # Relative: ../bar, ./foo
        if source_path.startswith("."):
            from_file = self._extract_file_from_id(from_id)
            if from_file:
                base_dir = Path(from_file).parent
                # os.path.normpath normalizes the `..` segments
                candidate = os.path.normpath(str(base_dir / source_path)).replace("\\", "/")
                resolved = self._try_extensions(candidate)
                if resolved:
                    return resolved

        # No alias prefix and no `.` — we try it as baseUrl-relative
        # (tsconfig often has baseUrl='.', and `import x from 'utils/foo'`
        # works).
        resolved = self._try_extensions(source_path)
        if resolved:
            return resolved

        return None

    def _try_extensions(self, path_no_ext: str) -> str | None:
        """Try appending an extension or /index.{ext} to path_no_ext.

        As long as we have no filesystem check — we use only the
        `by_file_default` map + a simple "first symbol referenced this file"
        lookup. The alternative is a real stat() on disk. YAGNI for the MVP:
        we just generate candidates and remember them as a "potential relative
        path". The resolver will then expect that a symbol from this file
        exists in by_name.
        """
        # Normalize
        path_no_ext = path_no_ext.replace("//", "/")
        if path_no_ext.endswith("/"):
            path_no_ext = path_no_ext.rstrip("/")

        # If the file already has an extension → take it as is
        if Path(path_no_ext).suffix in _RESOLVABLE_EXTS:
            return path_no_ext

        # We check inside repo_root
        repo_root = self.ctx.repo_root
        # 1) plain path + ext
        for ext in _RESOLVABLE_EXTS:
            candidate = f"{path_no_ext}{ext}"
            if (repo_root / candidate).is_file():
                return candidate
        # 2) directory/index.{ext}
        for idx in _INDEX_FILES:
            candidate = f"{path_no_ext}/{idx}"
            if (repo_root / candidate).is_file():
                return candidate

        return None

    # ─── lookups ───────────────────────────────────────────────────

    def _find_symbol_in_file(self, file: str, name: str) -> str | None:
        """Find the symbol_id with file=file and name=name."""
        if not name:
            # side-effect import — it can be tied to the file as the symbol
            # named 'default' if there is one; otherwise None
            return self.by_file_default.get(file)

        if name == "default":
            return self.by_file_default.get(file)

        # Linear search (~25k symbols in memory — fast). If it starts to hurt,
        # we will add a by_file → list[symbol_id] cache.
        candidates = self.by_name.get(name, [])
        for sid in candidates:
            if sid.startswith(f"{file}::"):
                return sid
        return None

    @staticmethod
    def _extract_file_from_id(symbol_id: str) -> str | None:
        """symbol_id = 'file::name' or 'file' — return the file part."""
        if "::" in symbol_id:
            return symbol_id.split("::", 1)[0]
        return symbol_id

    @staticmethod
    def _unresolved(edge: EdgeInfo) -> EdgeInfo:
        return EdgeInfo(
            from_id=edge.from_id,
            to_id=None,
            kind=edge.kind,
            confidence="unresolved",
            raw_target=edge.raw_target,
        )
