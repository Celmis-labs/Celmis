"""Factory: build the default LanguageRegistry with all available extractors.

Every implemented language is registered here as a separate entry. Settings
`enabled_language_adapters` later filters this registry.

Implemented extractors (Stage 2 — May 2026):
    typescript: .ts/.tsx/.js/.jsx/.cjs/.mjs   — Phase 5a
    vue:        .vue (with injection into TypeScript) — Phase 5b
    [Pending: python, go, php, java, csharp, cpp, dockerfile, compose, k8s]
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.languages.registry import LanguageRegistry

logger = logging.getLogger(__name__)


def build_default_registry(
    ctx: RepoContext | None = None,
    repo_root: Path | None = None,
    *,
    enabled: set[str] | None = None,
) -> LanguageRegistry:
    """Create a LanguageRegistry with all available extractors.

    Args:
        ctx: RepoContext (for resolver enrichment in the extractors)
        repo_root: for computing relative paths
        enabled: subset of names (Settings.enabled_language_adapters).
                 None → all extractors registered. Empty set → none.
    """
    registry = LanguageRegistry()

    # Lazy import — so a user can add/remove an extractor without circular deps
    from src.indexing.graph.languages.ci_workflow import (
        CIWorkflowExtractor,
        is_ci_workflow_file,
    )
    from src.indexing.graph.languages.compose import DockerComposeExtractor
    from src.indexing.graph.languages.cpp import CppExtractor
    from src.indexing.graph.languages.csharp import CSharpExtractor
    from src.indexing.graph.languages.dockerfile import DockerfileExtractor
    from src.indexing.graph.languages.go import GoExtractor
    from src.indexing.graph.languages.helm import HelmExtractor, is_helm_chart_yaml
    from src.indexing.graph.languages.java import JavaExtractor
    from src.indexing.graph.languages.k8s import K8sManifestExtractor, is_k8s_manifest
    from src.indexing.graph.languages.php import PHPExtractor
    from src.indexing.graph.languages.python import PythonExtractor
    from src.indexing.graph.languages.tags import TagsExtractor
    from src.indexing.graph.languages.terraform import TerraformExtractor
    from src.indexing.graph.languages.typescript import TypeScriptExtractor
    from src.indexing.graph.languages.vue import VueExtractor

    # ─── TypeScript / JavaScript family ─────────────────────────────
    # A single class covers .ts/.tsx/.js/.jsx/.cjs/.mjs
    registry.register(
        TypeScriptExtractor(ctx=ctx, repo_root=repo_root),
        name="typescript",
        priority=10,
        extensions=(".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".cts", ".mts"),
    )

    # ─── Vue SFC ────────────────────────────────────────────────────
    # Priority > TS — so that .vue files are picked up by Vue, not by the TS fallback.
    # The Vue extractor itself delegates the script content to TypeScript.
    registry.register(
        VueExtractor(ctx=ctx, repo_root=repo_root),
        name="vue",
        priority=20,
        extensions=(".vue",),
    )

    # ─── Python (Stage 2.B.1) ───────────────────────────────────────
    registry.register(
        PythonExtractor(ctx=ctx, repo_root=repo_root),
        name="python",
        priority=10,
        extensions=(".py", ".pyi"),
    )

    # ─── Go (Stage 2.B.2) ───────────────────────────────────────────
    registry.register(
        GoExtractor(ctx=ctx, repo_root=repo_root),
        name="go",
        priority=10,
        extensions=(".go",),
    )

    # ─── PHP (Stage 2.B.3) ──────────────────────────────────────────
    registry.register(
        PHPExtractor(ctx=ctx, repo_root=repo_root),
        name="php",
        priority=10,
        extensions=(".php", ".phtml"),
    )

    # ─── Java (Stage 2.B.4) ─────────────────────────────────────────
    registry.register(
        JavaExtractor(ctx=ctx, repo_root=repo_root),
        name="java",
        priority=10,
        extensions=(".java",),
    )

    # ─── C# (Stage 2.B.5) ───────────────────────────────────────────
    registry.register(
        CSharpExtractor(ctx=ctx, repo_root=repo_root),
        name="csharp",
        priority=10,
        extensions=(".cs", ".csx"),
    )

    # ─── C++ (Stage 2.B.6) ──────────────────────────────────────────
    # NOTE: .h shared between C and C++. CppExtractor parses it as C++ (superset
    # syntax) — for pure C the tests may give worse results but not fail.
    registry.register(
        CppExtractor(ctx=ctx, repo_root=repo_root),
        name="cpp",
        priority=10,
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h", ".c"),
    )

    # ─── Infrastructure (Stage 4 — Phase C) ─────────────────────────
    # Dockerfile — filename pattern (no extension): Dockerfile, Dockerfile.*,
    # *.Dockerfile. Priority high so it does not conflict with generic YAML.
    registry.register(
        DockerfileExtractor(ctx=ctx, repo_root=repo_root),
        name="dockerfile",
        priority=20,
        filename_patterns=("Dockerfile", "Dockerfile.*", "*.Dockerfile"),
    )

    # Docker Compose — filename pattern: docker-compose.yml, compose.yml +
    # variants with an override (.prod.yml, .dev.yml and so on).
    # Priority higher than k8s, so that compose files do not land in the K8s sniffer.
    registry.register(
        DockerComposeExtractor(ctx=ctx, repo_root=repo_root),
        name="compose",
        priority=20,
        filename_patterns=(
            "docker-compose.yml", "docker-compose.yaml",
            "docker-compose.*.yml", "docker-compose.*.yaml",
            "compose.yml", "compose.yaml",
            "compose.*.yml", "compose.*.yaml",
        ),
    )

    # K8s manifests — content-sniff (apiVersion + kind in the first ~4KB).
    # Priority lower than compose, because compose has a filename match while K8s
    # can be any *.yaml. If a file matches the compose pattern it goes there.
    registry.register(
        K8sManifestExtractor(ctx=ctx, repo_root=repo_root),
        name="k8s",
        priority=5,
        content_sniffer=is_k8s_manifest,
    )

    # ─── Helm charts (Stage 12.2) ──────────────────────────────────
    # Filename pattern + content sniff. Chart.yaml + values.yaml + requirements.yaml.
    # Priority higher than k8s, because Chart.yaml structurally has `apiVersion: v2`
    # which can false-positive in the k8s sniffer (the k8s sniffer also checks for
    # `kind:` — Chart.yaml has none, so in practice it would slip past the k8s
    # sniffer anyway. But a filename match is faster).
    registry.register(
        HelmExtractor(ctx=ctx, repo_root=repo_root),
        name="helm",
        priority=15,
        filename_patterns=(
            "Chart.yaml", "Chart.yml",
            "values.yaml", "values.yml",
            "requirements.yaml", "requirements.yml",
        ),
        content_sniffer=is_helm_chart_yaml,
    )

    # ─── Terraform (Stage 12.1) ────────────────────────────────────
    registry.register(
        TerraformExtractor(ctx=ctx, repo_root=repo_root),
        name="terraform",
        priority=10,
        extensions=(".tf",),
    )

    # ─── CI/CD workflows (Stage 13) ────────────────────────────────
    # Multi-provider detection via filename + path matchers.
    # Priority HIGHER than compose+k8s because the CI patterns are explicit (no overlap
    # with K8s manifests, which are content-sniffed).
    registry.register(
        CIWorkflowExtractor(ctx=ctx, repo_root=repo_root),
        name="ci_workflow",
        priority=18,
        filename_patterns=(
            ".gitlab-ci.yml", ".gitlab-ci.yaml",
            "bitbucket-pipelines.yml", "bitbucket-pipelines.yaml",
        ),
        # Path-based: GitHub Actions (.github/workflows/*.yml|*.yaml)
        path_matcher=is_ci_workflow_file,
    )

    # ─── Everything else the grammar authors wrote a tags query for ──
    #
    # Registered LAST and at the lowest priority on purpose: where a
    # hand-written extractor exists it resolves imports and module paths a
    # tags query cannot see, and must keep the file. This is the floor for
    # the languages that had nothing at all — measured on discourse, 8185
    # `.rb` files produced exactly zero symbols before it.
    for language, extensions in TAGS_LANGUAGES:
        registry.register(
            TagsExtractor(language, extensions, ctx=ctx, repo_root=repo_root),
            name=language,
            priority=1,
            extensions=extensions,
        )

    if enabled is not None:
        registry = registry.filter(enabled)
    logger.info("registry_built extractors=%s", registry.names())
    return registry


#: Languages served by the generic tags-query extractor, with the suffixes the
#: registry dispatches on. Every one of these has a tags query in the installed
#: `tree-sitter-language-pack`; the seven languages that already have a
#: hand-written extractor are deliberately absent, because theirs resolves
#: imports and module paths this cannot.
#:
#: Ambiguous suffixes are left OUT rather than guessed: `.m` is Objective-C and
#: MATLAB, `.cls` is Apex and a VB class module, `.cl` is Common Lisp and
#: OpenCL. A file indexed as the wrong language is worse than a file not
#: indexed, because the graph then states something false about it.
TAGS_LANGUAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruby", (".rb", ".rake", ".gemspec")),
    ("rust", (".rs",)),
    ("kotlin", (".kt", ".kts")),
    ("swift", (".swift",)),
    ("scala", (".scala", ".sc")),
    ("elixir", (".ex", ".exs")),
    ("dart", (".dart",)),
    ("lua", (".lua",)),
    ("r", (".r",)),
    ("solidity", (".sol",)),
    ("ocaml", (".ml", ".mli")),
    ("fsharp", (".fs", ".fsi", ".fsx")),
    ("elm", (".elm",)),
    ("gleam", (".gleam",)),
    ("racket", (".rkt",)),
    ("fortran", (".f90", ".f95", ".f03", ".f08")),
)


@lru_cache(maxsize=1)
def supported_suffixes() -> frozenset[str]:
    """Every file suffix some extractor in the default registry dispatches on.

    ONE source of truth, because there were two and they drifted. The review
    side kept its own hand-copied list of "suffixes the indexer parses" to
    decide whether a changed file missing from the graph is a stale index or a
    language nobody parses — and a copy of a list is a list that goes stale the
    first time somebody adds a language. Derived here from the extractors
    themselves, so adding one updates the review's answer in the same commit.

    Kinds dispatched by FILENAME or content sniff (Dockerfile, compose, k8s,
    CI workflows) are deliberately absent: this answers "would a file with this
    suffix be parsed", and those are not chosen by suffix.
    """
    out: set[str] = set()
    for extractor in build_default_registry().extractors():
        for ext in getattr(extractor, "extensions", ()) or ():
            if ext.startswith("."):
                out.add(ext.lower())
    return frozenset(out)


# ─── File discovery helpers ──────────────────────────────────────────


# Directories that must NOT be analysed — third-party / build artifacts
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    # Version control
    ".git", ".hg", ".svn",
    # Python
    "__pycache__", ".venv", "venv", "env", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage",
    # Node / JS
    "node_modules", ".next", ".nuxt", "dist", "build", "out",
    # Go
    "vendor",
    # Java / JVM
    "target", ".gradle",
    # PHP
    "composer",  # composer.lock keeps but composer/ vendor cache excluded
    # IDE
    ".vscode", ".idea",
    # Other
    ".cache", ".DS_Store", "coverage", ".nyc_output",
})


def walk_repo_files(
    root: Path,
    *,
    ignore_dirs: frozenset[str] | None = None,
) -> list[Path]:
    """Walk the repo with ignore patterns. Returns absolute paths.

    No filtering by extension — that is done by LanguageRegistry.match().
    """
    ignore = ignore_dirs or DEFAULT_IGNORE_DIRS
    results: list[Path] = []
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip files that have ignore directories in their path
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in ignore for part in rel_parts):
            continue
        results.append(path)
    return results
