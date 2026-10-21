"""`SECRET_PATH_GLOBS_EXTRA` applies to the example env names too.

THE DEFECT. `classify` returned `keys_only` for `.env.example` and friends BEFORE it looked at the
operator's globs, so `SECRET_PATH_GLOBS_EXTRA=deploy/*` hid `deploy/prod.txt` and left
`deploy/.env.example` readable (masked) whatever the operator had said.

THE CONTRACT. The operator's globs come first and only ever deny: they can hide an example file,
they cannot reveal anything the built-in lists hide.
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf


@pytest.mark.parametrize(("glob", "rel", "verdict"), [
    ("deploy/*", "deploy/.env.example", "deny"),
    ("deploy/*", "deploy/x.txt", "deny"),
    ("*.example", "a/b.example", "deny"),
    ("*.example", "config/.env.example", "deny"),
    (".env.example", "api/.env.example", "deny"),
    ("DEPLOY/*", "deploy/.env.sample", "deny"),
    ("deploy/*", "src/.env.example", "keys_only"),
    ("deploy/*", "src/app.py", "ok"),
    ("*.example", ".env", "deny"),
])
def test_an_operator_glob_decides_before_the_example_names(monkeypatch, glob, rel, verdict):
    monkeypatch.setattr(sf, "_extra_globs", lambda: (glob,))

    assert sf.classify(rel) == verdict
    assert sf.is_secret_path(rel) is (verdict != "ok")


def test_the_setting_reaches_the_example_names_end_to_end(monkeypatch, tmp_path):
    from src.config import get_settings

    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/.env.example").write_text("A=1\n")
    monkeypatch.setenv("SECRET_PATH_GLOBS_EXTRA", '["deploy/*"]')
    get_settings.cache_clear()
    try:
        assert sf.classify("deploy/.env.example") == "deny"
        assert sf.plain_file(tmp_path, "deploy/.env.example") is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_the_operators_globs_are_in_the_git_pathspecs_case_blind(monkeypatch):
    from src.mcp_server.dev_profile import paths

    monkeypatch.setattr(paths, "extra_globs", lambda: ("deploy/*",))

    assert ":(exclude,glob,icase)**/deploy/*" in paths.git_excludes()
