"""Does this repository's own code name the package, and where.

WHAT THIS IS NOT. It is not reachability. Reachability answers whether a
vulnerable function is actually called from an entry point, and it needs three
things this installation does not have: the dependency's own source in the
index (`node_modules`, `vendor` and `bower_components` are excluded on
purpose), advisories that name the vulnerable symbol (OSV's
`ecosystem_specific` carries that for a small minority, mostly Go), and a
notion of where execution starts. Calling this reachability would be the same
over-claim as saying a manifest that does not hash itself proves a pack was
not forged.

WHAT IT IS. An import-position search: your files, the ecosystems' own import
syntax, and a `file:line` for the first few matches. It splits a findings list
into "our code names this" and "this arrived transitively and nothing of ours
mentions it", which is the difference between a list to triage and a list to
read.

THREE ANSWERS, NOT TWO, and the third is the honest one:

    imported   a file of yours names it in an import position — file:line below
    not_found  nothing does, AND the package name maps to the import name by a
               rule that holds for this ecosystem
    unknown    we cannot tell, because the package name does not determine the
               module name

`unknown` exists for PyPI. `beautifulsoup4` imports as `bs4`, `pillow` as
`PIL`, `python-dateutil` as `dateutil`; the mapping lives in the distribution's
metadata, which a lock file does not carry. Reporting those as "not imported"
would be a silent zero — the failure this subsystem is built against — so a
PyPI package whose normalised name is absent comes back `unknown` rather than
`not_found`.

Dynamic imports are invisible to all of it. `importlib.import_module(name)`
and `require(var)` are calls, not import statements, so a `not_found` means
"no static import names it", never "this is not used".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Answers. Kept apart for the reason `unknown` exists at all.
IMPORTED = "imported"
NOT_FOUND = "not_found"
UNKNOWN = "unknown"

#: Same exclusions the manifest scanner uses, plus the obvious build output.
_SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", ".venv", "venv",
    "__pycache__", ".next", "target", "bower_components", ".tox", "site-packages",
}

#: Which files can name a package of each ecosystem.
_SUFFIXES: dict[str, tuple[str, ...]] = {
    "npm": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"),
    "PyPI": (".py", ".pyi"),
    "Go": (".go",),
    "crates.io": (".rs",),
}

#: A walk has to end. Reported when it bites, for the same reason the audit's
#: other caps now are — a partial answer that does not say it is partial is
#: worse than no answer.
MAX_FILES_PER_REPO = 4000
MAX_MATCHES_PER_PACKAGE = 5


@dataclass(frozen=True)
class ImportSite:
    """One place a package is named, as `path:line`."""

    path: str
    line: int
    text: str


@dataclass
class ImportAnswer:
    state: str
    sites: list[ImportSite] = field(default_factory=list)
    #: Why `unknown`, in the reader's words rather than a code.
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "sites": [{"path": s.path, "line": s.line, "text": s.text} for s in self.sites],
        }


def module_candidates(ecosystem: str, package: str) -> tuple[list[str], bool]:
    """Import names this package could appear under, and whether the rule is exact.

    Exact means: absent from the source implies the package is not statically
    imported. Where the rule is not exact, absence proves nothing and the
    caller must answer `unknown`.
    """
    name = (package or "").strip()
    if not name:
        return [], False
    eco = ecosystem
    if eco == "npm":
        # The specifier IS the package name, scope included. Subpath imports
        # ("lodash/merge") start with it, which the patterns below allow for.
        return [name], True
    if eco == "Go":
        # The module path is the import path.
        return [name], True
    if eco == "crates.io":
        # `foo-bar` is declared as `foo_bar` in source; the rule is mechanical.
        return [name.replace("-", "_")], True
    if eco == "PyPI":
        # A guess, and the function says so by returning exact=False. The
        # distribution's real top-level modules live in its metadata, which a
        # lock file does not carry.
        normalised = name.lower().replace("-", "_").replace(".", "_")
        candidates = [normalised]
        if normalised != name:
            candidates.append(name)
        return candidates, False
    return [], False


def _patterns(ecosystem: str, candidate: str) -> list[re.Pattern[str]]:
    """Import-position patterns only. A package named in a comment is not an import."""
    quoted = re.escape(candidate)
    if ecosystem == "npm":
        return [
            re.compile(rf"""require\(\s*['"]{quoted}(?:/[^'"]*)?['"]"""),
            re.compile(rf"""\bfrom\s+['"]{quoted}(?:/[^'"]*)?['"]"""),
            re.compile(rf"""\bimport\s+['"]{quoted}(?:/[^'"]*)?['"]"""),
            re.compile(rf"""\bimport\(\s*['"]{quoted}(?:/[^'"]*)?['"]"""),
        ]
    if ecosystem == "PyPI":
        return [
            re.compile(rf"^\s*import\s+{quoted}\b", re.M),
            re.compile(rf"^\s*from\s+{quoted}[\s.]", re.M),
        ]
    if ecosystem == "Go":
        return [re.compile(rf"""["`]{quoted}(?:/[^"`]*)?["`]""")]
    if ecosystem == "crates.io":
        return [
            re.compile(rf"^\s*use\s+{quoted}\b", re.M),
            re.compile(rf"\bextern\s+crate\s+{quoted}\b"),
        ]
    return []


def _readable_files(repo_path: Path, suffixes: tuple[str, ...]) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for path in sorted(repo_path.rglob("*")):
        if len(files) >= MAX_FILES_PER_REPO:
            truncated = True
            break
        if path.suffix not in suffixes or not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_path).parts):
            continue
        files.append(path)
    return files, truncated


def scan_imports(
    repo_path: Path,
    packages: list[tuple[str, str]],
    notes: list[dict] | None = None,
) -> dict[tuple[str, str], ImportAnswer]:
    """For each `(ecosystem, package)`, whether this repo's code names it.

    One pass over the source per ecosystem, not one per package: a findings
    list is hundreds of packages long and re-reading the tree for each would
    turn a fast answer into a slow one nobody waits for.
    """
    answers: dict[tuple[str, str], ImportAnswer] = {}
    by_eco: dict[str, list[str]] = {}
    for ecosystem, package in packages:
        by_eco.setdefault(ecosystem, []).append(package)

    for ecosystem, names in by_eco.items():
        suffixes = _SUFFIXES.get(ecosystem)
        if suffixes is None:
            for package in names:
                answers[(ecosystem, package)] = ImportAnswer(
                    UNKNOWN, detail=f"no import syntax is known for {ecosystem}",
                )
            continue

        files, truncated = _readable_files(repo_path, suffixes)
        if truncated:
            logger.warning("import_scan_truncated path=%s ecosystem=%s cap=%d",
                           repo_path, ecosystem, MAX_FILES_PER_REPO)
            if notes is not None:
                notes.append({
                    "what": f"{ecosystem} source files read for import sites",
                    "found": None,
                    "kept": MAX_FILES_PER_REPO,
                    "dropped": None,
                    "detail": "import answers for this ecosystem are partial",
                })

        compiled: dict[str, list[re.Pattern[str]]] = {}
        exactness: dict[str, bool] = {}
        for package in names:
            candidates, exact = module_candidates(ecosystem, package)
            exactness[package] = exact
            compiled[package] = [
                pattern
                for candidate in candidates
                for pattern in _patterns(ecosystem, candidate)
            ]

        found: dict[str, list[ImportSite]] = {p: [] for p in names}
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(repo_path))
            lines: list[str] | None = None
            for package, patterns in compiled.items():
                if len(found[package]) >= MAX_MATCHES_PER_PACKAGE:
                    continue
                for pattern in patterns:
                    if not pattern.search(text):
                        continue
                    if lines is None:
                        lines = text.splitlines()
                    for number, line in enumerate(lines, 1):
                        if pattern.search(line):
                            found[package].append(
                                ImportSite(rel, number, line.strip()[:200]),
                            )
                            if len(found[package]) >= MAX_MATCHES_PER_PACKAGE:
                                break
                    break

        for package in names:
            sites = found[package]
            if sites:
                answers[(ecosystem, package)] = ImportAnswer(IMPORTED, sites)
            elif exactness[package]:
                answers[(ecosystem, package)] = ImportAnswer(
                    NOT_FOUND,
                    detail="no static import names it; a dynamic import would "
                           "not appear here",
                )
            else:
                answers[(ecosystem, package)] = ImportAnswer(
                    UNKNOWN,
                    detail=f"a {ecosystem} package name does not determine its "
                           f"module name, so absence proves nothing",
                )
    return answers


__all__ = [
    "IMPORTED",
    "MAX_FILES_PER_REPO",
    "MAX_MATCHES_PER_PACKAGE",
    "NOT_FOUND",
    "UNKNOWN",
    "ImportAnswer",
    "ImportSite",
    "module_candidates",
    "scan_imports",
]
