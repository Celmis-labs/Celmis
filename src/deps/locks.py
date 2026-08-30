"""Lock-file readers — the *resolved* dependency tree, not the wish-list.

A manifest says "flask >= 2"; the lock file says "flask 2.3.2, werkzeug 2.3.6,
jinja2 3.1.2, …". Everything below the first level lives only here, and a
vulnerability in a dependency-of-a-dependency is exactly the class of problem
manifest scanning cannot see.

Two consumers:
  * the auditor — transitive packages to send to OSV for ecosystems where no
    native auditor is installed (native tools already resolve the tree
    themselves, so we don't duplicate their work);
  * the hygiene checks — manifest↔lock drift, install scripts, and
    dependencies that don't come from the official registry.

Pure parsing, no network, no subprocess. Every parser takes text/data and is
directly unit-testable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Registry hosts we consider "official" per ecosystem. Anything else in a
# lock's `resolved`/`source` field is a supply-chain question worth asking.
OFFICIAL_HOSTS = {
    "npm": ("registry.npmjs.org", "registry.yarnpkg.com"),
    "PyPI": ("pypi.org", "files.pythonhosted.org"),
    "crates.io": ("crates.io", "static.crates.io"),
    "Go": ("proxy.golang.org",),
}


@dataclass(frozen=True)
class LockEntry:
    ecosystem: str          # npm | PyPI | Go | crates.io  (OSV naming)
    package: str
    version: str
    is_dev: bool
    transitive: bool
    resolved: str | None    # tarball / registry URL / VCS source, when stated
    lockfile: str           # repo-relative path
    has_install_script: bool = False


def _clean(name: str) -> str:
    return str(name or "").strip()


# ─── npm: package-lock.json ──────────────────────────────────────────


def parse_package_lock(data: dict, lockfile: str = "package-lock.json") -> list[LockEntry]:
    """npm lockfileVersion 1, 2 and 3.

    v2/v3 keep a flat `packages` map keyed by install path — depth of the path
    *is* the transitivity signal, and `hasInstallScript` is stated outright.
    v1 keeps a nested `dependencies` tree; nesting depth plays the same role.
    """
    if not isinstance(data, dict):
        return []
    out: list[LockEntry] = []

    packages = data.get("packages")
    if isinstance(packages, dict) and packages:
        root = packages.get("") or {}
        direct = set(root.get("dependencies") or {}) | set(root.get("devDependencies") or {})
        for path, meta in packages.items():
            if not path or not isinstance(meta, dict) or meta.get("link"):
                continue
            # "node_modules/a/node_modules/b" → name "b", depth 2.
            segments = path.split("node_modules/")
            name = _clean(segments[-1])
            if not name:
                continue
            depth = len(segments) - 1
            version = str(meta.get("version") or "")
            out.append(LockEntry(
                ecosystem="npm",
                package=name,
                version=version,
                is_dev=bool(meta.get("dev")) or bool(meta.get("devOptional")),
                transitive=depth > 1 or (bool(direct) and name not in direct),
                resolved=meta.get("resolved") or None,
                lockfile=lockfile,
                has_install_script=bool(meta.get("hasInstallScript")),
            ))
        return out

    def _walk(tree: dict, depth: int) -> None:
        for name, meta in (tree or {}).items():
            if not isinstance(meta, dict):
                continue
            out.append(LockEntry(
                ecosystem="npm",
                package=_clean(name),
                version=str(meta.get("version") or ""),
                is_dev=bool(meta.get("dev")),
                transitive=depth > 0,
                resolved=meta.get("resolved") or None,
                lockfile=lockfile,
                has_install_script=bool(meta.get("hasInstallScript")),
            ))
            nested = meta.get("dependencies")
            if isinstance(nested, dict):
                _walk(nested, depth + 1)

    deps = data.get("dependencies")
    if isinstance(deps, dict):
        _walk(deps, 0)
    return out


# ─── pnpm: pnpm-lock.yaml ────────────────────────────────────────────

# v6+ keys: "/lodash@4.17.15" · "lodash@4.17.15(react@18.0.0)" · "/@babel/core@7.0.0"
_PNPM_KEY = re.compile(r"^/?(?P<name>(?:@[^/@]+/)?[^@/][^@]*)@(?P<ver>[0-9][^(]*)")
# v5 keys: "/lodash/4.17.21" · "/@babel/core/7.0.0"
_PNPM_KEY_V5 = re.compile(r"^/(?P<name>(?:@[^/]+/)?[^/]+)/(?P<ver>\d[^/(]*)")


def _pnpm_key(key: str) -> tuple[str, str] | None:
    for pattern in (_PNPM_KEY, _PNPM_KEY_V5):
        m = pattern.match(key)
        if m:
            return m.group("name"), m.group("ver").strip()
    return None


def parse_pnpm_lock(text: str, lockfile: str = "pnpm-lock.yaml") -> list[LockEntry]:
    """pnpm lockfile v5…v9. `importers` holds the direct deps per workspace
    package; `packages`/`snapshots` hold the resolved universe."""
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []

    direct: set[str] = set()
    dev_direct: set[str] = set()
    importers = data.get("importers")
    scopes = list(importers.values()) if isinstance(importers, dict) else [data]
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for key, dev in (("dependencies", False), ("devDependencies", True),
                         ("optionalDependencies", False)):
            block = scope.get(key)
            if isinstance(block, dict):
                direct.update(block)
                if dev:
                    dev_direct.update(block)

    out: list[LockEntry] = []
    seen: set[tuple[str, str]] = set()
    for section in ("packages", "snapshots"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for key, meta in block.items():
            parsed = _pnpm_key(str(key))
            if parsed is None:
                continue
            name, version = parsed
            if (name, version) in seen:
                continue
            seen.add((name, version))
            meta = meta if isinstance(meta, dict) else {}
            resolution = meta.get("resolution") if isinstance(meta.get("resolution"), dict) else {}
            out.append(LockEntry(
                ecosystem="npm",
                package=name,
                version=version,
                is_dev=bool(meta.get("dev")) or name in dev_direct,
                transitive=bool(direct) and name not in direct,
                resolved=(resolution or {}).get("tarball") or (resolution or {}).get("repo") or None,
                lockfile=lockfile,
                has_install_script=bool(meta.get("requiresBuild")),
            ))
    return out


# ─── yarn: yarn.lock (classic v1 and berry) ──────────────────────────

_YARN_SPEC = re.compile(r'"?(?P<name>(?:@[^/@\s]+/)?[^@\s"]+)@')
_YARN_VERSION = re.compile(r'^\s+version:?\s+"?(?P<ver>[^"\s]+)"?', re.M)


def parse_yarn_lock(text: str, lockfile: str = "yarn.lock") -> list[LockEntry]:
    """Both dialects. Berry (`__metadata:` present) is YAML; classic is a
    YAML-ish custom format that only `version`/`resolved` matter in.

    yarn.lock states no dev flag and no tree shape, so every entry is reported
    as `transitive=False` here — the caller intersects with the manifest to
    decide. Being honest about "unknown" beats inventing a tree."""
    out: list[LockEntry] = []
    if "__metadata:" in text:
        try:
            import yaml
            data = yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001
            data = {}
        for key, meta in (data.items() if isinstance(data, dict) else []):
            if key == "__metadata" or not isinstance(meta, dict):
                continue
            first = str(key).split(",")[0].strip()
            m = _YARN_SPEC.match(first)
            if not m:
                continue
            out.append(LockEntry(
                ecosystem="npm",
                package=m.group("name"),
                version=str(meta.get("version") or ""),
                is_dev=False,
                transitive=False,
                resolved=meta.get("resolution") or None,
                lockfile=lockfile,
            ))
        return out

    block_lines: list[str] = []
    header: str | None = None

    def _flush() -> None:
        if header is None:
            return
        first = header.split(",")[0].strip().rstrip(":").strip()
        m = _YARN_SPEC.match(first)
        if not m:
            return
        body = "\n".join(block_lines)
        vm = _YARN_VERSION.search(body)
        rm = re.search(r'^\s+resolved\s+"?(?P<url>[^"\s]+)"?', body, re.M)
        out.append(LockEntry(
            ecosystem="npm",
            package=m.group("name"),
            version=vm.group("ver") if vm else "",
            is_dev=False,
            transitive=False,
            resolved=rm.group("url") if rm else None,
            lockfile=lockfile,
        ))

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            _flush()
            header, block_lines = line, []
        else:
            block_lines.append(line)
    _flush()
    return out


# ─── Python: poetry.lock ─────────────────────────────────────────────


def parse_poetry_lock(text: str, lockfile: str = "poetry.lock") -> list[LockEntry]:
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001
        return []
    out: list[LockEntry] = []
    for pkg in data.get("package") or []:
        if not isinstance(pkg, dict):
            continue
        name = _clean(pkg.get("name")).lower()
        if not name:
            continue
        groups = pkg.get("groups") or []
        category = str(pkg.get("category") or "")
        source = pkg.get("source") if isinstance(pkg.get("source"), dict) else None
        out.append(LockEntry(
            ecosystem="PyPI",
            package=name,
            version=str(pkg.get("version") or ""),
            is_dev=category == "dev" or (bool(groups) and "main" not in groups),
            transitive=False,     # decided against the manifest by the caller
            resolved=(source or {}).get("url") or (source or {}).get("reference") or None,
            lockfile=lockfile,
        ))
    return out


# ─── Rust: Cargo.lock ────────────────────────────────────────────────


def parse_cargo_lock(text: str, lockfile: str = "Cargo.lock") -> list[LockEntry]:
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001
        return []
    out: list[LockEntry] = []
    for pkg in data.get("package") or []:
        if not isinstance(pkg, dict):
            continue
        name = _clean(pkg.get("name"))
        if not name:
            continue
        out.append(LockEntry(
            ecosystem="crates.io",
            package=name,
            version=str(pkg.get("version") or ""),
            is_dev=False,
            transitive=False,
            resolved=pkg.get("source") or None,
            lockfile=lockfile,
        ))
    return out


# ─── Go: go.sum ──────────────────────────────────────────────────────

_GO_SUM = re.compile(r"^(?P<mod>\S+)\s+v(?P<ver>\S+?)(?:/go\.mod)?\s+h1:")


def parse_go_sum(text: str, lockfile: str = "go.sum") -> list[LockEntry]:
    out: list[LockEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        m = _GO_SUM.match(line.strip())
        if not m:
            continue
        # "+incompatible" / "/go.mod" suffixes are checksum bookkeeping, not
        # part of the module version OSV knows about.
        version = m.group("ver").removesuffix("/go.mod")
        key = (m.group("mod"), version)
        if key in seen:
            continue
        seen.add(key)
        out.append(LockEntry(
            ecosystem="Go",
            package=m.group("mod"),
            version=version,
            is_dev=False,
            transitive=False,
            resolved=None,
            lockfile=lockfile,
        ))
    return out


# ─── Repo walker ─────────────────────────────────────────────────────

_LOCK_READERS = {
    "package-lock.json": lambda t, rel: parse_package_lock(_json(t), rel),
    "npm-shrinkwrap.json": lambda t, rel: parse_package_lock(_json(t), rel),
    "pnpm-lock.yaml": parse_pnpm_lock,
    "yarn.lock": parse_yarn_lock,
    "poetry.lock": parse_poetry_lock,
    "Cargo.lock": parse_cargo_lock,
    "go.sum": parse_go_sum,
}

_SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", ".venv",
              "venv", "__pycache__", ".next", "target"}
_MAX_LOCKFILES = 20
_MAX_ENTRIES_PER_LOCK = 4000


def _json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def lock_files(repo_path: Path, notes: list[dict] | None = None) -> list[Path]:
    """Every lock file worth reading, shallowest first.

    `notes` collects what the cap below threw away. A partial inventory that
    does not say it is partial is the thing this subsystem exists to avoid —
    `document.py` states the rule in as many words, "count of what was dropped
    is printed rather than silently truncated", and this walk was not keeping
    it. A monorepo with 25 lock files produced an SBOM missing five, and
    nothing anywhere said so.
    """
    found: list[tuple[int, Path]] = []
    for path in repo_path.rglob("*"):
        if path.name not in _LOCK_READERS or not path.is_file():
            continue
        rel_parts = path.relative_to(repo_path).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        found.append((len(rel_parts), path))
    found.sort(key=lambda t: (t[0], str(t[1])))
    if len(found) > _MAX_LOCKFILES:
        dropped = len(found) - _MAX_LOCKFILES
        logger.warning(
            "lock_files_truncated path=%s found=%d kept=%d dropped=%d",
            repo_path, len(found), _MAX_LOCKFILES, dropped,
        )
        if notes is not None:
            notes.append({
                "what": "lock files",
                "found": len(found),
                "kept": _MAX_LOCKFILES,
                "dropped": dropped,
                "detail": "deepest paths first; the inventory below is partial",
            })
    return [p for _, p in found[:_MAX_LOCKFILES]]


def scan_locks(repo_path: Path, notes: list[dict] | None = None) -> list[LockEntry]:
    """All resolved dependencies across every lock file in a repo clone.

    Two caps apply — how many lock files are read, and how many entries are
    taken from each — and both record what they dropped into `notes`.
    """
    out: list[LockEntry] = []
    for path in lock_files(repo_path, notes):
        rel = str(path.relative_to(repo_path))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        try:
            entries = _LOCK_READERS[path.name](text, rel)
        except Exception as exc:  # noqa: BLE001
            logger.debug("lock_parse_failed file=%s err=%s", rel, exc)
            continue
        if len(entries) > _MAX_ENTRIES_PER_LOCK:
            dropped = len(entries) - _MAX_ENTRIES_PER_LOCK
            logger.warning(
                "lock_entries_truncated file=%s found=%d kept=%d dropped=%d",
                rel, len(entries), _MAX_ENTRIES_PER_LOCK, dropped,
            )
            if notes is not None:
                notes.append({
                    "what": f"packages in {rel}",
                    "found": len(entries),
                    "kept": _MAX_ENTRIES_PER_LOCK,
                    "dropped": dropped,
                    "detail": "this lock file is larger than the reader's cap",
                })
        out.extend(entries[:_MAX_ENTRIES_PER_LOCK])
    return out


def mark_transitive(entries: list[LockEntry], direct: dict[str, set[str]]) -> list[LockEntry]:
    """Second pass for lock formats that don't state the tree shape: anything
    the manifests didn't declare is, by definition, pulled in by something
    else. `direct` maps ecosystem → declared package names."""
    out: list[LockEntry] = []
    for e in entries:
        names = direct.get(e.ecosystem)
        if not names or e.transitive:
            out.append(e)
            continue
        out.append(LockEntry(
            ecosystem=e.ecosystem, package=e.package, version=e.version,
            is_dev=e.is_dev, transitive=e.package.lower() not in names,
            resolved=e.resolved, lockfile=e.lockfile,
            has_install_script=e.has_install_script,
        ))
    return out


__all__ = [
    "LockEntry",
    "OFFICIAL_HOSTS",
    "lock_files",
    "mark_transitive",
    "parse_cargo_lock",
    "parse_go_sum",
    "parse_package_lock",
    "parse_pnpm_lock",
    "parse_poetry_lock",
    "parse_yarn_lock",
    "scan_locks",
]
