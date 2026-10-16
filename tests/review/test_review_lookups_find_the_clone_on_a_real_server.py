"""The clone is found through the application settings, where it lives.

The head fetch, the defect agent's code context and the undefined-constant
check each look the clone up by slug through `graph_context._clone_root`,
which asks the settings object it is given for `repo_path`. Only the
application settings carry `repo_path`; the review settings do not. Handing
the review settings over made `_clone_root` answer "no clone" on every real
server, and all three features went quiet while their unit tests, which
patch the lookup away, stayed green. These tests drive the real lookup.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.review import code_context, head_fetch, structural_constants


def _clone(tmp_path: Path) -> Path:
    root = tmp_path / "repos" / "acme-shop"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    root = _clone(tmp_path)
    fake = SimpleNamespace(repo_path=lambda slug: tmp_path / "repos" / slug)
    monkeypatch.setattr("src.config.get_settings", lambda: fake)
    return root


def _pr():
    return SimpleNamespace(repo_slug="acme-shop", local_slug="acme-shop")


def test_the_review_settings_do_not_know_where_the_clone_is():
    from src.review.settings import get_review_settings

    assert getattr(get_review_settings(), "repo_path", None) is None


def test_the_head_fetch_finds_the_clone(app_settings):
    assert head_fetch._find_clone(_pr()) == app_settings


def test_the_constant_check_finds_the_clone(app_settings):
    assert structural_constants.clone_root_for(_pr()) == app_settings


def test_the_code_context_finds_the_clone(app_settings):
    assert code_context.make_source(_pr()).root == app_settings
