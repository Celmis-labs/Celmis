"""Every module under src/ must import in the environment it ships in.

`src/review/lifecycle.py` imported `cachetools` at module level and nothing
declared it. In the built image the module raised ModuleNotFoundError, its test
file failed at COLLECTION — which aborts the whole run before a single test
executes — and the break survived because no other module imports it, so
nothing in production ever touched the broken path either.

A test suite cannot cover a module it cannot import, and an undeclared
dependency looks identical to working code right up until the first request
reaches it. This walks the package and imports everything, so the next missing
declaration fails here instead of in front of a user.

Deliberately an import check and nothing more: no module is executed beyond its
top level, so a module that opens a connection or spawns a thread inside a
function is untouched.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import src

#: Modules whose import legitimately depends on something absent in a bare test
#: environment. Empty on purpose — anything landing here needs a reason next to
#: it, because "it does not import" is otherwise indistinguishable from a bug.
ALLOWED_TO_FAIL: dict[str, str] = {}


def _module_names() -> list[str]:
    return sorted(
        m.name for m in pkgutil.walk_packages(src.__path__, prefix="src.")
        if not m.name.rsplit(".", 1)[-1].startswith("_test")
    )


@pytest.mark.parametrize("name", _module_names())
def test_module_imports(name: str) -> None:
    try:
        importlib.import_module(name)
    except ImportError as exc:
        reason = ALLOWED_TO_FAIL.get(name)
        if reason:
            pytest.skip(f"{name}: {reason}")
        pytest.fail(
            f"{name} cannot be imported: {exc}. If it needs a third-party "
            f"package, declare it in pyproject.toml — an undeclared import "
            f"only fails once the code is reached in production."
        )


def test_the_sweep_actually_found_modules() -> None:
    """Negative control. A broken walk would collect nothing and every
    parametrised case above would silently vanish into a green run."""
    names = _module_names()
    assert len(names) > 100, f"only {len(names)} modules found — the walk is broken"
    assert "src.review.lifecycle" in names, "the module that started this is missing"
