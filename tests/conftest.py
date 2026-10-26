"""Suite-wide fixtures.

Seat limits (src/users/seats.py) are process state: the licence code registers
what the licence in force allows, and the registration outlives the app that
made it. A test that applied a licence granting two seats would otherwise hand
two seats to every test that runs after it. So each test starts and ends on the
community limits (five seats, and the single-sign-on allowance of five people,
src/ee/sso/allowance.py) — a test that wants a licensed installation applies
one itself, inside the test.

Where the suite keeps its data. The settings default to folders under the
developer's home (``~/ObsidianVaults/code-analysis`` for the vault,
``~/code-analysis`` for the workspace, ``~/code-analysis/review-cache``), so a
test that set only ``WORKSPACE_DIR`` still wrote its notes into the developer's
own vault. For the whole run those three settings point at a scratch folder
(:func:`_scratch_data_dirs`), and every ``Settings`` a test builds is checked
(:func:`real_home_guard`): one that resolves the vault or the workspace under
the real home stops the test. A test that sets its own folders (``tmp_path``)
overrides the scratch ones as it always did.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _real_homes() -> tuple[Path, ...]:
    """The homes of the person running the tests, read before any test can change ``HOME``."""
    homes = {Path(os.path.expanduser("~"))}
    try:
        import pwd

        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):  # pragma: no cover - no passwd database (Windows)
        pass
    return tuple(sorted({h.resolve() for h in homes}))


REAL_HOMES = _real_homes()

#: The settings whose default is a folder under the home, and the folder of the scratch root they get.
_HOME_DEFAULTS = {
    "VAULT_DIR": "vault",
    "WORKSPACE_DIR": "workspace",
    "REVIEW_LOCAL_CACHE_DIR": "review-cache",
}


def _point_home_defaults_at_scratch() -> Path:
    """Set the folders before collection imports anything: some modules build a
    store at import time (the metrics collector opens the review-run store, the
    credential store is a singleton), and those read the settings long before a
    session fixture runs, so they would open the real home."""
    root = Path(tempfile.mkdtemp(prefix="celmis-test-data-"))
    # Nothing else owns this folder, so without this every run left one behind in the
    # system temp dir. Removed when the interpreter exits; atexit runs the handlers
    # last-in first-out, so whatever was imported later has had its say by then.
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    for var, sub in _HOME_DEFAULTS.items():
        os.environ[var] = str(root / sub)
    return root


IMPORT_TIME_DATA_ROOT = _point_home_defaults_at_scratch()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _clear_settings() -> None:
    from src.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _scratch_data_dirs(tmp_path_factory):
    """The vault, the workspace and the review cache are scratch folders for the whole run."""
    root = tmp_path_factory.mktemp("celmis-data")
    with pytest.MonkeyPatch.context() as patch:
        for var, sub in _HOME_DEFAULTS.items():
            patch.setenv(var, str(root / sub))
        _clear_settings()  # whatever was read before this point read the home
        yield root
    _clear_settings()


@dataclass
class RealHomeGuard:
    """What :func:`real_home_guard` knows: where the real homes are and what it has stopped."""

    safe: tuple[Path, ...]
    violations: list[str] = field(default_factory=list)

    def under_real_home(self, path: Path | str) -> bool:
        """True for a path inside the developer's home that is not scratch space (a pytest tmp dir)."""
        p = Path(path).expanduser().resolve()
        if any(_within(p, s) for s in self.safe):
            return False
        return any(_within(p, home) for home in REAL_HOMES)

    def check(self, what: str, path: Path | str) -> None:
        if self.under_real_home(path):
            message = (f"{what} resolves to {path}, under the real home: a test must point it at a "
                       "scratch folder (tmp_path, or the VAULT_DIR / WORKSPACE_DIR the suite sets)")
            self.violations.append(message)
            raise AssertionError(message)  # the code under test may swallow it: it is also reported at teardown


@pytest.fixture
def _forget_settings_after_a_real_workspace(request):
    """Asked for BEFORE ``monkeypatch``, so it ends after it: the settings read while a test
    had the real workspace are not handed to the next test."""
    yield
    if request.node.get_closest_marker("real_workspace") is not None:
        _clear_settings()


@pytest.fixture(autouse=True)
def real_home_guard(request, _forget_settings_after_a_real_workspace, monkeypatch, tmp_path_factory,
                    _scratch_data_dirs):
    """Stops a test that resolves the vault or the workspace to a folder under the real home.

    A test marked ``real_workspace`` reads the developer's own clones and graphs (the opt-in
    smoke tests of ``CELMIS_REAL_REPO``): its workspace is the real one, its vault still scratch.
    """
    from src import config

    real_workspace = request.node.get_closest_marker("real_workspace") is not None
    if real_workspace:
        monkeypatch.delenv("WORKSPACE_DIR", raising=False)
        _clear_settings()
    guard = RealHomeGuard(safe=(Path(tempfile.gettempdir()).resolve(), _scratch_data_dirs.resolve(),
                                tmp_path_factory.getbasetemp().resolve()))
    settings_init, repo_vault_path = config.Settings.__init__, config.Settings.repo_vault_path

    def init(self, *args, **kwargs):
        settings_init(self, *args, **kwargs)
        guard.check("vault_dir", self.vault_dir)
        if not real_workspace:
            guard.check("workspace_dir", self.workspace_dir)

    def vault_path(self, repo_slug):
        path = repo_vault_path(self, repo_slug)
        guard.check("the vault of a repository", path)
        return path

    monkeypatch.setattr(config.Settings, "__init__", init)
    monkeypatch.setattr(config.Settings, "repo_vault_path", vault_path)
    yield guard
    assert not guard.violations, "\n".join(guard.violations)


def _community_limits() -> None:
    from src.users import seats

    seats.clear_licence_limits()
    try:
        from src.ee.sso import allowance
    except ImportError:  # a build without the enterprise package
        return
    allowance.clear()


@pytest.fixture(autouse=True)
def _community_seat_limits():
    _community_limits()
    yield
    _community_limits()


@pytest.fixture(autouse=True)
def _review_model_preflight_off(monkeypatch):
    """Reviews in tests run without a model behind them. The preflight (a real
    request through the review's LLM client) is off unless a test turns it on
    with ``monkeypatch.setenv("CELMIS_REVIEW_MODEL_PREFLIGHT", "on")``."""
    monkeypatch.setenv("CELMIS_REVIEW_MODEL_PREFLIGHT", "off")
    from src.review import model_preflight

    model_preflight.reset_cache()
    yield
    model_preflight.reset_cache()
