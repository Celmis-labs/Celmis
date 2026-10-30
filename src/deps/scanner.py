"""Manifest scanners — extract declared dependencies from a repo clone.

Supported (v1): npm (package.json), Python (requirements.txt, pyproject.toml —
PEP 621 and Poetry), Go (go.mod), Rust (Cargo.toml). Version *specifiers* are
normalised to the concrete lower-bound/pinned version — the audit compares
"what you'd get at minimum" against latest + OSV.

Version provenance: a specifier is not an installed version. ``>=4.2`` is
reported at 4.2 only as the range FLOOR (``version_source="range"``); when a
lock file sits next to (or above) the manifest, the locked version replaces it
(``version_source="lock"``). Nothing downstream may present a floor as the
current version.

Pure parsing, no network, no LLM. Ecosystem names match OSV.dev exactly:
npm | PyPI | Go | crates.io.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeclaredDep:
    ecosystem: str        # npm | PyPI | Go | crates.io  (OSV naming)
    package: str
    version: str          # normalised concrete version ("1.2.3")
    raw_spec: str         # original specifier ("^1.2.0", ">=1.2,<2")
    is_dev: bool
    manifest: str         # repo-relative manifest path
    #: Where `version` came from — the provenance the report prints next to it.
    #:   pin      exact version stated in the manifest (== / npm exact / Go)
    #:   lock     the version a lock file next to the manifest resolved
    #:   resolved no lock; resolved by `uv pip compile` on the audit date
    #:   range    a range with no lock and no resolution: `version` is only
    #:            the range's FLOOR and is NOT what is installed
    version_source: str = "pin"


_VER_RE = re.compile(r"(\d+(?:\.\d+){0,3}(?:[-.+][0-9A-Za-z.-]+)?)")


def _norm_version(spec: str) -> str | None:
    """First concrete version inside a specifier — '^1.2.3' → '1.2.3',
    '>=2.0,<3' → '2.0'. None for wildcards/urls/tags ('*', 'latest', git+…)."""
    s = spec.strip()
    if not s or s in ("*", "latest") or "://" in s or s.startswith(("git", "file:", "link:", "workspace:")):
        return None
    m = _VER_RE.search(s)
    return m.group(1) if m else None


_EXACT_NPM = re.compile(r"^=?\s*v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_EXACT_PY = re.compile(r"^={2,3}\s*[0-9A-Za-z][0-9A-Za-z.+!_-]*$")
_BARE_VERSION = re.compile(r"^\d+(?:\.\d+)*(?:[-.+][0-9A-Za-z.-]+)?$")


def _is_exact(ecosystem: str, spec: str, *, bare_is_exact: bool = False) -> bool:
    """Whether a specifier names ONE version. Everything else is a range, and a
    range's first number is a floor, not an installation."""
    s = spec.strip()
    if ecosystem == "Go":
        return True
    if ecosystem == "PyPI":
        if "," in s or "*" in s:
            return False
        return bool(_EXACT_PY.match(s)) or (bare_is_exact and bool(_BARE_VERSION.match(s)))
    if ecosystem == "npm":
        return bool(_EXACT_NPM.match(s))
    if ecosystem == "crates.io":
        return s.startswith("=") and bool(_BARE_VERSION.match(s.lstrip("= ")))
    if ecosystem == "Maven":
        return bool(_BARE_VERSION.match(s))
    return False


def _dep(ecosystem: str, package: str, ver: str, spec: str, is_dev: bool,
         rel: str, *, bare_is_exact: bool = False) -> DeclaredDep:
    exact = _is_exact(ecosystem, spec, bare_is_exact=bare_is_exact)
    return DeclaredDep(ecosystem, package, ver, spec, is_dev, rel,
                       "pin" if exact else "range")


# ─── npm ─────────────────────────────────────────────────────────────


def _scan_package_json(path: Path, rel: str) -> list[DeclaredDep]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out: list[DeclaredDep] = []
    for key, is_dev in (("dependencies", False), ("devDependencies", True)):
        for name, spec in (data.get(key) or {}).items():
            ver = _norm_version(str(spec))
            if ver:
                out.append(_dep("npm", name, ver, str(spec), is_dev, rel))
    return out


# ─── Python ──────────────────────────────────────────────────────────

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(?:\[[^\]]*\])?\s*([=<>!~^].*)?$")


def _scan_requirements(path: Path, rel: str) -> list[DeclaredDep]:
    out: list[DeclaredDep] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        m = _REQ_LINE.match(line)
        if not m:
            continue
        name, spec = m.group(1), (m.group(2) or "").strip()
        ver = _norm_version(spec) if spec else None
        if ver:
            out.append(_dep("PyPI", name.lower(), ver, spec, False, rel))
    return out


def _scan_pyproject(path: Path, rel: str) -> list[DeclaredDep]:
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out: list[DeclaredDep] = []

    def _add(req: str, is_dev: bool) -> None:
        m = _REQ_LINE.match(req.split(";", 1)[0].strip())
        if not m:
            return
        name, spec = m.group(1), (m.group(2) or "").strip()
        ver = _norm_version(spec) if spec else None
        if ver:
            out.append(_dep("PyPI", name.lower(), ver, spec, is_dev, rel))

    project = data.get("project") or {}
    for req in project.get("dependencies") or []:
        _add(str(req), False)
    for group in (project.get("optional-dependencies") or {}).values():
        for req in group or []:
            _add(str(req), True)

    # Poetry layout.
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for section, is_dev in (("dependencies", False), ("dev-dependencies", True)):
        for name, spec in (poetry.get(section) or {}).items():
            if name.lower() == "python":
                continue
            spec_str = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
            ver = _norm_version(spec_str)
            if ver:
                out.append(_dep("PyPI", name.lower(), ver, spec_str, is_dev, rel,
                                bare_is_exact=True))
    return out


# ─── Go ──────────────────────────────────────────────────────────────

_GO_REQ = re.compile(r"^\s*([\w./-]+)\s+v(\d[^\s/]*)")


def _scan_go_mod(path: Path, rel: str) -> list[DeclaredDep]:
    out: list[DeclaredDep] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    in_require = False
    for line in text.splitlines():
        s = line.split("//", 1)[0].strip()
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require and s == ")":
            in_require = False
            continue
        candidate = s.removeprefix("require ").strip() if s.startswith("require ") else (s if in_require else "")
        if not candidate:
            continue
        m = _GO_REQ.match(candidate)
        if m:
            out.append(DeclaredDep("Go", m.group(1), m.group(2), f"v{m.group(2)}", False, rel))
    return out


# ─── Rust ────────────────────────────────────────────────────────────


def _scan_cargo_toml(path: Path, rel: str) -> list[DeclaredDep]:
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out: list[DeclaredDep] = []
    for section, is_dev in (("dependencies", False), ("dev-dependencies", True)):
        for name, spec in (data.get(section) or {}).items():
            spec_str = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
            ver = _norm_version(spec_str)
            if ver:
                out.append(_dep("crates.io", name, ver, spec_str, is_dev, rel))
    return out


# ─── Gradle version catalog ──────────────────────────────────────────


def _scan_version_catalog(path: Path, rel: str) -> list[DeclaredDep]:
    """`gradle/libs.versions.toml` → Maven coordinates (``group:artifact``, the
    name OSV uses). Without a `gradle.lockfile` this catalog is the only place
    a Gradle project states its versions, and `build.gradle.kts` just says
    ``libs.something``.

    ``[versions]`` + ``[libraries]`` with ``module`` or ``group``+``name`` and a
    literal / ``version.ref`` version. ``strictly`` / ``require`` / ``prefer``
    / ``[a,b)`` ranges are rich versions: ranges, not installations.
    """
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    if any(p.is_file() for p in (path.parent / "gradle.lockfile",
                                 path.parent.parent / "gradle.lockfile")):
        return []        # a lock file exists: osv-scanner reads the real tree
    versions = data.get("versions") or {}
    out: list[DeclaredDep] = []
    for lib in (data.get("libraries") or {}).values():
        coord = ""
        ver_spec: object = None
        if isinstance(lib, str):
            parts = lib.split(":")
            if len(parts) >= 2:
                coord = ":".join(parts[:2])
                ver_spec = parts[2] if len(parts) > 2 else None
        elif isinstance(lib, dict):
            if lib.get("module"):
                coord = str(lib["module"])
            elif lib.get("group") and lib.get("name"):
                coord = f"{lib['group']}:{lib['name']}"
            ver_spec = lib.get("version")
        if not coord or ":" not in coord:
            continue
        rich = False
        if isinstance(ver_spec, dict):
            if "ref" in ver_spec:
                ver_spec = versions.get(ver_spec["ref"])
                if isinstance(ver_spec, dict):
                    rich, ver_spec = True, (ver_spec.get("strictly") or ver_spec.get("require")
                                            or ver_spec.get("prefer"))
            else:
                rich = True
                ver_spec = (ver_spec.get("strictly") or ver_spec.get("require")
                            or ver_spec.get("prefer"))
        if ver_spec is None:
            continue
        spec = str(ver_spec)
        ver = _norm_version(spec)
        if not ver:
            continue
        exact = (not rich) and _is_exact("Maven", spec)
        out.append(DeclaredDep("Maven", coord, ver, spec, False, rel,
                               "pin" if exact else "range"))
    return out


# ─── Walker ──────────────────────────────────────────────────────────

_MANIFEST_SCANNERS = {
    "package.json": _scan_package_json,
    "requirements.txt": _scan_requirements,
    "pyproject.toml": _scan_pyproject,
    "go.mod": _scan_go_mod,
    "Cargo.toml": _scan_cargo_toml,
    "libs.versions.toml": _scan_version_catalog,
}

_SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", ".venv", "venv", "__pycache__", ".next"}
_MAX_MANIFESTS_PER_REPO = 40


def scan_repo(repo_path: Path, notes: list[dict] | None = None,
              *, use_locks: bool = True) -> list[DeclaredDep]:
    """All declared deps in a repo clone, deduplicated by (ecosystem, package)
    keeping the first (shallowest manifest wins — root over sub-packages).

    `notes` collects what the cap dropped. The walk stops after
    `_MAX_MANIFESTS_PER_REPO` manifests and used to stop in silence, so a
    monorepo past that count produced an inventory that looked complete and
    was not — the failure `document.py` already names: "count of what was
    dropped is printed rather than silently truncated".
    """
    found: list[tuple[int, DeclaredDep]] = []
    manifests = 0
    truncated_at: str | None = None
    for path in sorted(repo_path.rglob("*")):
        if manifests >= _MAX_MANIFESTS_PER_REPO:
            truncated_at = str(path.relative_to(repo_path))
            break
        if not path.is_file() or path.name not in _MANIFEST_SCANNERS:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_path).parts):
            continue
        rel = str(path.relative_to(repo_path))
        depth = rel.count("/")
        deps = _MANIFEST_SCANNERS[path.name](path, rel)
        if deps:
            manifests += 1
            found.extend((depth, d) for d in deps)

    if truncated_at is not None:
        logger.warning(
            "manifests_truncated path=%s kept=%d stopped_at=%s",
            repo_path, _MAX_MANIFESTS_PER_REPO, truncated_at,
        )
        if notes is not None:
            notes.append({
                "what": "manifests",
                "found": None,          # the walk stopped; nobody counted the rest
                "kept": _MAX_MANIFESTS_PER_REPO,
                "dropped": None,
                "detail": f"the walk stopped at {truncated_at}; anything after "
                          f"it in path order was not read",
            })

    found.sort(key=lambda t: t[0])
    seen: set[tuple[str, str]] = set()
    out: list[DeclaredDep] = []
    for _, dep in found:
        key = (dep.ecosystem, dep.package)
        if key in seen:
            continue
        seen.add(key)
        out.append(dep)
    return apply_locks(repo_path, out) if use_locks else out


def _norm_name(ecosystem: str, name: str) -> str:
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def apply_locks(repo_path: Path, deps: list[DeclaredDep]) -> list[DeclaredDep]:
    """Replace a range's floor with the version a lock file actually pins.

    The lock consulted is the nearest one: the manifest's own directory first,
    then each ancestor (an npm/uv workspace keeps ONE lock at the root for all
    its members). Exact pins are left alone — the manifest already says what is
    installed. A range with no lock keeps ``version_source="range"``.
    """
    if not any(d.version_source == "range" for d in deps):
        return deps
    from src.deps.locks import scan_locks

    try:
        entries = scan_locks(repo_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply_locks_failed path=%s err=%s", repo_path, exc)
        return deps
    by_dir: dict[tuple[str, str], dict[str, str]] = {}
    for e in entries:
        if not e.version:
            continue
        d = str(Path(e.lockfile).parent)
        # First writer wins inside one directory: a repo can hold several lock
        # files for the same ecosystem and the first (shallowest, sorted) is
        # the one the manifest sits beside.
        by_dir.setdefault((d, e.ecosystem), {}).setdefault(
            _norm_name(e.ecosystem, e.package), e.version)
    out: list[DeclaredDep] = []
    for dep in deps:
        if dep.version_source != "range":
            out.append(dep)
            continue
        folder = Path(dep.manifest).parent
        version = None
        for candidate in (folder, *folder.parents):
            table = by_dir.get((str(candidate), dep.ecosystem))
            if table and _norm_name(dep.ecosystem, dep.package) in table:
                version = table[_norm_name(dep.ecosystem, dep.package)]
                break
        out.append(replace(dep, version=version, version_source="lock")
                   if version else dep)
    return out


__all__ = ["DeclaredDep", "apply_locks", "scan_repo"]
