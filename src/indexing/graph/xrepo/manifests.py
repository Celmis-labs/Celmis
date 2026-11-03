"""Packages a repository publishes, read from its manifests.

pyproject `[project].name` (or `[tool.poetry].name`), package.json `name`,
go.mod `module`, Maven `groupId:artifactId`. Manifests are looked for up to
four directories deep, skipping vendored and build directories, so a monorepo
publishes one package per manifest.

Nothing here executes repository code: pyproject is parsed by `tomllib`,
package.json by `json`, pom.xml by regular expressions (a cloned repository is
untrusted input, and an XML parser is not needed for two tags).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from pathlib import Path

from src.indexing.graph.xrepo.model import PublishedPackage

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv",
              "__pycache__", ".next", ".tox", "site-packages", "testdata", "fixtures"}
_MAX_DEPTH = 4
_MAX_BYTES = 512_000
MANIFESTS = ("pyproject.toml", "package.json", "go.mod", "pom.xml")


def normalize_py_name(name: str) -> str:
    """PEP 503 / import form: `Acme-Billing.Client` -> `acme_billing_client`."""
    return re.sub(r"[-_.]+", "_", name).lower()


def published_packages(repo_root: Path) -> list[PublishedPackage]:
    out: list[PublishedPackage] = []
    root = Path(repo_root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if len(rel_dir.parts) >= _MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in filenames:
            if fname not in MANIFESTS:
                continue
            path = Path(dirpath) / fname
            rel = path.relative_to(root).as_posix()
            try:
                if path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                pkg = _READERS[fname](text, rel, path.parent)
            except Exception as exc:  # noqa: BLE001 — one odd manifest must not stop the index
                logger.debug("manifest_unreadable path=%s err=%s", rel, exc)
                continue
            out.extend(pkg)
    return out


def _pyproject(text: str, rel: str, folder: Path) -> list[PublishedPackage]:
    data = tomllib.loads(text)
    name = (data.get("project") or {}).get("name") \
        or ((data.get("tool") or {}).get("poetry") or {}).get("name")
    if not isinstance(name, str) or not name:
        return []
    modules = {normalize_py_name(name)}
    for base in (folder, folder / "src"):
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir() and (child / "__init__.py").exists() \
                        and not child.name.startswith(("test", ".")):
                    modules.add(child.name)
    return [PublishedPackage(ecosystem="pypi", name=name, file=rel, modules=sorted(modules))]


def _package_json(text: str, rel: str, folder: Path) -> list[PublishedPackage]:  # noqa: ARG001
    data = json.loads(text)
    name = data.get("name") if isinstance(data, dict) else None
    return [PublishedPackage(ecosystem="npm", name=name, file=rel)] if isinstance(name, str) and name else []


def _go_mod(text: str, rel: str, folder: Path) -> list[PublishedPackage]:  # noqa: ARG001
    m = re.search(r"^\s*module\s+(\S+)", text, re.M)
    return [PublishedPackage(ecosystem="go", name=m.group(1).strip('"'), file=rel)] if m else []


_BLOCKS = re.compile(
    r"<(parent|dependencies|dependencyManagement|build|profiles|reporting|plugins|"
    r"pluginManagement|properties)\b.*?</\1>", re.S)


def _pom(text: str, rel: str, folder: Path) -> list[PublishedPackage]:  # noqa: ARG001
    parent = re.search(r"<parent>.*?<groupId>\s*([^<\s]+)\s*</groupId>", text, re.S)
    body = _BLOCKS.sub("", re.sub(r"<!--.*?-->", "", text, flags=re.S))
    art = re.search(r"<artifactId>\s*([^<\s$]+)\s*</artifactId>", body)
    grp = re.search(r"<groupId>\s*([^<\s$]+)\s*</groupId>", body)
    group = grp.group(1) if grp else (parent.group(1) if parent else "")
    if not art or not group:
        return []
    return [PublishedPackage(ecosystem="maven", name=f"{group}:{art.group(1)}", file=rel)]


_READERS = {
    "pyproject.toml": _pyproject, "package.json": _package_json,
    "go.mod": _go_mod, "pom.xml": _pom,
}


def own_go_modules(packages: list[PublishedPackage]) -> list[str]:
    return [p.name for p in packages if p.ecosystem == "go"]
