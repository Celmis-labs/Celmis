"""`"api_version": "0.0.0+unknown"` — measured on production.

A LOOP, and both halves looked correct on their own. `pyproject.toml` declares
`version = {attr = "src.__version__"}`, and `src/__init__.py` had become an
`importlib.metadata.version("code-analysis-system")` lookup. setuptools
evaluates that attribute AT BUILD TIME, before the distribution exists, so it
read the not-installed fallback "0.0.0+unknown", wrote THAT into the
distribution metadata, and every runtime read handed it straight back.

A fixed point. The API would have answered "0.0.0+unknown" for every release
forever, and `pyproject.toml`'s own comment already said why this file should
hold a literal: "it is the copy that can be read without the package being
installed".

The version test in tests/vault/ was green the whole time. It asserted
`!= "unknown"`, and "0.0.0+unknown" is not "unknown" — the same failure back
in a new costume, one indirection later.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_version_attribute_is_a_literal():
    """The property, checked on the AST rather than by importing: an import
    would resolve the loop and report whatever the loop currently produces.

    setuptools reads this attribute with the package NOT installed. Anything
    that has to look something up to answer will answer with its fallback, and
    that fallback becomes the release number.
    """
    tree = ast.parse((ROOT / "src/__init__.py").read_text(encoding="utf-8"))
    assigned = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__"
                for t in node.targets)
    ]
    assert assigned, "src/__init__.py no longer defines __version__"
    assert all(isinstance(v, ast.Constant) and isinstance(v.value, str)
               for v in assigned), (
        "__version__ is computed. setuptools evaluates it before the package "
        "exists, so a computed value bakes its own fallback into the release."
    )


def test_pyproject_still_reads_that_attribute():
    """The other half of the contract. If pyproject stopped deriving from
    src/__init__.py, the literal above would be one of two versions again —
    which is what collapsing four copies into one was for."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'attr = "src.__version__"' in text


def test_the_version_is_not_the_all_zero_placeholder():
    from src import __version__

    numbers = __version__.split("+", 1)[0].split(".")
    assert any(int(p) for p in numbers if p.isdigit()), (
        f"{__version__!r} is 0.0.0 — what this codebase says when it does not "
        f"know, dressed as a release"
    )


# ─── what the API reports ────────────────────────────────────────────


def test_the_api_version_carries_the_build(monkeypatch):
    """Deploys here are rsync + `compose up --build`: no image tag, no
    registry digest to read back. The release number alone cannot answer "is
    my push running?", and 0.1.0 will be the answer for a long time while the
    code under it changes daily."""
    import src.ops.build as build_mod
    from src.api.main import _version

    build_mod.build_info.cache_clear()
    monkeypatch.setenv("CELMIS_GIT_SHA", "8f3e319c4aa5167c44328b84a992ad0f61a39454")
    try:
        assert _version().endswith("+8f3e319")
    finally:
        build_mod.build_info.cache_clear()


def test_without_a_build_stamp_it_is_still_a_version(monkeypatch):
    import src.ops.build as build_mod
    from src.api.main import _version

    monkeypatch.setattr(build_mod, "build_info", lambda: {"git_sha_short": None})
    out = _version()
    assert out and "unknown" not in out


def test_metadata_left_over_from_the_broken_build_is_ignored(monkeypatch):
    """A container built before the loop was broken carries "0.0.0+unknown" in
    its own metadata. Trusting that would reinstate the bug on exactly the
    machines that have it."""
    import importlib.metadata as md

    import src.api.main as main_mod
    monkeypatch.setattr(md, "version", lambda _n: "0.0.0+unknown")
    out = main_mod._version()
    assert not out.startswith("0.0.0+unknown"), out


def test_a_version_lookup_never_takes_the_app_down(monkeypatch):
    """It runs while the app object is being constructed."""
    import importlib.metadata as md

    import src.api.main as main_mod

    def _boom(_n):
        raise RuntimeError("metadata store is on fire")

    monkeypatch.setattr(md, "version", _boom)
    assert main_mod._version()
