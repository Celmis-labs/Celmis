"""Tests for auto-detection — Phase 9.

detect_features: keyword-overlap clustering на mock vault notes.
detect_integrations: SQL-граф + smoke на real graph.
"""

from __future__ import annotations

import os
from pathlib import Path

import frontmatter
import pytest

from src.config import get_settings
from src.generation.auto_detect import (
    _slugify,
    detect_features,
    detect_integrations,
)

# ─── slugify ────────────────────────────────────────────────────────


def test_slugify_basic():
    assert _slugify("Login Flow") == "login-flow"
    assert _slugify("user_management") == "user-management"
    assert _slugify("Special!Chars@Here") == "specialcharshere"


def test_slugify_empty():
    assert _slugify("") == "unnamed"
    assert _slugify("___") == "unnamed"


# ─── detect_features ────────────────────────────────────────────────


def _write_module_note(vault: Path, repo: str, module_name: str, keywords: list[str], symbols: list[str]):
    """Helper для створення mock module note."""
    note_dir = vault / "projects" / repo / "modules"
    note_dir.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "module",
        "module": module_name,
        "repo": repo,
        "commit": "test",
        "keywords": keywords,
        "symbols": symbols,
    }
    post = frontmatter.Post(content=f"# {module_name}", **fm)
    (note_dir / f"{module_name}.md").write_text(frontmatter.dumps(post))


@pytest.fixture
def vault_with_3_modules(tmp_path, monkeypatch):
    """3 модулі — 2 поділяють keywords {auth, login}, 1 окремий."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_dir", tmp_path)
    repo = "test-repo"

    _write_module_note(
        tmp_path, repo, "user-mod",
        keywords=["auth", "login", "session", "user"],
        symbols=["loginUser", "logout"],
    )
    _write_module_note(
        tmp_path, repo, "api-mod",
        keywords=["auth", "login", "request", "api"],
        symbols=["apiClient", "loginUser"],
    )
    _write_module_note(
        tmp_path, repo, "rendering-mod",
        keywords=["render", "ui", "components"],
        symbols=["renderTree"],
    )
    return repo


def test_detect_features_keyword_overlap(vault_with_3_modules):
    """user-mod + api-mod поділяють auth+login → одна feature."""
    features = detect_features(vault_with_3_modules)
    # ≥1 feature з ≥2 модулями (auth/login)
    assert len(features) >= 1
    biggest = max(features, key=lambda f: len(f.modules))
    assert len(biggest.modules) == 2
    assert "user-mod" in biggest.modules
    assert "api-mod" in biggest.modules


def test_detect_features_skips_generic_keywords(tmp_path, monkeypatch):
    """Модулі з тільки generic keywords ('module', 'utils') не утворюють фічу."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_dir", tmp_path)
    repo = "test-repo"

    for name in ("a-mod", "b-mod"):
        _write_module_note(
            tmp_path, repo, name,
            keywords=["module", "utils", "helper", "core"],  # all generic
            symbols=[],
        )

    features = detect_features(repo)
    assert features == []


def test_detect_features_too_few_notes(tmp_path, monkeypatch):
    """Менше 2 notes → no features."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_dir", tmp_path)
    repo = "test-repo"
    _write_module_note(tmp_path, repo, "only-one", ["auth", "login"], [])

    features = detect_features(repo)
    assert features == []


def test_detect_features_no_vault(tmp_path, monkeypatch):
    """No vault → empty list, no error."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_dir", tmp_path / "nonexistent")
    features = detect_features("missing-repo")
    assert features == []


# ─── detect_integrations (real graph required) ──────────────────────

# Slug локального клону для real-graph smoke — інакше тест скіпається.
REAL_REPO = os.environ.get("CELMIS_REAL_REPO", "acme-frontend")


@pytest.mark.skipif(
    not Path(f"~/code-analysis/data/{REAL_REPO}/graph.fdblite").expanduser().exists(),
    reason=f"graph not indexed (run `analyzer index {REAL_REPO}`)",
)
def test_detect_integrations_real_graph():
    """На real graph (CELMIS_REAL_REPO) → топ-кандидати reasonable."""
    candidates = detect_integrations(REAL_REPO)
    assert len(candidates) > 0
    names = {c.name for c in candidates}
    # Принаймні 1 з очікуваних exported singletons
    expected_any = {"store", "constructions-controller", "scene-controller", "i18n", "user", "projects-controller"}
    assert names & expected_any, f"None of expected services found in: {names}"


def test_detect_integrations_no_graph(tmp_path, monkeypatch):
    """No graph file → empty list, no error."""
    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "noworkspace")
    candidates = detect_integrations("missing-repo")
    assert candidates == []
