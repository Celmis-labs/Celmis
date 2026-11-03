"""The Python dependency set is fixed by uv.lock, in the image and in CI.

An image built with `uv pip install -e .` resolved the newest release inside
pyproject's ranges on the day the layer was built; two builds of one commit
could differ, and vulnerable releases of urllib3 and msgpack reached a shipped
image with no change in the repository. These are structural checks on files
(a weak form of test, so each asserts a line rather than a word).
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
CI = (ROOT / ".github/workflows/ci.yml").read_text()
LOCK = (ROOT / "uv.lock").read_text()
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _locked(name: str) -> tuple[int, ...]:
    m = re.search(rf'^name = "{name}"\nversion = "([\d.]+)"', LOCK, re.M)
    assert m, f"{name} is not in uv.lock"
    return tuple(int(x) for x in m.group(1).split("."))


def test_the_image_installs_from_the_lock_and_refuses_to_change_it():
    assert re.search(r"^RUN uv sync --frozen --no-dev", DOCKERFILE, re.M)
    assert "COPY pyproject.toml uv.lock" in DOCKERFILE
    assert "uv pip install --no-cache -e ." not in DOCKERFILE


def test_ci_checks_the_lock_with_the_uv_the_image_uses():
    image_uv = re.search(r"^ARG UV_VERSION=([\d.]+)", DOCKERFILE, re.M).group(1)
    assert f'"uv=={image_uv}"' in CI
    assert "uv lock --check" in CI


def test_the_vulnerable_releases_cannot_be_locked():
    assert _locked("urllib3") >= (2, 8, 0)
    constraints = PYPROJECT["tool"]["uv"]["constraint-dependencies"]
    assert "msgpack>=1.2.4" in constraints
    assert "urllib3>=2.8.0" in constraints
    assert any(d.replace(" ", "").startswith("urllib3>=2.8") for d in PYPROJECT["project"]["dependencies"])
    assert "msgpack>=1.2.4" in DOCKERFILE   # the pip-audit venv is outside the lock


def test_osv_scanner_is_built_with_a_go_that_has_the_stdlib_fixes():
    m = re.search(r"golang:(\d+)\.(\d+)\.(\d+)-", DOCKERFILE)
    assert m and tuple(map(int, m.groups())) >= (1, 27, 2)
    assert "@sha256:" in DOCKERFILE.split("AS osv-build")[0].splitlines()[-1]


def test_npm_is_upgraded_in_the_image():
    assert re.search(r"^ARG NPM_VERSION=\d+\.\d+\.\d+", DOCKERFILE, re.M)
    assert 'npm install -g --no-fund --no-audit "npm@${NPM_VERSION}"' in DOCKERFILE
