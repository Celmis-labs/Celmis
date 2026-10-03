"""The AGPL application runs, whole, without a line of `ee/`.

LICENSING.md promises that a deployment using no `ee/` file needs no
commercial licence. That promise is only true while the AGPL code does not
depend on the enterprise code, so it is pinned two ways:

1. **By import graph.** Exactly one AGPL module may import `src.ee` —
   `src/api/main.py` — and only inside a `try` that handles ImportError. Read
   with `ast`, not grep: a comment saying "this file does not import src.ee"
   must not count as an import, and an import must not hide from a regex.
2. **By running it.** The app is built with `src.ee` made unimportable, as in
   a build that ships without the package, and the community edition comes up
   with the enterprise routes absent and the AGPL ones present.

The web half is the same rule at a coarser grain: TypeScript has no optional
import, so the AGPL files allowed to import `@/ee/...` are named here, and each
of them gates what it renders on the API's answer.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
WEB = ROOT / "web"

MOUNT_POINT = SRC / "api" / "main.py"


def _is_ee(path: Path, base: Path) -> bool:
    """Under an `ee` directory, or named `*.ee.*` — the LICENSING.md rule,
    read by path segment so a directory called `tree/` is not swept in."""
    return "ee" in path.relative_to(base).parts[:-1] or ".ee." in path.name


def _ee_imports(tree: ast.AST) -> list[ast.AST]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "src.ee" or a.name.startswith("src.ee.") for a in node.names):
                found.append(node)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            from_package = mod == "src.ee" or mod.startswith("src.ee.")
            from_src = mod == "src" and any(a.name == "ee" for a in node.names)
            if from_package or from_src:
                found.append(node)
    return found


def test_only_the_mount_point_imports_the_enterprise_package():
    offenders = []
    for path in SRC.rglob("*.py"):
        if _is_ee(path, ROOT):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _ee_imports(tree) and path != MOUNT_POINT:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"AGPL modules importing src.ee: {offenders}"


def test_the_mount_point_survives_the_package_being_absent():
    tree = ast.parse(MOUNT_POINT.read_text(encoding="utf-8"))
    imports = _ee_imports(tree)
    assert imports, "main.py no longer mounts the enterprise package at all"

    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handles_import_error = any(
            isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError")
            for h in node.handlers
        )
        if handles_import_error:
            for inner in node.body:
                guarded |= {id(n) for n in ast.walk(inner)}
    unguarded = [n.lineno for n in imports if id(n) not in guarded]
    assert not unguarded, f"src.ee imported outside a try/except ImportError at lines {unguarded}"


def test_the_app_builds_as_the_community_edition_without_the_package(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("CELMIS_JWT_SECRET", "test-community-secret-0123456789abcdef")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345678")
    # As if src/ee were not in the image: every import of it fails.
    for name in [m for m in sys.modules if m == "src.ee" or m.startswith("src.ee.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "src.ee", None)

    from src.api.main import build_app
    from src.api.routers import capabilities as caps

    with caplog.at_level(logging.INFO, logger="src.api.main"):
        app = build_app()
    assert "enterprise_package_absent" in caplog.text

    doc = caps.build_capabilities(app)
    assert doc.edition == "community"
    assert doc.complete is True
    assert doc.features["sso"].available is False
    assert doc.features["review_analytics"].available is False
    assert doc.features["review_issues"].available is True
    assert doc.features["core"].available is True
    paths = caps.mounted_paths(app)
    assert "/api/auth/login" in paths
    assert "/api/auth/oidc" not in paths


# ─── web ─────────────────────────────────────────────────────────────

#: AGPL web files that may import from web/ee — each renders the EE part
#: only when the API says the feature is mounted (or, for auth.ts, registers
#: a provider whose callback the API refuses without a licence).
WEB_IMPORTERS = {
    "auth.ts",
    "app/login/page.tsx",
    "app/login/login-form.tsx",
    # The invite landing page offers the same SSO button as /login, behind the
    # same capability check (web/lib/sso-offer.ts → `ssoName`).
    "app/invite/[token]/invite-view.tsx",
    "app/(app)/analytics/page.tsx",
}

_IMPORT_EE = re.compile(r"""^\s*(?:import|export)\b[^;]*?from\s+["']@/ee/""", re.M | re.S)


def _web_sources():
    for path in WEB.rglob("*"):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        if path.suffix in (".ts", ".tsx") and path.is_file():
            yield path


def test_only_the_named_web_files_import_the_enterprise_code():
    importers = set()
    for path in _web_sources():
        if _is_ee(path, WEB):
            continue
        if _IMPORT_EE.search(path.read_text(encoding="utf-8")):
            importers.add(path.relative_to(WEB).as_posix())
    assert importers <= WEB_IMPORTERS, f"unexpected @/ee importers: {importers - WEB_IMPORTERS}"
    assert importers, "nothing imports web/ee — the regex or the layout broke"


@pytest.mark.parametrize("rel", sorted(WEB_IMPORTERS))
def test_the_named_importers_exist(rel):
    assert (WEB / rel).is_file(), rel
