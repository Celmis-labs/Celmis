"""Module discovery — finding logical modules in a repo.

Strategies:
1. Graph communities (if CodeGraphContext provides that information)
2. Path-based heuristic: src/<module>/, lib/<module>/, components/<name>/
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings, get_settings

# v3.0: we dropped CGC. We keep SymbolInfo as a minimal shim for compatibility
# with ModulePRDGenerator (while we do not fill the module symbols with
# graph-witnessed data — path-based discovery stays).


@dataclass
class SymbolInfo:
    """Shim for the symbol shape `ModulePRDGenerator` consumes.

    Discovery itself is path-based and produces none; `enrich_with_graph`
    fills them in from the code graph once it has been built.
    """
    name: str = ""
    kind: str = ""
    file: str = ""
    line: int = 0
    end_line: int | None = None

logger = logging.getLogger(__name__)


@dataclass
class Module:
    """A logical module — a group of tightly related files/symbols."""

    name: str
    path: str  # relative, e.g. "src/auth"
    files: list[str] = field(default_factory=list)
    symbols: list[SymbolInfo] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "files": sorted(self.files),
            "symbols_count": len(self.symbols),
        }


class ModuleDiscovery:
    """Discovers modules in a repo. Uses both the graph (if available) and a path heuristic."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def discover(self, repo_path: Path) -> list[Module]:
        """Returns the list of modules in the repo.

        Strategy: path heuristic for the structure.
        Graph enrichment is currently disabled (the FalkorDB Cypher schema does not
        match our assumptions — we need to pin down the exact node labels and property
        names). Module.symbols stays empty; ModulePRDGenerator and Q&A have a
        disk-based fallback.
        """
        return self._from_paths(repo_path)

    def _from_paths(self, repo_path: Path) -> list[Module]:
        """Finds modules by looking for ALL src/lib/app directories down to depth 3,
        then splits them into subdirectories as modules.
        """
        extensions = set(self.settings.supported_extensions)
        candidates: dict[str, set[str]] = defaultdict(set)

        # Find all source-root-like directories
        source_roots = self._find_source_roots(repo_path, max_depth=3)

        def collect(target: Path, module_key: str) -> None:
            for p in target.rglob("*"):
                if not p.is_file() or p.suffix not in extensions:
                    continue
                if any(part in _IGNORE_DIRS for part in p.parts):
                    continue
                if _is_non_source_file(p):
                    continue
                candidates[module_key].add(str(p.relative_to(repo_path)))

        def explore(path: Path, rel: str, depth: int) -> None:
            """Recursive: if the directory is a 'group' (many subfolders with code),
            we go deeper. If it is a 'module' (few subfolders or mostly files) —
            we take all the files as a module.
            """
            # Hard depth limit — we do not go further than src/a/b/c/d
            if depth > 4:
                collect(path, rel)
                return

            try:
                subdirs = [
                    s for s in path.iterdir()
                    if s.is_dir()
                    and not s.name.startswith(".")
                    and s.name not in _IGNORE_DIRS
                ]
            except (PermissionError, OSError):
                return

            # Count the subfolders that have code inside
            subdirs_with_code = [s for s in subdirs if _has_source_code(s, extensions)]

            # "Group" = 3+ subfolders with code. Then we go deeper into each one.
            # Otherwise — it is a leaf module, we collect its files.
            if len(subdirs_with_code) >= 3:
                for s in subdirs_with_code:
                    explore(s, f"{rel}/{s.name}", depth + 1)
                # Plus the files directly in this directory (if any) — under a separate key
                direct_files = _direct_source_files(path, extensions, repo_path)
                if len(direct_files) >= 2:
                    candidates[f"{rel}/_root"].update(direct_files)
            else:
                collect(path, rel)

        def explore_root(root: Path, root_rel: str) -> None:
            for sub in sorted(root.iterdir()):
                if not sub.is_dir() or sub.name.startswith(".") or sub.name in _IGNORE_DIRS:
                    continue
                module_rel = f"{root_rel}/{sub.name}"
                explore(sub, module_rel, depth=1)

        if source_roots:
            for root, rel in source_roots:
                explore_root(root, rel)

        # Fallback: if nothing was found — top-level
        if not candidates:
            for sub in sorted(repo_path.iterdir()):
                if (not sub.is_dir()
                        or sub.name.startswith(".")
                        or sub.name in _IGNORE_DIRS):
                    continue
                collect(sub, sub.name)

        modules: list[Module] = []
        for path, files in sorted(candidates.items()):
            if not files:
                continue
            modules.append(
                Module(
                    name=_derive_name(path),
                    path=path,
                    files=sorted(files),
                    symbols=[],
                )
            )
        logger.info(
            "module_discovery count=%d source_roots=%d",
            len(modules),
            len(source_roots),
        )
        return modules

    @staticmethod
    def _find_source_roots(repo_path: Path, max_depth: int = 3) -> list[tuple[Path, str]]:
        """Finds all src/lib/app/... directories down to max_depth levels deep.

        Example: for acme/frontend it will find 'src'.
        """
        source_names = {"src", "lib", "app", "server", "client", "packages"}
        results: list[tuple[Path, str]] = []

        def walk(path: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                subs = list(path.iterdir())
            except (PermissionError, OSError):
                return
            for sub in subs:
                if not sub.is_dir() or sub.name.startswith(".") or sub.name in _IGNORE_DIRS:
                    continue
                if sub.name in source_names:
                    rel = sub.relative_to(repo_path)
                    results.append((sub, str(rel)))
                    # We do not go deeper into an already found source root
                    continue
                walk(sub, depth + 1)

        walk(repo_path, 0)
        return results


_IGNORE_DIRS: set[str] = {
    "node_modules", "dist", "build", ".next", "__tests__", "tests", "test",
    "__pycache__", ".cache", "coverage", "storybook-static", "out",
    "public", "static", "assets", "vendor", "bower_components", ".vscode",
    "template", "templates",
}

_NON_SOURCE_FILE_PATTERNS: set[str] = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", "jsconfig.json", "tsconfig.base.json",
    ".eslintrc.js", ".eslintrc.cjs", ".prettierrc.js",
    "tailwind.config.js", "postcss.config.js", "svgo.config.js",
    "webpack.config.js", "vite.config.js", "jest.config.js",
    "babel.config.js", ".babelrc", "commitlint.config.cjs",
    "webfontrc.json",
}


def _is_non_source_file(p: Path) -> bool:
    """True for config/lock files that we do not want in the PRD."""
    if p.name in _NON_SOURCE_FILE_PATTERNS:
        return True
    # '*.config.js', '*.config.cjs', '*.config.ts'
    if ".config." in p.name and p.suffix in {".js", ".cjs", ".ts", ".mjs"}:
        return True
    if p.name.startswith(".") and p.suffix in {".js", ".cjs", ".ts"}:
        return True
    # *.d.ts — TypeScript declaration files (interface, not logic)
    return p.name.endswith(".d.ts")


def _derive_name(path: str) -> str:
    """'src/auth' → 'auth'; 'src/components' → 'components'.
    If there is a collision — a prefix gets added."""
    source_roots = {"src", "lib", "app", "server", "client", "packages"}
    parts = [p for p in path.replace("\\", "/").split("/") if p and p not in source_roots]
    if not parts:
        return path.replace("/", "-")
    return "-".join(parts)


def _has_source_code(path: Path, extensions: set[str]) -> bool:
    """True if there is at least one source file under path (recursively)."""
    try:
        for p in path.rglob("*"):
            if (p.is_file()
                    and p.suffix in extensions
                    and not any(part in _IGNORE_DIRS for part in p.parts)
                    and not _is_non_source_file(p)):
                return True
    except (PermissionError, OSError):
        return False
    return False


def _direct_source_files(path: Path, extensions: set[str], repo_path: Path) -> set[str]:
    """Source files ONLY in this directory (no subfolders). Returns relative paths."""
    out: set[str] = set()
    try:
        for p in path.iterdir():
            if (p.is_file()
                    and p.suffix in extensions
                    and not _is_non_source_file(p)):
                out.add(str(p.relative_to(repo_path)))
    except (PermissionError, OSError):
        pass
    return out


def _avg_files_deep(path: Path) -> float:
    """Average number of files per subfolder — a heuristic to tell a 'group' from a 'module'."""
    try:
        subs = [s for s in path.iterdir() if s.is_dir()]
    except (PermissionError, OSError):
        return 0.0
    if not subs:
        return 0.0
    extensions = {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".php"}
    total = 0
    for s in subs:
        try:
            for p in s.rglob("*"):
                if p.is_file() and p.suffix in extensions:
                    total += 1
        except (PermissionError, OSError):
            continue
    return total / len(subs) if subs else 0.0


#: Per module. Enough to seed a graph expansion and to write a useful symbol
#: list into the note's frontmatter, without turning it into a dump.
MAX_SYMBOLS_PER_MODULE = 200


def enrich_with_graph(
    modules: list[Module],
    repo_path: Path,
    settings: Settings | None = None,
) -> int:
    """Fill `Module.symbols` from the code graph. Returns how many were added.

    Discovery is path-based and leaves `symbols=[]` — a v3.0 decision that
    nothing downstream was told about. The consequences were quiet and
    compounding:

      * the module note's frontmatter carried no `symbols` key, so
      * `detect_features` read an empty list from every note, so
      * every FeatureSpec got `seed_symbols=[]`, so
      * `GraphRetriever.expand` returned immediately without touching the
        graph, so
      * the feature generator had no code locations to read, and the model —
        correctly refusing to invent — wrote "the source code is absent" and
        marked every step, business rule and line reference as *not
        determined*.

    Verified on production before this existed: all 12 feature notes for one
    repository carried that disclaimer and not a single `file.py:line`
    reference, while its graph held 383 fields, 199 functions and 116 methods
    the whole time.

    Never raises: a repository with no graph yet degrades to what it did
    before, which is the path-based module list.
    """
    settings = settings or get_settings()
    try:
        from src.indexing.graph.graph_store import make_graph_store
    except Exception as exc:  # noqa: BLE001
        logger.warning("module_enrich_unavailable err=%s", exc)
        return 0

    total = 0
    # Slug is the clone's directory name — the same rule GraphRetriever uses.
    db_path = settings.repo_graph_path(repo_path.name)
    if not db_path.exists():
        logger.info("module_enrich_no_graph path=%s", db_path)
        return 0
    try:
        store = make_graph_store(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("module_enrich_open_failed path=%s err=%s", db_path, exc)
        return 0
    try:
        for module in modules:
            try:
                found = store.symbols_in_path(
                    module.path, limit=MAX_SYMBOLS_PER_MODULE,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("module_enrich_query_failed module=%s err=%s",
                               module.name, exc)
                continue
            module.symbols = [
                SymbolInfo(
                    name=s.name, kind=s.kind, file=s.file,
                    line=getattr(s, "start_line", 0) or 0,
                    end_line=getattr(s, "end_line", None),
                )
                for s in found if s.name
            ]
            total += len(module.symbols)
    finally:
        with contextlib.suppress(Exception):
            store.close()
    logger.info("module_enrich modules=%d symbols=%d", len(modules), total)
    return total
