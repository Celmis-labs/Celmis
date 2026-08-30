"""Manifest scanners — extract declared dependencies from a repo clone.

Supported (v1): npm (package.json), Python (requirements.txt, pyproject.toml —
PEP 621 and Poetry), Go (go.mod), Rust (Cargo.toml). Version *specifiers* are
normalised to the concrete lower-bound/pinned version — the audit compares
"what you'd get at minimum" against latest + OSV.

Pure parsing, no network, no LLM. Ecosystem names match OSV.dev exactly:
npm | PyPI | Go | crates.io.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
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


_VER_RE = re.compile(r"(\d+(?:\.\d+){0,3}(?:[-.+][0-9A-Za-z.-]+)?)")


def _norm_version(spec: str) -> str | None:
    """First concrete version inside a specifier — '^1.2.3' → '1.2.3',
    '>=2.0,<3' → '2.0'. None for wildcards/urls/tags ('*', 'latest', git+…)."""
    s = spec.strip()
    if not s or s in ("*", "latest") or "://" in s or s.startswith(("git", "file:", "link:", "workspace:")):
        return None
    m = _VER_RE.search(s)
    return m.group(1) if m else None


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
                out.append(DeclaredDep("npm", name, ver, str(spec), is_dev, rel))
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
            out.append(DeclaredDep("PyPI", name.lower(), ver, spec, False, rel))
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
            out.append(DeclaredDep("PyPI", name.lower(), ver, spec, is_dev, rel))

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
                out.append(DeclaredDep("PyPI", name.lower(), ver, spec_str, is_dev, rel))
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
                out.append(DeclaredDep("crates.io", name, ver, spec_str, is_dev, rel))
    return out


# ─── Walker ──────────────────────────────────────────────────────────

_MANIFEST_SCANNERS = {
    "package.json": _scan_package_json,
    "requirements.txt": _scan_requirements,
    "pyproject.toml": _scan_pyproject,
    "go.mod": _scan_go_mod,
    "Cargo.toml": _scan_cargo_toml,
}

_SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", ".venv", "venv", "__pycache__", ".next"}
_MAX_MANIFESTS_PER_REPO = 40


def scan_repo(repo_path: Path, notes: list[dict] | None = None) -> list[DeclaredDep]:
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
    return out


__all__ = ["DeclaredDep", "scan_repo"]
