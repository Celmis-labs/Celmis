"""LanguageRegistry — centralized lookup of file_path → SymbolExtractor.

Instead of a hardcoded `if .vue else ts` in pipeline.py, every extractor now
registers itself in the registry. File matching uses a 3-tier strategy:

    1. Extension match (fast, deterministic) — most files land here
    2. Filename pattern match (Dockerfile, docker-compose.yml — without an
       extension or with a specific name)
    3. Content sniff (for .yaml files which may be K8s/CI/generic) — opens the
       file, reads the first ~5 lines, runs the registered sniffer

API:
    registry = LanguageRegistry()
    registry.register(PythonExtractor(...), priority=10)
    extractor = registry.match(Path("foo/bar.py"))  → PythonExtractor instance
    extractor = registry.match(Path("foo/k8s.yaml")) → K8sExtractor (after content sniff)

Stage 2.A.2 — implemented.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.indexing.graph.extractor import SymbolExtractor

logger = logging.getLogger(__name__)


# Extension/filename matchers return a bool quickly, without I/O
ExtensionMatcher = Callable[[Path], bool]

# Content sniffers — receive the first ~4KB of the file, return a bool
ContentSniffer = Callable[[bytes], bool]


@dataclass
class _Registration:
    """One registered extractor + metadata."""

    extractor: SymbolExtractor
    name: str  # "typescript" / "python" / ... — for logging + Settings.enabled_language_adapters
    priority: int  # higher = checked first
    extension_matcher: ExtensionMatcher | None = None
    filename_matcher: ExtensionMatcher | None = None
    path_matcher: ExtensionMatcher | None = None  # full Path-based (e.g. .github/workflows/)
    content_sniffer: ContentSniffer | None = None

    def matches(self, file_path: Path, content_head: bytes | None = None) -> bool:
        """Whether this extractor is responsible for the file."""
        if self.extension_matcher and self.extension_matcher(file_path):
            return True
        if self.filename_matcher and self.filename_matcher(file_path):
            return True
        if self.path_matcher and self.path_matcher(file_path):
            return True
        if self.content_sniffer and content_head is not None:
            try:
                return self.content_sniffer(content_head)
            except Exception as e:  # noqa: BLE001
                logger.debug("sniffer_error name=%s err=%s", self.name, e)
                return False
        return False


@dataclass
class LanguageRegistry:
    """Registry of all extractors with priority-based matching.

    Workflow:
        1. We register all enabled extractors (via `register()`)
        2. For every file the pipeline calls `match(file_path)`
        3. The first extractor that claims the file (by priority) is returned
        4. If no extension/filename matcher claims it — we try the
           content sniff (it can be costly, so only as a fallback)
    """

    _registrations: list[_Registration] = field(default_factory=list)

    def register(
        self,
        extractor: SymbolExtractor,
        *,
        name: str,
        priority: int = 0,
        extensions: tuple[str, ...] | None = None,
        filename_patterns: tuple[str, ...] | None = None,
        path_matcher: ExtensionMatcher | None = None,
        content_sniffer: ContentSniffer | None = None,
    ) -> None:
        """Register an extractor.

        Args:
            extractor: an instance implementing SymbolExtractor
            name: short identifier ('python', 'go', 'k8s', ...)
            priority: higher checked first (corner cases may have priority=100)
            extensions: tuple of extensions including the dot ('.py', '.pyi')
            filename_patterns: glob patterns by name ('Dockerfile*', 'compose*.yml')
            path_matcher: callable Path → bool — for path-based detection
                (for example, GitHub Actions: `.github/workflows/*.yml`)
            content_sniffer: callable bytes (the first ~4KB) → bool
        """
        ext_matcher: ExtensionMatcher | None = None
        if extensions:
            exts_lower = {e.lower() for e in extensions}
            # A matcher stored in a dataclass field, not a named function;
            # the default arg is what binds `exts_lower` by value. (E731)
            ext_matcher = lambda p, _e=exts_lower: p.suffix.lower() in _e  # noqa: E731

        fn_matcher: ExtensionMatcher | None = None
        if filename_patterns:
            patterns = tuple(filename_patterns)
            # Same shape as ext_matcher above: a callable stored in a dataclass
            # field, with `patterns` bound by default arg. (E731)
            fn_matcher = (  # noqa: E731
                lambda p, _pats=patterns: any(p.match(pat) for pat in _pats)
                or any(p.name == pat or _glob_match(p.name, pat) for pat in _pats)
            )

        reg = _Registration(
            extractor=extractor,
            name=name,
            priority=priority,
            extension_matcher=ext_matcher,
            filename_matcher=fn_matcher,
            path_matcher=path_matcher,
            content_sniffer=content_sniffer,
        )
        self._registrations.append(reg)
        # Sort highest priority first — so that match() returns in the right order
        self._registrations.sort(key=lambda r: r.priority, reverse=True)
        logger.debug(
            "registry_register name=%s priority=%d exts=%s patterns=%s",
            name, priority, extensions, filename_patterns,
        )

    def match(
        self,
        file_path: Path,
        *,
        read_for_sniff: bool = True,
        sniff_bytes: int = 4096,
    ) -> SymbolExtractor | None:
        """Find the extractor for a file. None if nobody claims it.

        Args:
            read_for_sniff: whether to read the first bytes for content_sniffers
                (needed for K8s detection). Disable for the performance test.
            sniff_bytes: how much to read for the sniff (default 4KB).
        """
        # Fast path: extension/filename match without I/O
        for reg in self._registrations:
            if reg.matches(file_path, content_head=None):
                return reg.extractor

        # Content sniff fallback — only needed if there are sniffers registered
        if not read_for_sniff:
            return None

        sniffers_present = any(r.content_sniffer for r in self._registrations)
        if not sniffers_present:
            return None

        # Read the file head once
        try:
            with open(file_path, "rb") as f:
                head = f.read(sniff_bytes)
        except OSError as e:
            logger.debug("sniff_read_failed file=%s err=%s", file_path, e)
            return None

        for reg in self._registrations:
            if reg.content_sniffer is None:
                continue
            if reg.matches(file_path, content_head=head):
                return reg.extractor

        return None

    def names(self) -> list[str]:
        """List of the registered extractor names — for CLI/logging."""
        return [r.name for r in self._registrations]

    def extractors(self) -> list[SymbolExtractor]:
        """Every registered extractor, in registration order.

        For callers that need what the registry can PARSE rather than what it
        would pick for one file — `factory.supported_suffixes()` builds the
        review side's "is this a language we read" answer from it, so the two
        cannot drift the way a hand-copied list did.
        """
        return [r.extractor for r in self._registrations]

    def filter(self, allowed_names: set[str]) -> LanguageRegistry:
        """Create a new registry with a subset of the extractors.

        Used by the pipeline to apply settings.enabled_language_adapters.
        """
        new = LanguageRegistry()
        new._registrations = [r for r in self._registrations if r.name in allowed_names]
        return new


def _glob_match(name: str, pattern: str) -> bool:
    """Simple fnmatch-style match for filename patterns ('Dockerfile*')."""
    import fnmatch

    return fnmatch.fnmatch(name, pattern)
