"""No test writes into the developer's own vault.

THE DEFECT. The settings default to folders under the home: the vault is
`~/ObsidianVaults/code-analysis`, the workspace `~/code-analysis`. A test that set
`WORKSPACE_DIR` and nothing else (the `world` of the access tests did exactly that) still
resolved the vault by default, and `tests/security/test_search_and_docs_obey_research_access.py`
wrote its notes, `modules/keys.md` among them, into the vault of whoever ran the suite.

THE CONTRACT (tests/conftest.py). For the whole run `VAULT_DIR`, `WORKSPACE_DIR` and
`REVIEW_LOCAL_CACHE_DIR` point at a scratch folder; a test that sets its own folders overrides
them as before. Every `Settings` a test builds, and every vault path it asks for, is checked:
one under the real home (and not in a pytest tmp dir) stops the test with an AssertionError,
and the test is failed at teardown even when the code under test swallowed the error.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import Settings, get_settings

#: Nothing is created under it: a path is only built and compared.
HOME_VAULT = Path("~/ObsidianVaults/code-analysis").expanduser()


def test_a_default_settings_object_keeps_its_data_out_of_the_home(real_home_guard):
    from src.review.settings import ReviewSettings

    s = Settings()
    assert not real_home_guard.under_real_home(s.vault_dir), s.vault_dir
    assert not real_home_guard.under_real_home(s.workspace_dir), s.workspace_dir
    assert not real_home_guard.under_real_home(s.repo_vault_path("acme-app"))
    assert not real_home_guard.under_real_home(get_settings().vault_dir)
    assert not real_home_guard.under_real_home(ReviewSettings().local_cache_dir)


def test_a_test_that_only_moves_the_workspace_still_gets_a_scratch_vault(tmp_path, monkeypatch, real_home_guard):
    """The `world` of the access tests used to do this and wrote into the developer's vault."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        vault = get_settings().repo_vault_path("acme-app")
        assert not real_home_guard.under_real_home(vault), vault
        assert HOME_VAULT not in vault.parents
    finally:
        get_settings.cache_clear()


def test_a_settings_object_aimed_at_the_vault_in_the_home_is_stopped(real_home_guard):
    with pytest.raises(AssertionError, match="under the real home"):
        Settings(vault_dir=HOME_VAULT)

    assert real_home_guard.violations
    real_home_guard.violations.clear()  # this test provoked it on purpose


def test_the_environment_aimed_at_the_vault_in_the_home_is_stopped(monkeypatch, real_home_guard):
    monkeypatch.setenv("VAULT_DIR", str(HOME_VAULT))
    with pytest.raises(AssertionError, match="under the real home"):
        Settings()

    real_home_guard.violations.clear()


def test_a_vault_path_asked_for_after_the_settings_were_changed_is_stopped(monkeypatch, real_home_guard):
    s = Settings()
    monkeypatch.setattr(s, "vault_dir", HOME_VAULT)
    with pytest.raises(AssertionError, match="under the real home"):
        s.repo_vault_path("acme-app")

    real_home_guard.violations.clear()


def test_a_violation_swallowed_by_the_code_under_test_still_fails_the_test(real_home_guard):
    """The check also raises where `except Exception` would eat it, so it is kept for teardown."""
    with contextlib.suppress(Exception):  # what the guard must survive
        Settings(vault_dir=HOME_VAULT)

    assert len(real_home_guard.violations) == 1
    real_home_guard.violations.clear()


def test_a_folder_in_a_pytest_tmp_dir_is_fine_even_when_the_home_is_above_it(tmp_path, real_home_guard):
    s = Settings(vault_dir=tmp_path / "vault", workspace_dir=tmp_path / "ws")

    assert tmp_path in s.repo_vault_path("acme-app").parents
    assert real_home_guard.violations == []


def test_a_swallowed_violation_fails_the_test_that_caused_it_at_teardown(tmp_path):
    """A test of its own, run in a child session with this suite's fixtures: it swallows the error and
    still has to be reported."""
    (tmp_path / "test_swallowing.py").write_text(
        "from pathlib import Path\n\nfrom src.config import Settings\n\n\n"
        "def test_it():\n    try:\n        Settings(vault_dir=Path('~/ObsidianVaults/x').expanduser())\n"
        "    except Exception:\n        pass\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "--rootdir", str(tmp_path), str(tmp_path / "test_swallowing.py")],
        cwd=root, capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode != 0, done.stdout[-600:]
    assert "ERROR at teardown" in done.stdout and "under the real home" in done.stdout, done.stdout[-600:]


def test_stores_opened_while_tests_are_collected_stay_out_of_the_home(tmp_path):
    """The metrics collector opens the review-run store at import time, before any
    fixture runs; a test that used to write a run row into the home's
    code-analysis/secrets must leave a fresh home untouched."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("VAULT_DIR", "WORKSPACE_DIR", "REVIEW_LOCAL_CACHE_DIR")}
    env["HOME"] = str(home)
    target = ("tests/review/test_a_manual_cadence_waits_for_the_command.py"
              "::test_the_job_carries_the_request_to_the_worker")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
        timeout=300, check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    leftovers = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert not [p for p in leftovers if p.startswith(("code-analysis", "ObsidianVaults"))], leftovers
